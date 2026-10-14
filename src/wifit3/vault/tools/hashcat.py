import logging
import os
import shlex
import subprocess
import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Dict, Optional

from wifit3.models import PersistedCapture, ToolCapability, ToolResult, ToolStatus
from wifit3.models.jobs import JobAction, JobState
from wifit3.vault.tools.base import VaultTool


logger = logging.getLogger(__name__)

_STILL_ACTIVE = 259                             # GetExitCodeProcess: process has not exited
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_LOG_TAIL_BYTES = 256 * 1024


def _read_log_tail(path, limit: int = _LOG_TAIL_BYTES, start: int = 0) -> list[str]:
    """Up to ``limit`` bytes of whole lines from the end of the log, never reading before
    ``start`` (where the running process began writing)."""
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            begin = max(start, size - limit)
            if begin > start:
                f.seek(begin - 1)
                starts_midline = f.read(1) != b"\n"
            else:
                starts_midline = False
            f.seek(begin)
            data = f.read()
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if starts_midline and lines:
        lines = lines[1:]
    return lines


def _split_args(raw: str) -> list[str]:
    # posix=False to retain Windows backslashes. Strip unnecessary quotes.
    return [token.strip('"') for token in shlex.split(raw, posix=False)]


def expand_wordlists(requested: list[str]) -> list[Path]:
    """One path per hashcat run: a requested directory contributes the files directly inside it."""
    expanded: list[Path] = []
    for raw in requested:
        path = Path(raw).expanduser()
        if path.is_dir():
            expanded.extend(sorted(child.resolve() for child in path.iterdir() if child.is_file()))
        elif path.is_file():
            expanded.append(path.resolve())
    return expanded


@dataclass
class HashcatQueue:
    """A job's wordlists and which one its process is on. hashcat's own multi-dictionary
    queue is driven by TTY keypresses, which a windowless child cannot receive on Windows,
    so wifit3 runs one hashcat per wordlist and advances the queue itself."""
    wordlists: list[str] = field(default_factory=list)
    index: int = 0
    log_offset: int = 0         # where the running process started writing into the job's log
    stopped: bool = False

    @classmethod
    def from_job(cls, job: JobState) -> "HashcatQueue":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in job.tool_state.items() if k in known})

    def write_to(self, job: JobState) -> None:
        job.tool_state.update(asdict(self))

    @property
    def current(self) -> Optional[str]:
        return self.wordlists[self.index] if 0 <= self.index < len(self.wordlists) else None

    @property
    def has_next(self) -> bool:
        return self.index + 1 < len(self.wordlists)

    @property
    def label(self) -> str:
        """``rockyou.txt (3/16)``; a lone wordlist carries no counter."""
        name = Path(self.current).name if self.current else ""
        return f"{name} ({self.index + 1}/{len(self.wordlists)})" if len(self.wordlists) > 1 else name


