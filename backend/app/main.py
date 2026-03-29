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
    # Create default admin if not exists
    conn = get_connection()
    admin = conn.execute("SELECT id FROM users WHERE email = ?", (settings.ADMIN_EMAIL,)).fetchone()
    if not admin:
        hashed = get_password_hash(settings.ADMIN_PASSWORD)
        conn.execute(
            "INSERT INTO users (email, hashed_password, is_admin) VALUES (?, ?, 1)",
            (settings.ADMIN_EMAIL, hashed),
        )
        conn.commit()
    conn.close()


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
