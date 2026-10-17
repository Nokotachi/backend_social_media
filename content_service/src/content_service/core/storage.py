import asyncio
import io
import json
import logging
import uuid
from pathlib import Path
from typing import Protocol

from fastapi import HTTPException, UploadFile, status
from PIL import Image, UnidentifiedImageError

from content_service.settings import Settings, get_settings

logger = logging.getLogger("content_service.storage")

_ALLOWED_FORMATS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}


async def read_image(upload: UploadFile, *, max_bytes: int) -> tuple[bytes, str, tuple[int, int]]:
    data = await upload.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="ImageTooLarge"
        )
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = img.format
            size = img.size
            img.verify()
    except (UnidentifiedImageError, OSError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="InvalidImage") from exc
    if fmt not in _ALLOWED_FORMATS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="InvalidImage")
    return data, _ALLOWED_FORMATS[fmt], size


_CONTENT_TYPES = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


def new_key(folder: str, ext: str) -> str:
    return f"{folder}/{uuid.uuid4().hex}.{ext}"


class Storage(Protocol):
    async def save(self, *, key: str, data: bytes, ext: str) -> str: ...

    async def delete_by_url(self, url: str) -> None: ...


class LocalStorage:
    def __init__(self, root: str, base_url: str) -> None:
        self._root = Path(root)
        self._base = base_url.rstrip("/") + "/"

    async def save(self, *, key: str, data: bytes, ext: str) -> str:
        path = self._root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return self._base + key

    async def delete_by_url(self, url: str) -> None:
        if url.startswith(self._base):
            (self._root / url[len(self._base) :]).unlink(missing_ok=True)


class S3Storage:
    def __init__(self, settings: Settings) -> None:
        from minio import Minio

        self._client = Minio(
            settings.s3_endpoint,
            access_key=settings.minio_root_user,
            secret_key=settings.minio_root_password,
            secure=settings.s3_secure,
        )
        self._bucket = settings.s3_bucket
        self._base = f"{settings.s3_public_url.rstrip('/')}/{self._bucket}/"

    def ensure_bucket(self) -> None:
        if not self._client.bucket_exists(self._bucket):
            self._client.make_bucket(self._bucket)
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": ["*"]},
                    "Action": ["s3:GetObject"],
                    "Resource": [f"arn:aws:s3:::{self._bucket}/*"],
                }
            ],
        }
        self._client.set_bucket_policy(self._bucket, json.dumps(policy))

    async def save(self, *, key: str, data: bytes, ext: str) -> str:
        await asyncio.to_thread(
            self._client.put_object,
            self._bucket,
            key,
            io.BytesIO(data),
            len(data),
            content_type=_CONTENT_TYPES[ext],
        )
        return self._base + key

    async def delete_by_url(self, url: str) -> None:
        if url.startswith(self._base):
            await asyncio.to_thread(
                self._client.remove_object, self._bucket, url[len(self._base) :]
            )


_s3: S3Storage | None = None


def _storage_for(settings: Settings) -> Storage:
    global _s3
    if settings.storage_backend == "s3":
        if _s3 is None:
            _s3 = S3Storage(settings)
        return _s3
    return LocalStorage(settings.media_root, settings.media_base_url)


def get_storage() -> Storage:
    return _storage_for(get_settings())


async def init_storage(settings: Settings, *, attempts: int = 30) -> None:
    storage = _storage_for(settings)
    if not isinstance(storage, S3Storage):
        return
    for attempt in range(1, attempts + 1):
        try:
            await asyncio.to_thread(storage.ensure_bucket)
            logger.info("Бакет %s готов", settings.s3_bucket)
            return
        except Exception as exc:
            if attempt == attempts:
                logger.error("MinIO недоступен, загрузка картинок работать не будет: %s", exc)
                return
            await asyncio.sleep(1)
