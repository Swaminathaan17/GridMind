"""Cross-platform subprocess lifecycle helpers for benchmark child processes."""
from __future__ import annotations

import subprocess
from typing import Mapping, Sequence


_TERMINATE_GRACE_SECONDS = 2.0


def _terminate_and_reap(proc: subprocess.Popen[str]) -> None:
    """Stop a live child and always wait for it before returning."""
    if proc.poll() is not None:
        proc.wait()
        return

    try:
        proc.terminate()
    except OSError:
        # The child may have exited between poll() and terminate().
        pass

    try:
        proc.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            # The child may have exited between wait() and kill().
            pass
        proc.wait()


def run_captured(
    command: Sequence[str],
    *,
    cwd: str,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a text-mode child, cleaning it up on timeout or interruption."""
    proc = subprocess.Popen(
        list(command),
        cwd=cwd,
        env=dict(env) if env is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_and_reap(proc)
        stdout, stderr = proc.communicate()
        raise subprocess.TimeoutExpired(
            command, timeout, output=stdout or exc.output, stderr=stderr or exc.stderr
        ) from exc
    except BaseException:
        _terminate_and_reap(proc)
        raise

    return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
