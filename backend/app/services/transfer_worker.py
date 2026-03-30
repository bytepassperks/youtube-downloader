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
_cancel_event = threading.Event()  # Set to cancel the running job


def is_job_cancelled() -> bool:
    """Check if the current job has been cancelled."""
    return _cancel_event.is_set()


def cancel_current_job():
    """Signal the running job to stop."""
    _cancel_event.set()


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
    """Main transfer pipeline: stream Mega → decrypt → S3 (zero disk) → notify.

    Uses streaming mode by default: files are downloaded from Mega, decrypted
    in memory, and uploaded directly to S3 via multipart upload.  Zero disk
    usage — supports any file size including 100 GB+.
    """
    global _current_job_id

    # Acquire lock to prevent concurrent downloads
    if not _transfer_lock.acquire(timeout=5):
        print(f"[Job {job_id}] Another job is already running (job {_current_job_id}), skipping")
        sys.stdout.flush()
        return

    _current_job_id = job_id
    _cancel_event.clear()  # Reset cancel flag for new job
    print(f"[Job {job_id}] Acquired transfer lock")
    sys.stdout.flush()

    conn = get_connection()
    job = conn.execute("SELECT * FROM transfer_jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()

    if not job:
        _transfer_lock.release()
        _current_job_id = None
        return

    # Skip if job was already stopped/failed
    if job["status"] in ("failed", "completed"):
        print(f"[Job {job_id}] Job already {job['status']}, skipping")
        sys.stdout.flush()
        _transfer_lock.release()
        _current_job_id = None
        return

    job = dict(job)
    excluded_files = json.loads(job["excluded_files"]) if job["excluded_files"] else []
    storage_target = job["storage_target"]
    title = job["title"]
    slug = job["download_slug"]

    downloader = MegaDownloader()
    storage = get_storage(storage_target)

    try:
        # Check which files have already been uploaded (for resume support)
        conn = get_connection()
        already_uploaded = set()
        rows = conn.execute(
            "SELECT file_path FROM content_items WHERE job_id = ?", (job_id,)
        ).fetchall()
        conn.close()
        for row in rows:
            already_uploaded.add(row["file_path"])

        uploaded_count = len(already_uploaded)
        start_time = time.time()

        def _on_file_uploaded(remote_key, file_name, relative_path, file_size):
            """Record each streamed file in the database."""
            nonlocal uploaded_count
            if relative_path in already_uploaded:
                return
            try:
                conn2 = get_connection()
                conn2.execute(
                    """INSERT INTO content_items
                       (job_id, file_name, file_path, storage_key, storage_target, file_size)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (job_id, file_name, relative_path, remote_key, storage_target, file_size),
                )
                conn2.commit()
                conn2.close()
                uploaded_count += 1
                elapsed = time.time() - start_time
                speed = (file_size / (1024 * 1024)) / max(elapsed, 0.1)
                print(f"[Upload] Recorded {file_name} ({uploaded_count} done, {speed:.1f} MB/s)")
                sys.stdout.flush()
            except Exception as e:
                print(f"[Upload] DB record error for {file_name}: {e}")
                sys.stdout.flush()

        # Stream: Mega → decrypt in memory → S3 multipart upload (zero disk)
        print(f"[Job {job_id}] Starting streaming transfer (zero disk mode)")
        sys.stdout.flush()
        total_uploaded = downloader.stream_folder(
            mega_link=job["mega_link"],
            job_id=job_id,
            storage=storage,
            remote_prefix=slug,
            excluded_files=excluded_files,
            file_uploaded_callback=_on_file_uploaded,
        )

        # Mark complete
        total_elapsed = time.time() - start_time
        _update_job(
            job_id,
            status="completed",
            progress=100,
            uploaded_files=total_uploaded,
            total_files=total_uploaded,
            completed_at=datetime.utcnow().isoformat(),
            current_file=f"Complete: {total_uploaded} files streamed to storage",
            upload_speed=f"{total_uploaded / max(total_elapsed / 60, 0.01):.0f} files/min",
        )

        # Send Telegram notification
        try:
            portal_url = f"{settings.PORTAL_BASE_URL}/content/{slug}"
            bot = TelegramBot()
            bot.send_transfer_complete_sync(title, portal_url)
            _update_job(job_id, telegram_sent=1)
        except Exception as e:
            print(f"Telegram notification failed: {e}")

    except Exception as e:
        import traceback
        traceback.print_exc()
        _update_job(job_id, status="failed", error_message=str(e))
    finally:
        _current_job_id = None
        _transfer_lock.release()
        print(f"[Job {job_id}] Released transfer lock")
        sys.stdout.flush()


_ALLOWED_JOB_COLUMNS = {
    "status", "progress", "updated_at", "total_files", "uploaded_files",
    "folder_path", "download_slug", "error_message", "current_file",
    "download_speed", "upload_speed", "downloaded_files", "completed_at",
    "telegram_sent",
}


def _update_job(job_id: int, **kwargs):
    """Update job fields in database."""
    if not kwargs:
        return
    # Filter to only allowed column names to prevent SQL injection
    safe_kwargs = {k: v for k, v in kwargs.items() if k in _ALLOWED_JOB_COLUMNS}
    if not safe_kwargs:
        return
    conn = get_connection()
    sets = ", ".join(f"{k} = ?" for k in safe_kwargs)
    values = list(safe_kwargs.values()) + [job_id]
    try:
        conn.execute(f"UPDATE transfer_jobs SET {sets} WHERE id = ?", values)
        conn.commit()
    except Exception as e:
        print(f"[DB] Failed to update job {job_id}: {e}")
        sys.stdout.flush()
    finally:
        conn.close()


def _count_files(path: str) -> int:
    count = 0
    for _root, _dirs, files in os.walk(path):
        count += len(files)
    return count
