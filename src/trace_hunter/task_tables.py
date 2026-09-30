"""Explicit repository-owned table bindings for the two legacy task stores."""
from dataclasses import dataclass


@dataclass(frozen=True)
class TaskTables:
    plugins: str
    batches: str
    jobs: str
    attempts: str
    results: str


EVALUATION_TABLES = TaskTables('plugin_versions', 'evaluation_batches', 'evaluation_jobs',
                              'evaluation_attempts', 'evaluation_results')
CONTRIBUTION_TABLES = TaskTables('plugin_handlers', 'plugin_batches', 'plugin_invocations',
                                'plugin_attempts', 'plugin_artifacts')
