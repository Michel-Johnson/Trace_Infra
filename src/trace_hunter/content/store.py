"""Content-addressed bytes; publishing metadata is a separate DB transaction."""

import hashlib
import io
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO, Iterator, Protocol


class ContentCorruption(ValueError):
    """A referenced object exists but no longer matches its immutable identity."""


class ContentLimitExceeded(ValueError):
    """The declared operation exceeds its byte budget."""


@dataclass(frozen=True)
class ContentRef:
    digest: str
    size_bytes: int
    media_type: str = "application/octet-stream"

    def __post_init__(self):
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", self.digest):
            raise ValueError("Content digest must be sha256:<64 lowercase hex characters>")
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise ValueError("Content size must be a non-negative integer")
        if not isinstance(self.media_type, str) or not self.media_type.strip():
            raise ValueError("Content media_type is required")

    def as_dict(self):
        return asdict(self)


class ContentStore(Protocol):
    def put(self, source: BinaryIO, *, media_type: str = "application/octet-stream",
            max_bytes: int | None = None) -> ContentRef: ...

    def open_verified(self, ref: ContentRef): ...


class LocalContentStore:
    """Filesystem provider for development and single-host persistent deployments.

    A temp file is fully written, synced and linked without replacing existing
    content. Digest checks detect corruption instead of silently repairing history.
    """

    def __init__(self, directory: Path | str, *, max_object_bytes: int = 256 * 1024 * 1024):
        if type(max_object_bytes) is not int or max_object_bytes < 0:
            raise ValueError("max_object_bytes must be a non-negative integer")
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.max_object_bytes = max_object_bytes

    def _path(self, ref: ContentRef) -> Path:
        digest = ref.digest.removeprefix("sha256:")
        return self.root / "sha256" / digest[:2] / digest[2:]

    @staticmethod
    def _sync_directory(directory: Path):
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def put(self, source: BinaryIO, *, media_type: str = "application/octet-stream",
            max_bytes: int | None = None) -> ContentRef:
        limit = self.max_object_bytes if max_bytes is None else max_bytes
        if type(limit) is not int or not 0 <= limit <= self.max_object_bytes:
            raise ValueError("max_bytes must be within the provider object limit")
        # Validate metadata before allocating or publishing an object.
        ContentRef("sha256:" + "0" * 64, 0, media_type)
        digest, size = hashlib.sha256(), 0
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.root, prefix=".upload-", delete=False) as output:
                temporary = Path(output.name)
                while chunk := source.read(64 * 1024):
                    if not isinstance(chunk, bytes):
                        raise TypeError("Content source must yield bytes")
                    size += len(chunk)
                    if size > limit:
                        raise ContentLimitExceeded("Content exceeds upload byte limit")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            ref = ContentRef("sha256:" + digest.hexdigest(), size, media_type)
            destination = self._path(ref)
            for directory in (destination.parent.parent, destination.parent):
                directory.mkdir(exist_ok=True, mode=0o700)
                self._sync_directory(directory.parent)
            try:
                os.link(temporary, destination)
            except FileExistsError:
                # Concurrent identical uploads are allowed; never overwrite a bad copy.
                with self.open_verified(ref):
                    pass
            else:
                self._sync_directory(destination.parent)
            return ref
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def put_bytes(self, value: bytes, *, media_type: str = "application/octet-stream") -> ContentRef:
        if not isinstance(value, bytes):
            raise TypeError("Content must be bytes")
        return self.put(io.BytesIO(value), media_type=media_type)

    @contextmanager
    def open_verified(self, ref: ContentRef) -> Iterator[BinaryIO]:
        """Verify the same open file descriptor that the caller subsequently reads."""
        with self._path(ref).open("rb") as source:
            digest, size = hashlib.sha256(), 0
            while chunk := source.read(64 * 1024):
                size += len(chunk)
                if size > ref.size_bytes:
                    raise ContentCorruption("Content size does not match its reference")
                digest.update(chunk)
            if size != ref.size_bytes or "sha256:" + digest.hexdigest() != ref.digest:
                raise ContentCorruption("Content does not match its reference")
            source.seek(0)
            yield source

    def read_bytes(self, ref: ContentRef, *, max_bytes: int | None = None) -> bytes:
        limit = self.max_object_bytes if max_bytes is None else max_bytes
        if type(limit) is not int or limit < 0:
            raise ValueError("max_bytes must be a non-negative integer")
        if ref.size_bytes > limit:
            raise ContentLimitExceeded("Content exceeds read byte limit")
        with self.open_verified(ref) as source:
            return source.read()
