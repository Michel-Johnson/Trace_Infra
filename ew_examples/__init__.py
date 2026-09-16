"""Trace infra: record a sandbox, freeze it, restore it, optionally replay acts.

Standard library only. Live Firecracker needs KVM on the test machine.
"""
from .engine import Action, ActionError, Budget, Episode, Task
from .tasks import TASKS, load_task
from .store import LocalStore, TraceError
from .trace import Runtime, new_id
from .fc import FirecrackerConfig
from .layers import LayeredDisk
from .uffd import UffdHandler, file_load_body, snapshot_load_body
from .forkd import ParentImage, map_private
from .analytics import ClickHouse
from .httpapi import serve

__all__ = ["Action", "ActionError", "Budget", "Episode", "Task", "TASKS",
           "load_task", "LocalStore", "Runtime", "TraceError", "new_id",
           "FirecrackerConfig", "LayeredDisk", "UffdHandler",
           "snapshot_load_body", "file_load_body",
           "ParentImage", "map_private", "ClickHouse", "serve"]
