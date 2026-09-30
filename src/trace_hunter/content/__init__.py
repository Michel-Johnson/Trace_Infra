"""Immutable content boundary shared by API, workers and remote adapters."""

from .store import ContentCorruption, ContentLimitExceeded, ContentRef, LocalContentStore

__all__ = ["ContentCorruption", "ContentLimitExceeded", "ContentRef", "LocalContentStore"]
