"""The generic MCP tools, registered according to the active backend's capabilities (a tool is offered only when the
device can do it), plus the backend's own extra tools. Device-independent: they work on ostinato.model through the
session; nothing here knows a firmware.

    reader:                ostinato_show_track, ostinato_show_all_tracks, ostinato_show_staff, ostinato_export_musicxml,
                           ostinato_track_data, ostinato_refresh, ostinato_changes
    keys (+ reader):       ostinato_scales
    parameters:            ostinato_parameters
    mixer:                 ostinato_mute_track
    writer (+ reader):     ostinato_write_notes, ostinato_write_midi, ostinato_clear_track, ostinato_undo,
                           ostinato_generators, ostinato_generate
    + controls:            ostinato_generative_mode
The device server's own tools (ostinato_status, ostinato_connect, ostinato_disconnect, ostinato_list_midi_ports) are in device_server.
"""
from __future__ import annotations

import json
import time
from fractions import Fraction
from pathlib import Path
from typing import Literal

import anyio
from mcp.types import ToolAnnotations

from .model import Sequence
from .music import notes_io as NIO
from .music import scales as SC
from .music import views as V

RO = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)
FILE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)


async def _run(fn, *a, **kw):
    return await anyio.to_thread.run_sync(lambda: fn(*a, **kw))


