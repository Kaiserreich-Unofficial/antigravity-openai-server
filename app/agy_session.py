"""Async ``agy`` CLI driver — one subprocess per turn.

Unlike the `claude` CLI (bidirectional stream-json over a persistent stdin/stdout
process), `agy` is invoked once per turn: ``agy -p <prompt> --output-format
stream-json`` runs the turn to completion and exits. Conversation continuity is
server-side in agy: ``--conversation <id>`` resumes the stored history, so a
follow-up turn passes only the newest user message.

The prompt travels as an argv argument — stdin piping is NOT supported (verified
against agy 1.1.4: ``echo .. | agy -p`` produces "no output produced"). Windows
caps a command line at ~32,767 chars; callers enforce
``settings.max_prompt_chars`` before spawning.

A reader task parses stdout JSONL lines into :data:`~app.events.ChatEvent`
values pushed onto an :class:`asyncio.Queue`; :data:`STREAM_CLOSED` marks EOF
(process exit). The last few unparseable stdout lines and a stderr tail are kept
for diagnostics (agy prints some errors as plain text, e.g. the headless
permission notice).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Optional, Union

from app.events import ChatEvent, parse_line
from app.timing import log_spawn

logger = logging.getLogger("agi.session")

# Sentinel placed on the event queue when the CLI's stdout reaches EOF (the
# process exited). Consumers stop waiting for turn events when they see it.
STREAM_CLOSED = object()

# StreamReader line buffer. Tool outputs embedded in step_update lines can be
# sizeable; keep the cap generous and skip — rather than crash on — any
# pathologically long line.
_READ_LIMIT = 64 * 1024 * 1024


class AgySession:
    """Drives one ``agy -p`` subprocess for a single turn."""

    def __init__(
        self,
        *,
        agy_bin: str,
        model: Optional[str],
        workdir: Union[str, Path],
        prompt: str,
        conversation_id: Optional[str] = None,
        agent: Optional[str] = None,
        print_timeout_s: int = 600,
        max_inline_prompt_chars: int = 28000,
        timing_log: bool = False,
        timing_label: str = "session",
    ) -> None:
        self.agy_bin = agy_bin
        self.model = model
        self.workdir = str(workdir)
        self.prompt = prompt
        self.conversation_id = conversation_id
        self.agent = agent
        self.print_timeout_s = print_timeout_s
        self.max_inline_prompt_chars = max_inline_prompt_chars
        self.timing_log = timing_log
        self.timing_label = timing_label

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._queue: asyncio.Queue[Union[ChatEvent, object]] = asyncio.Queue()
        self._reader_task: Optional[asyncio.Task[None]] = None
        self._stderr_task: Optional[asyncio.Task[None]] = None
        self._closed = False
        self._pushback: list[Union[ChatEvent, object]] = []
        # Diagnostics: last raw stdout lines the parser skipped + stderr tail.
        self.raw_tail: deque[str] = deque(maxlen=5)
        self.stderr_tail: deque[str] = deque(maxlen=5)
        self._prompt_path: Optional[Path] = None

    # ── command construction ────────────────────────────────────────────────

    def _build_args(self) -> list[str]:
        prompt = self._prompt_argument()
        args = [
            self.agy_bin,
            "-p", prompt,
            "--output-format", "stream-json",
            "--dangerously-skip-permissions",
            "--print-timeout", f"{self.print_timeout_s}s",
            "--add-dir", self.workdir,
        ]
        if self.model:
            args += ["--model", self.model]
        if self.conversation_id:
            args += ["--conversation", self.conversation_id]
        if self.agent:
            args += ["--agent", self.agent]
        return args

    def _prompt_argument(self) -> str:
        """Return an argv-safe prompt, staging oversized contexts in the workspace.

        Linux rejects a single argv item above MAX_ARG_STRLEN (typically 128 KiB),
        while agy currently has no prompt-file or stdin option.  agy can read a
        workspace file with its builtin file tool, so stage large OpenAI contexts
        as a private temporary file and give it a small, explicit instruction.
        """
        if len(self.prompt) <= self.max_inline_prompt_chars:
            return self.prompt
        if self._prompt_path is None:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", prefix=".agy-prompt-", suffix=".md",
                dir=self.workdir, delete=False,
            ) as handle:
                # Keep physical lines short: agy's view_file tool can otherwise
                # truncate a single enormous line before the model reaches the
                # request at its end.
                handle.write("\n".join(
                    self.prompt[i:i + 8192] for i in range(0, len(self.prompt), 8192)
                ))
                self._prompt_path = Path(handle.name)
            os.chmod(self._prompt_path, 0o600)
            logger.info(
                "staged oversized agy prompt: chars=%d threshold=%d path=%s",
                len(self.prompt), self.max_inline_prompt_chars, self._prompt_path,
            )
        return (
            "The complete request context is in this file: "
            f"{self._prompt_path}. First read the entire file with your file-reading "
            "tool. Treat its contents as the request context and then respond to it."
        )

    def _cleanup_prompt_file(self) -> None:
        if self._prompt_path is not None:
            with contextlib.suppress(FileNotFoundError):
                self._prompt_path.unlink()
            self._prompt_path = None

    # ── lifecycle ─────────────────────────────────────────────────────────—

    async def start(self) -> None:
        """Spawn the subprocess (idempotent) and launch reader/stderr tasks."""
        if self._proc is not None:
            return
        args = self._build_args()
        logger.info(
            "spawning agy: model=%s workdir=%s conv=%s prompt_chars=%d",
            self.model, self.workdir, self.conversation_id, len(self.prompt),
        )
        spawn_t0 = time.monotonic()
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.workdir,
                limit=_READ_LIMIT,
            )
        except FileNotFoundError as e:
            self._cleanup_prompt_file()
            raise RuntimeError(
                "agy CLI not found on PATH — install the Antigravity CLI "
                "(https://antigravity.google/docs/cli-install) and sign in"
            ) from e
        except Exception:
            self._cleanup_prompt_file()
            raise
        log_spawn(self.timing_log, self.timing_label, (time.monotonic() - spawn_t0) * 1000)
        self._reader_task = asyncio.create_task(self._read_stdout(), name="agy-stdout")
        self._stderr_task = asyncio.create_task(self._read_stderr(), name="agy-stderr")

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def exit_status(self, timeout: float = 2.0) -> Optional[int]:
        """Return the subprocess exit code, waiting briefly if it is still exiting.

        Called after stdout reaches EOF: the process has usually already exited,
        but may still be flushing. Returns ``None`` when it did not exit in time.
        """
        proc = self._proc
        if proc is None:
            return None
        if proc.returncode is not None:
            return proc.returncode
        try:
            return await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    async def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout
        try:
            while True:
                try:
                    raw = await stdout.readline()
                except ValueError:
                    # A line longer than the buffer cap: asyncio re-raises the
                    # LimitOverrunError as ValueError with the buffer already
                    # advanced — skip the line and keep reading.
                    logger.warning("skipped oversized stdout line (> %d bytes)", _READ_LIMIT)
                    continue
                except asyncio.IncompleteReadError:
                    break
                if not raw:
                    break
                line = raw.decode("utf-8", "replace")
                ev = parse_line(line)
                if ev is not None:
                    await self._queue.put(ev)
                elif line.strip():
                    self.raw_tail.append(line.strip()[:500])
        finally:
            await self._queue.put(STREAM_CLOSED)

    async def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        stderr = self._proc.stderr
        while True:
            try:
                raw = await stderr.readline()
            except (asyncio.LimitOverrunError, ValueError):
                continue
            if not raw:
                break
            msg = raw.decode("utf-8", "replace").rstrip()
            if msg:
                self.stderr_tail.append(msg[:500])
                logger.debug("agy stderr: %s", msg)

    # ── reading ───────────────────────────────────────────────────────────—

    def push_back(self, ev: Union[ChatEvent, object]) -> None:
        """Return an already-popped event to the front of the stream."""
        self._pushback.append(ev)

    async def next_event(
        self, timeout: Optional[float] = None
    ) -> Union[ChatEvent, object, None]:
        """Pop the next event. Returns :data:`STREAM_CLOSED` at EOF, or ``None``
        if ``timeout`` elapses first."""
        if self._pushback:
            return self._pushback.pop(0)
        try:
            if timeout is None:
                return await self._queue.get()
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def diagnostics(self) -> str:
        """A short description of why the stream may have ended without a result."""
        parts: list[str] = []
        if self.raw_tail:
            parts.append("stdout: " + " | ".join(self.raw_tail))
        if self.stderr_tail:
            parts.append("stderr: " + " | ".join(self.stderr_tail))
        return "; ".join(parts) if parts else "no diagnostics captured"

    # ── shutdown ──────────────────────────────────────────────────────────—

    async def aclose(self) -> None:
        """Terminate the process and cancel reader/stderr tasks."""
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._cleanup_prompt_file()
