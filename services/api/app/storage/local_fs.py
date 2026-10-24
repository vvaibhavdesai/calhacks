"""Local-filesystem DocumentStorage adapter for dev/CI.

Keys are sanitized to stay within `root` -- no path traversal via `..` or an
absolute path. This is the only document-storage backend implemented in A01;
see docs/adr/0001-runtime.md for why a real object store is deferred.
"""

from __future__ import annotations

from pathlib import Path

from app.storage.base import DocumentStorage


class LocalFilesystemStorage(DocumentStorage):
    def __init__(self, root: str) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        # Accept the locator `save()` returns ("local://<key>") as well as a
        # bare key, so a stored SourceVersion.storage_locator reads back.
        key = key.removeprefix("local://")
        candidate = (self._root / key).resolve()
        if self._root not in candidate.parents and candidate != self._root:
            raise ValueError(f"key escapes storage root: {key!r}")
        return candidate

    def save(self, key: str, data: bytes) -> str:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return f"local://{path.relative_to(self._root)}"

    def read(self, key: str) -> bytes:
        path = self._resolve(key)
        if not path.is_file():
            raise FileNotFoundError(key)
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        try:
            return self._resolve(key).is_file()
        except ValueError:
            return False


def get_document_storage() -> DocumentStorage:
    from app.config import get_settings

    settings = get_settings()
    if settings.document_storage_backend != "local":
        raise NotImplementedError(
            f"document storage backend {settings.document_storage_backend!r} "
            "is not implemented; only 'local' exists in A01"
        )
    return LocalFilesystemStorage(settings.document_storage_root)
