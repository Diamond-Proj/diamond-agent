from __future__ import annotations

from typing import Optional, Protocol

from ..schemas import Candidate, TaskSpec


class JobHandle:
    def __init__(self, job_id: str, system: str, partition: str):
        self.job_id = job_id
        self.system = system
        self.partition = partition


class Executor(Protocol):
    """Submits a placed task to a batch system and tracks it."""

    def submit(self, task: TaskSpec, placement: Candidate) -> JobHandle: ...

    def status(self, job_id: str) -> tuple[str, str]:
        """-> (PENDING|RUNNING|COMPLETED|FAILED|CANCELLED|UNKNOWN, note)"""
        ...

    def cancel(self, job_id: str) -> bool: ...


def walltime_hms(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}:00"


def render_note(note: Optional[str]) -> str:
    return note or ""
