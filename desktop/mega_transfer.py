#!/usr/bin/env python3
"""
MegaTransfer Desktop - Mega -> iDrive S3 Transfer Tool
Full-featured Windows desktop app with:
- Multi-threaded parallel downloads (8-16 connections per file)
- Psiphon IP rotation for unlimited Mega quota bypass
- Auto-upload to iDrive S3
- File exclusion patterns
- Telegram notifications
- Download queue with pause/resume
- Comprehensive debug logging
"""

import os
import sys
import json
import struct
import base64
import time
import re
import logging
import threading
import queue
import hashlib
import subprocess
import tempfile
import socket
import traceback
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from Crypto.Cipher import AES
from Crypto.Util import Counter as CryptoCounter
import boto3
from botocore.config import Config as BotoConfig

# ============================================================================
# LOGGING SETUP
# ============================================================================
LOG_DIR = os.path.join(os.path.expanduser("~"), "MegaTransfer", "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, f"megatransfer_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")

logger = logging.getLogger("MegaTransfer")
logger.setLevel(logging.DEBUG)

# File handler - debug level (everything)
fh = logging.FileHandler(LOG_FILE, encoding='utf-8')
fh.setLevel(logging.DEBUG)
fh.setFormatter(logging.Formatter(
    '%(asctime)s [%(levelname)s] %(name)s.%(funcName)s:%(lineno)d - %(message)s'
))
logger.addHandler(fh)

# Console handler - info level
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
ch.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
logger.addHandler(ch)

logger.info(f"Log file: {LOG_FILE}")


# ============================================================================
# SETTINGS
# ============================================================================
SETTINGS_DIR = os.path.join(os.path.expanduser("~"), "MegaTransfer")
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")

DEFAULT_SETTINGS = {
    "idrive_endpoint": "https://s3.us-west-2.idrivee2.com",
    "idrive_access_key": "",
    "idrive_secret_key": "",
    "idrive_bucket": "mega-transfers",
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "portal_domain": "https://bytecourses.online",
    "parallel_threads": 16,
    "exclude_files": ["edollarearn.com.url", "Upgrade Your Account VIP - edollarearn.com.txt"],
    "download_path": os.path.join(os.path.expanduser("~"), "MegaTransfer", "downloads"),
    "download_mode": "direct_to_s3",  # "direct_to_s3", "local_and_s3", "local_only"
    "use_psiphon": True,
    "psiphon_rotate_on_quota": True,
    "chunk_size_mb": 8,
    "max_retries": 10,
    "small_file_threshold_mb": 20,
}


def load_settings():
    """Load settings from JSON file, merging with defaults."""
    os.makedirs(SETTINGS_DIR, exist_ok=True)
    settings = dict(DEFAULT_SETTINGS)
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r') as f:
                saved = json.load(f)
            settings.update(saved)
            logger.debug(f"Loaded settings from {SETTINGS_FILE}")
        except Exception as e:
            logger.warning(f"Could not load settings: {e}")
    return settings


def save_settings(settings):
    """Save settings to JSON file."""
    os.makedirs(SETTINGS_DIR, exist_ok=True)
    try:
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(settings, f, indent=2)
        logger.debug(f"Saved settings to {SETTINGS_FILE}")
    except Exception as e:
        logger.error(f"Could not save settings: {e}")


# ============================================================================
# MEGA API
# ============================================================================
def a32_to_str(a):
    return struct.pack('>%dI' % len(a), *a)

def str_to_a32(b):
    if len(b) % 4:
        b += b'\0' * (4 - len(b) % 4)
    return struct.unpack('>%dI' % (len(b) // 4), b)

def base64_url_decode(data):
    data += '=' * (-len(data) % 4)
    return base64.urlsafe_b64decode(data)

def decrypt_node_key(encrypted_key_b64, folder_key, is_folder=False):
    encrypted_key = base64_url_decode(encrypted_key_b64)
    key_a32 = str_to_a32(encrypted_key)
    if len(key_a32) == 4:
        if is_folder:
            # In shared folders, folder keys are AES-ECB encrypted with the share key
            cipher = AES.new(a32_to_str(folder_key), AES.MODE_ECB)
            decrypted = cipher.decrypt(a32_to_str(key_a32))
            return str_to_a32(decrypted)
        else:
            return tuple(a ^ b for a, b in zip(key_a32, folder_key))
    elif len(key_a32) == 8:
        cipher = AES.new(a32_to_str(folder_key), AES.MODE_ECB)
        decrypted = cipher.decrypt(a32_to_str(key_a32[:4])) + \
                    cipher.decrypt(a32_to_str(key_a32[4:]))
        return str_to_a32(decrypted)
    return key_a32

def get_file_key(node_key):
    if len(node_key) == 8:
        return (node_key[0] ^ node_key[4], node_key[1] ^ node_key[5],
                node_key[2] ^ node_key[6], node_key[3] ^ node_key[7])
    return node_key

def get_file_iv(node_key):
    if len(node_key) >= 6:
        return (node_key[4], node_key[5], 0, 0)
    return (0, 0, 0, 0)

def decrypt_attr(attr_data, key):
    cipher = AES.new(a32_to_str(key), AES.MODE_CBC, b'\0' * 16)
    decrypted = cipher.decrypt(attr_data)
    try:
        idx = decrypted.index(b'MEGA')
        json_str = decrypted[idx+4:].rstrip(b'\0').decode('utf-8', errors='ignore')
        brace = json_str.rfind('}')
        if brace >= 0:
            json_str = json_str[:brace+1]
        return json.loads(json_str)
    except Exception:
        return None


class MegaAPI:
    """Mega API client with proxy support and quota handling."""

    def __init__(self, proxy=None):
        self.proxy = proxy
        self.session = self._create_session()
        self._req_id = 1

    def _create_session(self):
        session = requests.Session()
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503])
        adapter = HTTPAdapter(max_retries=retry)
        session.mount('https://', adapter)
        session.mount('http://', adapter)
        if self.proxy:
            session.proxies = {"http": self.proxy, "https": self.proxy}
        return session

    def set_proxy(self, proxy):
        """Update proxy for this API client."""
        self.proxy = proxy
        self.session = self._create_session()
        logger.debug(f"API proxy set to: {proxy}")

    def get_folder_files(self, folder_url, exclude_files=None):
        """Fetch all files from a Mega folder URL."""
        exclude_files = exclude_files or []
        m = re.match(r'https://mega\.nz/folder/([^#]+)#(.*)', folder_url)
        if not m:
            raise ValueError(f"Invalid Mega folder URL: {folder_url}")
        folder_id, folder_key_b64 = m.groups()
        master_key = str_to_a32(base64_url_decode(folder_key_b64))

        logger.info(f"Fetching folder {folder_id}...")
        resp = self.session.post(
            "https://g.api.mega.co.nz/cs",
            json=[{"a": "f", "c": 1, "r": 1, "ca": 1}],
            params={"id": self._req_id, "n": folder_id},
            timeout=30
        )
        self._req_id += 1
        data = resp.json()[0]

        # Two-pass: first collect all folders, then process files
        # This ensures parent folders are available when building file paths
        folders = {}
        raw_files = []
        root_handle = None

        for node in data.get('f', []):
            key_str = node.get('k', '')
            if ':' in key_str:
                key_str = key_str.split(':')[1]
            try:
                node_key = decrypt_node_key(key_str, master_key)
            except Exception:
                continue

            if node.get('t') in (1, 2):  # folder (1=subfolder, 2=root)
                node_key = decrypt_node_key(key_str, master_key, is_folder=True)
                fk = node_key  # For folders, the decrypted key IS the attr key
                attr = decrypt_attr(base64_url_decode(node.get('a', '')), fk)
                name = attr.get('n', 'Unknown') if attr else 'Unknown'
                parent = node.get('p', '')
                folders[node['h']] = {'name': name, 'parent': parent}
                # Root folder: parent not in this response
                if parent not in [n['h'] for n in data.get('f', [])]:
                    root_handle = node['h']
            elif node.get('t') == 0:  # file
                raw_files.append(node)

        # Second pass: process files with full folder tree available
        files = []
        for node in raw_files:
            key_str = node.get('k', '')
            if ':' in key_str:
                key_str = key_str.split(':')[1]
            try:
                node_key = decrypt_node_key(key_str, master_key)
            except Exception:
                continue

            fk = get_file_key(node_key)
            iv = get_file_iv(node_key)
            iv_int = (iv[0] << 32) | iv[1]
            attr = decrypt_attr(base64_url_decode(node.get('a', '')), fk)
            name = attr.get('n', 'Unknown') if attr else 'Unknown'

            if name in exclude_files:
                logger.info(f"  [EXCLUDE] {name}")
                continue

            # Build full path by walking up the folder tree
            path_parts = [name]
            parent = node.get('p', '')
            visited = set()
            while parent in folders and parent not in visited:
                visited.add(parent)
                # Skip the root folder name (it's the S3 prefix)
                if parent == root_handle:
                    break
                path_parts.insert(0, folders[parent]['name'])
                parent = folders[parent]['parent']

            files.append({
                'node_id': node['h'],
                'name': name,
                'path': '/'.join(path_parts),
                'size': node.get('s', 0),
                'key': fk,
                'iv_int': iv_int,
                'folder_id': folder_id,
                'node_key': node_key,
            })

        files.sort(key=lambda f: f['size'], reverse=True)
        logger.info(f"  Found {len(files)} files (after exclusions)")
        return files, folder_id

    def get_download_url(self, node_id, folder_id, proxy=None):
        """Get download URL for a file node."""
        session = self.session
        if proxy and proxy != self.proxy:
            session = requests.Session()
            session.proxies = {"http": proxy, "https": proxy}

        for attempt in range(5):
            try:
                resp = session.post(
                    "https://g.api.mega.co.nz/cs",
                    json=[{"a": "g", "g": 1, "n": node_id}],
                    params={"id": self._req_id, "n": folder_id},
                    timeout=30
                )
                self._req_id += 1
                json_resp = resp.json()
                result = json_resp[0] if isinstance(json_resp, list) and len(json_resp) > 0 else json_resp

                if isinstance(result, int):
                    error_code = result
                    if error_code == -9:
                        logger.warning(f"File {node_id} not found (error -9)")
                        return None
                    elif error_code == -18:
                        wait = 30 * (attempt + 1)
                        logger.warning(f"Mega quota exceeded (-18) for {node_id}, waiting {wait}s...")
                        time.sleep(wait)
                        continue
                    elif error_code == -16:
                        logger.warning(f"File {node_id} blocked by Mega (EBLOCKED -16)")
                        return None
                    else:
                        wait = 10 * (attempt + 1)
                        logger.warning(f"Mega API error {error_code} for {node_id}, waiting {wait}s (attempt {attempt+1}/5)")
                        time.sleep(wait)
                        continue

                if isinstance(result, dict):
                    url = result.get('g')
                    if url:
                        logger.debug(f"Got download URL for {node_id}")
                        return url
                    logger.warning(f"No 'g' in response for {node_id}: {result}")

                time.sleep(5)
            except Exception as e:
                logger.warning(f"get_download_url attempt {attempt+1}: {e}")
                time.sleep(5)
        return None


# ============================================================================
# PSIPHON MANAGER
# ============================================================================
# Uses Psiphon3.exe (the official Psiphon Windows application) for IP rotation.
#
# WHY Psiphon3.exe and NOT psiphon-tunnel-core:
#   The bare psiphon-tunnel-core binary from psiphon-tunnel-core-binaries repo
#   does NOT have embedded server entries, PropagationChannelId, SponsorId, or
#   remote server list URLs. It fails with "no broker specs" because the latest
#   version requires inproxy broker specs for server discovery (DSL fetch).
#
#   Psiphon3.exe (the full Windows app) has ALL of these compiled in:
#   - Embedded server entries for initial connection
#   - Valid PropagationChannelId and SponsorId
#   - Remote server list URLs + signature public key
#   - Proper broker specs for DSL fetch
#
# How it works:
#   1. User places Psiphon3.exe in ~/MegaTransfer/ or beside MegaTransfer.exe
#   2. We launch Psiphon3.exe (it auto-connects and creates local HTTP proxy)
#   3. Default HTTP proxy port is 8080 (we detect via netstat/connection test)
#   4. For IP rotation: kill process, wait, restart -> new server -> new IP
#
# Download: https://psiphon.ca/en/download.html (official ~10 MB portable exe)
PSIPHON3_DOWNLOAD_URL = "https://github.com/AliGhaleworkaround/Psiphon/releases/download/v186/psiphon3.exe"
PSIPHON3_DOWNLOAD_MIRRORS = [
    # Official GitHub releases (Psiphon-Inc/psiphon-windows) don't publish
    # pre-built binaries, so we use well-known mirrors. The user can also
    # manually download from https://psiphon.ca/en/download.html and place
    # psiphon3.exe in ~/MegaTransfer/ or beside MegaTransfer.exe.
]


class PsiphonManager:
    """Manages Psiphon3.exe (full Windows app) for IP rotation.

    Psiphon3.exe is the official Psiphon Windows client that:
    - Has embedded server entries (unlike bare psiphon-tunnel-core)
    - Auto-connects to Psiphon network on launch
    - Creates local HTTP proxy (default port 8080) and SOCKS proxy
    - Handles all server discovery, authentication, and tunnel management

    For IP rotation: stop -> wait -> restart -> new tunnel -> new IP.
    """

    def __init__(self, log_callback=None):
        self.process = None
        self.proxy_port = 0
        self.proxy_url = None
        self.current_ip = None
        self._lock = threading.Lock()
        self._log_callback = log_callback
        self._tunnel_connected = threading.Event()
        self.psiphon_path = self._find_psiphon()
        self.data_dir = os.path.join(SETTINGS_DIR, "psiphon_data")

    def _log(self, msg):
        """Log message to logger and optional UI callback."""
        logger.info(msg)
        if self._log_callback:
            try:
                self._log_callback(msg)
            except Exception:
                pass

    def _find_psiphon(self):
        """Find Psiphon3.exe (full Windows app).

        Search order:
        1. ~/MegaTransfer/psiphon3.exe (settings directory)
        2. Same directory as MegaTransfer.exe
        3. psiphon/ subdirectory
        Also checks for the old psiphon-tunnel-core.exe name.
        """
        app_dir = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            os.path.join(SETTINGS_DIR, "psiphon3.exe"),
            os.path.join(SETTINGS_DIR, "Psiphon3.exe"),
            os.path.join(app_dir, "psiphon3.exe"),
            os.path.join(app_dir, "Psiphon3.exe"),
            os.path.join(app_dir, "psiphon", "psiphon3.exe"),
            os.path.join(app_dir, "psiphon", "Psiphon3.exe"),
            # Legacy: also check for old psiphon-tunnel-core binary name
            os.path.join(SETTINGS_DIR, "psiphon-tunnel-core.exe"),
            os.path.join(app_dir, "psiphon-tunnel-core.exe"),
        ]
        for c in candidates:
            if os.path.exists(c):
                file_size = os.path.getsize(c)
                if file_size > 1_000_000:  # Binary should be >1MB
                    logger.info(f"Found Psiphon at: {c} ({file_size/1024/1024:.1f} MB)")
                    return c
        logger.warning("Psiphon3.exe not found locally.")
        return None

    def download_psiphon(self):
        """Auto-download Psiphon3.exe (official Windows client) if not found.

        Downloads from the official Psiphon S3 distribution URL.
        Psiphon3.exe is ~10 MB and is a single portable executable.
        """
        dest = os.path.join(SETTINGS_DIR, "psiphon3.exe")

        if os.path.exists(dest):
            file_size = os.path.getsize(dest)
            if file_size > 1_000_000:
                self.psiphon_path = dest
                self._log(f"Psiphon3.exe already downloaded ({file_size/1024/1024:.1f} MB)")
                return True
            else:
                os.remove(dest)

        # Also check if old psiphon-tunnel-core.exe exists and remove it
        old_binary = os.path.join(SETTINGS_DIR, "psiphon-tunnel-core.exe")
        if os.path.exists(old_binary):
            self._log("Removing old psiphon-tunnel-core.exe (replaced by Psiphon3.exe)")
            try:
                os.remove(old_binary)
            except Exception:
                pass

        os.makedirs(SETTINGS_DIR, exist_ok=True)
        self._log("Downloading Psiphon3.exe (official Windows client)...")

        urls = PSIPHON3_DOWNLOAD_MIRRORS + [PSIPHON3_DOWNLOAD_URL]
        for url in urls:
            try:
                self._log(f"  Trying: {url.split('/')[-1]} from {'/'.join(url.split('/')[2:4])}")
                resp = requests.get(url, timeout=120, stream=True)
                if resp.status_code != 200:
                    self._log(f"  HTTP {resp.status_code}, trying next mirror...")
                    continue

                total = int(resp.headers.get('content-length', 0))
                downloaded = 0
                tmp_path = dest + ".tmp"
                with open(tmp_path, 'wb') as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total > 0:
                            pct = downloaded * 100 // total
                            self._log(f"  Downloading Psiphon: {pct}% ({downloaded/1024/1024:.1f}/{total/1024/1024:.1f} MB)")

                if os.path.getsize(tmp_path) < 1_000_000:
                    self._log("  Download too small, trying next mirror...")
                    os.remove(tmp_path)
                    continue

                os.rename(tmp_path, dest)
                self.psiphon_path = dest
                self._log(f"Psiphon3.exe downloaded successfully ({os.path.getsize(dest)/1024/1024:.1f} MB)")
                return True

            except Exception as e:
                self._log(f"  Download failed: {e}")
                if os.path.exists(dest + ".tmp"):
                    os.remove(dest + ".tmp")
                continue

        self._log("[WARN] Could not download Psiphon3.exe. IP rotation will not be available.")
        self._log("[WARN] You can manually download from: https://psiphon.ca/en/download.html")
        self._log("[WARN] Place psiphon3.exe in: " + SETTINGS_DIR)
        return False

    def _detect_proxy_port(self):
        """Detect which HTTP proxy port Psiphon3.exe is listening on.

        Psiphon3.exe creates a local HTTP proxy (default 8080).
        We try common ports and verify with a test request.
        """
        # Try common Psiphon proxy ports
        candidate_ports = [8080, 8081, 8090, 8888, 58080]

        for port in candidate_ports:
            try:
                # Quick TCP connect test
                import socket
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2)
                result = sock.connect_ex(('127.0.0.1', port))
                sock.close()
                if result == 0:
                    # Port is open, verify it's an HTTP proxy
                    try:
                        test_url = "http://example.com"
                        proxy = f"http://127.0.0.1:{port}"
                        r = requests.get(test_url, proxies={"http": proxy}, timeout=10)
                        if r.status_code == 200:
                            self._log(f"  Detected Psiphon HTTP proxy on port {port}")
                            return port
                    except Exception:
                        continue
            except Exception:
                continue

        return None

    def _wait_for_proxy(self, timeout=120):
        """Wait for Psiphon3.exe to establish tunnel and create local proxy.

        Psiphon3.exe auto-connects on launch. We poll for the proxy port
        to become available, which indicates the tunnel is established.

        Args:
            timeout: Maximum seconds to wait for proxy to become available.
        """
        import socket
        start_time = time.time()
        # Psiphon3.exe default HTTP proxy port is 8080
        candidate_ports = [8080, 8081, 8090, 8888, 58080]

        self._log(f"  Waiting for Psiphon tunnel to connect (up to {timeout}s)...")

        while time.time() - start_time < timeout:
            # Check if process is still running
            if self.process and self.process.poll() is not None:
                self._log("  Psiphon3.exe process exited unexpectedly")
                return False

            for port in candidate_ports:
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(2)
                    result = sock.connect_ex(('127.0.0.1', port))
                    sock.close()
                    if result == 0:
                        # Port is open - verify it's actually a working HTTP proxy
                        try:
                            proxy = f"http://127.0.0.1:{port}"
                            r = requests.get(
                                "https://ifconfig.me/ip",
                                proxies={"http": proxy, "https": proxy},
                                timeout=15
                            )
                            if r.status_code == 200:
                                self.proxy_port = port
                                self.proxy_url = proxy
                                self.current_ip = r.text.strip()
                                self._log(f"  Psiphon tunnel established!")
                                self._log(f"  Psiphon HTTP proxy on port {port}")
                                self._log(f"  Psiphon ready -> IP: {self.current_ip}")
                                self._tunnel_connected.set()
                                return True
                        except requests.exceptions.ProxyError:
                            # Proxy port is open but tunnel not ready yet
                            pass
                        except Exception:
                            pass
                except Exception:
                    pass

            time.sleep(3)

        self._log(f"  Psiphon tunnel did not connect within {timeout}s")
        return False

    def start(self, port=8080):
        """Start Psiphon3.exe (full Windows app). Auto-downloads if needed.

        Psiphon3.exe auto-connects on launch and creates a local HTTP proxy.
        Default HTTP proxy port is 8080 (we auto-detect the actual port).

        Args:
            port: Hint for expected HTTP proxy port (default 8080).
        """
        if not self.psiphon_path:
            self._log("Psiphon3.exe not found, attempting auto-download...")
            if not self.download_psiphon():
                self._log("Psiphon not available, using direct connection")
                return False

        with self._lock:
            self.stop()
            self._tunnel_connected.clear()
            self.proxy_port = 0

            self._log(f"Starting Psiphon3.exe (full Windows client)...")
            try:
                # Launch Psiphon3.exe - it's a GUI app that auto-connects
                # SW_SHOWMINIMIZED via startupinfo to minimize the window
                startupinfo = None
                creationflags = 0
                if hasattr(subprocess, 'STARTUPINFO'):
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    startupinfo.wShowWindow = 6  # SW_MINIMIZE
                if hasattr(subprocess, 'CREATE_NO_WINDOW'):
                    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

                self.process = subprocess.Popen(
                    [self.psiphon_path],
                    startupinfo=startupinfo,
                    creationflags=creationflags,
                )
            except Exception as e:
                self._log(f"  Failed to start Psiphon3.exe: {e}")
                return False

            # Wait for the tunnel to establish and proxy to become available
            connected = self._wait_for_proxy(timeout=120)

            if not connected:
                self.stop()
                return False

            return True

    def rotate(self):
        """Rotate to a new Psiphon server (new IP).

        Kill Psiphon3.exe, wait for cleanup, restart.
        Each restart connects to a different server -> different IP.
        """
        self._log("Rotating Psiphon IP...")
        old_ip = self.current_ip
        self.stop()
        time.sleep(5)  # Wait for Psiphon to fully cleanup
        success = self.start(self.proxy_port or 8080)
        if success:
            self._log(f"Psiphon rotated: {old_ip} -> {self.current_ip}")
        return success

    def stop(self):
        """Stop Psiphon3.exe."""
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=10)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None

        # Also kill any other Psiphon3.exe instances that may be lingering
        if sys.platform == 'win32':
            try:
                subprocess.run(
                    ["taskkill", "/f", "/im", "psiphon3.exe"],
                    capture_output=True, timeout=5
                )
            except Exception:
                pass

        self.proxy_url = None
        self.proxy_port = 0
        self.current_ip = None
        self._tunnel_connected.clear()
        logger.debug("Psiphon stopped")

    def is_running(self):
        return self.process is not None and self.process.poll() is None


