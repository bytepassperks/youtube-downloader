from pydantic import BaseModel, EmailStr
from typing import Optional
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
    excluded_files: list[str] = []
    storage_target: str = "idrive"  # "idrive" or "b2"


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
