import os
import subprocess
import shutil
import json
import re
from typing import Optional

from app.database import get_connection


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


class MegaDownloader:
    """Downloads files from Mega.nz public folder links using megadl CLI."""

    def __init__(self, download_base: str = "/tmp/mega_downloads"):
        self.download_base = download_base
        self._megatools_available = _ensure_megatools()

    def download_folder(
        self,
        mega_link: str,
        job_id: int,
        excluded_files: list[str] = None,
        progress_callback=None,
    ) -> str:
        """Download a Mega folder, exclude specified files, return local path."""
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
            # Use megadl to download
            result = subprocess.run(
                ["megadl", mega_link, "--path", download_path],
                capture_output=True,
                text=True,
                timeout=7200,  # 2 hour timeout
            )

            if result.returncode != 0:
                error_msg = result.stderr or result.stdout or "Unknown megadl error"
                raise Exception(f"megadl failed: {error_msg}")

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

    def _remove_excluded_files(self, base_path: str, excluded_files: list[str]):
        """Remove excluded files from the downloaded folder."""
        if not excluded_files:
            return

        for root, dirs, files in os.walk(base_path):
            for fname in files:
                for excluded in excluded_files:
                    if excluded.strip() and excluded.strip().lower() in fname.lower():
                        filepath = os.path.join(root, fname)
                        os.remove(filepath)
                        break

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
