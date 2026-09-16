"""Tiny deterministic task for the episode backend.

Not a product demo. It exists so RestoreState / ReplaySpan / Branch can be
tested without the cloned Executable World example tasks.
"""
from __future__ import annotations

from ..engine import Action, Budget, Task


class Counter(Task):
    task_id = "counter"
    brief = "inc / peek / list_items / sample. Used by episode-backend tests."

    def __init__(self, seed: int = 0):
        self._value = 0
        self._cursor = 0
        self._items = [f"id_{seed:02d}_{i:04d}" for i in range(64)]

    def initial_budget(self) -> Budget:
        return Budget({"steps": 20})

    def actions(self) -> dict[str, Action]:
        return {
            "inc": Action(1, self._inc, "add 1 to the counter"),
            "peek": Action(0, self._peek, "read the counter"),
            "list_items": Action(1, self._list_items, "eight items, for summary truncation"),
            "sample": Action(1, self._sample, "next page of ids; advances a cursor"),
            "submit": Action(0, self._submit, "finish"),
        }

    def score(self, submission) -> dict:
        return {"ok": True, "value": self._value, "submission": submission}

    def _inc(self, ep, params):
        ep.budget.spend("steps", 1)
        self._value += 1
        return {"value": self._value}

    def _peek(self, ep, params):
        return {"value": self._value}

    def _list_items(self, ep, params):
        ep.budget.spend("steps", 1)
        return {"items": [f"item_{i}" for i in range(8)]}

    def _sample(self, ep, params):
        ep.budget.spend("steps", 1)
        n = int(params.get("n") or 3)
        start = self._cursor
        chunk = self._items[start:start + n]
        self._cursor += n
        return {"items": chunk}

    def _submit(self, ep, params):
        return ep.finish(params.get("answer"))
