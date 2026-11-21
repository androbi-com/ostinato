"""MIDI ports, device-neutral: finding a device's ports (by the name patterns its backend knows) and a transport over
them (mido + python-rtmidi). A backend then probes the port to see whether the device is its own."""
from __future__ import annotations

import logging
import re
from typing import Callable, Protocol

log = logging.getLogger("ostinato.midi")

class Transport(Protocol):
    name: str

    def send(self, data: list[int]) -> None: ...

    def set_receiver(self, cb: Callable[[list[int]], None] | None) -> None: ...

    def set_note_receiver(self, cb: Callable[[int, int, int], None] | None) -> None: ...

    def close(self) -> None: ...


# ------------------------------------------------------------------ real MIDI ports

def _mido():
    import mido

    mido.set_backend("mido.backends.rtmidi", load=True)
    return mido


_IDX_SUFFIX = re.compile(r"\s+\d+$")


def base_name(name: str) -> str:
    """Windows rtmidi appends the port index ("Felucca 1"): strip it to pair inputs with outputs."""
    return _IDX_SUFFIX.sub("", name or "")


def list_ports() -> dict:
    mido = _mido()
    return {"inputs": mido.get_input_names(), "outputs": mido.get_output_names()}


def candidate_pairs(hint: str | None, pattern: re.Pattern,
                    prefer: tuple[re.Pattern, ...] = ()) -> list[tuple[str, str]]:
    """(input, output) port-name pairs that may be the device: names matching `pattern`, those matching the earlier
    `prefer` patterns first. `hint` (a substring of the port name) narrows the list and also allows names the
    pattern does not know.
    """
    ports = list_ports()
    ins, outs = ports["inputs"], ports["outputs"]
    if hint:
        h = hint.lower()
        ins = [p for p in ins if h in p.lower()]
        outs = [p for p in outs if h in p.lower()]
    else:
        ins = [p for p in ins if pattern.search(p)]
        outs = [p for p in outs if pattern.search(p)]

    def rank(n: str) -> int:
        return next((k for k, p in enumerate(prefer) if p.search(n)), len(prefer))

    pairs = []
    for i in ins:
        o = next((x for x in outs if base_name(x) == base_name(i)), outs[0] if len(outs) == 1 else None)
        if o:
            pairs.append((i, o))
    return sorted(pairs, key=lambda p: rank(p[0]))


class MidoTransport:
    """A pair of real MIDI ports through mido + python-rtmidi (SysEx enabled)."""

    def __init__(self, in_name: str, out_name: str):
        mido = _mido()
        self.name = base_name(in_name)
        self.in_name, self.out_name = in_name, out_name
        self._cb: Callable[[list[int]], None] | None = None
        self._note_cb: Callable[[int, int, int], None] | None = None
        self._mido = mido
        self._out = mido.open_output(out_name)
        try:
            self._in = mido.open_input(in_name, callback=self._on_msg)
        except Exception:
            self._out.close()
            raise

    def _on_msg(self, msg) -> None:
        if msg.type == "note_on" and msg.velocity > 0:  # a key of the device (e.g. the FM-1 sends its keys)
            ncb = self._note_cb
            if ncb:
                try:
                    ncb(msg.channel, msg.note, msg.velocity)
                except Exception:
                    log.exception("note receiver failed")
            return
        if msg.type != "sysex":
            return  # note offs, clock... are not ours
        cb = self._cb
        if cb:
            try:
                cb(list(msg.bytes()))
            except Exception:  # never let an exception reach rtmidi's thread
                log.exception("receiver failed")

    def send(self, data: list[int]) -> None:
        self._out.send(self._mido.Message.from_bytes(data))

    def set_receiver(self, cb):
        self._cb = cb

    def set_note_receiver(self, cb: Callable[[int, int, int], None] | None) -> None:
        """cb(channel, note, velocity) for each key pressed on the device."""
        self._note_cb = cb

    def close(self) -> None:
        self._cb = None
        self._note_cb = None
        for p in (getattr(self, "_in", None), getattr(self, "_out", None)):
            try:
                if p is not None:
                    p.close()
            except Exception:
                pass
