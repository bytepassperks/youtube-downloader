"""Mega downloader with multi-proxy quota bypass and parallel chunk downloads.

Speed stack (all free):
  1. Cloudflare WARP  - 50-200 Mbps, free unlimited bandwidth
  2. Psiphon           - 5-15  Mbps, free open-source VPN
  3. Tor               - 1-5   Mbps, free anonymity network
  4. Free proxy pool   - variable, scraped SOCKS5 proxies
  5. Render restart    - last resort, new server IP

Parallel chunk downloads split large files across multiple proxies
for 4-8x speed multiplier.
"""

import os
import shutil
import json
import struct
import base64
import time
import sys
import subprocess
import select
import requests
import threading
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from Crypto.Cipher import AES
from Crypto.Util import Counter as CryptoCounter

from app.database import get_connection


# Configuration
DOWNLOAD_CHUNK_SIZE = 1024 * 1024  # 1MB chunks for streaming
PARALLEL_CHUNKS = int(os.getenv("PARALLEL_CHUNKS", "4"))  # chunks per large file
LARGE_FILE_THRESHOLD = 50 * 1024 * 1024  # 50MB - files above this use parallel chunks
PSIPHON_BINARY = os.getenv("PSIPHON_BINARY", "/usr/local/bin/psiphon-tunnel-core")
PSIPHON_BASE_SOCKS_PORT = 10800
PSIPHON_BASE_HTTP_PORT = 10900
PSIPHON_CONNECT_TIMEOUT = 60
WARP_BINARY = os.getenv("WARP_BINARY", "/usr/local/bin/warp-svc")
WARP_CLI = os.getenv("WARP_CLI", "/usr/local/bin/warp-cli")
RENDER_API_KEY = os.getenv("RENDER_API_KEY", "")
RENDER_SERVICE_ID = os.getenv("RENDER_SERVICE_ID", "")


# --- Mega Crypto Helpers ------------------------------------------------

def _b64_decode(data: str) -> bytes:
    data += '==' if len(data) % 4 == 2 else '=' if len(data) % 4 == 3 else ''
    return base64.urlsafe_b64decode(data)