# ============================================================================
# DOWNLOAD ENGINE
# ============================================================================
class DownloadEngine:
    """Handles multi-threaded parallel downloads from Mega with quota bypass."""

    def __init__(self, settings, psiphon=None, progress_callback=None, log_callback=None):
        self.settings = settings
        self.psiphon = psiphon
        self.mega_api = MegaAPI()
        self.s3_client = None
        self.progress_callback = progress_callback
        self.log_callback = log_callback
        self._paused = threading.Event()
        self._paused.set()  # Not paused initially
        self._cancelled = False
        self._current_proxy = None
        self._quota_hit_count = 0

    def log(self, msg, level="info"):
        """Log a message and send to UI callback."""
        getattr(logger, level)(msg)
        if self.log_callback:
            try:
                self.log_callback(msg)
            except Exception:
                pass

    def _init_s3(self):
        """Initialize S3 client."""
        self.s3_client = boto3.client(
            's3',
            endpoint_url=self.settings['idrive_endpoint'],
            aws_access_key_id=self.settings['idrive_access_key'],
            aws_secret_access_key=self.settings['idrive_secret_key'],
            config=BotoConfig(signature_version='s3v4')
        )
        logger.debug("S3 client initialized")

    def _get_proxy(self):
        """Get current proxy URL (Psiphon or None)."""
        if self.psiphon and self.psiphon.is_running():
            return self.psiphon.proxy_url
        return None

    def _handle_quota(self):
        """Handle quota exhaustion by rotating Psiphon IP."""
        self._quota_hit_count += 1
        self.log(f"Quota hit #{self._quota_hit_count} - rotating IP...")

        if self.psiphon and self.settings.get('psiphon_rotate_on_quota'):
            if self.psiphon.rotate():
                self._current_proxy = self.psiphon.proxy_url
                self.log(f"Rotated to new IP: {self.psiphon.current_ip}")
                return True

        # Psiphon not available or rotation failed - wait for quota reset
        wait = min(120 * self._quota_hit_count, 900)
        self.log(f"No IP rotation available, waiting {wait}s for quota reset...")
        time.sleep(wait)
        return False

    def _download_chunk(self, url, start, end, proxy, key, iv_int, chunk_idx, file_name):
        """Download a byte range, decrypt it."""
        headers = {"Range": f"bytes={start}-{end}"}
        proxies_dict = {"http": proxy, "https": proxy} if proxy else {}
        max_retries = self.settings.get('max_retries', 10)

        for attempt in range(max_retries):
            if self._cancelled:
                return None
            self._paused.wait()  # Block if paused

            try:
                logger.debug(f"Chunk {chunk_idx} attempt {attempt+1}: bytes {start}-{end}")
                resp = requests.get(url, headers=headers, proxies=proxies_dict, timeout=300)

                if resp.status_code == 509:
                    logger.warning(f"Chunk {chunk_idx}: 509 quota hit")
                    # Try rotating IP
                    if self._handle_quota():
                        proxy = self._get_proxy()
                        proxies_dict = {"http": proxy, "https": proxy} if proxy else {}
                        # Get fresh URL with new IP
                        continue
                    continue

                if resp.status_code not in (200, 206):
                    logger.warning(f"Chunk {chunk_idx}: HTTP {resp.status_code}")
                    time.sleep(5)
                    continue

                encrypted = resp.content
                # Decrypt
                block_offset = start // 16
                counter_start = ((iv_int << 64) + block_offset) & 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF
                ctr = CryptoCounter.new(128, initial_value=counter_start)
                cipher = AES.new(a32_to_str(key), AES.MODE_CTR, counter=ctr)
                skip = start % 16
                if skip:
                    pad = b'\0' * skip
                    decrypted = cipher.decrypt(pad + encrypted)[skip:]
                else:
                    decrypted = cipher.decrypt(encrypted)

                logger.debug(f"Chunk {chunk_idx}: downloaded {len(decrypted)} bytes")
                return decrypted

            except Exception as e:
                logger.warning(f"Chunk {chunk_idx} attempt {attempt+1}: {e}")
                time.sleep(5)

        logger.error(f"Chunk {chunk_idx} failed after {max_retries} attempts")
        return None

    def _single_stream_download(self, url, file_info, proxy, s3_key):
        """Single-stream download for small/large files with S3 upload."""
        file_size = file_info['size']
        key = file_info['key']
        iv_int = file_info['iv_int']
        name = file_info['name']
        chunk_size = self.settings.get('chunk_size_mb', 8) * 1024 * 1024
        use_proxy = proxy
        current_url = url
        max_retries = self.settings.get('max_retries', 10)

        for attempt in range(max_retries):
            if self._cancelled:
                return False
            self._paused.wait()

            proxies_dict = {"http": use_proxy, "https": use_proxy} if use_proxy else {}
            try:
                resp = requests.get(current_url, proxies=proxies_dict, timeout=300, stream=True)
                if resp.status_code == 509:
                    self.log(f"  [QUOTA] 509 for {name}, rotating IP...")
                    if self._handle_quota():
                        use_proxy = self._get_proxy()
                        new_url = self.mega_api.get_download_url(
                            file_info['node_id'], file_info['folder_id'], use_proxy)
                        if new_url:
                            current_url = new_url
                        continue
                    # No rotation, wait and retry with fresh URL
                    new_url = self.mega_api.get_download_url(
                        file_info['node_id'], file_info['folder_id'], use_proxy)
                    if new_url:
                        current_url = new_url
                    continue

                if resp.status_code != 200:
                    self.log(f"  [WARN] HTTP {resp.status_code} for {name}")
                    time.sleep(10)
                    continue

                # Stream download -> decrypt -> S3 upload
                mpu = self.s3_client.create_multipart_upload(
                    Bucket=self.settings['idrive_bucket'], Key=s3_key)
                upload_id = mpu['UploadId']

                try:
                    parts = []
                    part_num = 1
                    buffer = b''
                    downloaded = 0
                    ctr = CryptoCounter.new(128, initial_value=(iv_int << 64))
                    cipher = AES.new(a32_to_str(key), AES.MODE_CTR, counter=ctr)

                    for chunk in resp.iter_content(chunk_size):
                        if self._cancelled:
                            self.s3_client.abort_multipart_upload(
                                Bucket=self.settings['idrive_bucket'],
                                Key=s3_key, UploadId=upload_id)
                            return False
                        self._paused.wait()

                        decrypted = cipher.decrypt(chunk)
                        buffer += decrypted
                        downloaded += len(chunk)

                        while len(buffer) >= chunk_size:
                            part = self.s3_client.upload_part(
                                Bucket=self.settings['idrive_bucket'], Key=s3_key,
                                UploadId=upload_id, PartNumber=part_num,
                                Body=buffer[:chunk_size])
                            parts.append({'ETag': part['ETag'], 'PartNumber': part_num})
                            buffer = buffer[chunk_size:]
                            part_num += 1

                        if self.progress_callback:
                            self.progress_callback(downloaded, file_size, name)

                    if buffer:
                        part = self.s3_client.upload_part(
                            Bucket=self.settings['idrive_bucket'], Key=s3_key,
                            UploadId=upload_id, PartNumber=part_num, Body=buffer)
                        parts.append({'ETag': part['ETag'], 'PartNumber': part_num})

                    self.s3_client.complete_multipart_upload(
                        Bucket=self.settings['idrive_bucket'], Key=s3_key,
                        UploadId=upload_id, MultipartUpload={'Parts': parts})

                    proxy_label = "Psiphon" if use_proxy else "direct"
                    self.log(f"  Completed via {proxy_label}")
                    return True

                except Exception as e:
                    logger.error(f"Upload error for {name}: {e}")
                    try:
                        self.s3_client.abort_multipart_upload(
                            Bucket=self.settings['idrive_bucket'],
                            Key=s3_key, UploadId=upload_id)
                    except Exception:
                        pass
                    raise

            except Exception as e:
                logger.error(f"Single-stream attempt {attempt+1} for {name}: {e}")
                time.sleep(10)

        return False

    def _parallel_download(self, url, file_info, proxy, s3_key):
        """Multi-threaded parallel download using byte-range requests (MegaDownloader-style)."""
        file_size = file_info['size']
        key = file_info['key']
        iv_int = file_info['iv_int']
        name = file_info['name']
        num_threads = self.settings.get('parallel_threads', 16)

        self.log(f"  {num_threads}-thread parallel download ({file_size/1024/1024:.0f} MB)")

        mpu = self.s3_client.create_multipart_upload(
            Bucket=self.settings['idrive_bucket'], Key=s3_key)
        upload_id = mpu['UploadId']

        try:
            chunk_size = file_size // num_threads
            downloaded = [0]
            lock = threading.Lock()

            def dl_and_upload(idx, start, end):
                if self._cancelled:
                    return None
                data = self._download_chunk(url, start, end, proxy, key, iv_int, idx, name)
                if not data:
                    return None
                actual_end = min(end, file_size - 1)
                expected_size = actual_end - start + 1
                data = data[:expected_size]

                part = self.s3_client.upload_part(
                    Bucket=self.settings['idrive_bucket'], Key=s3_key,
                    UploadId=upload_id, PartNumber=idx + 1, Body=data)

                with lock:
                    downloaded[0] += len(data)
                    if self.progress_callback:
                        self.progress_callback(downloaded[0], file_size, name)

                return {'ETag': part['ETag'], 'PartNumber': idx + 1}

            # Build chunk ranges
            ranges_list = []
            for i in range(num_threads):
                start_byte = i * chunk_size
                end_byte = file_size - 1 if i == num_threads - 1 else (i + 1) * chunk_size - 1
                ranges_list.append((i, start_byte, end_byte))

            parts = [None] * num_threads
            with ThreadPoolExecutor(max_workers=num_threads) as executor:
                futures = {}
                for i, start_byte, end_byte in ranges_list:
                    f = executor.submit(dl_and_upload, i, start_byte, end_byte)
                    futures[f] = i

                for f in as_completed(futures):
                    idx = futures[f]
                    try:
                        result = f.result()
                        if result:
                            parts[idx] = result
                    except Exception as e:
                        logger.error(f"Chunk {idx} exception: {e}")

            # Retry failed chunks with rotated IP
            failed = [i for i, p in enumerate(parts) if p is None]
            if failed:
                self.log(f"  Retrying {len(failed)} failed chunks...")
                if self._handle_quota():
                    new_proxy = self._get_proxy()
                    new_url = self.mega_api.get_download_url(
                        file_info['node_id'], file_info['folder_id'], new_proxy)
                    if not new_url:
                        new_url = url
                    for idx in failed:
                        _, sb, eb = ranges_list[idx]
                        time.sleep(2)
                        result = dl_and_upload(idx, sb, eb)
                        if result:
                            parts[idx] = result

            if all(parts):
                self.s3_client.complete_multipart_upload(
                    Bucket=self.settings['idrive_bucket'], Key=s3_key,
                    UploadId=upload_id,
                    MultipartUpload={'Parts': [p for p in parts if p]})
                return True
            else:
                still_failed = [i for i, p in enumerate(parts) if p is None]
                self.log(f"  Still failed: chunks {still_failed}")
                self.s3_client.abort_multipart_upload(
                    Bucket=self.settings['idrive_bucket'],
                    Key=s3_key, UploadId=upload_id)
                return False

        except Exception as e:
            logger.error(f"Parallel download error for {name}: {e}")
            try:
                self.s3_client.abort_multipart_upload(
                    Bucket=self.settings['idrive_bucket'],
                    Key=s3_key, UploadId=upload_id)
            except Exception:
                pass
            return False

    def _save_to_local(self, url, file_info, proxy, local_path):
        """Download and decrypt a file, saving to local disk with parallel connections."""
        file_size = file_info['size']
        key = file_info['key']
        iv_int = file_info['iv_int']
        name = file_info['name']
        small_threshold = self.settings.get('small_file_threshold_mb', 20) * 1024 * 1024
        num_threads = self.settings.get('parallel_threads', 16)

        os.makedirs(os.path.dirname(local_path), exist_ok=True)

        if file_size < small_threshold:
            # Small files: single-stream to avoid overhead
            return self._save_to_local_single(url, file_info, proxy, local_path)

        # Parallel byte-range download for larger files
        self.log(f"  {num_threads}-thread parallel download to disk ({file_size/1024/1024:.0f} MB)")
        chunk_size = file_size // num_threads
        downloaded = [0]
        lock = threading.Lock()
        chunks = [None] * num_threads

        def dl_chunk(idx, start, end):
            if self._cancelled:
                return
            data = self._download_chunk(url, start, end, proxy, key, iv_int, idx, name)
            if data:
                actual_end = min(end, file_size - 1)
                expected_size = actual_end - start + 1
                data = data[:expected_size]
                chunks[idx] = data
                with lock:
                    downloaded[0] += len(data)
                    if self.progress_callback:
                        self.progress_callback(downloaded[0], file_size, name)

        ranges_list = []
        for i in range(num_threads):
            start_byte = i * chunk_size
            end_byte = file_size - 1 if i == num_threads - 1 else (i + 1) * chunk_size - 1
            ranges_list.append((i, start_byte, end_byte))

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = {}
            for i, start_byte, end_byte in ranges_list:
                f = executor.submit(dl_chunk, i, start_byte, end_byte)
                futures[f] = i
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception as e:
                    logger.error(f"Chunk {futures[f]} error: {e}")

        # Retry failed chunks
        failed = [i for i, c in enumerate(chunks) if c is None]
        if failed:
            self.log(f"  Retrying {len(failed)} failed chunks...")
            if self._handle_quota():
                new_proxy = self._get_proxy()
                new_url = self.mega_api.get_download_url(
                    file_info['node_id'], file_info['folder_id'], new_proxy)
                if not new_url:
                    new_url = url
                for idx in failed:
                    _, sb, eb = ranges_list[idx]
                    time.sleep(2)
                    dl_chunk(idx, sb, eb)

        if all(chunks):
            with open(local_path, 'wb') as f:
                for chunk in chunks:
                    f.write(chunk)
            self.log(f"  Saved to: {local_path}")
            return True
        else:
            still_failed = [i for i, c in enumerate(chunks) if c is None]
            self.log(f"  Still failed: chunks {still_failed}")
            return False

    def _save_to_local_single(self, url, file_info, proxy, local_path):
        """Single-stream download for small files to local disk."""
        file_size = file_info['size']
        key = file_info['key']
        iv_int = file_info['iv_int']
        name = file_info['name']
        chunk_size = self.settings.get('chunk_size_mb', 8) * 1024 * 1024
        use_proxy = proxy
        current_url = url
        max_retries = self.settings.get('max_retries', 10)

        os.makedirs(os.path.dirname(local_path), exist_ok=True)

        for attempt in range(max_retries):
            if self._cancelled:
                return False
            self._paused.wait()

            proxies_dict = {"http": use_proxy, "https": use_proxy} if use_proxy else {}
            try:
                resp = requests.get(current_url, proxies=proxies_dict, timeout=300, stream=True)
                if resp.status_code == 509:
                    self.log(f"  [QUOTA] 509 for {name}, rotating IP...")
                    if self._handle_quota():
                        use_proxy = self._get_proxy()
                        new_url = self.mega_api.get_download_url(
                            file_info['node_id'], file_info['folder_id'], use_proxy)
                        if new_url:
                            current_url = new_url
                        continue
                    new_url = self.mega_api.get_download_url(
                        file_info['node_id'], file_info['folder_id'], use_proxy)
                    if new_url:
                        current_url = new_url
                    continue

                if resp.status_code != 200:
                    self.log(f"  [WARN] HTTP {resp.status_code} for {name}")
                    time.sleep(10)
                    continue

                ctr = CryptoCounter.new(128, initial_value=(iv_int << 64))
                cipher = AES.new(a32_to_str(key), AES.MODE_CTR, counter=ctr)
                downloaded = 0

                with open(local_path, 'wb') as f:
                    for chunk in resp.iter_content(chunk_size):
                        if self._cancelled:
                            return False
                        self._paused.wait()
                        decrypted = cipher.decrypt(chunk)
                        f.write(decrypted)
                        downloaded += len(chunk)
                        if self.progress_callback:
                            self.progress_callback(downloaded, file_size, name)

                self.log(f"  Saved to: {local_path}")
                return True

            except Exception as e:
                logger.error(f"Local save attempt {attempt+1} for {name}: {e}")
                time.sleep(10)

        return False

    def _upload_local_to_s3(self, local_path, s3_key):
        """Upload a local file to S3."""
        try:
            file_size = os.path.getsize(local_path)
            chunk_size = 8 * 1024 * 1024
            name = os.path.basename(local_path)

            if file_size < chunk_size:
                with open(local_path, 'rb') as f:
                    self.s3_client.put_object(
                        Bucket=self.settings['idrive_bucket'], Key=s3_key, Body=f.read())
            else:
                mpu = self.s3_client.create_multipart_upload(
                    Bucket=self.settings['idrive_bucket'], Key=s3_key)
                upload_id = mpu['UploadId']
                parts = []
                part_num = 1
                uploaded = 0

                with open(local_path, 'rb') as f:
                    while True:
                        data = f.read(chunk_size)
                        if not data:
                            break
                        part = self.s3_client.upload_part(
                            Bucket=self.settings['idrive_bucket'], Key=s3_key,
                            UploadId=upload_id, PartNumber=part_num, Body=data)
                        parts.append({'ETag': part['ETag'], 'PartNumber': part_num})
                        part_num += 1
                        uploaded += len(data)
                        if self.progress_callback:
                            self.progress_callback(uploaded, file_size, f"Uploading {name}")

                self.s3_client.complete_multipart_upload(
                    Bucket=self.settings['idrive_bucket'], Key=s3_key,
                    UploadId=upload_id, MultipartUpload={'Parts': parts})

            self.log(f"  Uploaded to S3: {s3_key}")
            return True
        except Exception as e:
            logger.error(f"S3 upload error for {local_path}: {e}")
            return False

    def download_file(self, file_info, s3_prefix):
        """Download a single file from Mega. Mode determines destination."""
        proxy = self._get_proxy()
        url = self.mega_api.get_download_url(
            file_info['node_id'], file_info['folder_id'], proxy)
        if not url:
            self.log(f"  [ERROR] No download URL for {file_info['name']}")
            return False

        download_mode = self.settings.get('download_mode', 'direct_to_s3')
        s3_key = f"{s3_prefix}/{file_info['path']}"
        file_size = file_info['size']
        small_threshold = self.settings.get('small_file_threshold_mb', 20) * 1024 * 1024
        large_threshold = self.settings.get('large_file_threshold_mb', 400) * 1024 * 1024

        if download_mode == 'local_only':
            # Save to local disk only, no S3 upload
            dl_path = self.settings.get('download_path', '')
            local_path = os.path.join(dl_path, s3_prefix, file_info['path'])
            return self._save_to_local(url, file_info, proxy, local_path)

        elif download_mode == 'local_and_s3':
            # Save locally first, then upload to S3
            dl_path = self.settings.get('download_path', '')
            local_path = os.path.join(dl_path, s3_prefix, file_info['path'])
            ok = self._save_to_local(url, file_info, proxy, local_path)
            if ok:
                return self._upload_local_to_s3(local_path, s3_key)
            return False

        else:
            # direct_to_s3: stream directly to S3
            if file_size < small_threshold:
                self.log(f"  Single-stream (small file)")
                return self._single_stream_download(url, file_info, proxy, s3_key)
            else:
                # Parallel multi-connection for all files >= small threshold
                return self._parallel_download(url, file_info, proxy, s3_key)

    def get_uploaded_files(self, s3_prefix):
        """Get set of already-uploaded files in S3."""
        uploaded = set()
        try:
            paginator = self.s3_client.get_paginator('list_objects_v2')
            for page in paginator.paginate(
                    Bucket=self.settings['idrive_bucket'], Prefix=s3_prefix + '/'):
                for obj in page.get('Contents', []):
                    fname = obj['Key'].split('/')[-1]
                    uploaded.add((fname, obj['Size']))
            logger.info(f"Found {len(uploaded)} already-uploaded files in S3")
        except Exception as e:
            logger.warning(f"Could not check S3 for existing files: {e}")
        return uploaded

    def process_job(self, mega_url, title, s3_prefix):
        """Process a complete download job."""
        self._cancelled = False
        self._quota_hit_count = 0

        try:
            self._init_s3()
        except Exception as e:
            self.log(f"[ERROR] S3 initialization failed: {e}", "error")
            return False

        # Start Psiphon if enabled
        if self.settings.get('use_psiphon') and self.psiphon:
            self.log("Starting Psiphon for IP rotation...")
            if self.psiphon.start():
                self._current_proxy = self.psiphon.proxy_url
                self.mega_api.set_proxy(self._current_proxy)
            else:
                self.log("Psiphon not available, using direct connection")

        # Get file list (with retry and proxy fallback)
        self.log(f"Fetching Mega folder: {mega_url}")
        files = None
        folder_id = None
        for fetch_attempt in range(3):
            try:
                files, folder_id = self.mega_api.get_folder_files(
                    mega_url, self.settings.get('exclude_files', []))
                break
            except Exception as e:
                self.log(f"  Fetch attempt {fetch_attempt+1} failed: {e}")
                if fetch_attempt == 0 and self._current_proxy:
                    # First failure with proxy - try without proxy
                    self.log("  Retrying without proxy...")
                    self.mega_api.set_proxy(None)
                    try:
                        files, folder_id = self.mega_api.get_folder_files(
                            mega_url, self.settings.get('exclude_files', []))
                        # Restore proxy for downloads (folder listing doesn't need it)
                        self.mega_api.set_proxy(self._current_proxy)
                        break
                    except Exception as e2:
                        self.log(f"  Direct fetch also failed: {e2}")
                        self.mega_api.set_proxy(self._current_proxy)
                elif fetch_attempt < 2:
                    self.log("  Waiting 5s before retry...")
                    time.sleep(5)

        if files is None:
            self.log(f"[ERROR] Failed to fetch folder after 3 attempts", "error")
            return False

        total_size = sum(f['size'] for f in files)
        self.log(f"Found {len(files)} files ({total_size/1024/1024:.0f} MB)")

        # Check already uploaded
        uploaded = self.get_uploaded_files(s3_prefix)
        remaining = []
        skip_count = 0
        for f in files:
            if (f['name'], f['size']) in uploaded:
                self.log(f"  [SKIP] {f['name']} (already uploaded)")
                skip_count += 1
            else:
                remaining.append(f)

        if skip_count:
            self.log(f"Skipping {skip_count} already-uploaded files, {len(remaining)} remaining")

        # Download each file
        completed = skip_count
        failed_files = []
        start_time = time.time()
        total_downloaded = 0

        # Outer retry loop
        max_rounds = 6
        round_num = 0
        current_remaining = list(remaining)

        while current_remaining and round_num < max_rounds:
            round_num += 1
            if round_num > 1:
                cooldown = 1800
                self.log(f"\nRound {round_num}/{max_rounds}: Retrying {len(current_remaining)} files after {cooldown}s cooldown...")
                time.sleep(cooldown)
                # Rotate Psiphon for fresh IP
                if self.psiphon and self.psiphon.is_running():
                    self.psiphon.rotate()

            round_failed = []
            for idx, f in enumerate(current_remaining):
                if self._cancelled:
                    self.log("Job cancelled by user")
                    break
                self._paused.wait()

                file_start = time.time()
                self.log(f"\n[R{round_num} {idx+1}/{len(current_remaining)}] {f['name']} ({f['size']/1024/1024:.1f} MB)")

                ok = self.download_file(f, s3_prefix)
                elapsed = time.time() - file_start
                speed = f['size'] / elapsed / 1024 / 1024 if elapsed > 0 else 0

                if ok:
                    completed += 1
                    total_downloaded += f['size']
                    self.log(f"  DONE in {elapsed:.0f}s @ {speed:.1f} MB/s")
                else:
                    round_failed.append(f)
                    self.log(f"  FAILED after {elapsed:.0f}s (will retry)")

            current_remaining = round_failed
            if current_remaining:
                self.log(f"Round {round_num} done: {len(current_remaining)} files still need retry")

        failed_files = [f['name'] for f in current_remaining]
        total_time = time.time() - start_time
        avg_speed = total_downloaded / total_time / 1024 / 1024 if total_time > 0 else 0

        # Summary
        self.log(f"\n{'='*60}")
        self.log(f"  Job Complete: {title}")
        self.log(f"{'='*60}")
        self.log(f"Completed: {completed}/{len(files)}")
        if failed_files:
            self.log(f"Failed: {len(failed_files)} - {failed_files}")
        self.log(f"Downloaded: {total_downloaded/1024/1024:.0f} MB in {total_time:.0f}s")
        self.log(f"Average speed: {avg_speed:.1f} MB/s")
        self.log(f"IP rotations: {self._quota_hit_count}")

        # Send Telegram notification
        self._send_telegram(title, s3_prefix, completed, len(files),
                           total_downloaded, total_time, avg_speed, failed_files)

        # Stop Psiphon
        if self.psiphon:
            self.psiphon.stop()

        return len(failed_files) == 0

    def _send_telegram(self, title, s3_prefix, completed, total, downloaded,
                       total_time, avg_speed, failed_files):
        """Send Telegram notification."""
        bot_token = self.settings.get('telegram_bot_token', '')
        chat_id = self.settings.get('telegram_chat_id', '')
        portal_domain = self.settings.get('portal_domain', '')

        if not bot_token or not chat_id:
            self.log("Telegram not configured, skipping notification")
            return

        portal_url = f"{portal_domain}/content/{s3_prefix}"
        status = "\u2705" if not failed_files else "\u26a0\ufe0f"
        msg = (
            f"{status} <b>Transfer Complete</b>\n\n"
            f"<b>{title}</b>\n"
            f"Files: {completed}/{total}\n"
            f"Size: {downloaded/1024/1024:.0f} MB\n"
            f"Time: {total_time:.0f}s @ {avg_speed:.1f} MB/s\n"
        )
        if failed_files:
            msg += f"Failed: {len(failed_files)} files\n"
        msg += f"\n\U0001F517 <a href=\"{portal_url}\">View in Portal</a>"

        try:
            tg_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            resp = requests.post(tg_url, json={
                "chat_id": chat_id,
                "text": msg,
                "parse_mode": "HTML"
            }, timeout=15)
            if resp.ok:
                self.log("Telegram notification sent")
            else:
                self.log(f"Telegram error: {resp.text}")
        except Exception as e:
            self.log(f"Telegram notification failed: {e}")

    def pause(self):
        """Pause downloads."""
        self._paused.clear()
        self.log("Downloads paused")

    def resume(self):
        """Resume downloads."""
        self._paused.set()
        self.log("Downloads resumed")

    def cancel(self):
        """Cancel current job."""
        self._cancelled = True
        self._paused.set()  # Unblock if paused
        self.log("Cancelling job...")


