"""Pluggable document-storage interface.

A01 only needs a working local adapter for dev (docs/adr/0001-runtime.md).
A real object store (S3-compatible, etc.) is a later swap behind this same
interface, not a redesign -- callers must depend on DocumentStorage, never
on LocalFilesystemStorage directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class DocumentStorage(ABC):
    @abstractmethod
    def save(self, key: str, data: bytes) -> str:
        """Persist `data` under `key`. Returns an opaque locator string."""

    @abstractmethod
    def read(self, key: str) -> bytes:
        """Raise FileNotFoundError if `key` does not exist."""

    @abstractmethod
    def exists(self, key: str) -> bool: ...
