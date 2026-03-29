import os
import json
import re
import threading
from datetime import datetime

from app.database import get_connection
from app.services.mega_downloader import MegaDownloader
from app.services.storage import get_storage
from app.services.telegram import TelegramBot
from app.config import settings


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


def _run_transfer(job_id: int):
    """Main transfer pipeline: download -> exclude -> upload -> notify."""
    conn = get_connection()
    job = conn.execute("SELECT * FROM transfer_jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()

    if not job:
        return

    job = dict(job)
    excluded_files = json.loads(job["excluded_files"]) if job["excluded_files"] else []
    storage_target = job["storage_target"]
    title = job["title"]
    slug = job["download_slug"]

    downloader = MegaDownloader()

    try:
        # Step 1: Download from Mega
        _update_job(job_id, status="downloading", progress=10)
        download_path = downloader.download_folder(
            mega_link=job["mega_link"],
            job_id=job_id,
            excluded_files=excluded_files,
        )

        # Step 2: Upload to storage
        _update_job(job_id, status="uploading", progress=40)
        storage = get_storage(storage_target)

        # Walk through downloaded files and upload
        uploaded = 0
        total = _count_files(download_path)
        _update_job(job_id, total_files=total)

        for root, dirs, files in os.walk(download_path):
            for fname in files:
                local_file = os.path.join(root, fname)
                # Build the remote key preserving folder structure
                relative = os.path.relpath(local_file, download_path)
                remote_key = f"{slug}/{relative}"

                try:
                    file_size = os.path.getsize(local_file)
                    storage.upload_file(local_file, remote_key)

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

                    uploaded += 1
                    progress = 40 + int((uploaded / max(total, 1)) * 50)
                    _update_job(job_id, uploaded_files=uploaded, progress=progress)

                except Exception as e:
                    print(f"Failed to upload {fname}: {e}")
                    continue

        # Step 3: Mark complete
        _update_job(
            job_id,
            status="completed",
            progress=100,
            completed_at=datetime.utcnow().isoformat(),
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
