"""Mega downloader with Mega API-based quota bypass.

Instead of using megadl CLI (which downloads entire folders as a single session
and fails completely on 509 quota errors), this module uses Mega's HTTP API
directly to:
1. List all files in a shared folder
2. Get individual download URLs per file
3. Download and decrypt each file independently
4. Rotate proxies per-file when hitting 509 quota errors

This is the same technique used by MegaBasterd and MegaDownloader.exe:
- Mega's quota is per-IP, resetting when IP changes
- By rotating proxies/IPs between files, we can download unlimited data
- Each file download uses a fresh IP if the previous one was quota-limited
"""

import os
import shutil
import json
import struct
import base64
import time
import sys
import requests
from typing import Optional
from Crypto.Cipher import AES
from Crypto.Util import Counter as CryptoCounter

from app.database import get_connection


# Configuration
DOWNLOAD_WORKERS = int(os.getenv("DOWNLOAD_WORKERS", "3"))
RENDER_API_KEY = os.getenv("RENDER_API_KEY", "")
RENDER_SERVICE_ID = os.getenv("RENDER_SERVICE_ID", "")
DOWNLOAD_CHUNK_SIZE = 1024 * 1024  # 1MB chunks for streaming


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
        with open(dest_path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                if chunk:
                    f.write(cipher.decrypt(chunk))
                    downloaded += len(chunk)

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


# --- Proxy / IP Rotation -----------------------------------------------

def _get_proxy_list() -> list:
    proxies_str = os.getenv("MEGA_PROXIES", "")
    if not proxies_str.strip():
        return []
    return [p.strip() for p in proxies_str.split(",") if p.strip()]


def _trigger_render_restart() -> bool:
    if not RENDER_API_KEY or not RENDER_SERVICE_ID:
        print("[IP Rotation] Render API key or service ID not configured")
        return False
    try:
        import urllib.request
        url = f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/deploys"
        data = json.dumps({"clearCache": "do_not_clear"}).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={"Authorization": f"Bearer {RENDER_API_KEY}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        response = urllib.request.urlopen(req, timeout=30)
        if response.status in (200, 201):
            print("[IP Rotation] Render restart triggered")
            return True
        print(f"[IP Rotation] Render restart returned {response.status}")
        return False
    except Exception as e:
        print(f"[IP Rotation] Render restart failed: {e}")
        return False


class ProxyRotator:
    """Manages proxy rotation for quota bypass."""

    def __init__(self, proxies: list):
        self.proxies = proxies
        self.current_index = -1  # -1 = direct (no proxy)
        self.exhausted: set = set()
        self.direct_exhausted = False

    def get_current_proxy(self) -> Optional[str]:
        if self.current_index < 0:
            return None
        if self.current_index < len(self.proxies):
            return self.proxies[self.current_index]
        return None

    def mark_exhausted(self):
        proxy = self.get_current_proxy()
        if proxy is None:
            self.direct_exhausted = True
            print("[ProxyRotator] Direct connection quota exhausted")
        else:
            self.exhausted.add(proxy)
            print(f"[ProxyRotator] Proxy exhausted: {proxy}")

    def rotate(self) -> bool:
        start = self.current_index
        for _ in range(len(self.proxies) + 2):
            self.current_index += 1
            if self.current_index >= len(self.proxies):
                self.current_index = -1
            proxy = self.get_current_proxy()
            if proxy is None and not self.direct_exhausted:
                print("[ProxyRotator] Rotated to direct connection")
                return True
            if proxy is not None and proxy not in self.exhausted:
                print(f"[ProxyRotator] Rotated to proxy: {proxy}")
                return True
            if self.current_index == start:
                break
        print("[ProxyRotator] All proxies exhausted")
        return False

    def all_exhausted(self) -> bool:
        return self.direct_exhausted and len(self.exhausted) >= len(self.proxies)

    def reset(self):
        self.exhausted.clear()
        self.direct_exhausted = False
        self.current_index = -1
        print("[ProxyRotator] All proxies reset")


# --- Main Downloader Class ---------------------------------------------

class MegaDownloader:
    """Downloads files from Mega.nz public folder links.

    Uses Mega's HTTP API directly (not megadl CLI) for per-file proxy
    rotation, enabling unlimited downloads by bypassing per-IP quota.

    Quota bypass technique (same as MegaBasterd/MegaDownloader.exe):
    1. Use Mega API to get individual file download URLs
    2. Download each file through the current proxy/IP
    3. When hitting 509 quota, rotate to next proxy/IP
    4. Each new IP gets a fresh 5GB quota from Mega
    5. If all proxies exhausted, trigger Render restart for fresh IP
    """

    def __init__(self, download_base: str = "/data/mega_downloads"):
        self.download_base = download_base
        self.proxies = _get_proxy_list()
        if self.proxies:
            print(f"[MegaDownloader] {len(self.proxies)} proxies for rotation")

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

        conn = get_connection()
        conn.execute(
            "UPDATE transfer_jobs SET status = 'downloading' WHERE id = ?",
            (job_id,),
        )
        conn.commit()
        conn.close()

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
            total_size_mb = sum(f['size'] for f in files_to_download) / (1024 * 1024)
            print(f"[MegaDownloader] {total_files} files to download ({total_size_mb:.1f} MB)")
            sys.stdout.flush()

            conn = get_connection()
            conn.execute(
                "UPDATE transfer_jobs SET total_files = ? WHERE id = ?",
                (total_files, job_id),
            )
            conn.commit()
            conn.close()

            # Download each file with proxy rotation for quota bypass
            rotator = ProxyRotator(self.proxies)
            downloaded = 0
            failed_files = []
            render_restart_attempted = False

            for i, file_info in enumerate(files_to_download):
                fpath = file_info['path']
                fsize_mb = file_info['size'] / (1024 * 1024)
                dest_path = os.path.join(download_path, fpath)

                print(f"[Download {i+1}/{total_files}] {fpath} ({fsize_mb:.1f} MB)")
                sys.stdout.flush()

                # Resume support: skip already downloaded files
                if os.path.exists(dest_path) and os.path.getsize(dest_path) == file_info['size']:
                    print(f"  Already downloaded, skipping")
                    downloaded += 1
                    continue

                success = self._download_single_file(
                    file_info, folder_id, dest_path, rotator
                )

                if success:
                    downloaded += 1
                    progress = 10 + int(80 * downloaded / total_files)
                    conn = get_connection()
                    conn.execute(
                        "UPDATE transfer_jobs SET progress = ? WHERE id = ?",
                        (progress, job_id),
                    )
                    conn.commit()
                    conn.close()
                else:
                    if not render_restart_attempted and rotator.all_exhausted():
                        print("[MegaDownloader] All proxies exhausted. Triggering Render restart...")
                        sys.stdout.flush()
                        render_restart_attempted = True
                        if _trigger_render_restart():
                            self._save_resume_state(job_id, download_path, downloaded)
                            raise Exception(
                                "RENDER_RESTART: Job will resume with fresh IP."
                            )
                    failed_files.append(file_info)

            # Retry failed files with reset proxies
            if failed_files:
                print(f"[MegaDownloader] Retrying {len(failed_files)} failed files...")
                sys.stdout.flush()
                rotator.reset()
                for file_info in failed_files:
                    dest_path = os.path.join(download_path, file_info['path'])
                    if self._download_single_file(file_info, folder_id, dest_path, rotator):
                        downloaded += 1

            if downloaded == 0:
                raise Exception(
                    "Download failed: No files could be downloaded. "
                    "Mega quota may be exceeded on all available IPs."
                )

            print(f"[MegaDownloader] Downloaded {downloaded}/{total_files} files")
            sys.stdout.flush()

        except Exception as e:
            if "RENDER_RESTART" in str(e):
                raise
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
        rotator: ProxyRotator, max_proxy_attempts: int = 10,
    ) -> bool:
        attempts = 0
        while attempts < max_proxy_attempts:
            proxy = rotator.get_current_proxy()
            proxy_label = proxy or 'direct'

            dl_url = _get_download_url(file_info['handle'], folder_id, proxy=proxy)
            if dl_url is None:
                print(f"  Quota hit on {proxy_label}, rotating...")
                sys.stdout.flush()
                rotator.mark_exhausted()
                if not rotator.rotate():
                    return False
                attempts += 1
                continue

            success = _download_and_decrypt_file(
                dl_url=dl_url, dest_path=dest_path,
                file_key=file_info['file_key'], node_key=file_info['node_key'],
                file_size=file_info['size'], proxy=proxy,
            )
            if success:
                return True

            print(f"  Download failed on {proxy_label}, rotating...")
            sys.stdout.flush()
            rotator.mark_exhausted()
            if not rotator.rotate():
                return False
            attempts += 1
            if os.path.exists(dest_path):
                os.remove(dest_path)
        return False

    def _save_resume_state(self, job_id: int, download_path: str, downloaded: int):
        state = {'downloaded': downloaded}
        state_path = os.path.join(download_path, '.resume_state.json')
        with open(state_path, 'w') as f:
            json.dump(state, f)
        print(f"[Resume] Saved state: {downloaded} files downloaded")

    def _count_files(self, path: str) -> int:
        count = 0
        for root, dirs, files in os.walk(path):
            count += sum(1 for f in files if not f.startswith('.'))
        return count

    def cleanup(self, job_id: int):
        download_path = os.path.join(self.download_base, str(job_id))
        if os.path.exists(download_path):
            shutil.rmtree(download_path)
