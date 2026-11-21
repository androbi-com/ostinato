"""What a backend is: a device (or program) the generic levels read sequences from and write them to.

A backend implements only what it can. Each capability is a small interface; a backend sets the attribute to an
object implementing it, or leaves it None. The levels above check and degrade: without `instruments` a staff has no
part names, without `keys` no key signature, without `writer` the writing tools are not offered at all.

| attribute     | interface        | what it gives                                              |
| ------------- | ---------------- | ---------------------------------------------------------- |
| connection    | Connection       | connect / disconnect / status (always present)             |
| tracks        | Tracks           | the tracks, with their kind                                |
| reader        | SequenceReader   | a track's Sequence                                         |
| writer        | SequenceWriter   | write a Sequence (within the device's WriteLimits)         |
| keys          | KeyInfo          | a track's key and scale                                    |
| instruments   | InstrumentInfo   | a track's sound name                                       |
| tempo         | TempoInfo        | tempo and time signature                                   |
| changes       | ChangeFeed       | notices of edits made on the device                        |
| parameters    | ParameterInfo    | named parameters with their values                         |
| controls      | ControlInputs    | knobs / buttons usable as triggers (generative mode)       |
| mixer         | TrackMixer       | read / set a track's mute state, level and pan             |
| sounds        | SoundSelector    | the sounds a track can take (presets, kits) and set one    |
| states        | StateStore       | the device's whole state as data (saved states), and back  |
| transport     | TransportControl | play / stop, and whether it plays                          |

A backend that can be found on the MIDI ports (a hardware device) also has a `probe`; one that cannot (e.g. a DAW)
is chosen by configuration. Extra tools only one backend has come from `extra_tools()`.
"""
from __future__ import annotations

import re
from typing import Any, Callable, ClassVar, Protocol

from ..midi import Transport
from ..model import ControlEvent, DeviceEvent, Key, MixValue, Sequence, TrackInfo, WriteLimits, WriteReport


# ---------------------------------------------------------------------- capabilities

class Connection(Protocol):
    def connect(self, port_hint: str | None = None) -> str: ...  # -> a text for the user
    def disconnect(self) -> str: ...  # release the device; no reconnecting by itself until connect()
    def status(self) -> dict[str, Any]: ...  # {"connected", "port", "firmware", "sync"}
    def overview(self) -> str: ...  # a readable overview of the device (ostinato_status)
    def not_ready(self) -> str | None: ...  # why the device cannot be read now (None: it can)


class Tracks(Protocol):
    def list_tracks(self) -> list[TrackInfo]: ...


class SequenceReader(Protocol):
    def read(self, track: int, refresh: bool = False) -> Sequence: ...  # refresh: re-read the device first


class SequenceWriter(Protocol):
    def limits(self, track: int) -> WriteLimits: ...
    def write(self, track: int, seq: Sequence, label: str = "") -> WriteReport: ...  # the whole track's sequence


class KeyInfo(Protocol):
    def key(self, track: int) -> Key | None: ...
    def describe(self, track: int) -> str: ...  # the device's own key settings, in words
    def offered_scales(self) -> list[tuple[str, str]]: ...  # (device name, generic scale name) it can be set to


class InstrumentInfo(Protocol):
    def instrument(self, track: int) -> str | None: ...


class TempoInfo(Protocol):
    def tempo(self) -> float | None: ...
    def time_signature(self) -> tuple[int, int]: ...


class ChangeFeed(Protocol):
    def subscribe(self, fn: Callable[[DeviceEvent], None]) -> None: ...


class ParameterInfo(Protocol):
    def parameters(self, track: int | None, only_changed: bool = False) -> str: ...  # track None: the globals


class TrackMixer(Protocol):
    def muted(self, track: int) -> bool: ...
    def set_muted(self, track: int, muted: bool) -> bool: ...  # -> the state reported by the device
    def level(self, track: int) -> MixValue: ...
    def set_level(self, track: int, value: int) -> MixValue: ...  # value on level()'s scale; -> as the device took it
    def pan(self, track: int) -> MixValue | None: ...  # None: the track has no pan
    def set_pan(self, track: int, value: int) -> MixValue: ...


