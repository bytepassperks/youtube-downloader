from fastapi import APIRouter, HTTPException, Depends

from app.models.schemas import MemberCreate, MemberUpdate, UserResponse, AccessGrant
from app.utils.auth import get_password_hash, get_admin_user
from app.database import get_connection

router = APIRouter(prefix="/api/members", tags=["members"])


@router.post("/", response_model=UserResponse)
async def create_member(member: MemberCreate, admin: dict = Depends(get_admin_user)):
    conn = get_connection()
    existing = conn.execute("SELECT id FROM users WHERE email = ?", (member.email,)).fetchone()
    if existing:
        conn.close()
        raise HTTPException(status_code=400, detail="Email already registered")

    hashed = get_password_hash(member.password)
    cursor = conn.execute(
        "INSERT INTO users (email, hashed_password, is_admin) VALUES (?, ?, 0)",
        (member.email, hashed),
    )
    user_id = cursor.lastrowid
    conn.commit()

    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()

    return {
        "id": user["id"],
        "email": user["email"],
        "is_admin": bool(user["is_admin"]),
        "is_active": bool(user["is_active"]),
        "created_at": user["created_at"],
        "max_downloads_per_day": user["max_downloads_per_day"],
    }


@router.get("/", response_model=list[UserResponse])
async def list_members(admin: dict = Depends(get_admin_user)):
    conn = get_connection()
    users = conn.execute("SELECT * FROM users WHERE is_admin = 0 ORDER BY created_at DESC").fetchall()
    conn.close()
    return [
        {
            "id": u["id"],
            "email": u["email"],
            "is_admin": bool(u["is_admin"]),
            "is_active": bool(u["is_active"]),
            "created_at": u["created_at"],
            "max_downloads_per_day": u["max_downloads_per_day"],
        }
        for u in users
    ]


@router.put("/{user_id}", response_model=UserResponse)
async def update_member(user_id: int, update: MemberUpdate, admin: dict = Depends(get_admin_user)):
    conn = get_connection()
    user = conn.execute("SELECT * FROM users WHERE id = ? AND is_admin = 0", (user_id,)).fetchone()
    if not user:
        conn.close()
        raise HTTPException(status_code=404, detail="Member not found")

    if update.is_active is not None:
        conn.execute("UPDATE users SET is_active = ? WHERE id = ?", (int(update.is_active), user_id))
    if update.max_downloads_per_day is not None:
        conn.execute("UPDATE users SET max_downloads_per_day = ? WHERE id = ?", (update.max_downloads_per_day, user_id))

    conn.commit()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()

    return {
        "id": user["id"],
        "email": user["email"],
        "is_admin": bool(user["is_admin"]),
        "is_active": bool(user["is_active"]),
        "created_at": user["created_at"],
        "max_downloads_per_day": user["max_downloads_per_day"],
    }


@router.delete("/{user_id}")
async def delete_member(user_id: int, admin: dict = Depends(get_admin_user)):
    conn = get_connection()
    user = conn.execute("SELECT * FROM users WHERE id = ? AND is_admin = 0", (user_id,)).fetchone()
    if not user:
        conn.close()
        raise HTTPException(status_code=404, detail="Member not found")

    conn.execute("DELETE FROM allowed_access WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM download_logs WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    return {"message": "Member deleted"}


# Access management
@router.post("/access")
async def grant_access(access: AccessGrant, admin: dict = Depends(get_admin_user)):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO allowed_access (user_id, job_id) VALUES (?, ?)",
            (access.user_id, access.job_id),
        )
        conn.commit()
    except Exception:
        conn.close()
        raise HTTPException(status_code=400, detail="Access already granted or invalid IDs")
    conn.close()
    return {"message": "Access granted"}


@router.delete("/access/{user_id}/{job_id}")
async def revoke_access(user_id: int, job_id: int, admin: dict = Depends(get_admin_user)):
    conn = get_connection()
    conn.execute("DELETE FROM allowed_access WHERE user_id = ? AND job_id = ?", (user_id, job_id))
    conn.commit()
    conn.close()
    return {"message": "Access revoked"}


@router.get("/access/{user_id}")
async def get_user_access(user_id: int, admin: dict = Depends(get_admin_user)):
    conn = get_connection()
    access = conn.execute(
        """SELECT a.*, t.title, t.download_slug
           FROM allowed_access a
           JOIN transfer_jobs t ON a.job_id = t.id
           WHERE a.user_id = ?""",
        (user_id,),
    ).fetchall()
    conn.close()
    return [dict(a) for a in access]


@router.post("/grant-all/{job_id}")
async def grant_all_members_access(job_id: int, admin: dict = Depends(get_admin_user)):
    """Grant all active members access to a specific content."""
    conn = get_connection()
    members = conn.execute("SELECT id FROM users WHERE is_admin = 0 AND is_active = 1").fetchall()
    granted = 0
    for member in members:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO allowed_access (user_id, job_id) VALUES (?, ?)",
                (member["id"], job_id),
            )
            granted += 1
        except Exception:
            pass
    conn.commit()
    conn.close()
    return {"message": f"Access granted to {granted} members"}
