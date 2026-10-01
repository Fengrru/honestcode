"""
Sandboxed code execution for HonestCode.

Executes code in a fresh Python subprocess with resource limits:
  - POSIX: RLIMIT_AS (memory), RLIMIT_CPU (cpu time), RLIMIT_NOFILE (open files)
  - Windows: Job Object with process-memory and job CPU-time limits, plus
    kill-on-close, applied via ctypes (best effort; silently skipped when the
    Job Object API is unavailable)
  - Both: wall-clock timeout, isolated temp directory, restricted PYTHONPATH

Threat model — read this before trusting it: the sandbox is a guardrail
against *accidents* (infinite loops, fork bombs, runaway memory, typo'd
paths), not a security boundary. The child runs as the current user with full
file-system and network access, and environment scrubbing is name-based
guesswork. Do not execute code from untrusted authors inside it; use a real
isolation layer (container, VM) for that.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["ExecutionResult", "SandboxExecutor"]

# Environment variable names containing these markers are scrubbed before
# the sandboxed subprocess starts. This is best-effort name guessing: it
# catches the common credential names but cannot know every scheme a secret
# might hide under (see the threat model above).
_SENSITIVE_ENV_MARKERS = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "AUTH",
    "SIGNATURE",
    "DATABASE",
    "DSN",
)


@dataclass
class ExecutionResult:
    success: bool
    stdout: str = ""
    stderr: str = ""
    output: str = ""
    error: str = ""
    error_type: str = ""
    runtime_seconds: float = 0.0
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "output": self.output or self.stdout,
            "error": self.error or self.stderr,
            "error_type": self.error_type,
            "runtime_seconds": self.runtime_seconds,
            **self.details,
        }


class SandboxExecutor:
    """Execute Python code snippets in a restricted subprocess."""

    def __init__(self, timeout: int | None = None, memory_mb: int | None = None):
        # Environment overrides (HONESTCODE_TIMEOUT / HONESTCODE_MEMORY_MB)
        # apply when the caller does not pass explicit values.
        env_timeout = os.environ.get("HONESTCODE_TIMEOUT", "")
        self.timeout = (
            timeout if timeout is not None else int(env_timeout) if env_timeout.isdigit() else 10
        )
        env_memory = os.environ.get("HONESTCODE_MEMORY_MB", "")
        self.memory_mb = (
            memory_mb if memory_mb is not None else int(env_memory) if env_memory.isdigit() else 256
        )

    def execute(
        self,
        code: str,
        prelude: str = "",
        known_names: set[str] | None = None,
    ) -> ExecutionResult:
        """Run *code* in a fresh Python interpreter and return the result."""
        known_names = known_names or set()
        start = time.perf_counter()

        with tempfile.TemporaryDirectory(prefix="honestcode_sandbox_") as tmpdir:
            script_path = Path(tmpdir) / "script.py"
            full_code = f"{prelude}\n{code}\n"
            script_path.write_text(full_code, encoding="utf-8")

            env = {
                k: v
                for k, v in os.environ.items()
                if not any(marker in k.upper() for marker in _SENSITIVE_ENV_MARKERS)
            }
            env["PYTHONPATH"] = ""
            env["PYTHONDONTWRITEBYTECODE"] = "1"

            preexec = self._get_preexec_fn()
            creationflags = self._get_creationflags()

            try:
                proc = subprocess.Popen(
                    [sys.executable, "-u", str(script_path)],
                    cwd=tmpdir,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    preexec_fn=preexec,
                    creationflags=creationflags,
                )
            except Exception as e:  # noqa: BLE001
                return ExecutionResult(
                    success=False,
                    error=f"Sandbox failed: {e}",
                    runtime_seconds=round(time.perf_counter() - start, 3),
                )

            # Windows: bind the child to a Job Object *after* spawn (the
            # POSIX path applies limits pre-exec instead).
            job_handle = self._apply_windows_job(proc)
            try:
                try:
                    stdout, stderr = proc.communicate(timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    stdout, stderr = proc.communicate()
                    return ExecutionResult(
                        success=False,
                        error=f"Execution timed out after {self.timeout}s",
                        stderr=(stderr or "")[:50_000],
                        runtime_seconds=round(time.perf_counter() - start, 3),
                        details={"timeout": True},
                    )
            finally:
                self._release_windows_job(job_handle)

            runtime = time.perf_counter() - start
            stdout = stdout or ""
            stderr = stderr or ""
            success = proc.returncode == 0
            err_text = stderr[:50_000] if not success else ""
            error_type = ""
            if not success and err_text:
                m = re.search(r"^(\w+Error|\w+Exception)", err_text, re.MULTILINE)
                if m:
                    error_type = m.group(1)
            return ExecutionResult(
                success=success,
                stdout=stdout[:50_000],
                stderr=stderr[:50_000],
                output=stdout[:50_000],
                error=err_text,
                error_type=error_type,
                runtime_seconds=round(runtime, 3),
                details={"returncode": proc.returncode, "known_names": sorted(known_names)},
            )

    def _get_preexec_fn(self):
        """Return a POSIX preexec_fn that sets resource limits, if available.

        Each limit is applied best-effort: some platforms (e.g. macOS CI
        runners) reject certain ``setrlimit`` calls, and a single failure
        inside ``preexec_fn`` kills the child before it can run at all.
        """
        if sys.platform == "win32":
            return None
        try:
            import resource
        except Exception:  # noqa: BLE001
            return None

        def limit_resources():
            def _setrlimit(name, soft, hard):
                try:
                    resource.setrlimit(getattr(resource, name), (soft, hard))
                except (ValueError, OSError):
                    logger.warning("Could not set %s in sandbox: %s", name, sys.exc_info()[1])

            # Memory limit
            max_bytes = self.memory_mb * 1024 * 1024
            _setrlimit("RLIMIT_AS", max_bytes, max_bytes)
            # CPU time limit (soft = timeout, hard = timeout + 2s buffer)
            _setrlimit("RLIMIT_CPU", self.timeout, self.timeout + 2)
            # Limit open files to prevent fd exhaustion
            _setrlimit("RLIMIT_NOFILE", 64, 64)

        return limit_resources

    def _get_creationflags(self) -> int:
        """Return Windows-specific creation flags for process isolation."""
        if sys.platform != "win32":
            return 0
        # CREATE_NO_WINDOW: don't open a console window
        # BELOW_NORMAL_PRIORITY_CLASS: reduce scheduling priority
        return 0x08000000 | 0x00008000

    def _apply_windows_job(self, proc: subprocess.Popen) -> int | None:
        """Assign *proc* to a Job Object with memory/CPU limits (Windows only).

        Best effort by design: returns the job handle (to be released by
        :meth:`_release_windows_job`), or ``None`` when anything about the
        platform or API call refuses to cooperate — the wall-clock timeout
        still applies in that case. ``KILL_ON_JOB_CLOSE`` also ties the
        child's life to this process, so no orphan survives a crash.
        """
        if sys.platform != "win32":
            return None
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32

            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [
                    (name, ctypes.c_uint64)
                    for name in (
                        "ReadOperationCount",
                        "WriteOperationCount",
                        "OtherOperationCount",
                        "ReadTransferCount",
                        "WriteTransferCount",
                        "OtherTransferCount",
                    )
                ]

            class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            JobObjectExtendedLimitInformation = 9
            JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x0100
            JOB_OBJECT_LIMIT_JOB_TIME = 0x0200
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                return None
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = (
                JOB_OBJECT_LIMIT_PROCESS_MEMORY
                | JOB_OBJECT_LIMIT_JOB_TIME
                | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            info.ProcessMemoryLimit = self.memory_mb * 1024 * 1024
            # Job user CPU time is expressed in 100ns units.
            info.BasicLimitInformation.PerJobUserTimeLimit = int(self.timeout) * 10_000_000
            if not kernel32.SetInformationJobObject(
                job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
            ):
                kernel32.CloseHandle(job)
                return None
            if not kernel32.AssignProcessToJobObject(job, int(proc._handle)):
                kernel32.CloseHandle(job)
                return None
            return job
        except Exception as e:  # noqa: BLE001
            logger.debug("Windows Job Object unavailable: %s", e)
            return None

    def _release_windows_job(self, job_handle: int | None) -> None:
        """Close the Job Object handle once the child has been reaped.

        Closing is what triggers ``KILL_ON_JOB_CLOSE``; by the time this runs
        the child has exited or been killed, so the job object is empty.
        """
        if not job_handle:
            return
        try:
            import ctypes

            ctypes.windll.kernel32.CloseHandle(job_handle)
        except Exception as e:  # noqa: BLE001
            logger.debug("Could not close job handle: %s", e)

    def validate_snippet(self, code: str, known_names: set[str]) -> ExecutionResult:
        """Quickly run a snippet to see if it raises a NameError for unknown symbols."""
        return self.execute(code=code, known_names=known_names)
