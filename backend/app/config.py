import os
from pydantic_settings import BaseSettings
from dotenv import load_dotenv

load_dotenv()


class Settings(BaseSettings):
    # App
    APP_NAME: str = "MegaTransfer"
    SECRET_KEY: str = os.getenv("SECRET_KEY", "super-secret-key-change-in-production-2024")
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440  # 24 hours

    # Database
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./data/app.db")

    # Backblaze B2
    B2_KEY_ID: str = os.getenv("B2_KEY_ID", "")
    B2_APP_KEY: str = os.getenv("B2_APP_KEY", "")
    B2_BUCKET_NAME: str = os.getenv("B2_BUCKET_NAME", "BytePass")

    # iDrive E2 (S3-compatible)
    IDRIVE_ACCESS_KEY: str = os.getenv("IDRIVE_ACCESS_KEY", "")
    IDRIVE_SECRET_KEY: str = os.getenv("IDRIVE_SECRET_KEY", "")
    IDRIVE_ENDPOINT: str = os.getenv("IDRIVE_ENDPOINT", "")
    IDRIVE_BUCKET: str = os.getenv("IDRIVE_BUCKET", "mega-transfers")
    IDRIVE_REGION: str = os.getenv("IDRIVE_REGION", "us-west-2")

    # Telegram
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_GROUP_CHAT_ID: str = os.getenv("TELEGRAM_GROUP_CHAT_ID", "")

    # Signed URL expiry (seconds)
    SIGNED_URL_EXPIRY: int = 7200  # 2 hours

    # Download portal base URL
    PORTAL_BASE_URL: str = os.getenv("PORTAL_BASE_URL", "http://localhost:5173")

    # Admin credentials (first admin)
    ADMIN_EMAIL: str = os.getenv("ADMIN_EMAIL", "admin@bytecare.shop")
    ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "admin123")

    class Config:
        env_file = ".env"


settings = Settings()
