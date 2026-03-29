import os
import sys
import json
import re
import time
import threading
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from app.database import get_connection
from app.services.mega_downloader import MegaDownloader
from app.services.storage import get_storage
from app.services.telegram import TelegramBot
from app.config import settings

# Number of parallel upload threads
UPLOAD_WORKERS = 4

# Global lock to prevent concurrent downloads
_transfer_lock = threading.Lock()
_current_job_id: Optional[int] = None


def slugify(text: str) -> str:
    """Create a URL-friendly slug from text."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")


def process_transfer_job(job_id: int):
    """Process a single transfer job in a background thread."""
    thread = threading.Thread(target=_run_transfer, args=(job_id,), daemon=True)
    thread.start()
    return thread


def _upload_single_file(storage, local_file, remote_key, job_id, fname, relative, storage_target):
    """Upload a single file and record it in the database. Returns True on success."""
    try:
        file_size = os.path.getsize(local_file)
        print(f"[Upload] Uploading {fname} ({file_size} bytes) -> {remote_key}")
        sys.stdout.flush()
        upload_start = time.time()
        storage.upload_file(local_file, remote_key)
        upload_elapsed = time.time() - upload_start
        speed_mbps = (file_size / (1024 * 1024)) / max(upload_elapsed, 0.01)
        print(f"[Upload] Uploaded {fname} ({speed_mbps:.1f} MB/s)")
        sys.stdout.flush()

        # Record in database
        conn = get_connection()
        conn.execute(
            """INSERT INTO content_items
               (job_id, file_name, file_path, storage_key, storage_target, file_size)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (job_id, fname, relative, remote_key, storage_target, file_size),
        )
        conn.commit()
        conn.close()

        # Delete local file immediately after upload to save disk space
        try:
            os.remove(local_file)
        except OSError:
            pass

        return True
    except Exception as e:
        import traceback
        print(f"Failed to upload {fname}: {e}")
        traceback.print_exc()
        sys.stdout.flush()
        return False