def register(mcp, session) -> list[str]:
    """Add the tools the session's backend supports to `mcp`. -> their names."""
    b = session.backend
    names: list[str] = []

    def tool(annotations=None):
        def deco(fn):
            mcp.add_tool(fn, name=fn.__name__, description=fn.__doc__, annotations=annotations)
            names.append(fn.__name__)
            return fn
        return deco

    def ready(track: int | None = None) -> str | None:
        e = session.not_ready()
        if e:
            return e
        if track is not None:
            n = len(session.tracks())
            if not 1 <= track <= n:
                return f"track must be 1..{n}"
        return None

    def head(track: int, seq: Sequence) -> str:
        ti = session.track_info(track)
        inst = session.instrument(track)
        return f"Track {track} ({seq.kind})" + (f": {inst}" if inst else "") + "\n" + V.pattern_line(seq)

    def drum_names(track: int) -> dict[int, str] | None:
        if b.writer is not None:
            return b.writer.limits(track).drum_voices
        return None

    def show(track: int, view: str = "grid", steps: int | None = None, refresh: bool = False) -> str:
        seq = session.read(track, refresh)
        key = session.key(track) if seq.kind != "drums" else None
        return head(track, seq) + "\n\n" + V.view(seq, view, key, steps, drum_names(track))

    # ------------------------------------------------------------------ reading
    if b.reader is not None:
        @tool(RO)
        async def ostinato_show_track(track: int, view: Literal["grid", "list", "roll"] = "grid",
                                 steps: int | None = None) -> str:
            """Show a track's sequence as text. view: "grid" (compact: 16 steps a row; drums: a voice x step grid),
            "list" (one line per note / chord with every detail: length, muted, accent, slide, velocity, per-note
            level and ratchet, nudge, and the device's own per-step data), "roll" (piano roll).
            steps: how many steps to show (default: the pattern's length)."""
            if (e := ready(track)):
                return e
            return await _run(show, track, view, steps)

        @tool(RO)
        async def ostinato_show_all_tracks(view: Literal["grid", "list", "roll"] = "grid") -> str:
            """Show the sequences of every track (see ostinato_show_track for the views)."""
            if (e := ready()):
                return e
            return "\n\n".join([await _run(show, t.track, view) for t in session.tracks()])

        def score(track: int | None, max_bars: int):
            from .music import notation as N

            tracks = [track] if track else [t.track for t in session.tracks()
                                            if session.read(t.track).sounding()] or [1]
            parts = []
            for t in tracks:
                seq = session.read(t)
                inst = session.instrument(t)
                parts.append(N.Part(f"T{t} {inst}" if inst else f"Track {t}", seq,
                                    session.key(t) if seq.kind != "drums" else None, drum_names(t)))
            return N.build_score(parts, session.tempo(), max(1, min(16, max_bars)))

        @tool(RO)
        async def ostinato_show_staff(track: int | None = None, max_bars: int = 8) -> list:
            """Show sequences as music notation (an image). track: one track, or omit it for a score of every track
            with notes (each loops its own length, as they play). Ties become longer notes, chords are stacked;
            accents, slides, ghost notes and ratchets are marked; drums are a two-voice percussion staff; key
            signature and note spelling follow the track's key (when the device tells it). At most max_bars (1-16)."""
            from mcp.server.mcpserver import Image

            from .music import notation as N

            if (e := ready(track)):
                return [e]
            try:
                sc, remarks = await _run(score, track, max_bars)
                xml = await _run(N.to_musicxml, sc)
                pngs = await _run(N.render_png, xml, sc.ostinato_bars)
            except Exception as ex:
                return [f"Could not render the staff: {ex!r}"]
            return ["\n".join(remarks), *[Image(data=p, format="png") for p in pngs]]

        @tool(FILE)
        async def ostinato_export_musicxml(track: int | None = None, path: str | None = None, max_bars: int = 16) -> str:
            """Save sequences as a MusicXML file (MuseScore, Sibelius, Dorico...). track: one, or omit for every
            track with notes. path: the file (default: Documents/Ostinato/ostinato-<time>.musicxml). Writes a local file only."""
            from .music import notation as N

            if (e := ready(track)):
                return e
            if path:
                out = Path(path).expanduser()
                if out.suffix.lower() not in (".musicxml", ".xml"):
                    out = out.with_suffix(".musicxml")
            else:
                out = Path.home() / "Documents" / "Ostinato" / (
                    "ostinato-" + time.strftime("%Y%m%d-%H%M%S") + (f"-track{track}" if track else "") + ".musicxml")
            try:
                sc, remarks = await _run(score, track, max_bars)
                xml = await _run(N.to_musicxml, sc)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(xml, encoding="utf-8")
            except Exception as ex:
                return f"Could not export: {ex!r}"
            return f"Saved {out}\n" + "\n".join(remarks)

        @tool(RO)
        async def ostinato_track_data(track: int) -> dict:
            """A track's sequence as structured data (for analysis or transformation): its kind, length and grid,
            and per event its start and length (in beats and steps), notes (MIDI number, name spelled in the
            track's key, velocity, level, ratchet), accent, slide, muted, nudge and the device's own data."""
            if (e := ready(track)):
                return {"error": e}
            seq = await _run(session.read, track)
            key = session.key(track) if seq.kind != "drums" else None
            nm = SC.namer(key)
            from .music import drums as D

            step = seq.grid.step if seq.grid else None
            evs = []
            for ev in sorted(seq.events, key=lambda x: x.start):
                d = {"start_beat": float(ev.start) + 1, "length_beats": float(ev.length)}
                if step:
                    d["step"] = int(ev.start / step) + 1
                    d["steps"] = float(ev.length / step)
                d["notes"] = [{"note": n.pitch, "name": D.name(n.pitch) if seq.kind == "drums" else nm(n.pitch),
                               **({"velocity": n.velocity} if n.velocity else {}),
                               **({"level": n.level} if n.level != "normal" else {}),
                               **({"ratchet": n.ratchet} if n.ratchet > 1 else {})} for n in ev.notes]
                for f in ("accent", "slide", "muted"):
                    if getattr(ev, f):
                        d[f] = True
                if ev.offset:
                    d["offset_beats"] = float(ev.offset)
                if {k: v for k, v in ev.extras.items() if k != "step"}:
                    d["device"] = {k: v for k, v in ev.extras.items() if k != "step"}
                evs.append(d)
            return {"track": track, "kind": seq.kind, "instrument": session.instrument(track),
                    "length_beats": float(seq.length),
                    "grid": {"steps": seq.grid.steps, "step_beats": float(seq.grid.step), "name": seq.grid.name}
                    if seq.grid else None,
                    "key": SC.key_name(key) if key else None, "events": evs}

        @tool(RO)
        async def ostinato_refresh(track: int | None = None) -> str:
            """Re-read from the device: one track or every track (for changes the device does not report)."""
            if (e := ready(track)):
                return e
            tracks = [track] if track else [t.track for t in session.tracks()]
            for t in tracks:
                await _run(session.read, t, True)
            return "Refreshed.\n\n" + "\n\n".join([await _run(show, t) for t in tracks])

        @tool(RO)
        async def ostinato_changes(since_version: int = 0, limit: int = 50) -> dict:
            """What changed since a version (from a previous call): edits made on the device (steps, parameters,
            track selection, loads), and this session's writes, generative runs, undos. Use it to follow what the
            user does on the device."""
            evs = session.events_since(since_version)[-max(1, min(500, limit)):]
            return {"version": session.version,
                    "events": [{"version": e.version, "at": time.strftime("%H:%M:%S", time.localtime(e.at)),
                                "kind": e.kind, "track": e.track, "source": e.source, "text": e.text} for e in evs]}

    # ------------------------------------------------------------------ keys
    if b.keys is not None and b.reader is not None:
        @tool(RO)
        async def ostinato_scales(track: int | None = None, list_scales: bool = False) -> str:
            """The key and scale of the tracks as the device has them (the scale's notes spelled, the device's own
            settings such as quantise or chord mode), and which notes of the pattern fall outside the scale.
            track: one, or omit for all. list_scales: also every scale the device offers, with its notes."""
            if (e := ready(track)):
                return e

            def one(t: int) -> str:
                seq = session.read(t)
                inst = session.instrument(t)
                lines = [f"Track {t}" + (f" ({inst})" if inst else "")]
                if seq.kind == "drums":
                    return lines[0] + ": drums, no key"
                key = session.key(t)
                if key is not None:
                    lines.append(f"  Key: {SC.key_name(key)}" + (f" ({key.note})" if key.note else "")
                                 + ("" if key.scale == "chromatic" else f": {SC.scale_notes(key)}"))
                lines += ["  " + x for x in b.keys.describe(t).splitlines()]
                used = sorted({n.pitch for e in seq.sounding() for n in e.notes})
                if used:
                    nm = SC.namer(key)
                    lines.append("  Pattern notes: " + " ".join(nm(p) for p in used))
                    if key is not None and key.scale != "chromatic":
                        out = [p for p in used if not SC.in_scale(key, p)]
                        lines.append("  All in the scale" if not out else "  Outside the scale: "
                                     + " ".join(nm(p) for p in out) + " (the pattern keeps its notes when the scale "
                                     "changes)")
                else:
                    lines.append("  Pattern: no notes")
                return "\n".join(lines)

            tracks = [track] if track else [x.track for x in session.tracks()]
            text = "\n\n".join([await _run(one, t) for t in tracks])
            if list_scales:
                k0 = session.key(track or 1)
                root = k0.root if k0 else 0
                rows = [f"Scales the device offers, notes from {SC.NOTE_NAMES[root]}:"]
                from .model import Key

                for code, generic in b.keys.offered_scales():
                    k = Key(root, generic)
                    rows.append(f"  {code:<6} {generic:<26} {SC.intervals_text(SC.mask_of(generic)):<16} "
                                f"{SC.scale_notes(k)}")
                text += "\n\n" + "\n".join(rows)
            return text

    # ------------------------------------------------------------------ parameters
    if b.parameters is not None:
        @tool(RO)
        async def ostinato_parameters(track: int | None = None, only_changed: bool = False) -> str:
            """The device's parameters with names and values: a track's (sound, sequencer...) or, with track
            omitted, the global settings. only_changed: only those that differ from their defaults."""
            if (e := ready(track)):
                return e
            return await _run(b.parameters.parameters, track, only_changed)

    # ------------------------------------------------------------------ mixer
    if b.mixer is not None:
        @tool(WRITE)
        async def ostinato_mute_track(track: int, muted: bool = True) -> str:
            """Mute or unmute one track without changing its notes. muted: true mutes it, false unmutes it."""
            if (e := ready(track)):
                return e
            actual = await _run(session.set_track_muted, track, muted)
            return f"Track {track} is {'muted' if actual else 'unmuted'}."

    # ------------------------------------------------------------------ writing
    if b.writer is not None and b.reader is not None:
        def write_notes(track, notes_json, unit, div, mode, length, start_at, label, beats_only=False):
            seq_now = session.read(track)
            limits = b.writer.limits(track)
            kind = "drums" if seq_now.kind == "drums" else "melodic"
            cur_name = seq_now.grid.name if seq_now.grid else None
            if mode == "merge" and div is None:
                div = cur_name
            step = limits.grids.get(div) if div else (seq_now.grid.step if seq_now.grid else Fraction(1, 4))
            new = NIO.from_json(notes_json, kind, "beats" if beats_only else unit, step or Fraction(1, 4),
                                max(0, start_at - 1), limits.drum_voices)
            if mode == "merge":
                new.length = max(new.length, seq_now.length)
            fitted, notes = NIO.fit(new, limits, cur_name, div, length, round_to_beats=mode == "replace")
            if mode == "merge":
                fitted = NIO.merge(seq_now, fitted)
            rep = session.write(track, fitted, label)
            lines = [f"Wrote track {track}: {rep.changed} step(s) changed"
                     + (f", now {fitted.grid.steps} steps of {fitted.grid.name}" if fitted.grid and
                        (rep.length_changed or rep.grid_changed) else "") + ". Undo with ostinato_undo."]
            lines += [f"note: {x}" for x in (notes + rep.notes)[:10]]
            return "\n".join(lines) + "\n\n" + show(track, "list")

        @tool(WRITE)
        async def ostinato_write_notes(track: int, notes: list[dict], unit: Literal["steps", "beats"] = "steps",
                                  div: str | None = None, mode: Literal["replace", "merge"] = "replace",
                                  length: int | None = None, start_at: int = 1) -> str:
            """Write notes onto a track of the device (e.g. a melody you composed); ostinato_undo reverts.

            notes: [{"note": "E4" | 64 | ["C4","E4","G4"], "start": 1, "length": 2, "accent": false,
                     "slide": false, "velocity": 100, "level": "ghost|soft|normal|hard", "ratchet": 1-4}]
              drum tracks: {"lane": "KICK" | "SNARE" | "CLAP" | "HAT" | "OPEN HAT" | ... | GM note, "start": 1, ...}
            unit: "steps" (start / length in steps of the track's grid; start 1 = the first step) or "beats" (quarter
              notes; start 1 = the first beat: the step length is then chosen to hold every position).
            div: the step length to use (as the device names it, e.g. 1/16, 1/8, 8T).
            mode: "replace" (the pattern becomes exactly these notes, its length rounded up to whole beats) or
              "merge" (notes over the existing pattern; overlapped notes give way).
            length: the pattern's length in steps. start_at: shift everything to start at this step.
            The device's limits apply (notes at once, steps per pattern...): what had to be adapted is reported."""
            if (e := ready(track)):
                return e
            try:
                return await _run(write_notes, track, notes, unit, div, mode, length, start_at, "write notes")
            except (NIO.NotesError, ValueError) as ex:
                return f"Nothing written: {ex}"

        @tool(WRITE)
        async def ostinato_write_midi(track: int, path: str, midi_track: int | None = None, channel: int | None = None,
                                 div: str | None = None, mode: Literal["replace", "merge"] = "replace",
                                 length: int | None = None) -> str:
            """Write the notes of a Standard MIDI File (.mid) onto a track; ostinato_undo reverts. midi_track (0-based) /
            channel (1-16) choose which notes (default all; a drum track takes channel 10 if the file has it). The
            file starts on the bar of its first note; the step length is chosen to hold the notes unless `div`."""
            if (e := ready(track)):
                return e
            try:
                drums = (await _run(session.read, track)).kind == "drums"
                evs, info = await _run(NIO.midi_events, path, midi_track, channel, drums)
                out = await _run(write_notes, track, evs, "beats", div, mode, length, 1, f"midi {Path(path).name}",
                                 True)
                return (f"From {path}: {info['notes']} notes (channels {info['channels']}, from beat "
                        f"{info['from_beat']:g}).\n" + out)
            except FileNotFoundError:
                return f"No such file: {path}"
            except (NIO.NotesError, ValueError, OSError, EOFError) as ex:
                return f"Nothing written: {ex}"

        @tool(WRITE)
        async def ostinato_clear_track(track: int, keep_length: bool = True, note_only: str | None = None) -> str:
            """Clear a track: remove all its notes; ostinato_undo reverts.

            keep_length: keep the pattern's length and step length (default); false: back to one bar of 1/16.
            note_only: remove only this note, wherever it occurs (e.g. "A3", "F#2", "57"); in chords only that note
              goes. On a drum track it names the lane to empty: "HAT", "KICK", "SNARE", "OPEN HAT", "CLAP"... (or a
              GM note). With note_only the length is always kept.
            Refused while generative mode is armed on the track (disarm it first)."""
            if (e := ready(track)):
                return e
            try:
                rep, removed, what = await _run(session.clear, track, keep_length, note_only)
            except (NIO.NotesError, ValueError) as ex:
                return f"Nothing cleared: {ex}"
            if not rep.changed and not rep.length_changed and not rep.grid_changed:
                return f"Track {track}: nothing to clear ({what})."
            return (f"Cleared {what} on track {track}: {removed} note(s) removed, {rep.changed} step(s) changed. "
                    "Undo with ostinato_undo.\n\n" + await _run(show, track, "list"))

        @tool(WRITE)
        async def ostinato_undo(track: int) -> str:
            """Undo the last write to a track (written notes, a generative run, a restore). Repeat for earlier ones."""
            if (e := ready(track)):
                return e
            label = await _run(session.undo, track)
            if label is None:
                return f"Nothing to undo on track {track}."
            return (f"Undid \"{label}\" on track {track}; {session.undo_depth(track)} more undo step(s).\n\n"
                    + await _run(show, track, "list"))

        @tool(RO)
        async def ostinato_generators(algorithm: str | None = None, create_config: bool = False) -> str:
            """The generative algorithms, their setup files and the values in force; the gestures of generative
            mode; the controls this device offers. create_config: write the setup files from the defaults (if they
            do not exist yet) to edit them; a missing or broken file means the defaults, a wrong value its default."""
            from . import generative as G
            from .generative.engine import GestureSetup

            names = [algorithm] if algorithm else G.names()
            out = []
            for n in names:
                try:
                    cls = G.get(n)
                except KeyError as ex:
                    return str(ex)
                if create_config:
                    await _run(cls.write_user_config)
                conf = cls.load_config()
                out.append(f"{n}: {cls.description} (works on {' / '.join(cls.kinds)} tracks)")
                out.append(f"  setup: {conf.path} ({'exists' if conf.path.exists() else 'not created: defaults in use'}"
                           f"; values from {conf.source})")
                out += [f"  ! {x}" for x in conf.notes]
                if algorithm:
                    out.append("  values: " + json.dumps(conf.values, indent=2).replace("\n", "\n  "))
            if create_config:
                await _run(GestureSetup.write_user_config)
            g = GestureSetup.load_config()
            out.append(f"\ngestures (generative mode): {g.path} "
                       f"({'exists' if g.path.exists() else 'defaults in use'}): {json.dumps(g.values)}")
            if b.controls is not None:
                out.append("controls of this device: " + "; ".join(f"{n} ({k}): {d}" for n, k, d in b.controls.sources()))
            armed = ", ".join(f"track {a.track}: {a.algorithm} on {a.source} ({a.runs} runs)"
                              for a in session.engine.armed.values())
            out.append("armed: " + (armed or "none"))
            return "\n".join(out)

        @tool(WRITE)
        async def ostinato_generate(track: int, algorithm: str = "melodic_swapper", times: int = 1,
                               options: dict | None = None) -> str:
            """Run a generative algorithm on a track now (times: 1-16 runs in a row) and write the result; ostinato_undo
            reverts one run at a time. options: values that override the setup for this call, e.g.
            {"notes_per_trigger": {"2": 1}, "scale_source": 2}."""
            if (e := ready(track)):
                return e
            lines = []
            try:
                for k in range(max(1, min(16, times))):
                    r = await _run(session.engine.run_once, track, algorithm, options)
                    if k == 0:
                        lines.append(f"{algorithm} on track {track}" + (f", scale: {r['scale']}" if r["scale"] else "")
                                     + f" (setup: {r['config']})")
                        lines += [f"  ! {x}" for x in r["config_notes"]]
                    lines.append(f"  run {k + 1}: " + "; ".join(r["changes"]))
            except (KeyError, ValueError) as ex:
                return f"Not run: {ex}"
            return "\n".join(lines) + "\n\n" + await _run(show, track, "list")

        if b.controls is not None:
            @tool(WRITE)
            async def ostinato_generative_mode(track: int, on: bool, algorithm: str = "melodic_swapper",
                                          control: str | None = None, restore: bool = False,
                                          options: dict | None = None) -> str:
                """Arm (on=true) or disarm a track for generative mode: a control on the device runs the algorithm
                over the track's current pattern and writes the changes, while it plays.
                control: which of the device's controls (ostinato_generators lists them; default: its encoder, e.g. a
                knob). An encoder: turn clockwise to vary (a longer turn changes more), counter-clockwise to undo the
                runs. A button: each press runs once.
                Disarm (on=false) keeps the pattern as it is; restore=true brings back the one from before arming.
                options: setup overrides for this session (as ostinato_generate)."""
                if (e := ready(track)):
                    return e
                eng = session.engine
                if not on:
                    a = await _run(eng.disarm, track, restore)
                    if a is None:
                        return f"Track {track} was not armed."
                    return (f"Track {track} disarmed after {a.runs} run(s) of {a.algorithm}; "
                            + ("the pattern from before arming is back." if restore else "the pattern stays as it is.")
                            + "\n\n" + await _run(show, track, "list"))
                try:
                    a = await _run(eng.arm, track, algorithm, control, options)
                except (KeyError, ValueError) as ex:
                    return f"Not armed: {ex}"
                desc = next((d for n, k, d in b.controls.sources() if n == a.source), a.source)
                how = (f"turn {desc}: clockwise varies (longer turn = more notes), counter-clockwise "
                       + ("undoes runs." if a.gesture["left"] == "undo" else "varies too.")
                       if a.kind == "encoder" else f"press {desc}: one run per press.")
                return (f"Track {track} armed with {algorithm}: {how}\n" + "".join(f"! {w}\n" for w in a.warnings)
                        + f"Disarm with ostinato_generative_mode(track={track}, on=false); restore=true brings back the "
                          "original.")

    # ------------------------------------------------------------------ the backend's own
    for fn, meta in b.extra_tools():
        mcp.add_tool(fn, name=meta.get("name", fn.__name__), description=meta.get("description", fn.__doc__),
                     annotations=meta.get("annotations"))
        names.append(meta.get("name", fn.__name__))
    return names


def status_lines(session) -> str:
    """Generic additions to ostinato_status: what the session runs (generative mode)."""
    eng = session.engine
    if not eng or not eng.armed:
        return ""
    text = "\n\nGenerative mode: " + "; ".join(
        f"track {a.track} {a.algorithm} on {a.source} ({a.runs} runs, {a.depth} undoable)"
        + (f", last: {'; '.join(a.last)}" if a.last else "") for a in eng.armed.values())
    if eng.errors:
        text += "\nGenerative errors: " + "; ".join(eng.errors[-3:])
    return text
