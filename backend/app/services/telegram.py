import httpx
from app.config import settings


def _get_tg_setting(key: str) -> str:
    """Get Telegram setting from DB first, then fall back to env."""
    try:
        from app.routes.settings import get_setting
        return get_setting(key)
    except Exception:
        if key == "telegram_bot_token":
            return settings.TELEGRAM_BOT_TOKEN
        if key == "telegram_group_chat_id":
            return settings.TELEGRAM_GROUP_CHAT_ID
        return ""


class TelegramBot:
    """Sends messages to a Telegram private group."""

    def __init__(self):
        self.token = _get_tg_setting("telegram_bot_token")
        self.chat_id = _get_tg_setting("telegram_group_chat_id")
        self.base_url = f"https://api.telegram.org/bot{self.token}"

    async def send_message(self, text: str, parse_mode: str = "HTML") -> dict:
        """Send a message to the configured Telegram group."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
                timeout=30,
            )
            return response.json()

    async def send_transfer_complete(self, title: str, download_url: str) -> dict:
        """Send a transfer completion message with title and download link."""
        message = f"<b>{title}</b>\n\n{download_url}"
        return await self.send_message(message)

    def send_message_sync(self, text: str, parse_mode: str = "HTML") -> dict:
        """Synchronous version for use in background workers."""
        with httpx.Client() as client:
            response = client.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
                timeout=30,
            )
            return response.json()

    def send_transfer_complete_sync(self, title: str, download_url: str) -> dict:
        """Synchronous version of send_transfer_complete."""
        message = f"<b>{title}</b>\n\n{download_url}"
        return self.send_message_sync(message)
