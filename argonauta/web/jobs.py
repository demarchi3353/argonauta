"""Background jobs for long operations (index, propose, tests, fetch, pull, push).

One thread per job, one running job per repository, results polled over HTTP.
"""

from __future__ import annotations

import secrets
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from argonauta.logging_setup import get_logger
from argonauta.models import utcnow

log = get_logger()
JobFn = Callable[[Callable[[str], None]], dict]


@dataclass
class Job:
    id: str
    kind: str
    repo_id: str | None
    status: str = "queued"
    progress: str = ""
    result: dict | None = None
    error: str | None = None
    created_at: str = ""
    finished_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "repo_id": self.repo_id, "status": self.status,
            "progress": self.progress, "result": self.result, "error": self.error,
            "created_at": self.created_at, "finished_at": self.finished_at,
        }


class JobConflict(RuntimeError):
    def __init__(self, job: Job):
        super().__init__(f"a {job.kind} job is already running for this repository")
        self.job = job


class JobRunner:
    def __init__(self, max_jobs: int = 200):
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._max = max_jobs

    def running_for(self, repo_id: str | None) -> Job | None:
        if repo_id is None:
            return None
        with self._lock:
            for job in self._jobs.values():
                if job.repo_id == repo_id and job.status in ("queued", "running"):
                    return job
        return None

    def submit(self, kind: str, repo_id: str | None, fn: JobFn) -> Job:
        with self._lock:
            for job in self._jobs.values():
                if repo_id is not None and job.repo_id == repo_id and job.status in ("queued", "running"):
                    raise JobConflict(job)
            job = Job(id="j_" + secrets.token_hex(6), kind=kind, repo_id=repo_id, created_at=utcnow())
            self._jobs[job.id] = job
            while len(self._jobs) > self._max:
                oldest_id, oldest = next(iter(self._jobs.items()))
                if oldest.status in ("queued", "running"):
                    break
                self._jobs.popitem(last=False)
                self._threads.pop(oldest_id, None)
        thread = threading.Thread(target=self._run, args=(job, fn), name=f"job-{job.kind}", daemon=True)
        self._threads[job.id] = thread
        thread.start()
        return job

    def _run(self, job: Job, fn: JobFn) -> None:
        job.status = "running"

        def progress(text: str) -> None:
            job.progress = str(text)[:200]

        try:
            result = fn(progress)
            job.result = result if isinstance(result, dict) else {}
            job.status = "done"
        except Exception as exc:  # noqa: BLE001 - every failure becomes a message for the UI
            job.error = str(exc) or exc.__class__.__name__
            job.status = "failed"
            log.warning("job %s %s failed", job.id, job.kind)
        finally:
            job.finished_at = utcnow()

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def wait(self, job_id: str, timeout: float = 30) -> Job:
        thread = self._threads.get(job_id)
        if thread is not None:
            thread.join(timeout)
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def shutdown(self) -> None:
        for thread in list(self._threads.values()):
            thread.join(0.1)
