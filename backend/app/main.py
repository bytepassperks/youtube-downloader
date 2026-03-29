import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.database import init_db, get_connection
from app.utils.auth import get_password_hash
from app.config import settings as app_settings
from app.routes import auth, jobs, members, portal
from app.routes import settings as settings_route

app = FastAPI(title="MegaTransfer API")

# Disable CORS. Do not remove this for full-stack development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

# Include routers
app.include_router(auth.router)
app.include_router(jobs.router)
app.include_router(members.router)
app.include_router(portal.router)
app.include_router(settings_route.router)


@app.on_event("startup")
async def startup():
    # Clean up old downloads FIRST to free disk space
    # (prevents sqlite3.OperationalError: disk I/O error when disk is full)
    _cleanup_disk_before_db()

    init_db()
    # Create or update default admin
    conn = get_connection()
    admin = conn.execute("SELECT id FROM users WHERE email = ?", (app_settings.ADMIN_EMAIL,)).fetchone()
    hashed = get_password_hash(app_settings.ADMIN_PASSWORD)
    if not admin:
        conn.execute(
            "INSERT INTO users (email, hashed_password, is_admin) VALUES (?, ?, 1)",
            (app_settings.ADMIN_EMAIL, hashed),
        )
    else:
        # Always update admin password to match env var
        conn.execute(
            "UPDATE users SET hashed_password = ? WHERE email = ?",
            (hashed, app_settings.ADMIN_EMAIL),
        )
    conn.commit()

    # Kill all stale proxy processes from previous deploys
    _kill_stale_processes()

    # Resume ONLY the latest incomplete job (not all — prevents duplicates)
    from app.services.transfer_worker import process_transfer_job
    incomplete = conn.execute(
        "SELECT id FROM transfer_jobs WHERE status IN ('downloading', 'uploading') ORDER BY id DESC LIMIT 1"
    ).fetchall()
    # Mark all OTHER incomplete jobs as failed to prevent future resume
    all_incomplete = conn.execute(
        "SELECT id FROM transfer_jobs WHERE status IN ('downloading', 'uploading') ORDER BY id DESC"
    ).fetchall()
    for i, row in enumerate(all_incomplete):
        if i > 0:  # Skip the latest one
            conn.execute(
                "UPDATE transfer_jobs SET status = 'failed', error_message = 'Cancelled: superseded by newer job' WHERE id = ?",
                (row["id"],)
            )
            print(f"[Startup] Cancelled stale job {row['id']}")
    conn.commit()
    conn.close()

    for row in incomplete:
        job_id = row["id"]
        print(f"[Startup] Resuming latest job {job_id}")
        process_transfer_job(job_id)


def _cleanup_disk_before_db():
    """Free disk space before SQLite init to prevent disk I/O errors."""
    import shutil
    download_base = "/data/mega_downloads"
    if os.path.isdir(download_base):
        try:
            size = sum(
                os.path.getsize(os.path.join(d, f))
                for d, _, files in os.walk(download_base)
                for f in files
            )
            shutil.rmtree(download_base, ignore_errors=True)
            os.makedirs(download_base, exist_ok=True)
            print(f"[Startup] Freed {size / (1024*1024):.1f} MB from old downloads")
        except Exception as e:
            print(f"[Startup] Disk cleanup warning: {e}")


def _kill_stale_processes():
    """Kill all stale proxy processes from previous deploys."""
    import subprocess
    for cmd in [
        ["killall", "-9", "wireproxy"],
        ["killall", "-9", "tor"],
        ["killall", "-9", "psiphon-tunnel-core"],
    ]:
        try:
            subprocess.run(cmd, capture_output=True, timeout=5)
        except Exception:
            pass
    # Wait for ports to be released
    import time
    time.sleep(2)
    print("[Startup] Killed all stale proxy processes")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/api/test-psiphon")
async def test_psiphon():
    """Test if Psiphon can connect on this server."""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    def _test():
        from app.services.mega_downloader import PsiphonManager
        pm = PsiphonManager(instance_id=99)
        try:
            connected = pm.start()
            proxy = pm.get_proxy_url()
            return {"connected": connected, "proxy": proxy}
        finally:
            pm.stop()

    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as pool:
        result = await loop.run_in_executor(pool, _test)
    return result
