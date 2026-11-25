"""Async ``agy`` CLI driver — one subprocess per turn.

Unlike the `claude` CLI (bidirectional stream-json over a persistent stdin/stdout
process), `agy` is invoked once per turn and exits when the turn completes.
Conversation continuity is server-side in agy: ``--conversation <id>`` resumes
the stored history, so a follow-up turn passes only the newest user message.

Two delivery channels for the prompt:

* small prompts ride as argv: ``agy -p <prompt> --output-format stream-json``
  (Windows caps a command line at ~32,767 chars);
* larger ones go over stdin with ``--input-format stream-json`` — one NDJSON
  ``{"event":"user","message":{"content":…}}`` line, then EOF (agy runs one
  turn per input line and exits at EOF).

Stdin is used instead of staging the prompt in a workspace file because agy's
file reader sniffs encodings: a mostly-ASCII UTF-8 file with a few CJK
characters is mistaken for GBK under a Chinese Windows locale, and the model
then works from mojibake (U+FFFD) and emits malformed tool arguments
(upstream HTTP 400 ``INVALID_ARGUMENT``). Stdin is decoded as UTF-8 — no
guessing — and "read the file first" instruction turns disappear with it.

A reader task parses stdout JSONL lines into :data:`~app.events.ChatEvent`
values pushed onto an :class:`asyncio.Queue`; :data:`STREAM_CLOSED` marks EOF
(process exit). The last few unparseable stdout lines and a stderr tail are kept
for diagnostics (agy prints some errors as plain text, e.g. the headless
permission notice).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
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
        self._stdin_task: Optional[asyncio.Task[None]] = None
        self._closed = False
        self._pushback: list[Union[ChatEvent, object]] = []
        # Diagnostics: last raw stdout lines the parser skipped + stderr tail.
        self.raw_tail: deque[str] = deque(maxlen=5)
        self.stderr_tail: deque[str] = deque(maxlen=5)

    # ── command construction ────────────────────────────────────────────────

    @property
    def _stdin_delivery(self) -> bool:
        return len(self.prompt) > self.max_inline_prompt_chars

    def _build_args(self) -> list[str]:
        args = [self.agy_bin]
        if self._stdin_delivery:
            args += ["--input-format", "stream-json"]
        else:
            args += ["-p", self.prompt]
        args += [
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

    def _stdin_payload(self) -> bytes:
        """Serialize the turn as one stream-json NDJSON line, encoded UTF-8.

        The bytes are produced by this process, so the exact encoding is known:
        no file reader of agy can re-guess it as GBK. The digest makes the
        delivered payload auditable in the log.
        """
        line = json.dumps(
            {"event": "user", "message": {"role": "user", "content": self.prompt}},
            ensure_ascii=False,
        ) + "\n"
        try:
            payload = line.encode("utf-8")
        except UnicodeEncodeError as e:
            raise RuntimeError(
                f"prompt cannot be encoded as UTF-8: {e.reason} at char {e.start}"
            ) from e
        logger.info(
            "agy stdin prompt: chars=%d bytes=%d sha256=%s",
            len(self.prompt), len(payload), hashlib.sha256(payload).hexdigest()[:16],
        )
        return payload

    # ── lifecycle ─────────────────────────────────────────────────────────—

    async def start(self) -> None:
        """Spawn the subprocess (idempotent) and launch reader/stderr tasks."""
        if self._proc is not None:
            return
        args = self._build_args()
        payload = self._stdin_payload() if self._stdin_delivery else None
        logger.info(
            "spawning agy: model=%s workdir=%s conv=%s prompt_chars=%d stdin=%s",
            self.model, self.workdir, self.conversation_id, len(self.prompt),
            payload is not None,
        )
        spawn_t0 = time.monotonic()
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=(
                    asyncio.subprocess.PIPE if payload is not None
                    else asyncio.subprocess.DEVNULL
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.workdir,
                limit=_READ_LIMIT,
            )
        except FileNotFoundError as e:
            raise RuntimeError(
                "agy CLI not found on PATH — install the Antigravity CLI "
                "(https://antigravity.google/docs/cli-install) and sign in"
            ) from e
        log_spawn(self.timing_log, self.timing_label, (time.monotonic() - spawn_t0) * 1000)
        self._reader_task = asyncio.create_task(self._read_stdout(), name="agy-stdout")
        self._stderr_task = asyncio.create_task(self._read_stderr(), name="agy-stderr")
        if payload is not None:
            self._stdin_task = asyncio.create_task(
                self._feed_stdin(payload), name="agy-stdin"
            )

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

    async def _feed_stdin(self, payload: bytes) -> None:
        """Write the turn message, then close stdin.

        Closing is part of the protocol: stream-json input runs one turn per
        line and agy exits only at EOF. A broken pipe means agy died first
        (bad flag on an older build, auth failure); stderr captures why.
        """
        proc = self._proc
        if proc is None or proc.stdin is None:
            return
        stdin = proc.stdin
        try:
            stdin.write(payload)
            await stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            logger.warning("agy closed stdin before %d bytes were delivered", len(payload))
        finally:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                stdin.close()
            with contextlib.suppress(OSError):
                await stdin.wait_closed()

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

    async def _teardown(self) -> None:
        """Stop the process and its I/O tasks, leaving the session re-startable."""
        proc = self._proc
        self._proc = None
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
        for task in (self._stdin_task, self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._stdin_task = self._reader_task = self._stderr_task = None

    async def restart(self) -> None:
        """Run the same turn again on a fresh subprocess.

        agy has no in-process recovery, and a rejected turn produces no result:
        a new process re-runs it from the same prompt, model and (when set)
        conversation. The event queue is replaced so no ``STREAM_CLOSED`` from
        the dead process leaks into the new stream.
        """
        await self._teardown()
        self._queue = asyncio.Queue()
        self._pushback.clear()
        self.raw_tail.clear()
        self.stderr_tail.clear()
        await self.start()

    async def aclose(self) -> None:
        """Terminate the process and cancel reader/stderr tasks."""
        if self._closed:
            return
        self._closed = True
        await self._teardown()


def is_invalid_argument(message: str) -> bool:
    """True when agy reports the backend refusing the generated turn.

    agy surfaces ``INVALID_ARGUMENT (code 400)`` when the model's turn is
    malformed upstream. Nothing this server sent caused it, and the turn
    produced no result, so a fresh process is the only recovery.
    """
    return "INVALID_ARGUMENT" in message
