"""Episode-backend tasks. One tiny counter; firecracker does not use this."""
from .counter import Counter

TASKS = {
    "counter": Counter,
}


def load_task(task_id: str, seed: int = 0):
    """Build a task by id. Same id and seed always give the same task."""
    try:
        cls = TASKS[task_id]
    except KeyError:
        raise KeyError(
            f"no task {task_id!r}; available: {', '.join(sorted(TASKS))}") from None
    return cls(seed=seed)


__all__ = ["TASKS", "load_task", "Counter"]