# ============================================================================
# GUI APPLICATION
# ============================================================================
# GUI imports are deferred to avoid import errors on headless systems
HAS_CTK = False
ctk = None
tk = None
ttk = None
scrolledtext = None
messagebox = None
filedialog = None

def _init_gui():
    """Initialize GUI modules. Call before creating any GUI."""
    global HAS_CTK, ctk, tk, ttk, scrolledtext, messagebox, filedialog
    try:
        import customtkinter as _ctk
        ctk = _ctk
        HAS_CTK = True
        logger.info("Using CustomTkinter for modern UI")
    except ImportError:
        HAS_CTK = False
        logger.warning("customtkinter not available, using tkinter")
    if not HAS_CTK:
        import tkinter as _tk
        from tkinter import ttk as _ttk, scrolledtext as _st, messagebox as _mb, filedialog as _fd
        tk = _tk
        ttk = _ttk
        scrolledtext = _st
        messagebox = _mb
        filedialog = _fd


class MegaTransferApp:
    """Main application window."""

    def __init__(self):
        _init_gui()  # Initialize GUI modules
        self.settings = load_settings()
        self.psiphon = PsiphonManager()  # log_callback set after UI is built
        self.engine = None
        self.job_thread = None
        self._build_ui()
        # Now that UI is built, wire up Psiphon log callback
        self.psiphon._log_callback = self._log_to_ui

    def _build_ui(self):
        """Build the main UI."""
        if HAS_CTK:
            ctk.set_appearance_mode("dark")
            ctk.set_default_color_theme("blue")
            self.root = ctk.CTk()
        else:
            self.root = tk.Tk()

        self.root.title("MegaTransfer Desktop v1.0")
        self.root.geometry("900x700")
        self.root.minsize(800, 600)

        # Main container
        if HAS_CTK:
            self._build_ctk_ui()
        else:
            self._build_tk_ui()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ctk_ui(self):
        """Build CustomTkinter UI."""
        # Tabview
        self.tabview = ctk.CTkTabview(self.root)
        self.tabview.pack(fill="both", expand=True, padx=10, pady=10)

        # Transfer tab
        tab_transfer = self.tabview.add("Transfer")
        self._build_transfer_tab(tab_transfer)

        # Queue tab
        tab_queue = self.tabview.add("Queue")
        self._build_queue_tab(tab_queue)

        # Settings tab
        tab_settings = self.tabview.add("Settings")
        self._build_settings_tab(tab_settings)

        # Log tab
        tab_log = self.tabview.add("Log")
        self._build_log_tab(tab_log)

    def _build_tk_ui(self):
        """Build standard tkinter UI (fallback)."""
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=5, pady=5)

        tab_transfer = ttk.Frame(notebook)
        notebook.add(tab_transfer, text="Transfer")
        self._build_transfer_tab_tk(tab_transfer)

        tab_settings = ttk.Frame(notebook)
        notebook.add(tab_settings, text="Settings")
        self._build_settings_tab_tk(tab_settings)

        tab_log = ttk.Frame(notebook)
        notebook.add(tab_log, text="Log")
        self._build_log_tab_tk(tab_log)

    def _build_transfer_tab(self, parent):
        """Build Transfer tab with CTk."""
        # Title input
        frame_input = ctk.CTkFrame(parent)
        frame_input.pack(fill="x", padx=10, pady=(10, 5))

        ctk.CTkLabel(frame_input, text="Job Title:").pack(side="left", padx=5)
        self.entry_title = ctk.CTkEntry(frame_input, width=300,
                                         placeholder_text="e.g. Course Name")
        self.entry_title.pack(side="left", padx=5, fill="x", expand=True)

        # Mega link input
        frame_link = ctk.CTkFrame(parent)
        frame_link.pack(fill="x", padx=10, pady=5)

        ctk.CTkLabel(frame_link, text="Mega Link:").pack(side="left", padx=5)
        self.entry_link = ctk.CTkEntry(frame_link, width=500,
                                        placeholder_text="https://mega.nz/folder/...")
        self.entry_link.pack(side="left", padx=5, fill="x", expand=True)

        # S3 prefix input
        frame_prefix = ctk.CTkFrame(parent)
        frame_prefix.pack(fill="x", padx=10, pady=5)

        ctk.CTkLabel(frame_prefix, text="S3 Prefix:").pack(side="left", padx=5)
        self.entry_prefix = ctk.CTkEntry(frame_prefix, width=300,
                                          placeholder_text="folder-name-in-s3")
        self.entry_prefix.pack(side="left", padx=5, fill="x", expand=True)

        # Buttons
        frame_buttons = ctk.CTkFrame(parent)
        frame_buttons.pack(fill="x", padx=10, pady=10)

        self.btn_start = ctk.CTkButton(frame_buttons, text="Start Transfer",
                                        command=self._start_transfer, fg_color="green")
        self.btn_start.pack(side="left", padx=5)

        self.btn_pause = ctk.CTkButton(frame_buttons, text="Pause",
                                        command=self._pause_transfer, state="disabled")
        self.btn_pause.pack(side="left", padx=5)

        self.btn_resume = ctk.CTkButton(frame_buttons, text="Resume",
                                         command=self._resume_transfer, state="disabled")
        self.btn_resume.pack(side="left", padx=5)

        self.btn_cancel = ctk.CTkButton(frame_buttons, text="Cancel",
                                         command=self._cancel_transfer,
                                         fg_color="red", state="disabled")
        self.btn_cancel.pack(side="left", padx=5)

        # Progress
        frame_progress = ctk.CTkFrame(parent)
        frame_progress.pack(fill="x", padx=10, pady=5)

        self.label_status = ctk.CTkLabel(frame_progress, text="Ready",
                                          font=("", 14, "bold"))
        self.label_status.pack(anchor="w", padx=5)

        self.progress_bar = ctk.CTkProgressBar(frame_progress)
        self.progress_bar.pack(fill="x", padx=5, pady=5)
        self.progress_bar.set(0)

        self.label_speed = ctk.CTkLabel(frame_progress, text="")
        self.label_speed.pack(anchor="w", padx=5)

        self.label_file = ctk.CTkLabel(frame_progress, text="")
        self.label_file.pack(anchor="w", padx=5)

        # Log output (in transfer tab)
        self.transfer_log = ctk.CTkTextbox(parent, height=200)
        self.transfer_log.pack(fill="both", expand=True, padx=10, pady=10)

    def _build_transfer_tab_tk(self, parent):
        """Build Transfer tab with standard tkinter."""
        # Title
        frame = ttk.Frame(parent)
        frame.pack(fill="x", padx=5, pady=5)
        ttk.Label(frame, text="Job Title:").pack(side="left")
        self.entry_title = ttk.Entry(frame, width=40)
        self.entry_title.pack(side="left", fill="x", expand=True, padx=5)

        # Link
        frame2 = ttk.Frame(parent)
        frame2.pack(fill="x", padx=5, pady=5)
        ttk.Label(frame2, text="Mega Link:").pack(side="left")
        self.entry_link = ttk.Entry(frame2, width=60)
        self.entry_link.pack(side="left", fill="x", expand=True, padx=5)

        # S3 prefix
        frame3 = ttk.Frame(parent)
        frame3.pack(fill="x", padx=5, pady=5)
        ttk.Label(frame3, text="S3 Prefix:").pack(side="left")
        self.entry_prefix = ttk.Entry(frame3, width=40)
        self.entry_prefix.pack(side="left", fill="x", expand=True, padx=5)

        # Buttons
        frame_btn = ttk.Frame(parent)
        frame_btn.pack(fill="x", padx=5, pady=5)
        self.btn_start = ttk.Button(frame_btn, text="Start Transfer",
                                     command=self._start_transfer)
        self.btn_start.pack(side="left", padx=5)
        self.btn_pause = ttk.Button(frame_btn, text="Pause",
                                     command=self._pause_transfer, state="disabled")
        self.btn_pause.pack(side="left", padx=5)
        self.btn_resume = ttk.Button(frame_btn, text="Resume",
                                      command=self._resume_transfer, state="disabled")
        self.btn_resume.pack(side="left", padx=5)
        self.btn_cancel = ttk.Button(frame_btn, text="Cancel",
                                      command=self._cancel_transfer, state="disabled")
        self.btn_cancel.pack(side="left", padx=5)

        # Status
        self.label_status = ttk.Label(parent, text="Ready", font=("", 12, "bold"))
        self.label_status.pack(anchor="w", padx=5)
        self.label_speed = ttk.Label(parent, text="")
        self.label_speed.pack(anchor="w", padx=5)
        self.label_file = ttk.Label(parent, text="")
        self.label_file.pack(anchor="w", padx=5)

        # Progress
        self.progress_bar = ttk.Progressbar(parent, mode='determinate')
        self.progress_bar.pack(fill="x", padx=5, pady=5)

        # Log
        self.transfer_log = scrolledtext.ScrolledText(parent, height=10)
        self.transfer_log.pack(fill="both", expand=True, padx=5, pady=5)

    def _build_queue_tab(self, parent):
        """Build Queue tab."""
        ctk.CTkLabel(parent, text="Download Queue",
                      font=("", 16, "bold")).pack(anchor="w", padx=10, pady=10)

        # Queue list
        self.queue_textbox = ctk.CTkTextbox(parent, height=200)
        self.queue_textbox.pack(fill="both", expand=True, padx=10, pady=5)

        # Add to queue
        frame_add = ctk.CTkFrame(parent)
        frame_add.pack(fill="x", padx=10, pady=5)

        ctk.CTkLabel(frame_add, text="Title:").pack(side="left", padx=2)
        self.queue_title = ctk.CTkEntry(frame_add, width=150)
        self.queue_title.pack(side="left", padx=2)

        ctk.CTkLabel(frame_add, text="Link:").pack(side="left", padx=2)
        self.queue_link = ctk.CTkEntry(frame_add, width=300)
        self.queue_link.pack(side="left", padx=2)

        ctk.CTkLabel(frame_add, text="Prefix:").pack(side="left", padx=2)
        self.queue_prefix = ctk.CTkEntry(frame_add, width=150)
        self.queue_prefix.pack(side="left", padx=2)

        ctk.CTkButton(frame_add, text="Add", command=self._add_to_queue,
                       width=60).pack(side="left", padx=5)

        # Queue controls
        frame_qctl = ctk.CTkFrame(parent)
        frame_qctl.pack(fill="x", padx=10, pady=5)

        ctk.CTkButton(frame_qctl, text="Start Queue",
                       command=self._start_queue, fg_color="green").pack(side="left", padx=5)
        ctk.CTkButton(frame_qctl, text="Clear Queue",
                       command=self._clear_queue).pack(side="left", padx=5)

        self.download_queue = []

    def _build_settings_tab(self, parent):
        """Build Settings tab with CTk."""
        scroll = ctk.CTkScrollableFrame(parent)
        scroll.pack(fill="both", expand=True, padx=10, pady=10)

        # iDrive Settings
        ctk.CTkLabel(scroll, text="iDrive S3 Settings",
                      font=("", 14, "bold")).pack(anchor="w", pady=(10, 5))

        self.setting_entries = {}
        settings_fields = [
            ("idrive_endpoint", "Endpoint URL"),
            ("idrive_access_key", "Access Key ID"),
            ("idrive_secret_key", "Secret Access Key"),
            ("idrive_bucket", "Bucket Name"),
        ]
        for key, label in settings_fields:
            frame = ctk.CTkFrame(scroll)
            frame.pack(fill="x", pady=2)
            ctk.CTkLabel(frame, text=f"{label}:", width=150).pack(side="left", padx=5)
            entry = ctk.CTkEntry(frame, width=400)
            entry.pack(side="left", padx=5, fill="x", expand=True)
            entry.insert(0, self.settings.get(key, ''))
            self.setting_entries[key] = entry

        # Telegram Settings
        ctk.CTkLabel(scroll, text="Telegram Settings",
                      font=("", 14, "bold")).pack(anchor="w", pady=(15, 5))

        tg_fields = [
            ("telegram_bot_token", "Bot Token"),
            ("telegram_chat_id", "Chat ID"),
            ("portal_domain", "Portal Domain"),
        ]
        for key, label in tg_fields:
            frame = ctk.CTkFrame(scroll)
            frame.pack(fill="x", pady=2)
            ctk.CTkLabel(frame, text=f"{label}:", width=150).pack(side="left", padx=5)
            entry = ctk.CTkEntry(frame, width=400)
            entry.pack(side="left", padx=5, fill="x", expand=True)
            entry.insert(0, self.settings.get(key, ''))
            self.setting_entries[key] = entry

        # Download Settings
        ctk.CTkLabel(scroll, text="Download Settings",
                      font=("", 14, "bold")).pack(anchor="w", pady=(15, 5))

        # Download mode selector
        frame_mode = ctk.CTkFrame(scroll)
        frame_mode.pack(fill="x", pady=5)
        ctk.CTkLabel(frame_mode, text="Download Mode:", width=200).pack(side="left", padx=5)
        mode_options = ["Direct to iDrive (fastest)", "Save locally + Upload to iDrive", "Save locally only"]
        mode_map = {"direct_to_s3": 0, "local_and_s3": 1, "local_only": 2}
        self._mode_values = ["direct_to_s3", "local_and_s3", "local_only"]
        current_mode = self.settings.get('download_mode', 'direct_to_s3')
        self.mode_var = ctk.StringVar(value=mode_options[mode_map.get(current_mode, 0)])
        self.mode_dropdown = ctk.CTkOptionMenu(frame_mode, variable=self.mode_var,
                                                values=mode_options, width=280)
        self.mode_dropdown.pack(side="left", padx=5)

        # Download path (for local modes)
        frame_dlpath = ctk.CTkFrame(scroll)
        frame_dlpath.pack(fill="x", pady=2)
        ctk.CTkLabel(frame_dlpath, text="Local Save Path:", width=200).pack(side="left", padx=5)
        self.dlpath_entry = ctk.CTkEntry(frame_dlpath, width=350)
        self.dlpath_entry.pack(side="left", padx=5, fill="x", expand=True)
        self.dlpath_entry.insert(0, self.settings.get('download_path', ''))
        ctk.CTkButton(frame_dlpath, text="Browse", width=70,
                       command=self._browse_download_path).pack(side="left", padx=5)

        num_fields = [
            ("parallel_threads", "Parallel Threads (per file)"),
            ("chunk_size_mb", "Chunk Size (MB)"),
            ("max_retries", "Max Retries"),
            ("small_file_threshold_mb", "Small File Threshold (MB)"),
        ]
        for key, label in num_fields:
            frame = ctk.CTkFrame(scroll)
            frame.pack(fill="x", pady=2)
            ctk.CTkLabel(frame, text=f"{label}:", width=200).pack(side="left", padx=5)
            entry = ctk.CTkEntry(frame, width=100)
            entry.pack(side="left", padx=5)
            entry.insert(0, str(self.settings.get(key, '')))
            self.setting_entries[key] = entry

        # Psiphon toggle
        frame_psiphon = ctk.CTkFrame(scroll)
        frame_psiphon.pack(fill="x", pady=5)
        self.psiphon_var = ctk.BooleanVar(value=self.settings.get('use_psiphon', True))
        ctk.CTkCheckBox(frame_psiphon, text="Enable Psiphon IP Rotation (unlimited quota)",
                         variable=self.psiphon_var).pack(anchor="w", padx=5)

        # Exclude files
        ctk.CTkLabel(scroll, text="Exclude Files (one per line)",
                      font=("", 14, "bold")).pack(anchor="w", pady=(15, 5))
        self.exclude_textbox = ctk.CTkTextbox(scroll, height=80)
        self.exclude_textbox.pack(fill="x", padx=5, pady=5)
        for f in self.settings.get('exclude_files', []):
            self.exclude_textbox.insert("end", f + "\n")

        # Save button
        ctk.CTkButton(scroll, text="Save Settings", command=self._save_settings,
                       fg_color="green").pack(anchor="w", padx=5, pady=15)

    def _build_settings_tab_tk(self, parent):
        """Build Settings tab with standard tkinter."""
        # Simplified settings for tkinter fallback
        fields = [
            ("idrive_endpoint", "iDrive Endpoint"),
            ("idrive_access_key", "iDrive Access Key"),
            ("idrive_secret_key", "iDrive Secret Key"),
            ("idrive_bucket", "iDrive Bucket"),
            ("telegram_bot_token", "Telegram Bot Token"),
            ("telegram_chat_id", "Telegram Chat ID"),
            ("portal_domain", "Portal Domain"),
            ("parallel_threads", "Parallel Threads"),
        ]
        self.setting_entries = {}
        for key, label in fields:
            frame = ttk.Frame(parent)
            frame.pack(fill="x", padx=5, pady=2)
            ttk.Label(frame, text=f"{label}:", width=20).pack(side="left")
            entry = ttk.Entry(frame, width=50)
            entry.pack(side="left", fill="x", expand=True, padx=5)
            entry.insert(0, str(self.settings.get(key, '')))
            self.setting_entries[key] = entry

        ttk.Button(parent, text="Save Settings",
                    command=self._save_settings).pack(anchor="w", padx=5, pady=10)

    def _build_log_tab(self, parent):
        """Build Log tab with CTk."""
        # Log file path
        ctk.CTkLabel(parent, text=f"Log file: {LOG_FILE}",
                      font=("", 11)).pack(anchor="w", padx=10, pady=5)

        self.log_textbox = ctk.CTkTextbox(parent, font=("Consolas", 11))
        self.log_textbox.pack(fill="both", expand=True, padx=10, pady=5)

        frame_log_btn = ctk.CTkFrame(parent)
        frame_log_btn.pack(fill="x", padx=10, pady=5)

        ctk.CTkButton(frame_log_btn, text="Open Log Folder",
                       command=lambda: os.startfile(LOG_DIR) if os.name == 'nt'
                       else subprocess.Popen(['xdg-open', LOG_DIR])).pack(side="left", padx=5)

        ctk.CTkButton(frame_log_btn, text="Clear Log Display",
                       command=lambda: self.log_textbox.delete("1.0", "end")).pack(side="left", padx=5)

    def _build_log_tab_tk(self, parent):
        """Build Log tab with standard tkinter."""
        ttk.Label(parent, text=f"Log: {LOG_FILE}").pack(anchor="w", padx=5)
        self.log_textbox = scrolledtext.ScrolledText(parent, font=("Consolas", 9))
        self.log_textbox.pack(fill="both", expand=True, padx=5, pady=5)

    # ---- Actions ----
    def _browse_download_path(self):
        """Open folder browser for download path."""
        if HAS_CTK:
            from tkinter import filedialog as fd
        else:
            fd = filedialog
        path = fd.askdirectory(title="Choose Download Folder")
        if path:
            self.dlpath_entry.delete(0, "end")
            self.dlpath_entry.insert(0, path)

    def _save_settings(self):
        """Save settings from UI entries."""
        int_fields = {'parallel_threads', 'chunk_size_mb', 'max_retries',
                      'small_file_threshold_mb'}
        for key, entry in self.setting_entries.items():
            val = entry.get().strip()
            if key in int_fields:
                try:
                    val = int(val)
                except ValueError:
                    pass
            self.settings[key] = val

        if hasattr(self, 'psiphon_var'):
            self.settings['use_psiphon'] = self.psiphon_var.get()

        # Download mode
        if hasattr(self, 'mode_var'):
            mode_text = self.mode_var.get()
            mode_lookup = {
                "Direct to iDrive (fastest)": "direct_to_s3",
                "Save locally + Upload to iDrive": "local_and_s3",
                "Save locally only": "local_only",
            }
            self.settings['download_mode'] = mode_lookup.get(mode_text, 'direct_to_s3')

        # Download path
        if hasattr(self, 'dlpath_entry'):
            self.settings['download_path'] = self.dlpath_entry.get().strip()

        if hasattr(self, 'exclude_textbox'):
            text = self.exclude_textbox.get("1.0", "end").strip()
            self.settings['exclude_files'] = [l.strip() for l in text.splitlines() if l.strip()]

        save_settings(self.settings)
        self._log_to_ui("Settings saved!")

    def _log_to_ui(self, msg):
        """Add message to UI log displays."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {msg}\n"

        def update():
            if hasattr(self, 'transfer_log'):
                if HAS_CTK:
                    self.transfer_log.insert("end", line)
                    self.transfer_log.see("end")
                else:
                    self.transfer_log.insert("end", line)
                    self.transfer_log.see("end")
            if hasattr(self, 'log_textbox'):
                if HAS_CTK:
                    self.log_textbox.insert("end", line)
                    self.log_textbox.see("end")
                else:
                    self.log_textbox.insert("end", line)
                    self.log_textbox.see("end")

        self.root.after(0, update)

    def _update_progress(self, downloaded, total, filename):
        """Update progress bar and labels."""
        if total <= 0:
            return
        pct = downloaded / total
        speed_text = ""

        if not hasattr(self, '_dl_start_time'):
            self._dl_start_time = time.time()
            self._dl_last_bytes = 0

        elapsed = time.time() - self._dl_start_time
        if elapsed > 0:
            speed = downloaded / elapsed / 1024 / 1024
            speed_text = f"{speed:.1f} MB/s"

        def update():
            if HAS_CTK:
                self.progress_bar.set(pct)
            else:
                self.progress_bar['value'] = pct * 100
            self.label_speed.configure(text=f"Speed: {speed_text} | {downloaded/1024/1024:.1f}/{total/1024/1024:.1f} MB ({pct*100:.0f}%)")
            self.label_file.configure(text=f"File: {filename}")

        self.root.after(0, update)

    def _start_transfer(self):
        """Start a new transfer job."""
        mega_link = self.entry_link.get().strip()
        title = self.entry_title.get().strip()
        s3_prefix = self.entry_prefix.get().strip()

        if not mega_link:
            self._log_to_ui("[ERROR] Please enter a Mega link")
            return
        if not title:
            self._log_to_ui("[ERROR] Please enter a job title")
            return
        if not s3_prefix:
            # Auto-generate from title
            s3_prefix = re.sub(r'[^a-zA-Z0-9-]', '-', title.lower()).strip('-')
            if HAS_CTK:
                self.entry_prefix.delete(0, "end")
                self.entry_prefix.insert(0, s3_prefix)
            else:
                self.entry_prefix.delete(0, "end")
                self.entry_prefix.insert(0, s3_prefix)

        # Validate settings based on download mode
        download_mode = self.settings.get('download_mode', 'direct_to_s3')
        if download_mode != 'local_only':
            if not self.settings.get('idrive_access_key') or not self.settings.get('idrive_secret_key'):
                self._log_to_ui("[ERROR] Please configure iDrive S3 credentials in Settings tab")
                return
        if download_mode in ('local_only', 'local_and_s3'):
            if not self.settings.get('download_path'):
                self._log_to_ui("[ERROR] Please set a local download path in Settings tab")
                return

        self._save_settings()

        # Update UI state
        self.btn_start.configure(state="disabled")
        self.btn_pause.configure(state="normal")
        self.btn_cancel.configure(state="normal")
        self.label_status.configure(text=f"Transferring: {title}")

        self._dl_start_time = time.time()

        # Create engine and start job thread
        self.engine = DownloadEngine(
            self.settings,
            psiphon=self.psiphon,
            progress_callback=self._update_progress,
            log_callback=self._log_to_ui
        )

        def run_job():
            try:
                success = self.engine.process_job(mega_link, title, s3_prefix)
                self.root.after(0, lambda: self._job_finished(success, title))
            except Exception as e:
                logger.error(f"Job error: {traceback.format_exc()}")
                self.root.after(0, lambda: self._job_finished(False, title, str(e)))

        self.job_thread = threading.Thread(target=run_job, daemon=True)
        self.job_thread.start()

    def _job_finished(self, success, title, error=None):
        """Called when job completes."""
        self.btn_start.configure(state="normal")
        self.btn_pause.configure(state="disabled")
        self.btn_resume.configure(state="disabled")
        self.btn_cancel.configure(state="disabled")

        if success:
            self.label_status.configure(text=f"Completed: {title}")
            self._log_to_ui(f"Job completed successfully: {title}")
        else:
            msg = f"Job failed: {title}"
            if error:
                msg += f" - {error}"
            self.label_status.configure(text=msg)
            self._log_to_ui(msg)

        # Process queue if there are more jobs
        if self.download_queue:
            self._process_next_queue_item()

    def _pause_transfer(self):
        if self.engine:
            self.engine.pause()
            self.btn_pause.configure(state="disabled")
            self.btn_resume.configure(state="normal")
            self.label_status.configure(text="Paused")

    def _resume_transfer(self):
        if self.engine:
            self.engine.resume()
            self.btn_pause.configure(state="normal")
            self.btn_resume.configure(state="disabled")
            self.label_status.configure(text="Transferring...")

    def _cancel_transfer(self):
        if self.engine:
            self.engine.cancel()
            self.btn_start.configure(state="normal")
            self.btn_pause.configure(state="disabled")
            self.btn_resume.configure(state="disabled")
            self.btn_cancel.configure(state="disabled")
            self.label_status.configure(text="Cancelled")

    # ---- Queue ----
    def _add_to_queue(self):
        title = self.queue_title.get().strip()
        link = self.queue_link.get().strip()
        prefix = self.queue_prefix.get().strip()
        if not link:
            return
        if not prefix:
            prefix = re.sub(r'[^a-zA-Z0-9-]', '-', title.lower()).strip('-')

        self.download_queue.append({"title": title, "link": link, "prefix": prefix})
        if HAS_CTK:
            self.queue_textbox.insert("end", f"{len(self.download_queue)}. {title} - {link}\n")
            self.queue_title.delete(0, "end")
            self.queue_link.delete(0, "end")
            self.queue_prefix.delete(0, "end")

    def _clear_queue(self):
        self.download_queue.clear()
        if HAS_CTK:
            self.queue_textbox.delete("1.0", "end")

    def _start_queue(self):
        if self.download_queue:
            self._process_next_queue_item()

    def _process_next_queue_item(self):
        if not self.download_queue:
            self._log_to_ui("Queue complete!")
            return
        job = self.download_queue.pop(0)
        if HAS_CTK:
            self.entry_title.delete(0, "end")
            self.entry_title.insert(0, job['title'])
            self.entry_link.delete(0, "end")
            self.entry_link.insert(0, job['link'])
            self.entry_prefix.delete(0, "end")
            self.entry_prefix.insert(0, job['prefix'])
        self._start_transfer()

    def _on_close(self):
        """Handle window close."""
        if self.engine:
            self.engine.cancel()
        if self.psiphon:
            self.psiphon.stop()
        self.root.destroy()

    def run(self):
        """Start the application."""
        self.root.mainloop()


# ============================================================================
# CLI MODE (for testing without GUI)
# ============================================================================
def cli_mode():
    """Run in CLI mode for testing."""
    import argparse
    parser = argparse.ArgumentParser(description="MegaTransfer Desktop - CLI Mode")
    parser.add_argument("mega_url", help="Mega folder URL")
    parser.add_argument("title", help="Job title")
    parser.add_argument("--prefix", help="S3 prefix (auto-generated from title if not provided)")
    parser.add_argument("--threads", type=int, default=16, help="Parallel threads per file")
    parser.add_argument("--no-psiphon", action="store_true", help="Disable Psiphon")
    args = parser.parse_args()

    settings = load_settings()
    settings['parallel_threads'] = args.threads
    if args.no_psiphon:
        settings['use_psiphon'] = False

    s3_prefix = args.prefix or re.sub(r'[^a-zA-Z0-9-]', '-', args.title.lower()).strip('-')

    psiphon = PsiphonManager() if settings.get('use_psiphon') else None

    def progress(downloaded, total, name):
        pct = downloaded * 100 // total if total else 0
        speed = downloaded / (time.time() - start_time) / 1024 / 1024 if time.time() > start_time else 0
        print(f"\r  {pct}% @ {speed:.1f} MB/s ({name})", end="", flush=True)

    engine = DownloadEngine(settings, psiphon=psiphon, progress_callback=progress,
                            log_callback=lambda msg: print(msg))
    start_time = time.time()
    success = engine.process_job(args.mega_url, args.title, s3_prefix)
    sys.exit(0 if success else 1)


# ============================================================================
# ENTRY POINT
# ============================================================================
if __name__ == "__main__":
    if len(sys.argv) > 1 and not sys.argv[1].startswith("--gui"):
        cli_mode()
    else:
        app = MegaTransferApp()
        app.run()
