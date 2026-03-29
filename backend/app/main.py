from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.database import init_db, get_connection
from app.utils.auth import get_password_hash
from app.config import settings
from app.routes import auth, jobs, members, portal

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


@app.on_event("startup")
async def startup():
    init_db()
    # Create or update default admin
    conn = get_connection()
    admin = conn.execute("SELECT id FROM users WHERE email = ?", (settings.ADMIN_EMAIL,)).fetchone()
    hashed = get_password_hash(settings.ADMIN_PASSWORD)
    if not admin:
        conn.execute(
            "INSERT INTO users (email, hashed_password, is_admin) VALUES (?, ?, 1)",
            (settings.ADMIN_EMAIL, hashed),
        )
    else:
        # Always update admin password to match env var
        conn.execute(
            "UPDATE users SET hashed_password = ? WHERE email = ?",
            (hashed, settings.ADMIN_EMAIL),
        )
    conn.commit()

    # Resume jobs that were interrupted mid-process (not failed/queued ones
    # to avoid restart loops when quota is exhausted)
    from app.services.transfer_worker import process_transfer_job
    incomplete = conn.execute(
        "SELECT id FROM transfer_jobs WHERE status IN ('downloading', 'uploading')"
    ).fetchall()
    conn.close()

    for row in incomplete:
        job_id = row["id"]
        print(f"[Startup] Resuming interrupted job {job_id}")
        process_transfer_job(job_id)


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
