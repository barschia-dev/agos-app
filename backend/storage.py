"""Receipt files, stored in MongoDB (uploads are capped below its 16 MB document limit)."""

from bson import Binary

from core import APP_NAME, db


async def put_object(path: str, data: bytes, content_type: str) -> dict:
    await db.file_blobs.replace_one(
        {"_id": path}, {"_id": path, "data": Binary(data), "content_type": content_type}, upsert=True
    )
    return {"path": path, "size": len(data)}


async def get_object(path: str) -> tuple[bytes, str]:
    doc = await db.file_blobs.find_one({"_id": path})
    if doc is None:
        raise FileNotFoundError(path)
    return bytes(doc["data"]), doc["content_type"]
