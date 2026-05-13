from __future__ import annotations
import os
import httpx
from shared.logger import get_logger

logger = get_logger("storage")

BUCKET = "menu-images"


def _supabase_url() -> str:
    return os.getenv("SUPABASE_URL", "")


def _service_key() -> str:
    return os.getenv("SUPABASE_SERVICE_KEY", "")


async def upload_menu_image(image_bytes: bytes, item_id: str, mime_type: str = "image/jpeg") -> str | None:
    """
    Upload image bytes to Supabase Storage bucket 'menu-images'.
    Returns the public URL or None if upload fails or storage is not configured.
    """
    base_url = _supabase_url()
    key = _service_key()
    if not base_url or not key:
        logger.warning("supabase_storage_not_configured")
        return None

    ext = "jpg" if "jpeg" in mime_type else mime_type.split("/")[-1]
    path = f"{item_id}.{ext}"
    upload_url = f"{base_url}/storage/v1/object/{BUCKET}/{path}"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                upload_url,
                content=image_bytes,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": mime_type,
                    "x-upsert": "true",
                },
            )
            if resp.status_code in (200, 201):
                return f"{base_url}/storage/v1/object/public/{BUCKET}/{path}"
            logger.error("storage_upload_failed", status=resp.status_code, body=resp.text[:200])
            return None
    except Exception as exc:
        logger.error("storage_upload_exception", error=str(exc))
        return None
