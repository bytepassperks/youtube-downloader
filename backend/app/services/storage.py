import os
import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config as BotoConfig
from b2sdk.v2 import InMemoryAccountInfo, B2Api

from app.config import settings

# Multipart upload config for faster large file uploads
MULTIPART_CONFIG = TransferConfig(
    multipart_threshold=8 * 1024 * 1024,  # 8MB threshold
    max_concurrency=10,  # 10 parallel parts
    multipart_chunksize=8 * 1024 * 1024,  # 8MB chunks
)


class IDriveStorage:
    """iDrive E2 S3-compatible storage."""

    def __init__(self):
        endpoint = settings.IDRIVE_ENDPOINT
        if not endpoint.startswith("http"):
            endpoint = f"https://{endpoint}"
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=settings.IDRIVE_ACCESS_KEY,
            aws_secret_access_key=settings.IDRIVE_SECRET_KEY,
            region_name=settings.IDRIVE_REGION,
            config=BotoConfig(signature_version="s3v4"),
        )
        self.bucket = settings.IDRIVE_BUCKET

    def upload_file(self, local_path: str, remote_key: str) -> str:
        self.client.upload_file(
            local_path, self.bucket, remote_key,
            Config=MULTIPART_CONFIG,
        )
        return remote_key

    # --- Streaming multipart upload (zero disk) ---

    def create_multipart_upload(self, remote_key: str) -> str:
        """Start a multipart upload and return the upload ID."""
        resp = self.client.create_multipart_upload(
            Bucket=self.bucket, Key=remote_key,
        )
        return resp["UploadId"]

    def upload_part(self, remote_key: str, upload_id: str,
                    part_number: int, data: bytes) -> str:
        """Upload a single part and return its ETag."""
        resp = self.client.upload_part(
            Bucket=self.bucket, Key=remote_key,
            UploadId=upload_id, PartNumber=part_number,
            Body=data,
        )
        return resp["ETag"]

    def complete_multipart_upload(self, remote_key: str, upload_id: str,
                                  parts: list[dict]) -> str:
        """Finish a multipart upload.  *parts* is a list of
        ``{"PartNumber": int, "ETag": str}`` dicts."""
        self.client.complete_multipart_upload(
            Bucket=self.bucket, Key=remote_key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
        return remote_key

    def abort_multipart_upload(self, remote_key: str, upload_id: str):
        """Cancel a multipart upload and discard uploaded parts."""
        try:
            self.client.abort_multipart_upload(
                Bucket=self.bucket, Key=remote_key, UploadId=upload_id,
            )
        except Exception:
            pass

    def upload_fileobj(self, fileobj, remote_key: str) -> str:
        """Upload from a file-like object (supports streaming)."""
        self.client.upload_fileobj(
            fileobj, self.bucket, remote_key,
            Config=MULTIPART_CONFIG,
        )
        return remote_key

    def generate_signed_url(self, remote_key: str, expiry: int = None) -> str:
        if expiry is None:
            expiry = settings.SIGNED_URL_EXPIRY
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": remote_key},
            ExpiresIn=expiry,
        )

    def delete_file(self, remote_key: str):
        self.client.delete_object(Bucket=self.bucket, Key=remote_key)

    def list_files(self, prefix: str) -> list[dict]:
        result = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                result.append({
                    "key": obj["Key"],
                    "size": obj["Size"],
                    "last_modified": obj["LastModified"].isoformat(),
                })
        return result


class B2Storage:
    """Backblaze B2 storage."""

    def __init__(self):
        info = InMemoryAccountInfo()
        self.api = B2Api(info)
        self.api.authorize_account("production", settings.B2_KEY_ID, settings.B2_APP_KEY)
        self.bucket = self.api.get_bucket_by_name(settings.B2_BUCKET_NAME)

    def upload_file(self, local_path: str, remote_key: str) -> str:
        self.bucket.upload_local_file(
            local_file=local_path,
            file_name=remote_key,
        )
        return remote_key

    def generate_signed_url(self, remote_key: str, expiry: int = None) -> str:
        if expiry is None:
            expiry = settings.SIGNED_URL_EXPIRY
        download_url = self.api.get_download_url_for_fileid(
            self.bucket.get_file_info_by_name(remote_key).id_
        )
        # For B2, use authorization token
        auth_token = self.bucket.get_download_authorization(
            file_name_prefix=remote_key, valid_duration_in_seconds=expiry
        )
        return f"{download_url}?Authorization={auth_token}"

    def get_public_url(self, remote_key: str) -> str:
        download_url = self.api.account_info.get_download_url()
        return f"{download_url}/file/{settings.B2_BUCKET_NAME}/{remote_key}"

    def delete_file(self, remote_key: str):
        file_version = self.bucket.get_file_info_by_name(remote_key)
        self.api.delete_file_version(file_version.id_, file_version.file_name)

    def list_files(self, prefix: str) -> list[dict]:
        result = []
        for file_info, _ in self.bucket.ls(folder_to_list=prefix, recursive=True):
            result.append({
                "key": file_info.file_name,
                "size": file_info.size,
            })
        return result


def get_storage(target: str = "idrive"):
    if target == "b2":
        return B2Storage()
    return IDriveStorage()