class HashcatTool(VaultTool):
    name = "hashcat"
    description = "Advanced password recovery (WPA/WPA2/PMKID)"
    # ADOPTABLE: hashcat is left running when wifit3 exits and re-attached on the next launch, so a
    # long crack survives closing the app. KILLABLE is the user's explicit Stop, not shutdown.
    capabilities = ToolCapability.KILLABLE | ToolCapability.SINGLETON | ToolCapability.ADOPTABLE

    def __init__(self) -> None:
        self._procs: Dict[int, subprocess.Popen] = {}

    def can_crack(self, capture: PersistedCapture) -> bool:
        return capture.path.endswith(".hc22000")

    def launch(self, job: JobState, capture: PersistedCapture) -> None:
        hashcat_exe = job.config.get("hashcat_exe")
        requested = job.config.get("wordlists") or []
        if not hashcat_exe or not requested:
            logger.error(f"Missing hashcat_exe {hashcat_exe!r} or wordlists {requested!r} in config")
            raise ValueError("Missing hashcat_exe or wordlists in config")
        if not Path(hashcat_exe).is_file():
            raise ValueError(f"hashcat executable not found: {hashcat_exe}")
        wordlists = expand_wordlists(requested)
        if not wordlists:
            raise ValueError(f"Wordlist not found: {', '.join(requested)}")

        potfile = self._potfile(job.capture_path)
        if potfile and os.path.exists(potfile):
            try:
                logger.info(f"Removing old potfile {potfile}")
                os.remove(potfile)
            except OSError:
                logger.warning(f"Could not remove old potfile {potfile}", exc_info=True)

        job.log_path = str(Path(job.capture_path).with_suffix('.hashcat.log').resolve())
        Path(job.log_path).write_bytes(b"")      # one log per job, every wordlist appended to it
        self._start(job, HashcatQueue(wordlists=[str(path) for path in wordlists]))

    def _start(self, job: JobState, queue: HashcatQueue) -> None:
        """Run hashcat against the queue's current wordlist, appending to the job's log."""
        hashcat_exe = str(job.config.get("hashcat_exe"))
        cmd = [
            hashcat_exe,
            "-m", "22000",
            "--potfile-path", str(self._potfile(job.capture_path)),
            "--status",
            "--status-json",
            "--status-timer", "1",
            *_split_args(str(job.config.get("hashcat_args") or "")),
            str(Path(job.capture_path).resolve()),
            str(queue.current),
        ]

        creationflags = 0
        if os.name == 'nt':
            creationflags = subprocess.CREATE_NO_WINDOW

        with open(job.log_path, 'ab') as f:
            f.write(f"\n=== wordlist: {queue.current} ===\n".encode("utf-8"))
            f.flush()
            queue.log_offset = f.tell()
            logger.info(f"Launching {' '.join(cmd)}, saving output to {job.log_path}")
            proc = subprocess.Popen(
                cmd,
                cwd=str(Path(hashcat_exe).parent),
                # Detach stdin: with a real console on stdin hashcat runs interactively (its
                # [s]/[p]/[q] prompt), reading the same keys Textual needs and freezing the UI.
                stdin=subprocess.DEVNULL,
                stdout=f,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
                # setsid on POSIX so hashcat leaves wifit3's session and survives SIGHUP when the
                # terminal closes; no effect on Windows.
                start_new_session=True,
            )
        self._procs[proc.pid] = proc
        job.pid = proc.pid
        queue.write_to(job)

    def poll_status(self, job: JobState, assume_dead: bool = False) -> ToolResult:
        queue = HashcatQueue.from_job(job)
        lines = _read_log_tail(job.log_path, start=queue.log_offset)
        progress_msg = self._parse_progress(lines) or "Running..."

        # The potfile is the source of truth for a recovered key. Check it FIRST, regardless of
        # liveness: hashcat sometimes hangs after printing its result, and blocking the poll until
        # the process exits froze the whole UI. If the key is there, reap the process and succeed.
        key = self._key_from_potfile(self._potfile(job.capture_path))
        if key is not None:
            self._terminate(job.pid)
            return ToolResult(status=ToolStatus.SUCCESS, value=f"Cracked! Key: {key}",
                              result_data={"key": key})

        if not assume_dead and self._is_running(job.pid, job.config.get("hashcat_exe")):
            return ToolResult(status=ToolStatus.RUNNING, value=self._running_msg(progress_msg, queue),
                              actions=self._running_actions(queue))

        self._terminate(job.pid)
        if queue.stopped:
            return ToolResult(status=ToolStatus.STOPPED, value="Stopped by user")
        if "Exhausted" in progress_msg and queue.has_next:
            queue.index += 1
            self._start(job, queue)
            return ToolResult(status=ToolStatus.RUNNING, value=self._running_msg("Starting...", queue),
                              actions=self._running_actions(queue))
        if "Exhausted" in progress_msg:
            return ToolResult(status=ToolStatus.FAILURE, value=self._exhausted_msg(queue))
        reason = self._last_log_message(lines)
        return ToolResult(status=ToolStatus.ERROR,
                          value=reason or "hashcat exited without recovering the key")

    def apply(self, action: JobAction, job: JobState) -> bool:
        queue = HashcatQueue.from_job(job)
        if action is JobAction.SKIP:
            if not queue.has_next:
                return False
            queue.index += 1
            self._kill(job)
            self._start(job, queue)
            return True
        if action is JobAction.STOP:
            queue.stopped = True
            queue.write_to(job)
            self._kill(job)
            return True
        return False

    @staticmethod
    def _running_actions(queue: HashcatQueue) -> JobAction:
        return (JobAction.STOP | JobAction.SKIP) if queue.has_next else JobAction.STOP

    @staticmethod
    def _running_msg(progress_msg: str, queue: HashcatQueue) -> str:
        return f"{progress_msg} · {queue.label}" if queue.label else progress_msg

    @staticmethod
    def _exhausted_msg(queue: HashcatQueue) -> str:
        """What the NOT FOUND badge reports: the one wordlist by name, or how many were tried."""
        if len(queue.wordlists) == 1:
            return Path(queue.wordlists[0]).name
        return f"{len(queue.wordlists)} wordlists"

    @staticmethod
    def _potfile(capture_path: str) -> Optional[str]:
        return str(Path(capture_path).resolve().with_suffix('.potfile')) if capture_path else None

    def _key_from_potfile(self, potfile):
        """The recovered passphrase from a per-capture potfile, or None. A line is
        ``<hash>[*<essid>]:<password>``, so the passphrase is everything after the first colon."""
        if not potfile or not os.path.exists(potfile):
            return None
        try:
            lines = Path(potfile).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        for line in reversed(lines):
            line = line.strip()
            if ":" in line:
                return line.split(":", 1)[1]
        return None

    def _is_running(self, pid, exe: Optional[str] = None) -> bool:
        """Whether the job's process is alive: via our Popen handle if we launched it, else via the
        OS, and (when ``exe`` is given) only if the live PID is still that program."""
        proc = self._procs.get(pid)
        if proc is not None:
            return proc.poll() is None
        if not self._pid_alive(pid):
            return False
        if exe:
            running_exe = self._pid_exe_path(pid)
            # stem-in-name (not exact) so a symlinked/versioned hashcat -> hashcat.bin still matches
            stem = Path(exe).stem.lower()
            if running_exe and stem and stem not in Path(running_exe).name.lower():
                return False
        return True

    def _pid_alive(self, pid) -> bool:
        """True if a process with this PID currently exists. Avoids os.kill(pid, 0) on Windows,
        where signal 0 routes to TerminateProcess."""
        if not pid:
            return False
        if os.name != "nt":
            try:
                os.kill(int(pid), 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                return True                       # exists, owned by another user
            return True
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == _STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)

    def _pid_exe_path(self, pid) -> Optional[str]:
        """Path to a running PID's executable (Windows API / Linux /proc), or None if unreadable."""
        if not pid:
            return None
        if os.name != "nt":
            try:
                return os.readlink(f"/proc/{int(pid)}/exe")   # Linux; OSError elsewhere/gone
            except OSError:
                return None
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return None
        try:
            size = wintypes.DWORD(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value
            return None
        finally:
            k32.CloseHandle(handle)

    def _terminate(self, pid) -> None:
        """Reap a process that has finished on its own."""
        proc = self._procs.pop(pid, None)
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                logger.exception(f"Failed to terminate hashcat pid {pid}")

    def _kill(self, job: JobState) -> None:
        """Stop a process that is still running, ours or one adopted from a previous session."""
        pid = job.pid
        if not pid:
            return
        proc = self._procs.pop(pid, None)
        if proc is not None:
            if proc.poll() is not None:
                return
            try:
                proc.kill()
            except Exception:
                logger.exception(f"Failed to kill hashcat pid {pid}")
            return
        # A job adopted from a previous session (no handle we own). Only signal it if still alive
        # and still hashcat, so a reused PID is left alone. SIGTERM -> TerminateProcess on Windows.
        if not self._is_running(pid, job.config.get("hashcat_exe")):
            return
        try:
            import signal
            os.kill(pid, signal.SIGTERM)
        except OSError:
            logger.warning(f"Could not terminate hashcat pid {pid}", exc_info=True)

    def _last_log_message(self, lines: list[str]) -> Optional[str]:
        """hashcat's last human-readable line (skips JSON status, prompts, banners), so a
        failed job reports hashcat's own error rather than a generic message."""
        skip = ("{", "[s]tatus", "Started:", "Stopped:", "hashcat (", "=== wordlist")
        candidate = None
        for raw in lines:
            line = raw.strip()
            if line and not line.startswith(skip):
                candidate = line
        if candidate and len(candidate) > 200:
            candidate = candidate[:197] + "..."
        return candidate

    def _parse_progress(self, lines: list[str]) -> Optional[str]:
        """hashcat's own status, from the newest ``--status-json`` line the process has written."""
        progress_msg = None
        for line in reversed(lines):
            line = line.strip()
            if line.startswith('{') and line.endswith('}'):
                try:
                    status_data = json.loads(line)
                    progress = status_data.get('progress', [0, 1])
                    percent = (progress[0] / max(progress[1], 1)) * 100
                    status_code = status_data.get('status', 0)
                    if status_code == 2:
                        progress_msg = "Autotuning (%.2f%%)" % percent
                    elif status_code == 3:
                        progress_msg = "Running (%.2f%%)" % percent
                    elif status_code == 4:
                        progress_msg = "Paused (%.2f%%)" % percent
                    elif status_code == 5:
                        progress_msg = "Exhausted"
                    elif status_code == 6:
                        progress_msg = "Cracked! (100%)"
                    else:
                        progress_msg = f"Status {status_code} (%.2f%%)" % percent
                    break
                except json.JSONDecodeError:
                    logger.warning(f"Could not parse hashcat status JSON: {line}", exc_info=True)
        return progress_msg
