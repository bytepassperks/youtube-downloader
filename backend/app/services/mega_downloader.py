"""Mega downloader with Psiphon-based quota bypass.

Uses Psiphon (free, open-source VPN/proxy) to rotate IPs automatically.
On 509 quota hit: kill Psiphon, restart, get new IP in ~3-5s, retry download.
"""

import os
import shutil
import json
import struct
import base64
import time
import sys
import subprocess
import requests
from typing import Optional
from Crypto.Cipher import AES
from Crypto.Util import Counter as CryptoCounter

from app.database import get_connection


# Configuration
DOWNLOAD_CHUNK_SIZE = 1024 * 1024  # 1MB chunks for streaming
PSIPHON_BINARY = os.getenv("PSIPHON_BINARY", "/usr/local/bin/psiphon-tunnel-core")
PSIPHON_BASE_SOCKS_PORT = 10800
PSIPHON_BASE_HTTP_PORT = 10900
PSIPHON_CONNECT_TIMEOUT = 20


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
    except Exception as e:
        print(f"[Download] Error: {e}")
        return False


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
            "RemoteServerListSignaturePublicKey": "",
            "RemoteServerListUrl": "",
            "SponsorId": "FFFFFFFFFFFFFFFF",
            "UseIndistinguishableTLS": True,
            "DataStoreDirectory": self.data_dir,
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
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
        except Exception as e:
            print(f"[Psiphon-{self.instance_id}] Failed to start: {e}")
            return False
        start_time = time.time()
        while time.time() - start_time < PSIPHON_CONNECT_TIMEOUT:
            if self.process.poll() is not None:
                print(f"[Psiphon-{self.instance_id}] Process exited prematurely")
                return False
            line = self.process.stdout.readline()
            if not line:
                time.sleep(0.1)
                continue
            if '"noticeType":"Tunnels"' in line and '"count":1' in line:
                self.connected = True
                elapsed = time.time() - start_time
                print(f"[Psiphon-{self.instance_id}] Connected in {elapsed:.1f}s (SOCKS5: {self.socks_port})")
                sys.stdout.flush()
                return True
        print(f"[Psiphon-{self.instance_id}] Connection timeout after {PSIPHON_CONNECT_TIMEOUT}s")
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

class IPRotator:
    """Manages IP rotation: direct -> Psiphon tunnels -> external proxies."""

    def __init__(self):
        self.psiphon_instances: list = []
        self.external_proxies = self._get_external_proxies()
        self.current_mode = "direct"
        self.current_psiphon_idx = 0
        self.current_external_idx = 0
        self.direct_exhausted = False
        self.psiphon_available = os.path.exists(PSIPHON_BINARY)
        self.rotation_count = 0
        if self.psiphon_available:
            print(f"[IPRotator] Psiphon binary found - free IP rotation available")
        else:
            print(f"[IPRotator] Psiphon binary not found at {PSIPHON_BINARY}")
        if self.external_proxies:
            print(f"[IPRotator] {len(self.external_proxies)} external proxies configured")
        sys.stdout.flush()

    def _get_external_proxies(self) -> list:
        proxies_str = os.getenv("MEGA_PROXIES", "")
        if not proxies_str.strip():
            return []
        return [p.strip() for p in proxies_str.split(",") if p.strip()]

    def get_current_proxy(self) -> Optional[str]:
        if self.current_mode == "direct":
            return None
        elif self.current_mode == "psiphon":
            if self.current_psiphon_idx < len(self.psiphon_instances):
                return self.psiphon_instances[self.current_psiphon_idx].get_proxy_url()
        elif self.current_mode == "external":
            if self.current_external_idx < len(self.external_proxies):
                return self.external_proxies[self.current_external_idx]
        return None

    def get_proxy_label(self) -> str:
        if self.current_mode == "direct":
            return "direct"
        elif self.current_mode == "psiphon":
            return f"psiphon-{self.current_psiphon_idx}"
        elif self.current_mode == "external":
            return f"proxy-{self.current_external_idx}"
        return "unknown"

    def rotate(self) -> bool:
        """Rotate to next IP. Returns True if a new IP is available."""
        self.rotation_count += 1
        print(f"[IPRotator] Rotation #{self.rotation_count} from {self.get_proxy_label()}")
        sys.stdout.flush()
        if self.current_mode == "direct":
            self.direct_exhausted = True
        # Try Psiphon rotation
        if self.psiphon_available:
            if self.current_mode == "psiphon" and self.current_psiphon_idx < len(self.psiphon_instances):
                psiphon = self.psiphon_instances[self.current_psiphon_idx]
                if psiphon.restart_for_new_ip():
                    self.current_mode = "psiphon"
                    print(f"[IPRotator] Rotated to {self.get_proxy_label()} (restarted)")
                    sys.stdout.flush()
                    return True
            # Create new Psiphon instance (max 3)
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
            # Restart instance 0 for new IP
            if self.psiphon_instances:
                psiphon = self.psiphon_instances[0]
                if psiphon.restart_for_new_ip():
                    self.current_psiphon_idx = 0
                    self.current_mode = "psiphon"
                    print(f"[IPRotator] Rotated to psiphon-0 (recycled)")
                    sys.stdout.flush()
                    return True
        # Try external proxies
        if self.external_proxies:
            self.current_external_idx = (self.current_external_idx + 1) % len(self.external_proxies)
            self.current_mode = "external"
            print(f"[IPRotator] Rotated to {self.get_proxy_label()}")
            sys.stdout.flush()
            return True
        if not self.direct_exhausted:
            self.current_mode = "direct"
            return True
        # All exhausted - wait and reset Psiphon
        print(f"[IPRotator] All IPs exhausted. Waiting 30s then resetting...")
        sys.stdout.flush()
        time.sleep(30)
        self._reset_psiphon()
        return True

    def _reset_psiphon(self):
        for p in self.psiphon_instances:
            p.stop()
        self.psiphon_instances.clear()
        self.current_psiphon_idx = 0
        self.direct_exhausted = False
        self.current_mode = "direct"
        print(f"[IPRotator] Reset complete")
        sys.stdout.flush()

    def cleanup(self):
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
    ) -> str:
        if excluded_files is None:
            excluded_files = []

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
        speed_callback=None,
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

    def cleanup(self, job_id: int):
        download_path = os.path.join(self.download_base, str(job_id))
        if os.path.exists(download_path):
            shutil.rmtree(download_path)
