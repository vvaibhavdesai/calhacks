import pytest
from app.storage.local_fs import LocalFilesystemStorage


def test_save_and_read_round_trip(tmp_path):
    storage = LocalFilesystemStorage(str(tmp_path))
    locator = storage.save("sources/doc1.txt", b"hello world")
    assert locator.startswith("local://")
    assert storage.exists("sources/doc1.txt")
    assert storage.read("sources/doc1.txt") == b"hello world"


def test_missing_key_raises(tmp_path):
    storage = LocalFilesystemStorage(str(tmp_path))
    assert not storage.exists("nope.txt")
    with pytest.raises(FileNotFoundError):
        storage.read("nope.txt")


def test_path_traversal_is_rejected(tmp_path):
    storage = LocalFilesystemStorage(str(tmp_path))
    with pytest.raises(ValueError):
        storage.save("../escape.txt", b"nope")