def _run_transfer(job_id: int):
    """Main transfer pipeline: download -> exclude -> upload (parallel) -> notify.

    Supports resuming interrupted jobs:
    - If job was 'uploading', skip download and go straight to upload from /data
    - If job was 'downloading', re-download (with file-level resume support)
    """
    global _current_job_id

    # Acquire lock to prevent concurrent downloads
    if not _transfer_lock.acquire(timeout=5):
        print(f"[Job {job_id}] Another job is already running (job {_current_job_id}), skipping")
        sys.stdout.flush()
        return

    _current_job_id = job_id
    print(f"[Job {job_id}] Acquired transfer lock")
    sys.stdout.flush()

    conn = get_connection()
    job = conn.execute("SELECT * FROM transfer_jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()

    if not job:
        _transfer_lock.release()
        _current_job_id = None
        return

    job = dict(job)
    excluded_files = json.loads(job["excluded_files"]) if job["excluded_files"] else []
    storage_target = job["storage_target"]
    title = job["title"]
    slug = job["download_slug"]
    previous_status = job["status"]

    downloader = MegaDownloader()
    download_path = os.path.join(downloader.download_base, str(job_id))

    try:
        # Prepare storage for inline upload during download
        storage = get_storage(storage_target)

        # Check which files have already been uploaded (for resume support)
        conn = get_connection()
        already_uploaded = set()
        rows = conn.execute(
            "SELECT file_path FROM content_items WHERE job_id = ?", (job_id,)
        ).fetchall()
        conn.close()
        for row in rows:
            already_uploaded.add(row["file_path"])

        uploaded = len(already_uploaded)
        uploaded_bytes = 0
        upload_start_time = time.time()

        def _on_file_done(local_path, relative_path, file_name, file_size):
            """Called after each file downloads — upload to storage + delete local."""
            nonlocal uploaded, uploaded_bytes
            if relative_path in already_uploaded:
                return
            remote_key = f"{slug}/{relative_path}"
            if _upload_single_file(
                storage, local_path, remote_key, job_id,
                file_name, relative_path, storage_target,
            ):
                uploaded += 1
                uploaded_bytes += file_size
                elapsed = time.time() - upload_start_time
                avg_speed = (uploaded_bytes / (1024 * 1024)) / max(elapsed, 0.1)
                _update_job(
                    job_id, uploaded_files=uploaded,
                    current_file=f"Uploaded: {file_name} ({uploaded} done)",
                    upload_speed=f"{avg_speed:.1f} MB/s",
                )

        # Step 1: Download from Mega with inline upload per file
        if previous_status == "uploading" and os.path.exists(download_path):
            file_count = _count_files(download_path)
            if file_count > 0:
                print(f"[Resume] Job {job_id}: Skipping download, {file_count} files already on disk")
                # Upload any remaining files on disk
                for root, dirs, files in os.walk(download_path):
                    for fname in files:
                        if fname.startswith('.'):
                            continue
                        local_file = os.path.join(root, fname)
                        relative = os.path.relpath(local_file, download_path)
                        if relative in already_uploaded:
                            continue
                        _on_file_done(local_file, relative, fname, os.path.getsize(local_file))
            else:
                print(f"[Resume] Job {job_id}: No files on disk despite uploading status, re-downloading")
                _update_job(job_id, status="downloading", progress=10, error_message="")
                download_path = downloader.download_folder(
                    mega_link=job["mega_link"],
                    job_id=job_id,
                    excluded_files=excluded_files,
                    file_done_callback=_on_file_done,
                )
        else:
            _update_job(job_id, status="downloading", progress=10, error_message="")
            download_path = downloader.download_folder(
                mega_link=job["mega_link"],
                job_id=job_id,
                excluded_files=excluded_files,
                file_done_callback=_on_file_done,
            )

        # Step 2: Upload any files still on disk (fallback for callback failures)
        print(f"[Job {job_id}] Download complete. Checking for remaining uploads...")
        sys.stdout.flush()
        if os.path.exists(download_path):
            for root, dirs, files in os.walk(download_path):
                for fname in files:
                    if fname.startswith('.'):
                        continue
                    local_file = os.path.join(root, fname)
                    relative = os.path.relpath(local_file, download_path)
                    if relative in already_uploaded:
                        continue
                    _on_file_done(local_file, relative, fname, os.path.getsize(local_file))

        # Step 3: Mark complete
        total = uploaded
        total_elapsed = time.time() - upload_start_time
        final_speed = (uploaded_bytes / (1024 * 1024)) / max(total_elapsed, 0.1)
        _update_job(
            job_id,
            status="completed",
            progress=100,
            uploaded_files=uploaded,
            total_files=total,
            completed_at=datetime.utcnow().isoformat(),
            current_file=f"Complete: {uploaded} files uploaded",
            upload_speed=f"{final_speed:.1f} MB/s",
        )

        # Step 4: Send Telegram notification
        try:
            portal_url = f"{settings.PORTAL_BASE_URL}/content/{slug}"
            bot = TelegramBot()
            bot.send_transfer_complete_sync(title, portal_url)
            _update_job(job_id, telegram_sent=1)
        except Exception as e:
            print(f"Telegram notification failed: {e}")

        # Step 5: Cleanup local files
        downloader.cleanup(job_id)

    except Exception as e:
        _update_job(job_id, status="failed", error_message=str(e))
        downloader.cleanup(job_id)
    finally:
        _current_job_id = None
        _transfer_lock.release()
        print(f"[Job {job_id}] Released transfer lock")
        sys.stdout.flush()


def _update_job(job_id: int, **kwargs):
    """Update job fields in database."""
    if not kwargs:
        return
    conn = get_connection()
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [job_id]
    conn.execute(f"UPDATE transfer_jobs SET {sets} WHERE id = ?", values)
    conn.commit()
    conn.close()


def _count_files(path: str) -> int:
    count = 0
    for root, dirs, files in os.walk(path):
        count += len(files)
    return count
