from fastapi import APIRouter, Depends

from app.models.schemas import SettingsUpdate, SettingsResponse
from app.utils.auth import get_admin_user
from app.database import get_connection
from app.config import settings as app_settings

router = APIRouter(prefix="/api/settings", tags=["settings"])

# Keys we support in the settings table
_SETTING_KEYS = [
    "telegram_bot_token", "telegram_group_chat_id",
    "idrive_access_key", "idrive_secret_key", "idrive_endpoint",
    "idrive_bucket", "idrive_region",
    "b2_key_id", "b2_app_key", "b2_bucket_name",
    "portal_base_url", "admin_email", "admin_password",
]

# Mapping from setting key -> app_settings attribute for defaults
_ENV_DEFAULTS = {
    "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
    "telegram_group_chat_id": "TELEGRAM_GROUP_CHAT_ID",
    "idrive_access_key": "IDRIVE_ACCESS_KEY",
    "idrive_secret_key": "IDRIVE_SECRET_KEY",
    "idrive_endpoint": "IDRIVE_ENDPOINT",
    "idrive_bucket": "IDRIVE_BUCKET",
    "idrive_region": "IDRIVE_REGION",
    "b2_key_id": "B2_KEY_ID",
    "b2_app_key": "B2_APP_KEY",
    "b2_bucket_name": "B2_BUCKET_NAME",
    "portal_base_url": "PORTAL_BASE_URL",
    "admin_email": "ADMIN_EMAIL",
    "admin_password": "ADMIN_PASSWORD",
}


def get_setting(key: str) -> str:
    """Get a setting value: DB override first, then env/config default."""
    conn = get_connection()
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = ?", (key,)
    ).fetchone()
    conn.close()
    if row:
        return row["value"]
    attr = _ENV_DEFAULTS.get(key)
    if attr:
        return getattr(app_settings, attr, "")
    return ""


# Keys whose values should be masked in API responses
_SENSITIVE_KEYS = {
    "telegram_bot_token", "idrive_access_key", "idrive_secret_key",
    "b2_key_id", "b2_app_key", "admin_password",
}


def _mask(value: str) -> str:
    """Mask a sensitive value, showing only last 4 chars."""
    if not value or len(value) <= 4:
        return "****"
    return "*" * (len(value) - 4) + value[-4:]


@router.get("/", response_model=SettingsResponse)
async def get_settings(admin: dict = Depends(get_admin_user)):
    result = {}
    for key in _SETTING_KEYS:
        val = get_setting(key)
        if key in _SENSITIVE_KEYS and val:
            result[key] = _mask(val)
        else:
            result[key] = val
    return result


@router.get("/raw", response_model=SettingsResponse)
async def get_settings_raw(admin: dict = Depends(get_admin_user)):
    """Return unmasked settings - used internally for service connections."""
    result = {}
    for key in _SETTING_KEYS:
        result[key] = get_setting(key)
    return result


@router.put("/", response_model=SettingsResponse)
async def update_settings(
    data: SettingsUpdate, admin: dict = Depends(get_admin_user)
):
    conn = get_connection()
    updates = data.model_dump(exclude_none=True)
    for key, value in updates.items():
        if key in _SETTING_KEYS:
            conn.execute(
                """INSERT INTO app_settings (key, value, updated_at)
                   VALUES (?, ?, datetime('now'))
                   ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = datetime('now')""",
                (key, value, value),
            )
    conn.commit()
    conn.close()
    # Return full settings
    result = {}
    for key in _SETTING_KEYS:
        result[key] = get_setting(key)
    return result


@router.post("/reshare-telegram")
async def reshare_all_telegram(admin: dict = Depends(get_admin_user)):
    """Re-send Telegram notifications for ALL completed jobs."""
    import httpx

    bot_token = get_setting("telegram_bot_token")
    chat_id = get_setting("telegram_group_chat_id")
    portal_url = get_setting("portal_base_url")

    if not bot_token or not chat_id:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Telegram credentials not configured")

    conn = get_connection()
    jobs = conn.execute(
        "SELECT id, title, download_slug FROM transfer_jobs WHERE status = 'completed' ORDER BY id"
    ).fetchall()
    conn.close()

    sent = 0
    failed = 0
    base_url = f"https://api.telegram.org/bot{bot_token}"

    async with httpx.AsyncClient() as client:
        for job in jobs:
            download_url = f"{portal_url}/content/{job['download_slug']}"
            message = f"<b>{job['title']}</b>\n\n{download_url}"
            try:
                resp = await client.post(
                    f"{base_url}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": message,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                    timeout=30,
                )
                if resp.status_code == 200:
                    sent += 1
                else:
                    failed += 1
            except Exception:
                failed += 1

    # Update telegram_sent flag for all completed jobs
    conn = get_connection()
    conn.execute(
        "UPDATE transfer_jobs SET telegram_sent = 1 WHERE status = 'completed'"
    )
    conn.commit()
    conn.close()

    return {"message": f"Re-shared {sent} jobs to Telegram ({failed} failed)", "sent": sent, "failed": failed}
