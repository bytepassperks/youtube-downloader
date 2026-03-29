import json
from fastapi import APIRouter, HTTPException, Depends

from app.models.schemas import JobCreate, JobResponse
from app.utils.auth import get_admin_user
from app.database import get_connection
from app.services.transfer_worker import slugify, process_transfer_job

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _job_to_response(job: dict) -> dict:
    excluded = job.get("excluded_files", "[]")
    if isinstance(excluded, str):
        try:
            excluded = json.loads(excluded)
        except json.JSONDecodeError:
            excluded = []
    return {
        "id": job["id"],
        "title": job["title"],
        "mega_link": job["mega_link"],
        "excluded_files": excluded,
        "storage_target": job["storage_target"],
        "status": job["status"],
        "progress": job["progress"],
        "total_files": job["total_files"],
        "uploaded_files": job["uploaded_files"],
        "folder_path": job["folder_path"] or "",
        "download_slug": job["download_slug"] or "",
        "error_message": job["error_message"] or "",
        "created_at": job["created_at"],
        "completed_at": job.get("completed_at"),
        "telegram_sent": bool(job["telegram_sent"]),
    }


@router.post("/", response_model=JobResponse)
async def create_job(job: JobCreate, admin: dict = Depends(get_admin_user)):
    slug = slugify(job.title)

    # Ensure unique slug
    conn = get_connection()
    existing = conn.execute(
        "SELECT id FROM transfer_jobs WHERE download_slug = ?", (slug,)
    ).fetchone()

    if existing:
        import time
        slug = f"{slug}-{int(time.time())}"

    excluded_json = json.dumps(job.excluded_files)

    cursor = conn.execute(
        """INSERT INTO transfer_jobs (title, mega_link, excluded_files, storage_target, download_slug)
           VALUES (?, ?, ?, ?, ?)""",
        (job.title, job.mega_link, excluded_json, job.storage_target, slug),
    )
    job_id = cursor.lastrowid
    conn.commit()

    new_job = conn.execute("SELECT * FROM transfer_jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()

    # Auto-start the job in background
    process_transfer_job(job_id)

    return _job_to_response(dict(new_job))


@router.get("/", response_model=list[JobResponse])
async def list_jobs(admin: dict = Depends(get_admin_user)):
    conn = get_connection()
    jobs = conn.execute("SELECT * FROM transfer_jobs ORDER BY created_at DESC").fetchall()
    conn.close()
    return [_job_to_response(dict(j)) for j in jobs]


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(job_id: int, admin: dict = Depends(get_admin_user)):
    conn = get_connection()
    job = conn.execute("SELECT * FROM transfer_jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_to_response(dict(job))


@router.post("/{job_id}/start")
async def start_job(job_id: int, admin: dict = Depends(get_admin_user)):
    conn = get_connection()
    job = conn.execute("SELECT * FROM transfer_jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] not in ("queued", "failed"):
        raise HTTPException(status_code=400, detail=f"Job is already {job['status']}")

    process_transfer_job(job_id)
    return {"message": "Job started", "job_id": job_id}


@router.delete("/{job_id}")
async def delete_job(job_id: int, admin: dict = Depends(get_admin_user)):
    conn = get_connection()
    conn.execute("DELETE FROM content_items WHERE job_id = ?", (job_id,))
    conn.execute("DELETE FROM allowed_access WHERE job_id = ?", (job_id,))
    conn.execute("DELETE FROM download_logs WHERE job_id = ?", (job_id,))
    conn.execute("DELETE FROM transfer_jobs WHERE id = ?", (job_id,))
    conn.commit()
    conn.close()
    return {"message": "Job deleted"}


@router.post("/{job_id}/retry")
async def retry_job(job_id: int, admin: dict = Depends(get_admin_user)):
    conn = get_connection()
    job = conn.execute("SELECT * FROM transfer_jobs WHERE id = ?", (job_id,)).fetchone()
    if not job:
        conn.close()
        raise HTTPException(status_code=404, detail="Job not found")

    conn.execute(
        """UPDATE transfer_jobs
           SET status = 'queued', progress = 0, error_message = '',
               uploaded_files = 0, total_files = 0,
               completed_at = NULL, telegram_sent = 0
           WHERE id = ?""",
        (job_id,),
    )
    conn.execute("DELETE FROM content_items WHERE job_id = ?", (job_id,))
    conn.commit()
    conn.close()

    process_transfer_job(job_id)
    return {"message": "Job retried", "job_id": job_id}
