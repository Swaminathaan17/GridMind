"""Tests for benchmark subprocess cleanup and output capture."""
from __future__ import annotations

import subprocess

import pytest

from benchmarks import process


class FakeProcess:
    def __init__(self, *, communicate_error=None, returncode=0):
        self.communicate_error = communicate_error
        self.returncode = returncode
        self.terminated = False
        self.killed = False
        self.wait_calls = []
        self.communicate_calls = 0

    def communicate(self, timeout=None):
        self.communicate_calls += 1
        if self.communicate_error is not None and self.communicate_calls == 1:
            raise self.communicate_error
        return "captured stdout", "captured stderr"

    def poll(self):
        return self.returncode if self.returncode is not None else None

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.returncode is None:
            self.returncode = -9
        return self.returncode


def test_run_captured_returns_completed_process_and_output(monkeypatch):
    child = FakeProcess()
    monkeypatch.setattr(process.subprocess, "Popen", lambda *args, **kwargs: child)

    result = process.run_captured(["worker"], cwd=".")

    assert result.returncode == 0
    assert result.stdout == "captured stdout"
    assert result.stderr == "captured stderr"
    assert child.terminated is False
    assert child.wait_calls == []


def test_run_captured_terminates_and_reaps_child_on_keyboard_interrupt(monkeypatch):
    child = FakeProcess(communicate_error=KeyboardInterrupt())
    monkeypatch.setattr(process.subprocess, "Popen", lambda *args, **kwargs: child)

    with pytest.raises(KeyboardInterrupt):
        process.run_captured(["worker"], cwd=".")

    assert child.terminated is True
    assert child.killed is False
    assert child.wait_calls == [process._TERMINATE_GRACE_SECONDS]


def test_run_captured_kills_child_when_timeout_expires(monkeypatch):
    child = FakeProcess(
        communicate_error=subprocess.TimeoutExpired(["worker"], 0.01)
    )
    monkeypatch.setattr(process.subprocess, "Popen", lambda *args, **kwargs: child)

    with pytest.raises(subprocess.TimeoutExpired):
        process.run_captured(["worker"], cwd=".", timeout=0.01)

    assert child.terminated is True
    assert child.wait_calls == [process._TERMINATE_GRACE_SECONDS]
    assert child.communicate_calls == 2


def test_run_captured_kills_child_if_terminate_does_not_finish(monkeypatch):
    class SlowToStop(FakeProcess):
        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            if timeout is not None:
                raise subprocess.TimeoutExpired(["worker"], timeout)
            self.returncode = -9
            return self.returncode

    child = SlowToStop(communicate_error=KeyboardInterrupt())
    monkeypatch.setattr(process.subprocess, "Popen", lambda *args, **kwargs: child)

    with pytest.raises(KeyboardInterrupt):
        process.run_captured(["worker"], cwd=".")

    assert child.terminated is True
    assert child.killed is True
    assert child.wait_calls == [process._TERMINATE_GRACE_SECONDS, None]
