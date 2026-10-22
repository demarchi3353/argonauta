import threading
import time

import pytest

from argonauta.web.jobs import JobConflict, JobRunner


def test_job_runs_and_reports_result():
    runner = JobRunner()
    def work(progress):
        progress("half")
        return {"ok": True}
    job = runner.submit("test", "r_1", work)
    done = runner.wait(job.id)
    assert done.status == "done" and done.result == {"ok": True} and done.progress == "half"
    assert done.to_dict()["id"] == job.id and done.finished_at


def test_job_failure_is_captured_as_message_not_traceback():
    runner = JobRunner()
    def boom(progress):
        raise ValueError("bad input")
    job = runner.wait(runner.submit("test", None, boom).id)
    assert job.status == "failed" and job.error == "bad input" and job.result is None


def test_one_running_job_per_repo():
    runner = JobRunner()
    gate = threading.Event()
    def slow(progress):
        gate.wait(5)
        return {}
    first = runner.submit("a", "r_1", slow)
    with pytest.raises(JobConflict) as exc:
        runner.submit("b", "r_1", slow)
    assert exc.value.job.id == first.id
    other = runner.submit("c", "r_2", slow)  # other repo is fine
    gate.set()
    assert runner.wait(first.id).status == "done" and runner.wait(other.id).status == "done"
    assert runner.running_for("r_1") is None


def test_old_jobs_are_dropped():
    runner = JobRunner(max_jobs=3)
    ids = [runner.wait(runner.submit("t", None, lambda p: {}).id).id for _ in range(5)]
    assert runner.get(ids[0]) is None and runner.get(ids[-1]) is not None
