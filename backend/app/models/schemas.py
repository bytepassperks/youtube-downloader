from pydantic import BaseModel, EmailStr, field_validator
from typing import Optional, Union
from datetime import datetime


# Auth
class UserCreate(BaseModel):
    email: str
    password: str


class UserResponse(BaseModel):
    id: int
    email: str
    is_admin: bool
    is_active: bool
    created_at: str
    max_downloads_per_day: int


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TokenData(BaseModel):
    email: Optional[str] = None


# Transfer Jobs
class JobCreate(BaseModel):
    title: str
    mega_link: str
    exclude_files: Union[str, list[str]] = []
    storage_target: str = "idrive"  # "idrive" or "b2"

    @field_validator("exclude_files", mode="before")
    @classmethod
    def parse_exclude_files(cls, v):
        if isinstance(v, str):
            return [f.strip() for f in v.split(",") if f.strip()]
        return v

    @property
    def excluded_files(self) -> list[str]:
        return self.exclude_files


class JobResponse(BaseModel):
    id: int
    title: str
    mega_link: str
    excluded_files: list[str]
    storage_target: str
    status: str
    progress: int
    total_files: int
    uploaded_files: int
    folder_path: str
    download_slug: str
    error_message: str
    created_at: str
    completed_at: Optional[str]
    telegram_sent: bool
    current_file: str = ""
    download_speed: str = ""
    upload_speed: str = ""
    downloaded_files: int = 0


class JobStatusUpdate(BaseModel):
    status: Optional[str] = None


# Members
class MemberCreate(BaseModel):
    email: str
    password: str


class MemberUpdate(BaseModel):
    is_active: Optional[bool] = None
    max_downloads_per_day: Optional[int] = None


class AccessGrant(BaseModel):
    user_id: int
    job_id: int


# Content
class ContentItemResponse(BaseModel):
    id: int
    job_id: int
    file_name: str
    file_path: str
    file_size: int
    created_at: str


class FolderViewResponse(BaseModel):
    title: str
    slug: str
    files: list[ContentItemResponse]
    total_size: int
    file_count: int


# Download
class SignedUrlResponse(BaseModel):
    url: str
    expires_in: int
    file_name: str


# Settings
class SettingsUpdate(BaseModel):
    telegram_bot_token: Optional[str] = None
    telegram_group_chat_id: Optional[str] = None
    idrive_access_key: Optional[str] = None
    idrive_secret_key: Optional[str] = None
    idrive_endpoint: Optional[str] = None
    idrive_bucket: Optional[str] = None
    idrive_region: Optional[str] = None
    b2_key_id: Optional[str] = None
    b2_app_key: Optional[str] = None
    b2_bucket_name: Optional[str] = None
    portal_base_url: Optional[str] = None
    admin_email: Optional[str] = None
    admin_password: Optional[str] = None


class SettingsResponse(BaseModel):
    telegram_bot_token: str = ""
    telegram_group_chat_id: str = ""
    idrive_access_key: str = ""
    idrive_secret_key: str = ""
    idrive_endpoint: str = ""
    idrive_bucket: str = ""
    idrive_region: str = ""
    b2_key_id: str = ""
    b2_app_key: str = ""
    b2_bucket_name: str = ""
    portal_base_url: str = ""
    admin_email: str = ""
    admin_password: str = ""
