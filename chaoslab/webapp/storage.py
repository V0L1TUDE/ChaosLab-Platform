"""
Pluggable storage for uploaded application files
================================================
`LocalStorage` (default) writes under CHAOSLAB_UPLOAD_DIR. `S3Storage` writes to a private S3 bucket
using the standard boto3 credential chain (IAM role, env vars, profile). The DB records which backend
holds each file (`applications.storage_backend`) and an opaque reference (`file_path`: a local path or an
S3 object key), so switching backends never orphans old files.

Contract: `put(key, data)` returns the reference to store; `delete(ref)` is idempotent.
Files are only ever stored and deleted here; they are never read back, extracted or executed.
"""

import os
from typing import Any, Dict, Optional

from . import config


class StorageError(Exception):
    """The storage backend failed (network, permissions, misconfiguration)."""


class Storage:
    name = ""

    def put(self, key: str, data: bytes) -> str:
        raise NotImplementedError

    def delete(self, ref: str) -> None:
        raise NotImplementedError

    def check(self) -> Optional[str]:
        """Return None if healthy, else a short human-readable problem (never raises)."""
        return None


class LocalStorage(Storage):
    name = "local"

    def put(self, key: str, data: bytes) -> str:
        root = os.path.realpath(config.UPLOAD_DIR)
        path = os.path.realpath(os.path.join(root, *key.split("/")))
        if os.path.commonpath([root, path]) != root:
            raise StorageError("Invalid storage key.")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
        except OSError as e:
            raise StorageError(f"Local storage write failed: {e.strerror or e}")
        return path

    def delete(self, ref: str) -> None:
        if not ref:
            return
        root = os.path.realpath(config.UPLOAD_DIR)
        real = os.path.realpath(ref)
        if os.path.commonpath([root, real]) == root and os.path.isfile(real):   # never delete outside the upload dir
            try:
                os.remove(real)
            except OSError as e:
                raise StorageError(f"Local storage delete failed: {e.strerror or e}")

    def check(self) -> Optional[str]:
        return None if os.access(config.UPLOAD_DIR, os.W_OK) else f"Upload directory not writable: {config.UPLOAD_DIR}"


class S3Storage(Storage):
    name = "s3"

    def __init__(self, client: Any = None):
        if not config.S3_BUCKET:
            raise StorageError("CHAOSLAB_S3_BUCKET is required when CHAOSLAB_STORAGE=s3.")
        if client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError:
                raise StorageError("S3 storage needs boto3: pip install boto3")
            kwargs: Dict[str, Any] = {"config": Config(retries={"max_attempts": 3, "mode": "standard"},
                                                        connect_timeout=5, read_timeout=30)}
            if config.S3_REGION:
                kwargs["region_name"] = config.S3_REGION
            if config.S3_ENDPOINT_URL:                    # MinIO / LocalStack / S3-compatible stores
                kwargs["endpoint_url"] = config.S3_ENDPOINT_URL
            client = boto3.client("s3", **kwargs)
        self.client = client
        self.bucket = config.S3_BUCKET

    def _full_key(self, key: str) -> str:
        return f"{config.S3_PREFIX}{key}"

    def put(self, key: str, data: bytes) -> str:
        full = self._full_key(key)
        args: Dict[str, Any] = {
            "Bucket": self.bucket, "Key": full, "Body": data,
            # Never trust or reflect the uploaded type; the bucket must not serve these as web content.
            "ContentType": "application/octet-stream",
        }
        if config.S3_SSE != "none":
            args["ServerSideEncryption"] = config.S3_SSE
            if config.S3_SSE == "aws:kms" and config.S3_KMS_KEY_ID:
                args["SSEKMSKeyId"] = config.S3_KMS_KEY_ID
        try:
            self.client.put_object(**args)
        except Exception as e:
            raise StorageError(f"S3 upload failed: {_describe(e)}")
        return full

    def delete(self, ref: str) -> None:
        if not ref:
            return
        # Only keys under our prefix may be deleted by this backend.
        if not ref.startswith(config.S3_PREFIX):
            raise StorageError("Refusing to delete an object outside the configured prefix.")
        try:
            self.client.delete_object(Bucket=self.bucket, Key=ref)   # idempotent: missing keys succeed
        except Exception as e:
            raise StorageError(f"S3 delete failed: {_describe(e)}")

    def check(self) -> Optional[str]:
        try:
            self.client.head_bucket(Bucket=self.bucket)
            return None
        except Exception as e:
            return f"S3 bucket '{self.bucket}' not reachable: {_describe(e)}"


def _describe(e: Exception) -> str:
    """Short, secret-free error text (botocore ClientError code if present)."""
    code = getattr(e, "response", {}).get("Error", {}).get("Code") if hasattr(e, "response") else None
    return code or type(e).__name__


_instances: Dict[str, Storage] = {}


def get_storage(name: Optional[str] = None) -> Storage:
    """Backend by name ('local'/'s3'); default is the configured one. Instances are cached."""
    name = name or config.STORAGE_BACKEND
    if name not in ("local", "s3"):
        raise StorageError(f"Unknown storage backend '{name}'.")
    if name not in _instances:
        _instances[name] = LocalStorage() if name == "local" else S3Storage()
    return _instances[name]


def reset_cache() -> None:
    _instances.clear()