def _str_to_a32(b) -> tuple:
    if isinstance(b, str):
        b = b.encode()
    if len(b) % 4:
        b += b'\0' * (4 - len(b) % 4)
    return struct.unpack('>%dI' % (len(b) // 4), b)


def _a32_to_str(a) -> bytes:
    return struct.pack('>%dI' % len(a), *a)


def _decrypt_attr(attr_data: bytes, key: tuple) -> Optional[dict]:
    try:
        cipher = AES.new(_a32_to_str(key), AES.MODE_CBC, b'\0' * 16)
        decrypted = cipher.decrypt(attr_data).decode('utf-8', errors='ignore')
        if 'MEGA{' in decrypted:
            json_str = decrypted[decrypted.index('MEGA{') + 4:]
            brace_count = 0
            for i, c in enumerate(json_str):
                if c == '{':
                    brace_count += 1
                elif c == '}':
                    brace_count -= 1
                if brace_count == 0:
                    return json.loads(json_str[:i + 1])
    except Exception:
        pass
    return None


def _decrypt_node_key(encrypted_key_b64: str, folder_key: tuple) -> tuple:
    encrypted_key = _b64_decode(encrypted_key_b64)
    key_a32 = _str_to_a32(encrypted_key)
    if len(key_a32) == 4:
        return tuple(a ^ b for a, b in zip(key_a32, folder_key))
    elif len(key_a32) == 8:
        cipher = AES.new(_a32_to_str(folder_key), AES.MODE_ECB)
        decrypted = cipher.decrypt(_a32_to_str(key_a32[:4])) + \
                    cipher.decrypt(_a32_to_str(key_a32[4:]))
        return _str_to_a32(decrypted)
    return key_a32


def _get_file_key(node_key: tuple) -> tuple:
    if len(node_key) == 8:
        return (node_key[0] ^ node_key[4], node_key[1] ^ node_key[5],
                node_key[2] ^ node_key[6], node_key[3] ^ node_key[7])
    return node_key


def _get_file_iv(node_key: tuple) -> tuple:
    if len(node_key) >= 6:
        return (node_key[4], node_key[5], 0, 0)
    return (0, 0, 0, 0)


# --- Mega API -----------------------------------------------------------

def _mega_api_request(data: dict, folder_id: str = None,
                      proxy: str = None, timeout: int = 30):
    params = {'id': 1}
    if folder_id:
        params['n'] = folder_id
    url = 'https://g.api.mega.co.nz/cs'
    proxies = {'http': proxy, 'https': proxy} if proxy else None
    resp = requests.post(url, params=params, data=json.dumps([data]),
                         proxies=proxies, timeout=timeout)
    result = resp.json()
    if isinstance(result, list) and len(result) > 0:
        return result[0]
    return result


def _parse_folder_link(mega_link: str) -> tuple:
    if '/folder/' in mega_link:
        parts = mega_link.split('#')
        folder_id = parts[0].split('/')[-1]
        folder_key_b64 = parts[1] if len(parts) > 1 else ''
    elif '#F!' in mega_link:
        parts = mega_link.split('!')
        folder_id = parts[1] if len(parts) > 1 else ''
        folder_key_b64 = parts[2] if len(parts) > 2 else ''
    else:
        raise ValueError(f"Unsupported Mega link format: {mega_link}")
    folder_key = _str_to_a32(_b64_decode(folder_key_b64))
    return folder_id, folder_key


def _list_folder_files(folder_id: str, folder_key: tuple) -> list:
    result = _mega_api_request({'a': 'f', 'c': 1, 'r': 1, 'ca': 1},
                               folder_id=folder_id)
    if isinstance(result, int):
        raise Exception(f"Mega API error {result} when listing folder")

    raw_nodes = result.get('f', [])
    nodes = {}

    for f in raw_nodes:
        h = f['h']
        t = f['t']
        p = f.get('p', '')
        key_str = f.get('k', '')
        if ':' in key_str:
            key_str = key_str.split(':')[1]
        try:
            node_key = _decrypt_node_key(key_str, folder_key)
            file_key = _get_file_key(node_key) if t == 0 else node_key
            attr_data = _b64_decode(f.get('a', ''))
            attrs = _decrypt_attr(attr_data, file_key)
            name = attrs.get('n', f'file_{h}') if attrs else f'file_{h}'
        except Exception:
            name = f'file_{h}'
            node_key = None
            file_key = None

        nodes[h] = {
            'handle': h, 'parent': p, 'type': t, 'name': name,
            'size': f.get('s', 0), 'node_key': node_key, 'file_key': file_key,
        }

    # Find root
    root_handle = None
    for h, n in nodes.items():
        if n['type'] in (1, 2) and n['parent'] not in nodes:
            root_handle = h
            break

    def get_path(h):
        parts = []
        current = h
        while current and current != root_handle and current in nodes:
            parts.append(nodes[current]['name'])
            current = nodes[current]['parent']
        parts.reverse()
        return '/'.join(parts)

    files = []
    for h, n in nodes.items():
        if n['type'] == 0 and n['node_key'] is not None:
            files.append({
                'handle': h, 'name': n['name'], 'size': n['size'],
                'path': get_path(h), 'node_key': n['node_key'],
                'file_key': n['file_key'],
            })
    return files


# --- File Download & Decryption ----------------------------------------

def _get_download_url(file_handle: str, folder_id: str,
                      proxy: str = None) -> Optional[str]:
    try:
        result = _mega_api_request(
            {'a': 'g', 'g': 1, 'n': file_handle},
            folder_id=folder_id, proxy=proxy, timeout=30,
        )
        if isinstance(result, int):
            if result == -18:
                print(f"[MegaAPI] Over quota (error -18) for {file_handle}")
            else:
                print(f"[MegaAPI] Error {result} getting URL for {file_handle}")
            return None
        if isinstance(result, dict) and 'g' in result:
            return result['g']
        print(f"[MegaAPI] Unexpected response for {file_handle}: {result}")
        return None
    except Exception as e:
        print(f"[MegaAPI] Exception getting URL for {file_handle}: {e}")
        return None


def _download_and_decrypt_file(
    dl_url: str, dest_path: str, file_key: tuple, node_key: tuple,
    file_size: int, proxy: str = None, timeout: int = 3600,
    speed_callback=None,
) -> bool:
    try:
        proxies = {'http': proxy, 'https': proxy} if proxy else None
        resp = requests.get(dl_url, stream=True, proxies=proxies,
                            timeout=(30, timeout))
        if resp.status_code == 509:
            print(f"[Download] 509 over quota from download server")
            return False
        if resp.status_code != 200:
            print(f"[Download] HTTP {resp.status_code} from download server")
            return False

        iv = _get_file_iv(node_key)
        initial_value = int.from_bytes(_a32_to_str(iv), 'big')
        ctr = CryptoCounter.new(128, initial_value=initial_value)
        cipher = AES.new(_a32_to_str(file_key), AES.MODE_CTR, counter=ctr)

        os.makedirs(os.path.dirname(dest_path), exist_ok=True)

        downloaded = 0
        start_time = time.time()
        last_report = start_time
        with open(dest_path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                if chunk:
                    f.write(cipher.decrypt(chunk))
                    downloaded += len(chunk)
                    now = time.time()
                    if speed_callback and (now - last_report) >= 1.0:
                        elapsed = now - start_time
                        speed_mbps = (downloaded / (1024 * 1024)) / max(elapsed, 0.1)
                        speed_callback(downloaded, file_size, speed_mbps)
                        last_report = now

        # Trim to actual size (Mega pads to AES block boundary)
        if file_size > 0 and downloaded > file_size:
            with open(dest_path, 'r+b') as f:
                f.truncate(file_size)
        return True
    except requests.exceptions.ConnectionError as e:
        print(f"[Download] Connection error: {e}")
        return False
    except requests.exceptions.Timeout:
        print(f"[Download] Timeout")
        return False
    except OSError as e:
        if e.errno == 28:  # ENOSPC - No space left on device
            print(f"[Download] DISK FULL: {e}")
            raise  # Propagate disk full errors up instead of retrying
        print(f"[Download] OS Error: {e}")
        return False
    except Exception as e:
        print(f"[Download] Error: {e}")
        return False


# --- Cloudflare WARP Manager -------------------------------------------

class WARPManager:
    """Manages Cloudflare WARP for free high-speed IP rotation.

    WARP provides a free SOCKS5 proxy through Cloudflare's global CDN.
    Speed: 50-200 Mbps (single-hop through Cloudflare edge).
    Cost: $0 (free tier, unlimited bandwidth).
    """

    def __init__(self, instance_id: int = 0):
        self.instance_id = instance_id
        self.socks_port = 40000 + instance_id
        self.process: Optional[subprocess.Popen] = None
        self.connected = False
        self.data_dir = f"/tmp/warp_data_{instance_id}"

    def start(self) -> bool:
        """Start WARP proxy using wgcf + wireproxy (lightweight, no root)."""
        wireproxy_bin = shutil.which("wireproxy")
        if not wireproxy_bin:
            print(f"[WARP-{self.instance_id}] wireproxy binary not found")
            sys.stdout.flush()
            return False

        os.makedirs(self.data_dir, exist_ok=True)
        wg_conf = os.path.join(self.data_dir, "warp.conf")
        wp_conf = os.path.join(self.data_dir, "wireproxy.conf")

        # Generate WARP WireGuard config if not exists
        if not os.path.exists(wg_conf):
            wgcf_bin = shutil.which("wgcf")
            if not wgcf_bin:
                print(f"[WARP-{self.instance_id}] wgcf binary not found")
                sys.stdout.flush()
                return False
            try:
                # Register new WARP account
                subprocess.run(
                    [wgcf_bin, "register", "--accept-tos"],
                    cwd=self.data_dir, capture_output=True, timeout=30,
                )
                # Generate WireGuard config
                subprocess.run(
                    [wgcf_bin, "generate"],
                    cwd=self.data_dir, capture_output=True, timeout=10,
                )
                wgcf_profile = os.path.join(self.data_dir, "wgcf-profile.conf")
                if os.path.exists(wgcf_profile):
                    shutil.copy(wgcf_profile, wg_conf)
                else:
                    print(f"[WARP-{self.instance_id}] wgcf failed to generate config")
                    sys.stdout.flush()
                    return False
            except Exception as e:
                print(f"[WARP-{self.instance_id}] wgcf error: {e}")
                sys.stdout.flush()
                return False

        # Write wireproxy config that wraps WireGuard as SOCKS5
        try:
            with open(wg_conf, 'r') as f:
                wg_content = f.read()
            # Strip [Interface]/[Peer] DNS and Address lines for wireproxy
            wp_content = wg_content.rstrip() + f"\n\n[Socks5]\nBindAddress = 127.0.0.1:{self.socks_port}\n"
            with open(wp_conf, 'w') as f:
                f.write(wp_content)
        except Exception as e:
            print(f"[WARP-{self.instance_id}] Config write error: {e}")
            sys.stdout.flush()
            return False

        # Start wireproxy
        self.stop()
        try:
            self.process = subprocess.Popen(
                [wireproxy_bin, "-c", wp_conf],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
        except Exception as e:
            print(f"[WARP-{self.instance_id}] Failed to start: {e}")
            sys.stdout.flush()
            return False

        # Wait for SOCKS5 proxy to become available
        import socket
        start_time = time.time()
        while time.time() - start_time < 15:
            if self.process.poll() is not None:
                try:
                    out = self.process.stdout.read()
                    if out:
                        print(f"[WARP-{self.instance_id}] Exited: {out.decode('utf-8', errors='ignore')[:200]}")
                except Exception:
                    pass
                sys.stdout.flush()
                return False
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(1)
                s.connect(('127.0.0.1', self.socks_port))
                s.close()
                self.connected = True
                elapsed = time.time() - start_time
                print(f"[WARP-{self.instance_id}] Connected in {elapsed:.1f}s (SOCKS5: {self.socks_port})")
                sys.stdout.flush()
                return True
            except (ConnectionRefusedError, OSError):
                time.sleep(0.5)

        print(f"[WARP-{self.instance_id}] Connection timeout")
        sys.stdout.flush()
        self.stop()
        return False

    def stop(self):
        if self.process:
            try:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
            except Exception:
                pass
            self.process = None
        self.connected = False

    def get_proxy_url(self) -> Optional[str]:
        if self.connected:
            return f"socks5h://127.0.0.1:{self.socks_port}"
        return None

    def restart_for_new_ip(self) -> bool:
        """Restart to get a new Cloudflare edge IP."""
        # Delete old warp config to force new registration = new IP
        wg_conf = os.path.join(self.data_dir, "warp.conf")
        wgcf_account = os.path.join(self.data_dir, "wgcf-account.toml")
        for f in [wg_conf, wgcf_account]:
            if os.path.exists(f):
                os.remove(f)
        self.stop()
        return self.start()

    def __del__(self):
        self.stop()


# --- Tor Manager --------------------------------------------------------

class TorManager:
    """Manages Tor for free IP rotation via SOCKS5 proxy."""

    def __init__(self):
        self.process: Optional[subprocess.Popen] = None
        self.socks_port = 9150
        self.control_port = 9151
        self.data_dir = "/tmp/tor_data_new"
        self.torrc_path = "/tmp/torrc_new"
        self.connected = False

    def _write_torrc(self):
        os.makedirs(self.data_dir, exist_ok=True)
        config = (
            f"SocksPort {self.socks_port}\n"
            f"ControlPort {self.control_port}\n"
            f"DataDirectory {self.data_dir}\n"
            f"CookieAuthentication 0\n"
            f"Log notice stderr\n"
        )
        with open(self.torrc_path, 'w') as f:
            f.write(config)

    def _kill_existing_tor(self):
        """Kill any existing Tor processes to free ports."""
        try:
            subprocess.run(
                ["pkill", "-9", "-f", "tor"],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass
        try:
            subprocess.run(["killall", "-9", "tor"], capture_output=True, timeout=5)
        except Exception:
            pass
        # Wait for ports to be released
        time.sleep(2)
        # Force free the ports if still bound
        import socket
        for port in [self.socks_port, self.control_port]:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.5)
                s.connect(('127.0.0.1', port))
                s.close()
                print(f"[Tor] Port {port} still in use after kill, waiting...")
                time.sleep(3)
            except (ConnectionRefusedError, OSError):
                pass  # Port is free

    def start(self) -> bool:
        """Start Tor daemon. Returns True when ready."""
        tor_bin = shutil.which("tor")
        if not tor_bin:
            print("[Tor] Binary not found")
            return False
        self.stop()
        self._kill_existing_tor()  # Kill any leftover Tor from previous runs
        self._write_torrc()
        print(f"[Tor] Starting (SOCKS5 port {self.socks_port})...")
        sys.stdout.flush()
        try:
            self.process = subprocess.Popen(
                [tor_bin, "-f", self.torrc_path],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
        except Exception as e:
            print(f"[Tor] Failed to start: {e}")
            return False
        # Wait for Tor to bootstrap with non-blocking read
        import fcntl
        fd = self.process.stdout.fileno()
        fl = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        buffer = b""
        start_time = time.time()
        timeout = 60  # Tor can take up to 60s to bootstrap
        while time.time() - start_time < timeout:
            if self.process.poll() is not None:
                try:
                    remaining = self.process.stdout.read()
                    if remaining:
                        buffer += remaining
                except Exception:
                    pass
                print(f"[Tor] Process exited (code {self.process.returncode})")
                if buffer:
                    text = buffer.decode('utf-8', errors='ignore')
                    lines = [l for l in text.split('\n') if l.strip()][-5:]
                    for l in lines:
                        print(f"[Tor] {l[:200]}")
                sys.stdout.flush()
                return False
            ready, _, _ = select.select([fd], [], [], 1.0)
            if ready:
                try:
                    data = os.read(fd, 65536)
                    if data:
                        buffer += data
                        text = buffer.decode('utf-8', errors='ignore')
                        if 'Bootstrapped 100%' in text:
                            self.connected = True
                            elapsed = time.time() - start_time
                            print(f"[Tor] Connected in {elapsed:.1f}s (SOCKS5: {self.socks_port})")
                            sys.stdout.flush()
                            return True
                        # Log bootstrap progress
                        for line in text.split('\n'):
                            if 'Bootstrapped' in line and '%' in line:
                                pct = line.split('Bootstrapped')[1].split(':')[0].strip()
                                print(f"[Tor] Bootstrap {pct}")
                                sys.stdout.flush()
                except OSError:
                    pass
        print(f"[Tor] Connection timeout after {timeout}s")
        sys.stdout.flush()
        self.stop()
        return False

    def stop(self):
        if self.process:
            try:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
            except Exception:
                pass
            self.process = None
        self.connected = False

    def get_proxy_url(self) -> Optional[str]:
        if self.connected:
            return f"socks5h://127.0.0.1:{self.socks_port}"
        return None

    def rotate_ip(self) -> bool:
        """Get new Tor circuit (new exit IP) via control port."""
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect(('127.0.0.1', self.control_port))
            s.send(b'AUTHENTICATE ""\r\n')
            resp = s.recv(256)
            if b'250' not in resp:
                # Try without password
                s.close()
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.connect(('127.0.0.1', self.control_port))
                s.send(b'AUTHENTICATE\r\n')
                resp = s.recv(256)
            s.send(b'SIGNAL NEWNYM\r\n')
            resp = s.recv(256)
            s.close()
            if b'250' in resp:
                print(f"[Tor] New circuit requested (new IP in ~5s)")
                sys.stdout.flush()
                time.sleep(5)  # Wait for new circuit
                return True
            print(f"[Tor] NEWNYM failed: {resp}")
            return False
        except Exception as e:
            print(f"[Tor] Circuit rotation error: {e}")
            return False

    def __del__(self):
        self.stop()


# --- Psiphon VPN Manager ------------------------------------------------

class PsiphonManager:
    """Manages Psiphon tunnel for free IP rotation."""

    def __init__(self, instance_id: int = 0):
        self.instance_id = instance_id
        self.socks_port = PSIPHON_BASE_SOCKS_PORT + instance_id
        self.http_port = PSIPHON_BASE_HTTP_PORT + instance_id
        self.process: Optional[subprocess.Popen] = None
        self.data_dir = f"/tmp/psiphon_data_{instance_id}"
        self.config_path = f"/tmp/psiphon_config_{instance_id}.json"
        self.connected = False

    def _write_config(self):
        os.makedirs(self.data_dir, exist_ok=True)
        config = {
            "LocalHttpProxyPort": self.http_port,
            "LocalSocksProxyPort": self.socks_port,
            "PropagationChannelId": "FFFFFFFFFFFFFFFF",
            "RemoteServerListDownloadFilename": f"{self.data_dir}/server_list",
            "RemoteServerListSignaturePublicKey": "MIICIDANBgkqhkiG9w0BAQEFAAOCAg0AMIICCAKCAgEAt7Ls+/39r+T6zNW7GiVpJfzq/xvL9SBH5rIFnk0RXYEYavax3WS6HOD35eTAqn8AniOwiH+DOkvgSKF2caqk/y1dfq47Pdymtwzp9ikpB1C5OfAysXzBiwVJlCdajBKvBZDerV1cMvRzCKvKwRmvDmHgphQQ7WfXIGbRbmmk6opMBh3roE42KcotLFtqp0RRwLtcBRNtCdsrVsjiI1Lqz/lH+T61sGjSjQ3CHMuZYSQJZo/KrvzgQXpkaCTdbObxHqb6/+i1qaVOfEsvjoiyzTxJADvSytVtcTjijhPEV6XskJVHE1Zgl+7rATr/pDQkw6DPCNBS1+Y6fy7GstZALQXwEDN/qhQI9kWkHijT8ns+i1vGg00Mk/6J75arLhqcodWsdeG/M/moWgqQAnlZAGVtJI1OgeF5fsPpXu4kctOfuZlGjVZXQNW34aOzm8r8S0eVZitPlbhcPiR4gT/aSMz/wd8lZlzZYsje/Jr8u/YtlwjjreZrGRmG8KMOzukV3lLmMppXFMvl4bxv6YFEmIuTsOhbLTwFgh7KYNjodLj/LsqRVfwz31PgWQFTEPICV7GCvgVlPRxnofqKSjgTWI4mxDhBpVcATvaoBl1L/6WLbFvBsoAUBItWwctO2xalKxF5szhGm8lccoc5MZr8kfE0uxMgsxz4er68iCID+rsCAQM=",
            "RemoteServerListUrl": "https://s3.amazonaws.com//psiphon/web/mjr4-p23r-puwl/server_list_compressed",
            "ObfuscatedServerListRootURL": "https://s3.amazonaws.com//psiphon/web/mjr4-p23r-puwl/",
            "SponsorId": "FFFFFFFFFFFFFFFF",
            "UseIndistinguishableTLS": True,
            "DataStoreDirectory": self.data_dir,
            # Approach 1: Force domain-fronted meek protocols that bypass cloud
            # network restrictions by routing through major CDNs (Cloudflare,
            # Akamai, Azure). Falls back to TLS-OSSH if meek unavailable.
            "LimitTunnelProtocols": [
                "FRONTED-MEEK-OSSH",
                "FRONTED-MEEK-HTTP-OSSH",
                "FRONTED-MEEK-QUIC-OSSH",
                "TLS-OSSH",
                "UNFRONTED-MEEK-HTTPS-OSSH",
                "UNFRONTED-MEEK-SESSION-TICKET-OSSH",
            ],
            "EstablishTunnelTimeoutSeconds": 60,
        }
        with open(self.config_path, 'w') as f:
            json.dump(config, f)

    def start(self) -> bool:
        """Start Psiphon tunnel. Returns True when connected."""
        if not os.path.exists(PSIPHON_BINARY):
            print(f"[Psiphon-{self.instance_id}] Binary not found at {PSIPHON_BINARY}")
            return False
        self.stop()
        self._write_config()
        print(f"[Psiphon-{self.instance_id}] Starting tunnel (SOCKS5 port {self.socks_port})...")
        sys.stdout.flush()
        try:
            self.process = subprocess.Popen(
                [PSIPHON_BINARY, "-config", self.config_path],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
        except Exception as e:
            print(f"[Psiphon-{self.instance_id}] Failed to start: {e}")
            return False
        start_time = time.time()
        # Use non-blocking read with select to avoid blocking on readline()
        import fcntl
        fd = self.process.stdout.fileno()
        fl = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        buffer = b""
        while time.time() - start_time < PSIPHON_CONNECT_TIMEOUT:
            if self.process.poll() is not None:
                # Read remaining output for debugging
                try:
                    remaining = self.process.stdout.read()
                    if remaining:
                        buffer += remaining
                except Exception:
                    pass
                print(f"[Psiphon-{self.instance_id}] Process exited (code {self.process.returncode})")
                if buffer:
                    print(f"[Psiphon-{self.instance_id}] Output: {buffer[:500]}")
                sys.stdout.flush()
                return False
            ready, _, _ = select.select([fd], [], [], 0.5)
            if ready:
                try:
                    data = os.read(fd, 65536)
                    if data:
                        buffer += data
                        text = buffer.decode('utf-8', errors='ignore')
                        if '"noticeType":"Tunnels"' in text and '"count":1' in text:
                            self.connected = True
                            elapsed = time.time() - start_time
                            print(f"[Psiphon-{self.instance_id}] Connected in {elapsed:.1f}s (SOCKS5: {self.socks_port})")
                            sys.stdout.flush()
                            return True
                except OSError:
                    pass
        # Timeout - print what we got for debugging
        if buffer:
            text = buffer.decode('utf-8', errors='ignore')
            # Print last few relevant lines
            lines = [l for l in text.split('\n') if l.strip()][-5:]
            for l in lines:
                print(f"[Psiphon-{self.instance_id}] {l[:200]}")
        print(f"[Psiphon-{self.instance_id}] Connection timeout after {PSIPHON_CONNECT_TIMEOUT}s")
        sys.stdout.flush()
        self.stop()
        return False

    def stop(self):
        """Stop the Psiphon tunnel."""
        if self.process:
            try:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
            except Exception:
                pass
            self.process = None
        self.connected = False

    def get_proxy_url(self) -> Optional[str]:
        if self.connected:
            return f"socks5h://127.0.0.1:{self.socks_port}"
        return None

    def restart_for_new_ip(self) -> bool:
        """Kill and restart to get a new IP address."""
        print(f"[Psiphon-{self.instance_id}] Rotating IP (kill + restart)...")
        sys.stdout.flush()
        self.stop()
        try:
            shutil.rmtree(self.data_dir, ignore_errors=True)
        except Exception:
            pass
        return self.start()

    def __del__(self):
        self.stop()


# --- IP Rotation Manager -----------------------------------------------

# --- Free Proxy Pool Scraper -------------------------------------------

class FreeProxyPool:
    """Scrapes and maintains a pool of free SOCKS5 proxies."""

    SOURCES = [
        "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks5.txt",
        "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt",
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
    ]

    def __init__(self):
        self.proxies: list = []
        self.current_idx = 0
        self._lock = threading.Lock()

    def refresh(self) -> int:
        """Scrape fresh proxies from public lists. Returns count."""
        new_proxies = []
        for url in self.SOURCES:
            try:
                resp = requests.get(url, timeout=10)
                if resp.status_code == 200:
                    for line in resp.text.strip().split('\n'):
                        line = line.strip()
                        if ':' in line and line[0].isdigit():
                            new_proxies.append(f"socks5h://{line}")
            except Exception:
                continue
        with self._lock:
            self.proxies = new_proxies
            self.current_idx = 0
        count = len(new_proxies)
        if count > 0:
            print(f"[FreeProxyPool] Scraped {count} SOCKS5 proxies")
        else:
            print(f"[FreeProxyPool] No proxies found")
        sys.stdout.flush()
        return count

    def get_next(self) -> Optional[str]:
        """Get next proxy from pool (round-robin)."""
        with self._lock:
            if not self.proxies:
                return None
            proxy = self.proxies[self.current_idx % len(self.proxies)]
            self.current_idx += 1
            return proxy

    def test_proxy(self, proxy: str, timeout: int = 5) -> bool:
        """Quick connectivity test."""
        try:
            resp = requests.get(
                "https://httpbin.org/ip",
                proxies={"http": proxy, "https": proxy},
                timeout=timeout,
            )
            return resp.status_code == 200
        except Exception:
            return False

    def get_working_proxies(self, count: int = 4, timeout: int = 5) -> list:
        """Get N working proxies by testing from pool."""
        working = []
        tested = 0
        max_test = min(len(self.proxies), count * 10)  # Test up to 10x candidates
        while len(working) < count and tested < max_test:
            proxy = self.get_next()
            if not proxy:
                break
            if self.test_proxy(proxy, timeout):
                working.append(proxy)
            tested += 1
        return working


# --- Parallel Chunk Downloader -----------------------------------------

def _download_chunk(
    dl_url: str, dest_path: str, file_key: tuple, node_key: tuple,
    file_size: int, start_byte: int, end_byte: int,
    proxy: str = None, chunk_id: int = 0,
) -> bool:
    """Download a byte-range chunk of a MEGA file and decrypt it.

    AES-CTR is a stream cipher so we can decrypt arbitrary offsets by
    computing the correct counter value for *start_byte*.
    """
    try:
        proxies = {'http': proxy, 'https': proxy} if proxy else None
        headers = {'Range': f'bytes={start_byte}-{end_byte}'}
        resp = requests.get(
            dl_url, stream=True, proxies=proxies,
            headers=headers, timeout=(30, 3600),
        )
        if resp.status_code == 509:
            print(f"[Chunk-{chunk_id}] 509 over quota")
            return False
        if resp.status_code not in (200, 206):
            print(f"[Chunk-{chunk_id}] HTTP {resp.status_code}")
            return False

        # Compute AES-CTR counter for this offset
        iv = _get_file_iv(node_key)
        initial_value = int.from_bytes(_a32_to_str(iv), 'big')
        # AES-CTR counter increments per 16-byte block
        block_offset = start_byte // 16
        ctr_value = initial_value + block_offset
        ctr = CryptoCounter.new(128, initial_value=ctr_value)
        cipher = AES.new(_a32_to_str(file_key), AES.MODE_CTR, counter=ctr)

        # If start_byte is not aligned to 16-byte boundary, we need to
        # advance the cipher by the sub-block offset
        sub_offset = start_byte % 16
        if sub_offset > 0:
            cipher.decrypt(b'\x00' * sub_offset)  # Advance cipher state

        # Stream decrypted data directly to disk to avoid OOM on large files
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        # Use 'r+b' if file exists (pre-allocated), else create it
        mode = 'r+b' if os.path.exists(dest_path) else 'wb'
        written = 0
        with open(dest_path, mode) as f:
            f.seek(start_byte)
            for raw_chunk in resp.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                if raw_chunk:
                    decrypted = cipher.decrypt(raw_chunk)
                    f.write(decrypted)
                    written += len(decrypted)

        chunk_mb = written / (1024 * 1024)
        print(f"[Chunk-{chunk_id}] Done ({chunk_mb:.1f} MB, bytes {start_byte}-{end_byte})")
        sys.stdout.flush()
        return True
    except Exception as e:
        print(f"[Chunk-{chunk_id}] Error: {e}")
        sys.stdout.flush()
        return False


def _parallel_download_file(
    dl_url: str, dest_path: str, file_key: tuple, node_key: tuple,
    file_size: int, proxies: list, speed_callback=None,
) -> bool:
    """Download a file using parallel chunks through different proxies.

    Each chunk goes through a different proxy = different IP = separate quota.
    This multiplies effective bandwidth by the number of proxies.
    """
    num_chunks = min(len(proxies), PARALLEL_CHUNKS)
    if num_chunks < 2:
        # Fall back to single-stream download
        return _download_and_decrypt_file(
            dl_url, dest_path, file_key, node_key, file_size,
            proxy=proxies[0] if proxies else None,
            speed_callback=speed_callback,
        )

    chunk_size = file_size // num_chunks
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)

    # Pre-allocate file
    with open(dest_path, 'wb') as f:
        f.truncate(file_size)

    print(f"[Parallel] Downloading {file_size/(1024*1024):.1f} MB in {num_chunks} chunks")
    sys.stdout.flush()
    start_time = time.time()

    results = [False] * num_chunks
    with ThreadPoolExecutor(max_workers=num_chunks) as executor:
        futures = {}
        for i in range(num_chunks):
            start_byte = i * chunk_size
            end_byte = file_size - 1 if i == num_chunks - 1 else (i + 1) * chunk_size - 1
            proxy = proxies[i % len(proxies)]
            future = executor.submit(
                _download_chunk, dl_url, dest_path, file_key, node_key,
                file_size, start_byte, end_byte, proxy, i,
            )
            futures[future] = i

        for future in as_completed(futures):
            idx = futures[future]
            try:
                results[idx] = future.result()
            except Exception as e:
                print(f"[Parallel] Chunk {idx} exception: {e}")
                results[idx] = False

    elapsed = time.time() - start_time
    speed = (file_size / (1024 * 1024)) / max(elapsed, 0.1)

    if all(results):
        # Trim to actual size
        if os.path.getsize(dest_path) > file_size:
            with open(dest_path, 'r+b') as f:
                f.truncate(file_size)
        print(f"[Parallel] Complete: {file_size/(1024*1024):.1f} MB in {elapsed:.1f}s ({speed:.1f} MB/s)")
        sys.stdout.flush()
        if speed_callback:
            speed_callback(file_size, file_size, speed)
        return True
    else:
        failed = [i for i, r in enumerate(results) if not r]
        print(f"[Parallel] Failed chunks: {failed}")
        sys.stdout.flush()
        # Clean up partial file
        if os.path.exists(dest_path):
            os.remove(dest_path)
        return False


# --- IP Rotation Manager -----------------------------------------------

class IPRotator:
    """Manages IP rotation: direct -> WARP (fastest) -> Psiphon -> Tor -> free proxies."""

    def __init__(self):
        self.warp_instances: list = []
        self.psiphon_instances: list = []
        self.external_proxies = self._get_external_proxies()
        self.free_proxy_pool = FreeProxyPool()
        self.current_mode = "direct"
        self.current_warp_idx = 0
        self.current_psiphon_idx = 0
        self.current_external_idx = 0
        self.direct_exhausted = False
        self.warp_available = shutil.which("wireproxy") is not None and shutil.which("wgcf") is not None
        self.warp_failed = False
        self.psiphon_available = os.path.exists(PSIPHON_BINARY)
        self.psiphon_failed = False
        self.tor_manager: Optional[TorManager] = None
        self.tor_available = shutil.which("tor") is not None
        self.tor_failed = False
        self.free_pool_failed = False
        self.rotation_count = 0
        # Log available tools
        if self.warp_available:
            print(f"[IPRotator] WARP (wireproxy+wgcf) found - FASTEST (50-200 Mbps)")
        if self.psiphon_available:
            print(f"[IPRotator] Psiphon found - FAST (5-15 Mbps)")
        if self.tor_available:
            print(f"[IPRotator] Tor found - BACKUP (1-5 Mbps)")
        if not self.warp_available and not self.psiphon_available and not self.tor_available:
            print(f"[IPRotator] No proxy tools found")
        if self.external_proxies:
            print(f"[IPRotator] {len(self.external_proxies)} external proxies configured")
        sys.stdout.flush()
        # Proactively start ALL proxy tools for parallel downloads from the start
        self._warmup_proxies()

    def _get_external_proxies(self) -> list:
        proxies_str = os.getenv("MEGA_PROXIES", "")
        if not proxies_str.strip():
            return []
        return [p.strip() for p in proxies_str.split(",") if p.strip()]

    def _warmup_proxies(self):
        """Proactively start proxy tools at init for parallel downloads.

        Standard plan (2GB RAM) budget:
        - FastAPI + worker: ~150MB
        - WARP (wireproxy): ~80MB per instance
        - Psiphon: ~80MB per instance
        - Tor: ~60MB
        - Budget: ~1.5GB for proxies

        Start everything for maximum parallel channels.
        Each tool wrapped in try/except so failures don't crash the job.
        """
        print("[IPRotator] Warming up ALL proxy tools for parallel downloads...")
        sys.stdout.flush()
        started = 0

        # 1. Start 3 WARP instances (each = separate Cloudflare registration = separate IP)
        # This enables parallel chunk downloads through different IPs
        if self.warp_available:
            for i in range(3):
                try:
                    warp = WARPManager(instance_id=i)
                    if warp.start():
                        self.warp_instances.append(warp)
                        started += 1
                        if i == 0:
                            self.current_mode = "warp"
                            self.current_warp_idx = 0
                    else:
                        if i == 0:
                            self.warp_failed = True
                        print(f"[IPRotator] WARP-{i} not available")
                        break
                except Exception as e:
                    if i == 0:
                        self.warp_failed = True
                    print(f"[IPRotator] WARP-{i} error: {e}")
                    break
                sys.stdout.flush()

        # 2. Start 1 Psiphon instance as backup (~80MB)
        if self.psiphon_available:
            try:
                psiphon = PsiphonManager(instance_id=0)
                if psiphon.start():
                    self.psiphon_instances.append(psiphon)
                    started += 1
                    print(f"[IPRotator] Psiphon-0 ready (port {psiphon.socks_port})")
                    if self.current_mode == "direct":
                        self.current_mode = "psiphon"
                else:
                    self.psiphon_failed = True
                    print(f"[IPRotator] Psiphon-0 failed")
            except Exception as e:
                self.psiphon_failed = True
                print(f"[IPRotator] Psiphon-0 error: {e}")
            sys.stdout.flush()

        # 3. Start Tor (~60MB)
        if self.tor_available:
            try:
                self.tor_manager = TorManager()
                if self.tor_manager.start():
                    started += 1
                    print(f"[IPRotator] Tor ready (port 9050)")
                    if self.current_mode == "direct":
                        self.current_mode = "tor"
                else:
                    self.tor_failed = True
                    print(f"[IPRotator] Tor failed")
            except Exception as e:
                self.tor_failed = True
                print(f"[IPRotator] Tor error: {e}")
            sys.stdout.flush()

        # 4. Pre-fetch free proxy pool (0 memory — just a list of IPs)
        try:
            pool_count = self.free_proxy_pool.refresh()
            if pool_count > 0:
                started += 1
                print(f"[IPRotator] Free proxy pool: {pool_count} proxies")
        except Exception as e:
            print(f"[IPRotator] Free pool error: {e}")
        sys.stdout.flush()

        all_proxies = self.get_all_proxies()
        print(f"[IPRotator] Warmup done: {started} tools, {len(all_proxies)} channels, mode={self.current_mode}")
        sys.stdout.flush()

    def get_current_proxy(self) -> Optional[str]:
        if self.current_mode == "direct":
            return None
        elif self.current_mode == "warp":
            if self.current_warp_idx < len(self.warp_instances):
                return self.warp_instances[self.current_warp_idx].get_proxy_url()
        elif self.current_mode == "tor":
            if self.tor_manager:
                return self.tor_manager.get_proxy_url()
        elif self.current_mode == "psiphon":
            if self.current_psiphon_idx < len(self.psiphon_instances):
                return self.psiphon_instances[self.current_psiphon_idx].get_proxy_url()
        elif self.current_mode == "external":
            if self.current_external_idx < len(self.external_proxies):
                return self.external_proxies[self.current_external_idx]
        elif self.current_mode == "free_pool":
            return self.free_proxy_pool.get_next()
        return None

    def get_proxy_label(self) -> str:
        if self.current_mode == "direct":
            return "direct"
        elif self.current_mode == "warp":
            return f"warp-{self.current_warp_idx}"
        elif self.current_mode == "tor":
            return "tor"
        elif self.current_mode == "psiphon":
            return f"psiphon-{self.current_psiphon_idx}"
        elif self.current_mode == "external":
            return f"proxy-{self.current_external_idx}"
        elif self.current_mode == "free_pool":
            return "free-proxy"
        return "unknown"

    def get_all_proxies(self) -> list:
        """Get a list of all available proxy URLs for parallel downloads.

        Prioritises fast proxies (WARP, Psiphon) and only falls back to
        slower ones (Tor, free pool) when there aren't enough fast proxies
        to fill the chunk count.
        """
        fast = []
        slow = []
        # WARP — fastest (Cloudflare CDN, 50-200 Mbps)
        for w in self.warp_instances:
            url = w.get_proxy_url()
            if url:
                fast.append(url)
        # Psiphon — medium-fast (single hop, 5-15 Mbps)
        for p in self.psiphon_instances:
            url = p.get_proxy_url()
            if url:
                fast.append(url)
        # Tor — slow (3 hops, 1-5 Mbps), only as fallback
        if self.tor_manager and self.tor_manager.connected:
            url = self.tor_manager.get_proxy_url()
            if url:
                slow.append(url)
        # Current proxy if not already included
        current = self.get_current_proxy()
        if current and current not in fast and current not in slow:
            slow.append(current)
        # Return fast proxies first; add slow only if we have < 4 fast
        proxies = fast
        if len(proxies) < 4:
            proxies.extend(slow)
        return proxies if proxies else [None]

    def rotate(self) -> bool:
        """Rotate to next IP. Priority: WARP > Psiphon > Tor > free pool > external > Render restart."""
        self.rotation_count += 1
        print(f"[IPRotator] Rotation #{self.rotation_count} from {self.get_proxy_label()}")
        sys.stdout.flush()
        if self.current_mode == "direct":
            self.direct_exhausted = True

        # 1. Try WARP first (fastest — Cloudflare CDN, 50-200 Mbps)
        if self.warp_available and not self.warp_failed:
            if self.current_mode == "warp" and self.current_warp_idx < len(self.warp_instances):
                warp = self.warp_instances[self.current_warp_idx]
                if warp.restart_for_new_ip():
                    self.current_mode = "warp"
                    print(f"[IPRotator] Rotated to {self.get_proxy_label()} (restarted)")
                    sys.stdout.flush()
                    return True
            idx = len(self.warp_instances)
            if idx < 3:
                warp = WARPManager(instance_id=idx)
                if warp.start():
                    self.warp_instances.append(warp)
                    self.current_warp_idx = idx
                    self.current_mode = "warp"
                    print(f"[IPRotator] Rotated to {self.get_proxy_label()} (new)")
                    sys.stdout.flush()
                    return True
            self.warp_failed = True
            print(f"[IPRotator] WARP unavailable, trying next...")
            sys.stdout.flush()

        # 2. Try Psiphon (fast — single-hop tunnel via CDN, ~5-15 MB/s)
        if self.psiphon_available and not self.psiphon_failed:
            if self.current_mode == "psiphon" and self.current_psiphon_idx < len(self.psiphon_instances):
                psiphon = self.psiphon_instances[self.current_psiphon_idx]
                if psiphon.restart_for_new_ip():
                    self.current_mode = "psiphon"
                    print(f"[IPRotator] Rotated to {self.get_proxy_label()} (restarted)")
                    sys.stdout.flush()
                    return True
            idx = len(self.psiphon_instances)
            if idx < 3:
                psiphon = PsiphonManager(instance_id=idx)
                if psiphon.start():
                    self.psiphon_instances.append(psiphon)
                    self.current_psiphon_idx = idx
                    self.current_mode = "psiphon"
                    print(f"[IPRotator] Rotated to {self.get_proxy_label()} (new)")
                    sys.stdout.flush()
                    return True
                else:
                    self.psiphon_failed = True
                    print(f"[IPRotator] Psiphon failed, trying next...")
                    sys.stdout.flush()

        # 3. Try Tor (slower — 3-hop relay, ~1-5 MB/s)
        if self.tor_available and not self.tor_failed:
            if self.current_mode == "tor" and self.tor_manager and self.tor_manager.connected:
                if self.tor_manager.rotate_ip():
                    print(f"[IPRotator] Rotated Tor circuit (new exit IP)")
                    sys.stdout.flush()
                    return True
            if not self.tor_manager:
                self.tor_manager = TorManager()
            if not self.tor_manager.connected:
                if self.tor_manager.start():
                    self.current_mode = "tor"
                    print(f"[IPRotator] Rotated to tor")
                    sys.stdout.flush()
                    return True
                else:
                    self.tor_failed = True
                    print(f"[IPRotator] Tor failed, trying next...")
                    sys.stdout.flush()

        # 4. Try free SOCKS5 proxy pool
        if not self.free_pool_failed:
            if not self.free_proxy_pool.proxies:
                self.free_proxy_pool.refresh()
            if self.free_proxy_pool.proxies:
                self.current_mode = "free_pool"
                print(f"[IPRotator] Rotated to free proxy pool ({len(self.free_proxy_pool.proxies)} proxies)")
                sys.stdout.flush()
                return True
            self.free_pool_failed = True

        # 5. Try external proxies
        if self.external_proxies:
            self.current_external_idx = (self.current_external_idx + 1) % len(self.external_proxies)
            self.current_mode = "external"
            print(f"[IPRotator] Rotated to {self.get_proxy_label()}")
            sys.stdout.flush()
            return True

        if not self.direct_exhausted:
            self.current_mode = "direct"
            return True

        # 6. Try Render service restart for new IP as last resort
        if RENDER_API_KEY and RENDER_SERVICE_ID:
            print(f"[IPRotator] All IPs exhausted. Trying Render restart...")
            sys.stdout.flush()
            if self._render_restart():
                self._reset_all()
                return True

        # All exhausted - wait and reset
        print(f"[IPRotator] All IPs exhausted. Waiting 60s then resetting...")
        sys.stdout.flush()
        time.sleep(60)
        self._reset_all()
        return True

    def _reset_all(self):
        """Reset all proxy state so rotation starts fresh."""
        for w in self.warp_instances:
            w.stop()
        self.warp_instances.clear()
        self.current_warp_idx = 0
        self.warp_failed = False
        for p in self.psiphon_instances:
            p.stop()
        self.psiphon_instances.clear()
        self.current_psiphon_idx = 0
        self.psiphon_failed = False
        self.tor_failed = False
        self.free_pool_failed = False
        self.direct_exhausted = False
        self.current_mode = "direct"
        print(f"[IPRotator] Full reset complete")
        sys.stdout.flush()

    def _render_restart(self) -> bool:
        """Restart the Render service to get a new IP."""
        try:
            resp = requests.post(
                f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/restart",
                headers={"Authorization": f"Bearer {RENDER_API_KEY}"},
                timeout=10,
            )
            if resp.status_code in (200, 202):
                print(f"[IPRotator] Render restart triggered, waiting 60s...")
                sys.stdout.flush()
                time.sleep(60)
                return True
            else:
                print(f"[IPRotator] Render restart failed: HTTP {resp.status_code}")
                return False
        except Exception as e:
            print(f"[IPRotator] Render restart error: {e}")
            return False

    def cleanup(self):
        for w in self.warp_instances:
            w.stop()
        self.warp_instances.clear()
        if self.tor_manager:
            self.tor_manager.stop()
            self.tor_manager = None
        for p in self.psiphon_instances:
            p.stop()
        self.psiphon_instances.clear()


# --- Main Downloader Class ---------------------------------------------

class MegaDownloader:
    """Downloads files from Mega.nz with Psiphon-based IP rotation for quota bypass.

    Uses Psiphon for free IP rotation (same technique as MegaDownloader.exe):
    1. Download each file through current IP
    2. On 509 quota -> rotate to Psiphon tunnel (new IP in ~3-5s)
    3. Psiphon provides unlimited free IPs via its global tunnel network
    4. No paid proxies, VPNs, or accounts needed
    """

    def __init__(self, download_base: str = "/data/mega_downloads"):
        self.download_base = download_base

    def download_folder(
        self,
        mega_link: str,
        job_id: int,
        excluded_files: list = None,
        progress_callback=None,
        file_done_callback=None,
    ) -> str:
        """Download all files from a Mega folder.

        Args:
            file_done_callback: Optional callback called after each file is
                downloaded successfully.  Signature:
                    file_done_callback(local_path, relative_path, file_name, file_size)
                The callback can upload + delete the file to free disk space
                before the next file is downloaded.
        """
        if excluded_files is None:
            excluded_files = []

        # Clean up old job downloads to free disk space before starting
        self._cleanup_old_downloads(job_id)

        download_path = os.path.join(self.download_base, str(job_id))
        os.makedirs(download_path, exist_ok=True)

        self._update_job_status(job_id, status="downloading", progress=5,
                                current_file="Listing folder contents...")

        ip_rotator = None
        try:
            folder_id, folder_key = _parse_folder_link(mega_link)
            print(f"[MegaDownloader] Folder ID: {folder_id}")
            sys.stdout.flush()

            print("[MegaDownloader] Listing folder contents via Mega API...")
            sys.stdout.flush()
            all_files = _list_folder_files(folder_id, folder_key)
            print(f"[MegaDownloader] Found {len(all_files)} files in folder")
            sys.stdout.flush()

            # Filter excluded files before download
            files_to_download = []
            for f in all_files:
                skip = False
                for excluded in excluded_files:
                    if excluded.strip() and excluded.strip().lower() in f['name'].lower():
                        print(f"[Exclude] Skipping: {f['name']}")
                        skip = True
                        break
                if not skip:
                    files_to_download.append(f)

            total_files = len(files_to_download)
            total_size = sum(f['size'] for f in files_to_download)
            total_size_mb = total_size / (1024 * 1024)
            print(f"[MegaDownloader] {total_files} files to download ({total_size_mb:.1f} MB)")
            sys.stdout.flush()

            self._update_job_status(
                job_id, total_files=total_files,
                current_file=f"Starting download of {total_files} files ({total_size_mb:.1f} MB)",
            )

            # Download with Psiphon-based IP rotation
            ip_rotator = IPRotator()
            downloaded = 0
            downloaded_bytes = 0
            failed_files = []
            download_start_time = time.time()

            for i, file_info in enumerate(files_to_download):
                fpath = file_info['path']
                fsize = file_info['size']
                fsize_mb = fsize / (1024 * 1024)
                dest_path = os.path.join(download_path, fpath)

                print(f"[Download {i+1}/{total_files}] {fpath} ({fsize_mb:.2f} MB)")
                sys.stdout.flush()

                # Resume support: skip already downloaded files
                if os.path.exists(dest_path) and os.path.getsize(dest_path) == fsize:
                    print(f"  Already downloaded, skipping")
                    downloaded += 1
                    downloaded_bytes += fsize
                    continue

                elapsed = time.time() - download_start_time
                avg_speed = (downloaded_bytes / (1024 * 1024)) / max(elapsed, 0.1)
                self._update_job_status(
                    job_id,
                    current_file=f"Downloading: {file_info['name']} ({fsize_mb:.2f} MB)",
                    download_speed=f"{avg_speed:.1f} MB/s",
                    downloaded_files=downloaded,
                    progress=5 + int(35 * downloaded / max(total_files, 1)),
                )

                def make_speed_cb(fname):
                    def cb(dl_bytes, total, speed_mbps):
                        pct = int(100 * dl_bytes / max(total, 1))
                        self._update_job_status(
                            job_id,
                            current_file=f"Downloading: {fname} ({pct}% @ {speed_mbps:.1f} MB/s)",
                            download_speed=f"{speed_mbps:.1f} MB/s",
                        )
                    return cb

                success = self._download_single_file(
                    file_info, folder_id, dest_path, ip_rotator,
                    speed_callback=make_speed_cb(file_info['name']),
                    use_parallel=(fsize >= LARGE_FILE_THRESHOLD),
                )

                if success:
                    downloaded += 1
                    downloaded_bytes += fsize
                    elapsed = time.time() - download_start_time
                    avg_speed = (downloaded_bytes / (1024 * 1024)) / max(elapsed, 0.1)
                    self._update_job_status(
                        job_id,
                        downloaded_files=downloaded,
                        progress=5 + int(35 * downloaded / max(total_files, 1)),
                        download_speed=f"{avg_speed:.1f} MB/s",
                        current_file=f"Downloaded {downloaded}/{total_files} files ({avg_speed:.1f} MB/s avg)",
                    )
                    # Upload + delete immediately to free disk space
                    if file_done_callback:
                        try:
                            file_done_callback(dest_path, fpath, file_info['name'], fsize)
                        except Exception as cb_err:
                            print(f"[file_done_callback] Error: {cb_err}")
                            sys.stdout.flush()
                else:
                    failed_files.append(file_info)
                    print(f"  Failed to download after all IP rotations")
                    sys.stdout.flush()

            # Retry failed files
            if failed_files:
                print(f"[MegaDownloader] Retrying {len(failed_files)} failed files...")
                sys.stdout.flush()
                self._update_job_status(job_id, current_file=f"Retrying {len(failed_files)} failed files...")
                still_failed = []
                for file_info in failed_files:
                    dest_path = os.path.join(download_path, file_info['path'])
                    if os.path.exists(dest_path) and os.path.getsize(dest_path) == file_info['size']:
                        downloaded += 1
                        downloaded_bytes += file_info['size']
                        continue
                    if self._download_single_file(
                        file_info, folder_id, dest_path, ip_rotator,
                        speed_callback=make_speed_cb(file_info['name']),
                    ):
                        downloaded += 1
                        downloaded_bytes += file_info['size']
                        if file_done_callback:
                            try:
                                file_done_callback(
                                    dest_path, file_info['path'],
                                    file_info['name'], file_info['size'],
                                )
                            except Exception as cb_err:
                                print(f"[file_done_callback] Error: {cb_err}")
                                sys.stdout.flush()
                    else:
                        still_failed.append(file_info['name'])
                if still_failed:
                    print(f"[MegaDownloader] {len(still_failed)} files could not be downloaded: {still_failed[:5]}")

            ip_rotator.cleanup()

            if downloaded == 0:
                raise Exception("Download failed: No files could be downloaded.")

            elapsed = time.time() - download_start_time
            avg_speed = (downloaded_bytes / (1024 * 1024)) / max(elapsed, 0.1)
            print(f"[MegaDownloader] Downloaded {downloaded}/{total_files} files "
                  f"({downloaded_bytes / (1024*1024):.1f} MB in {elapsed:.0f}s, avg {avg_speed:.1f} MB/s)")
            sys.stdout.flush()

            self._update_job_status(
                job_id, downloaded_files=downloaded,
                current_file=f"Download complete: {downloaded}/{total_files} files ({avg_speed:.1f} MB/s avg)",
                download_speed=f"{avg_speed:.1f} MB/s",
            )

        except Exception as e:
            if ip_rotator:
                ip_rotator.cleanup()
            raise

        file_count = self._count_files(download_path)
        conn = get_connection()
        conn.execute(
            "UPDATE transfer_jobs SET total_files = ? WHERE id = ?",
            (file_count, job_id),
        )
        conn.commit()
        conn.close()
        return download_path

    def _download_single_file(
        self, file_info: dict, folder_id: str, dest_path: str,
        ip_rotator: IPRotator, max_attempts: int = 15,
        speed_callback=None, use_parallel: bool = False,
    ) -> bool:
        attempts = 0
        while attempts < max_attempts:
            proxy = ip_rotator.get_current_proxy()
            proxy_label = ip_rotator.get_proxy_label()

            dl_url = _get_download_url(file_info['handle'], folder_id, proxy=proxy)
            if dl_url is None:
                print(f"  Quota hit on {proxy_label}, rotating IP...")
                sys.stdout.flush()
                if not ip_rotator.rotate():
                    return False
                attempts += 1
                continue

            # Try parallel chunk download for large files
            if use_parallel and file_info['size'] >= LARGE_FILE_THRESHOLD:
                all_proxies = ip_rotator.get_all_proxies()
                if len(all_proxies) >= 2:
                    print(f"  Parallel download ({len(all_proxies)} proxies) via {proxy_label}...")
                    sys.stdout.flush()
                    success = _parallel_download_file(
                        dl_url=dl_url, dest_path=dest_path,
                        file_key=file_info['file_key'],
                        node_key=file_info['node_key'],
                        file_size=file_info['size'],
                        proxies=all_proxies,
                        speed_callback=speed_callback,
                    )
                    if success:
                        fsize_mb = file_info['size'] / (1024 * 1024)
                        print(f"  OK ({fsize_mb:.2f} MB parallel via {len(all_proxies)} proxies)")
                        sys.stdout.flush()
                        return True
                    # Fall through to single-stream on parallel failure

            success = _download_and_decrypt_file(
                dl_url=dl_url, dest_path=dest_path,
                file_key=file_info['file_key'], node_key=file_info['node_key'],
                file_size=file_info['size'], proxy=proxy,
                speed_callback=speed_callback,
            )
            if success:
                fsize_mb = file_info['size'] / (1024 * 1024)
                print(f"  OK ({fsize_mb:.2f} MB via {proxy_label})")
                sys.stdout.flush()
                return True

            print(f"  Download failed on {proxy_label}, rotating IP...")
            sys.stdout.flush()
            if not ip_rotator.rotate():
                return False
            attempts += 1
            if os.path.exists(dest_path):
                os.remove(dest_path)
        return False

    def _update_job_status(self, job_id: int, **kwargs):
        if not kwargs:
            return
        conn = get_connection()
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [job_id]
        try:
            conn.execute(f"UPDATE transfer_jobs SET {sets} WHERE id = ?", values)
            conn.commit()
        except Exception:
            pass
        finally:
            conn.close()

    def _count_files(self, path: str) -> int:
        count = 0
        for root, dirs, files in os.walk(path):
            count += sum(1 for f in files if not f.startswith('.'))
        return count

    def _cleanup_old_downloads(self, current_job_id: int):
        """Remove download files from ALL previous jobs to free disk space.

        The persistent disk on Render is limited (1-5 GB). Old job files
        should already be uploaded to cloud storage, so they can be safely
        deleted.
        """
        if not os.path.exists(self.download_base):
            return
        freed = 0
        for entry in os.listdir(self.download_base):
            if entry == str(current_job_id):
                continue  # Keep current job's partial downloads for resume
            entry_path = os.path.join(self.download_base, entry)
            if os.path.isdir(entry_path):
                try:
                    dir_size = sum(
                        os.path.getsize(os.path.join(r, f))
                        for r, _, files in os.walk(entry_path)
                        for f in files
                    )
                    shutil.rmtree(entry_path)
                    freed += dir_size
                    print(f"[Cleanup] Removed old job {entry} files ({dir_size / (1024*1024):.1f} MB)")
                except Exception as e:
                    print(f"[Cleanup] Failed to remove {entry}: {e}")
        # Also clean Psiphon/Tor temp data
        for tmp_dir in ["/tmp/psiphon_data", "/tmp/tor_data"]:
            if os.path.exists(tmp_dir):
                try:
                    dir_size = sum(
                        os.path.getsize(os.path.join(r, f))
                        for r, _, files in os.walk(tmp_dir)
                        for f in files
                    )
                    shutil.rmtree(tmp_dir)
                    freed += dir_size
                except Exception:
                    pass
        if freed > 0:
            print(f"[Cleanup] Total freed: {freed / (1024*1024):.1f} MB")
        sys.stdout.flush()

    def cleanup(self, job_id: int):
        download_path = os.path.join(self.download_base, str(job_id))
        if os.path.exists(download_path):
            shutil.rmtree(download_path)