class SoundSelector(Protocol):
    def sounds(self, track: int) -> list[tuple[str, str]]: ...  # (group, e.g. the synth engine; sound name)
    def set_sound(self, track: int, name: str, group: str | None = None) -> str: ...  # -> the track's sound now


class TransportControl(Protocol):
    def available(self) -> bool: ...  # (the connected firmware can be started / stopped)
    def playing(self, fresh: bool = False) -> bool | None: ...  # None: not known; fresh: ask the device now
    def set_playing(self, how: str) -> bool: ...  # "play" / "stop" / "toggle" -> playing afterwards
    def details(self) -> dict: ...  # e.g. song mode, as the device reports it


class StateStore(Protocol):
    """Everything the backend knows of the device, as JSON-able data, and writing it back (ostinato.states keeps
    it in files next to the generic part)."""
    parts: tuple[str, ...]  # what apply() can write back, e.g. ("notes", "sound", "mix", "globals")

    def capture(self) -> dict: ...  # re-reads the device first
    def compatible(self, data: dict) -> str | None: ...  # why the data cannot go onto this device (None: it can)
    def apply(self, data: dict, tracks: list[int], parts: list[str]) -> list[str]: ...  # -> what was done
    def exact_supported(self) -> bool: ...  # an exact copy in the device's own format
    def capture_exact(self) -> bytes: ...
    def apply_exact(self, blob: bytes) -> None: ...


class ControlHandle(Protocol):
    def close(self) -> None: ...  # stop listening, put the control back as it was
    warnings: list[str]


class ControlInputs(Protocol):
    """Knobs and buttons the generic gestures can use (generative mode). An external MIDI controller would be
    another implementation of this interface (not implemented yet)."""

    def sources(self) -> list[tuple[str, str, str]]: ...  # (name, "encoder" | "button", description)
    def open(self, name: str, track: int, fn: Callable[[ControlEvent], None],
             options: dict | None = None) -> ControlHandle: ...


# ---------------------------------------------------------------------- the backend

class Backend:
    name: ClassVar[str] = ""  # short: also its setup folder (ostinato.config.backend_dir)
    title: ClassVar[str] = ""
    port_pattern: ClassVar[re.Pattern | None] = None  # MIDI port names it may be found under (None: not probed)
    port_prefer: ClassVar[tuple[re.Pattern, ...]] = ()  # of those, the names to try first
    instructions: str = ""  # for Claude: what is special about this device

    connection: Connection
    tracks: Tracks | None = None
    reader: SequenceReader | None = None
    writer: SequenceWriter | None = None
    keys: KeyInfo | None = None
    instruments: InstrumentInfo | None = None
    tempo: TempoInfo | None = None
    changes: ChangeFeed | None = None
    parameters: ParameterInfo | None = None
    controls: ControlInputs | None = None
    mixer: TrackMixer | None = None
    sounds: SoundSelector | None = None
    states: StateStore | None = None
    transport: TransportControl | None = None

    # ---- detection (hardware backends)
    @classmethod
    def probe(cls, transport: Transport) -> dict | None:
        """Is the device on this open transport this backend's? -> {"firmware": text, ...} or None. Quick, and
        changes nothing on the device."""
        return None

    @classmethod
    def simulator(cls) -> Callable[[], Transport] | None:
        return None

    # ---- life
    def start(self, port_hint: str | None, transport_factory: Callable[[], Transport] | None = None) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def capabilities(self) -> list[str]:
        return [n for n in ("tracks", "reader", "writer", "keys", "instruments", "tempo", "changes", "parameters",
                            "controls", "mixer", "sounds", "states", "transport") if getattr(self, n) is not None]

    def setups(self) -> list:
        """The backend's own JSON setups (ostinato.setup.Setup subclasses), for settings forms."""
        return []

    def extra_tools(self) -> list[tuple[Callable, dict]]:
        """Tools only this backend has: (async function, {"name", "description", "annotations"})."""
        return []
