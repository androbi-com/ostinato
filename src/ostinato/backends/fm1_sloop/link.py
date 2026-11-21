"""The SLOOP editor protocol's request/reply link over a MIDI transport (ostinato.midi).

The device holds one incoming SysEx frame, so the link sends one request at a time, waits for the reply
(matched by cmd and echoed args), retries on a timeout, and hands push frames (CHANGED, RELOAD,
STEP_CHANGED, TRACK_CHANGED) to a callback whenever they arrive, also between a request and its reply.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from ...midi import MidoTransport, Transport, base_name, candidate_pairs, list_ports  # noqa: F401 (re-exported)
from .protocol import PUSH_CMDS, frame, reply_matches, unframe

log = logging.getLogger("ostinato.sloop.link")


class LinkError(RuntimeError):
    pass


class LinkTimeout(LinkError):
    pass


class LinkClosed(LinkError):
    pass


# ------------------------------------------------------------------ the link

class _Pending:
    __slots__ = ("cmd", "args", "event", "reply")

    def __init__(self, cmd: int, args: list[int]):
        self.cmd, self.args = cmd, args
        self.event = threading.Event()
        self.reply: list[int] | None = None


class Link:
    """Strict request/response over a transport: one frame in flight, callers serialised by a lock."""

    def __init__(self, transport: Transport, on_push: Callable[[int, list[int]], None] | None = None,
                 timeout: float = 0.3):
        self.t = transport
        self.on_push = on_push
        self.timeout = timeout
        self._lock = threading.Lock()
        self._cur: _Pending | None = None
        self._closed = False
        self.last_sent = 0.0  # time.monotonic() of the last frame sent (WATCH keep-alive)
        self.stats = {"requests": 0, "retries": 0, "timeouts": 0, "pushes": 0, "unknown": 0}
        transport.set_receiver(self.receive)
        self.on_note: Callable[[int, int, int], None] | None = None
        if hasattr(transport, "set_note_receiver"):
            transport.set_note_receiver(lambda ch, n, v: self.on_note and self.on_note(ch, n, v))

    @property
    def closed(self) -> bool:
        return self._closed

    def request(self, r: tuple[int, list[int]], timeout: float | None = None, retries: int = 1) -> list[int]:
        """Send (cmd, args), wait for the matching reply's args. Raises LinkTimeout / LinkClosed."""
        cmd, args = int(r[0]), list(r[1])
        data = frame(cmd, args)
        to = timeout or self.timeout
        with self._lock:
            if self._closed:
                raise LinkClosed("closed")
            p = _Pending(cmd, args)
            self._cur = p
            self.stats["requests"] += 1
            try:
                for attempt in range(retries + 1):
                    if attempt:
                        self.stats["retries"] += 1
                    self.last_sent = time.monotonic()
                    self.t.send(data)
                    if p.event.wait(to):
                        if p.reply is None:
                            raise LinkClosed("closed")
                        return p.reply
                    if self._closed:
                        raise LinkClosed("closed")
                self.stats["timeouts"] += 1
                raise LinkTimeout(f"timeout (cmd {cmd})")
            finally:
                self._cur = None

    def receive(self, data: list[int]) -> None:
        """Raw incoming MIDI bytes (called from the transport's thread)."""
        f = unframe(data)
        if not f:
            return
        cmd, a = f
        if cmd in PUSH_CMDS:
            self.stats["pushes"] += 1
            if self.on_push:
                try:
                    self.on_push(cmd, a)
                except Exception:
                    log.exception("push handler failed")
            return
        c = self._cur
        if c is None or c.event.is_set() or cmd != c.cmd or not reply_matches(c.cmd, c.args, a):
            self.stats["unknown"] += 1
            log.debug("ignored frame cmd=%s args=%s", cmd, a[:8])
            return
        c.reply = a
        c.event.set()

    def close(self) -> None:
        self._closed = True
        c = self._cur
        if c:
            c.event.set()
        self.t.set_receiver(None)
        if hasattr(self.t, "set_note_receiver"):
            self.t.set_note_receiver(None)
        self.t.close()
