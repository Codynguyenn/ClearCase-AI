"""GridFS-backed binary storage for original and exported PDFs."""

from __future__ import annotations

from typing import Any

from bson import ObjectId
from gridfs import GridFSBucket, NoFile

from app.services.errors import NotFoundError


class FileStorage:
    def __init__(self, bucket: GridFSBucket) -> None:
        self._bucket = bucket

    def put(self, data: bytes, *, filename: str, metadata: dict[str, Any]) -> ObjectId:
        return self._bucket.upload_from_stream(filename, data, metadata=metadata)

    def get_bytes(self, file_id: ObjectId, *, owner_id: str) -> bytes:
        stream = self._open(file_id, owner_id=owner_id)
        try:
            return stream.read()
        finally:
            stream.close()

    def open_stream(self, file_id: ObjectId, *, owner_id: str):
        """Return a readable GridFS stream; the caller must close it."""
        return self._open(file_id, owner_id=owner_id)

    def delete(self, file_id: ObjectId) -> bool:
        try:
            self._bucket.delete(file_id)
            return True
        except NoFile:
            return False

    def _open(self, file_id: ObjectId, *, owner_id: str):
        try:
            stream = self._bucket.open_download_stream(file_id)
        except NoFile as exc:
            raise NotFoundError("File not found.") from exc
        # GridFS files carry our metadata; refuse to serve another owner's bytes
        # even if someone guesses a valid file id.
        meta = stream.metadata or {}
        if meta.get("owner_id") != owner_id:
            stream.close()
            raise NotFoundError("File not found.")
        return stream
