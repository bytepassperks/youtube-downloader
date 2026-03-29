import os
import subprocess
import shutil
import json
import re
import time
from typing import Optional

from app.database import get_connection


# Number of parallel download workers (for future use with file-level parallelism)
DOWNLOAD_WORKERS = int(os.getenv("DOWNLOAD_WORKERS", "3"))

# Render API for service restart (IP rotation)
RENDER_API_KEY = os.getenv("RENDER_API_KEY", "")
RENDER_SERVICE_ID = os.getenv("RENDER_SERVICE_ID", "")


def _ensure_megatools():
    """Ensure megatools (megadl) is installed. Install if missing."""
    try:
        subprocess.run(["megadl", "--version"], capture_output=True, timeout=5)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Try installing megatools
    print("megadl not found, attempting to install megatools...")
    try:
        subprocess.run(
            ["apt-get", "update", "-qq"],
            capture_output=True, timeout=60,
        )
        result = subprocess.run(
            ["apt-get", "install", "-y", "-qq", "megatools"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            print("megatools installed successfully")
            return True
        print(f"apt-get install failed: {result.stderr}")
    except Exception as e:
        print(f"Failed to install megatools via apt: {e}")

    # Try downloading a static binary as fallback
    try:
        print("Trying static binary download...")
        subprocess.run(
            ["wget", "-q", "-O", "/usr/local/bin/megadl",
             "https://megatools.megous.com/builds/builds/megatools-1.11.1.20230212-linux-x86_64/megadl"],
            capture_output=True, timeout=30,
        )
        subprocess.run(["chmod", "+x", "/usr/local/bin/megadl"], capture_output=True)
        result = subprocess.run(["megadl", "--version"], capture_output=True, timeout=5)
        if result.returncode == 0:
            print("megadl static binary installed successfully")
            return True
    except Exception as e:
        print(f"Static binary fallback failed: {e}")

    return False


def _get_proxy_list() -> list[str]:
    """Get list of SOCKS5/HTTP proxy URLs from env var.

    Format: comma-separated proxy URLs
    e.g. MEGA_PROXIES=socks5://1.2.3.4:1080,socks5://5.6.7.8:1080
    """
    proxies_str = os.getenv("MEGA_PROXIES", "")
    if not proxies_str.strip():
        return []
    return [p.strip() for p in proxies_str.split(",") if p.strip()]


def _download_with_retry(mega_link: str, download_path: str, proxy: str = None,
                         max_retries: int = 3, timeout: int = 7200) -> bool:
    """Download from Mega with retry logic and optional proxy.

    Returns True on success, False if throttled/failed after retries.
    """
    for attempt in range(max_retries):
        try:
            cmd = ["megadl", mega_link, "--path", download_path]
            if proxy:
                cmd.extend(["--proxy", proxy])

            print(f"[Download] Attempt {attempt + 1}/{max_retries}, proxy={proxy or 'direct'}")

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            stdout = result.stdout or ""
            stderr = result.stderr or ""
            output = stdout + stderr

            # Check for success
            if result.returncode == 0:
                print(f"[Download] Success with proxy={proxy or 'direct'}")
                return True

            # Check for quota/bandwidth limit errors
            throttle_indicators = [
                "bandwidth limit",
                "over quota",
                "temporarily unavailable",
                "too many connections",
                "509",
                "rate limit",
            ]
            is_throttled = any(ind in output.lower() for ind in throttle_indicators)

            if is_throttled:
                print(f"[Download] Throttled on attempt {attempt + 1}, proxy={proxy or 'direct'}")
                if attempt < max_retries - 1:
                    wait_time = 10 * (attempt + 1)
                    print(f"[Download] Waiting {wait_time}s before retry...")
                    time.sleep(wait_time)
                continue

            # Non-throttle error
            print(f"[Download] Error: {output[:500]}")
            if attempt < max_retries - 1:
                time.sleep(5)
                continue

        except subprocess.TimeoutExpired:
            print(f"[Download] Timeout on attempt {attempt + 1}")
            if attempt < max_retries - 1:
                continue

    return False


def _trigger_render_restart():
    """Restart the Render service to get a fresh IP address.

    Used as a last resort when Mega is throttling the direct connection.
    """
    if not RENDER_API_KEY or not RENDER_SERVICE_ID:
        print("[IP Rotation] Render API key or service ID not configured, skipping restart")
        return False

    try:
        import urllib.request

        url = f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/deploys"
        data = json.dumps({"clearCache": "do_not_clear"}).encode()
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {RENDER_API_KEY}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        response = urllib.request.urlopen(req, timeout=30)
        if response.status in (200, 201):
            print("[IP Rotation] Render service restart triggered successfully")
            return True
        else:
            print(f"[IP Rotation] Render restart returned status {response.status}")
            return False
    except Exception as e:
        print(f"[IP Rotation] Failed to trigger Render restart: {e}")
        return False


class MegaDownloader:
    """Downloads files from Mega.nz public folder links using megadl CLI.

    Speed optimizations:
    - Proxy rotation: megadl --proxy socks5://... for IP rotation when throttled
    - Render auto-restart: triggers service restart for fresh IP as fallback
    - Resumable downloads: megadl resumes by default after restarts
    - Retry with backoff: automatic retries with exponential backoff
    """

    def __init__(self, download_base: str = "/data/mega_downloads"):
        self.download_base = download_base
        self._megatools_available = _ensure_megatools()
        self.proxies = _get_proxy_list()
        if self.proxies:
            print(f"[MegaDownloader] Loaded {len(self.proxies)} proxies for rotation")

    def download_folder(
        self,
        mega_link: str,
        job_id: int,
        excluded_files: list[str] = None,
        progress_callback=None,
    ) -> str:
        """Download a Mega folder with speed optimizations.

        Strategy:
        1. Try direct download first (fastest if not throttled)
        2. If throttled, rotate through proxy list
        3. If all proxies exhausted, trigger Render restart for fresh IP
        4. megadl resumes automatically, so restarts don't lose progress
        """
        if not self._megatools_available:
            raise Exception(
                "megatools (megadl) is not available and could not be installed. "
                "Please ensure megatools is installed on the server."
            )

        if excluded_files is None:
            excluded_files = []

        download_path = os.path.join(self.download_base, str(job_id))
        os.makedirs(download_path, exist_ok=True)

        # Update job status to downloading
        conn = get_connection()
        conn.execute(
            "UPDATE transfer_jobs SET status = 'downloading' WHERE id = ?",
            (job_id,),
        )
        conn.commit()
        conn.close()

        try:
            success = self._download_with_rotation(mega_link, download_path)

            if not success:
                raise Exception(
                    "Download failed after all retry attempts. "
                    "Mega may be throttling. The job will resume on next service restart."
                )

        except subprocess.TimeoutExpired:
            raise Exception("Download timed out after 2 hours")

        # Remove excluded files
        self._remove_excluded_files(download_path, excluded_files)

        # Count files
        file_count = self._count_files(download_path)

        conn = get_connection()
        conn.execute(
            "UPDATE transfer_jobs SET total_files = ? WHERE id = ?",
            (file_count, job_id),
        )
        conn.commit()
        conn.close()

        return download_path

    def _download_with_rotation(self, mega_link: str, download_path: str) -> bool:
        """Try downloading with proxy rotation for speed.

        Order:
        1. Direct connection (no proxy)
        2. Each proxy in the proxy list
        3. Trigger Render restart for fresh IP, then retry direct
        """
        # Attempt 1: Direct connection
        print("[Download] Trying direct connection...")
        if _download_with_retry(mega_link, download_path, proxy=None, max_retries=2):
            return True

        # Attempt 2: Try each proxy
        for i, proxy in enumerate(self.proxies):
            print(f"[Download] Trying proxy {i + 1}/{len(self.proxies)}: {proxy}")
            if _download_with_retry(mega_link, download_path, proxy=proxy, max_retries=2):
                return True

        # Attempt 3: Trigger Render restart for fresh IP
        print("[Download] All proxies exhausted, attempting Render IP rotation...")
        if _trigger_render_restart():
            print("[Download] Render restart triggered. Job will resume with fresh IP.")
            return False

        # Final attempt: Try direct one more time (maybe throttle expired)
        print("[Download] Final direct attempt...")
        return _download_with_retry(mega_link, download_path, proxy=None, max_retries=1, timeout=7200)

    def _remove_excluded_files(self, base_path: str, excluded_files: list[str]):
        """Remove excluded files from the downloaded folder."""
        if not excluded_files:
            return

        removed = 0
        for root, dirs, files in os.walk(base_path):
            for fname in files:
                for excluded in excluded_files:
                    if excluded.strip() and excluded.strip().lower() in fname.lower():
                        filepath = os.path.join(root, fname)
                        os.remove(filepath)
                        removed += 1
                        print(f"[Exclude] Removed: {fname}")
                        break

        if removed:
            print(f"[Exclude] Total files removed: {removed}")

    def _count_files(self, path: str) -> int:
        count = 0
        for root, dirs, files in os.walk(path):
            count += len(files)
        return count

    def cleanup(self, job_id: int):
        """Remove downloaded files for a job."""
        download_path = os.path.join(self.download_base, str(job_id))
        if os.path.exists(download_path):
            shutil.rmtree(download_path)
