"""Trusted worker hosts share the core invocation and artifact protocol."""

from .official import OfficialWorker
from .registry import WorkerRegistry

__all__ = ['OfficialWorker', 'WorkerRegistry']
