from fastapi import APIRouter, HTTPException, Depends, Request
from datetime import datetime

from app.models.schemas import ContentItemResponse, FolderViewResponse, SignedUrlResponse
from app.utils.auth import get_current_user
from app.database import get_connection
from app.services.storage import get_storage
from app.config import settings

router = APIRouter(prefix="/api/portal", tags=["portal"])


@router.get("/content/{slug}", response_model=FolderViewResponse)
async def get_content_folder(slug: str, current_user: dict = Depends(get_current_user)):
    conn = get_connection()

    # Find the job
    job = conn.execute(
        "SELECT * FROM transfer_jobs WHERE download_slug = ? AND status = 'completed'",
        (slug,),
    ).fetchone()

    if not job:
        conn.close()
        raise HTTPException(status_code=404, detail="Content not found")

    # Check access (admins have access to everything)
    if not current_user["is_admin"]:
        access = conn.execute(
            "SELECT id FROM allowed_access WHERE user_id = ? AND job_id = ?",
            (current_user["id"], job["id"]),
        ).fetchone()
        if not access:
            conn.close()
            raise HTTPException(status_code=403, detail="You don't have access to this content")

    # Get files
    files = conn.execute(
        "SELECT * FROM content_items WHERE job_id = ? ORDER BY file_path",
        (job["id"],),
    ).fetchall()
    conn.close()

    total_size = sum(f["file_size"] for f in files)

    return {
        "title": job["title"],
        "slug": slug,
        "files": [
            {
                "id": f["id"],
                "job_id": f["job_id"],
                "file_name": f["file_name"],
                "file_path": f["file_path"],
                "file_size": f["file_size"],
                "created_at": f["created_at"],
            }
            for f in files
        ],
        "total_size": total_size,
        "file_count": len(files),
    }


@router.get("/download/{file_id}", response_model=SignedUrlResponse)
async def get_download_url(file_id: int, request: Request, current_user: dict = Depends(get_current_user)):
    conn = get_connection()

    # Get file info
    file_item = conn.execute(
        "SELECT * FROM content_items WHERE id = ?", (file_id,)
    ).fetchone()

    if not file_item:
        conn.close()
        raise HTTPException(status_code=404, detail="File not found")

    # Check access
    if not current_user["is_admin"]:
        access = conn.execute(
            "SELECT id FROM allowed_access WHERE user_id = ? AND job_id = ?",
            (current_user["id"], file_item["job_id"]),
        ).fetchone()
        if not access:
            conn.close()
            raise HTTPException(status_code=403, detail="Access denied")

        # Check daily download limit
        today = datetime.utcnow().strftime("%Y-%m-%d")
        download_count = conn.execute(
            """SELECT COUNT(*) as cnt FROM download_logs
               WHERE user_id = ? AND downloaded_at LIKE ?""",
            (current_user["id"], f"{today}%"),
        ).fetchone()["cnt"]

        if download_count >= current_user["max_downloads_per_day"]:
            conn.close()
            raise HTTPException(status_code=429, detail="Daily download limit reached")

    # Log the download
    client_ip = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent", "unknown")
    conn.execute(
        """INSERT INTO download_logs (user_id, content_item_id, job_id, ip_address, user_agent)
           VALUES (?, ?, ?, ?, ?)""",
        (current_user["id"], file_id, file_item["job_id"], client_ip, user_agent),
    )
    conn.commit()
    conn.close()

    # Generate signed URL
    storage = get_storage(file_item["storage_target"])
    signed_url = storage.generate_signed_url(
        file_item["storage_key"], settings.SIGNED_URL_EXPIRY
    )

    return {
        "url": signed_url,
        "expires_in": settings.SIGNED_URL_EXPIRY,
        "file_name": file_item["file_name"],
    }


@router.get("/my-content")
async def get_my_content(current_user: dict = Depends(get_current_user)):
    """Get all content the current user has access to."""
    conn = get_connection()

    if current_user["is_admin"]:
        jobs = conn.execute(
            "SELECT * FROM transfer_jobs WHERE status = 'completed' ORDER BY created_at DESC"
        ).fetchall()
    else:
        jobs = conn.execute(
            """SELECT t.* FROM transfer_jobs t
               JOIN allowed_access a ON t.id = a.job_id
               WHERE a.user_id = ? AND t.status = 'completed'
               ORDER BY t.created_at DESC""",
            (current_user["id"],),
        ).fetchall()

    conn.close()

    return [
        {
            "id": j["id"],
            "title": j["title"],
            "download_slug": j["download_slug"],
            "created_at": j["created_at"],
        }
        for j in jobs
    ]
