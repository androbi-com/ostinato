# Ostinato

**Ostinato** lets Claude (Desktop and Code) read and write music on a device over USB-MIDI: today an **M-VAVE FM-1
running SLOOP**
([isod89/sloop-fm1](https://github.com/isod89/sloop-fm1)). Everything musical (sequences, scales, staffs, note
import, generative algorithms, gestures, undo) is generic; only getting things out of a device and into it is
device-specific, in a **backend** that offers what it can as **capabilities**.

## How it fits together

```
 Claude Desktop chat --stdio--> ostinato-mcp (shim) --+
 Claude Code session --stdio--> ostinato-mcp (shim) --+--HTTP 127.0.0.1:5317--> ostinato-device --USB MIDI--> FM-1
```

Inside `ostinato-device`:

```
 tools (generic)       ostinato_show_track, ostinato_show_staff, ostinato_write_notes, ostinato_generate, ostinato_undo ...
       |               offered only when the backend has the capability; + the backend's own extra tools
 session (generic)     the active backend, undo (Sequence snapshots), change log, generative engine
       |
 music (generic)       Sequence model, scales and spelling, text views, staffs, JSON / MIDI in, fitting to a
       |               device's limits, generative algorithms, gestures (turn = vary, turn back = undo)
 backends              fm1_sloop (FM-1 hardware with SLOOP firmware) . later: others (e.g. a DAW)
```

- **`ostinato-device`** is the device server: the only program that talks to the device. You start it yourself and it
  runs until you stop it. It finds the FM-1's MIDI port, asks each backend whether the firmware is its own, and runs
  that backend with a session; with nothing found it keeps looking every 3 s. It serves the tools over MCP at
  `http://127.0.0.1:5317/mcp`, on this computer only; every request needs the token it writes to `device.json` in
  the setup folder at start (Host and Origin are checked too, so web pages cannot reach it). One at a time.
- **`ostinato-mcp`** is what Claude starts: a stdio shim that knows nothing about devices. It gets the tools from the device
  server (and shows the list it saved last, `tools.json`, while it is not running), forwards each call, and tells
  Claude when the list changes. Claude Code does not refresh a running session's tools on that notice (tested): after
  the very first start of `ostinato-device`, or when the backend changes, restart Claude once.

## The generic model (`ostinato/model.py`)

- **Sequence**: a list of events, not a grid. An event has a start and a length in beats, one or more notes (pitch,
  velocity, level ghost/soft/normal/hard, ratchet), accent, slide, muted, a timing offset, and `extras`: the
  backend's own per-event data (SLOOP: fill condition, parameter locks), carried through untouched. A step sequencer
  reports an optional **Grid** (step length, number of steps). Kind: melodic, chords, drums; drum notes are General
  MIDI voices (36 kick, 38 snare, 42 closed hat...), mapped by each backend to its own sounds.
- **TrackInfo** (how many tracks and which kind: the backend says; SLOOP has 4), **Key** (root, generic scale name),
  **WriteLimits** (what a writer takes: step lengths, steps, notes at once, which articulations), **DeviceEvent**,
  **ControlEvent** (an encoder turn or a button press).

## Backends and capabilities (`ostinato/backends/base.py`)

A backend implements only what it can; the levels above check and degrade (no instrument name: a staff without part
names; no key: no key signature, sharps; no writer: no writing tools).

| Capability | What it gives | SLOOP |
| --- | --- | --- |
| `connection` | connect / disconnect / status / overview (always) | yes |
| `tracks` | the tracks and their kind | 4 (3 synth, drums) |
| `reader` / `writer` | a track's Sequence; write one within the device's WriteLimits | yes (steps <-> events) |
| `keys` | a track's key; the device's own key settings; the scales it offers | yes |
| `instruments` | a track's sound name | engine / preset, drum kit |
| `tempo` | tempo, time signature | yes |
| `changes` | notices of edits made on the device | yes (pushes) |
| `parameters` | named parameters with values | yes |
| `controls` | knobs / buttons usable as triggers (generative mode) | the PHS knob, the keys |

An external MIDI controller would be another implementation of `controls` (interface only for now). A hardware
backend also has `probe` (found on the MIDI ports); one that cannot be found that way (e.g. a DAW) would be chosen by
configuration. Adding a backend: a package `ostinato/backends/<name>/` with a `Backend` subclass, added to `BACKENDS` in
`ostinato/backends/__init__.py`.

## Install and run

Requires [uv](https://docs.astral.sh/uv/). In `%APPDATA%\Claude\claude_desktop_config.json`:

```json
"mcpServers": {
  "ostinato": {
    "command": "C:\\Users\\<you>\\.local\\bin\\uv.exe",
    "args": ["--directory", "C:\\path\\to\\ostinato-mcp", "run", "ostinato-mcp"]
  }
}
```

Start the device server in a terminal (Ctrl+C stops it and releases the device):

```
uv run --directory C:\path\to\ostinato-mcp ostinato-device
```

| Command | |
| --- | --- |
| `ostinato-device` | Run the device server here |
| `ostinato-device status` / `stop` | Is it running (which backend, connected?) / stop it from anywhere |
| `ostinato-device ui` | Open the browser UI of the running device server |
| `ostinato-device --port 5318` | Another HTTP port (or `OSTINATO_HTTP_PORT`) |
| `ostinato-device --midi-port Felucca` | Only MIDI ports whose name contains this (or `OSTINATO_MIDI_PORT`) |
| `ostinato-device --simulate sloop` | A simulated FM-1 with SLOOP (no hardware; or `OSTINATO_SIMULATE`) |

Other environment variables: `OSTINATO_CONFIG_DIR` (the setup folder, default `%APPDATA%\ostinato`), `OSTINATO_LOG`, and for SLOOP
`SLOOP_SELECT_FOR_DESC` (`1`, default: may select another track for a moment to read its engine's parameter names).

The setup folder holds `device.json` / `device.lock` (while the device server runs), `tools.json` (its last tool
list), `generators\` (the algorithms' setups and `gesture.json`) and one folder per backend (`fm1_sloop\knob.json`).

**One program talking to the device at a time.** The FM-1 answers one editor request at a time (with Windows MIDI
Services several programs can open its port, but each receives every reply). Before using the official SLOOP web
editor, release it with `ostinato_disconnect`; `ostinato_connect` takes it back (or Release / Reconnect in the UI).

## Browser UI

`ostinato-device ui` opens the device server's page in your browser. It shows:

- **Header**: the backend, firmware and port, the tempo, and **Release** / **Reconnect** for the device.
- **Tracks**: each track as a step roll (or a drum grid) or as a staff, live: edits on the device, writes from
  Claude and generative runs show up within a second. Per track: an algorithm and **Vary** (one run), **Undo**, and
  generative mode (**Arm** with a control such as the knob; **Disarm** keeps the notes, **Restore** goes back).
- **Settings**: a form for every setup (the algorithms, `gesture.json`, the backend's controls such as the knob),
  generated from the defaults files and their `_ui` hints. **Save** writes your file in the setup folder (checked:
  a bad value falls back to its default, and the card says so); **Reset to defaults** removes it.
- **Activity**: the change log (device edits, writes, runs, undos, connection).

The page is for this computer only. The server listens on 127.0.0.1 and checks the `Host` header (against DNS
rebinding). `ostinato-device ui` opens `/ui/login?key=<token>` (the token from `device.json`), which sets an
HttpOnly, SameSite=Strict cookie; the page and `/api/ui/*` need that cookie (or the bearer token), and every write
also needs the `X-Ostinato: 1` header and a same-origin `Origin`. The shim's routes (`/mcp`, `/api/status`...)
accept the bearer token only.

## Tools

All named `ostinato_*`; tracks are numbered from 1, notes with 60 = C4.

| Tool | Needs | What it does |
| --- | --- | --- |
| `ostinato_status`, `ostinato_connect`, `ostinato_disconnect`, `ostinato_list_midi_ports` | (the device server) | Status and overview; take / release the device; MIDI ports |
| `ostinato_show_track`, `ostinato_show_all_tracks` | reader | Text: `grid`, `list` (every detail), `roll` (piano roll); drums as a voice x step grid |
| `ostinato_show_staff`, `ostinato_export_musicxml` | reader | Notation as an image; the same score as MusicXML |
| `ostinato_track_data` | reader | A track's Sequence as structured data |
| `ostinato_refresh`, `ostinato_changes` | reader | Re-read from the device; the change log (device edits, writes, runs, undos) |
| `ostinato_scales` | keys | Key, scale notes, the device's key settings, notes outside the scale; the scales it offers |
| `ostinato_parameters` | parameters | A track's parameters or the globals |
| `ostinato_write_notes`, `ostinato_write_midi` | writer | JSON notes / a .mid file onto a track, fitted to the device's limits (what was adapted is reported) |
| `ostinato_undo` | writer | Undo the last write to a track (up to 30) |
| `ostinato_generators`, `ostinato_generate` | writer | The algorithms and their setups; run one now |
| `ostinato_generative_mode` | writer + controls | Arm a track: a control on the device drives the algorithm |
| `ostinato_link_info` | (SLOOP's own) | Diagnostics of the MIDI link |

A JSON note: `{"note": "E4" | 64 | ["C4","E4","G4"], "start": 1, "length": 2, "accent", "slide", "velocity",
"level", "ratchet"}`; drums `{"lane": "KICK" | GM note, ...}`; positions in steps of the track's grid or in beats
(`unit`), where the grid is chosen to hold every position (the current one, else the finest that does).

## Generative algorithms and gestures (`ostinato/generative/`)

One module per algorithm (a `Generator` with `@register`) and its defaults in `generative/defaults/<name>.json`, also
the schema. Your setup: `%APPDATA%\ostinato\generators\<name>.json` (`ostinato_generators create_config` writes it): a missing or
broken file means the defaults, a wrong value its default, and the tools say which.

**melodic_swapper** swaps 1-3 notes per run (`notes_per_trigger`; times `1 + intensity_gain x (intensity - 1)` for a
longer turn) for new notes of the scale, keeping start, length, articulations and the device's extras. The scale is
the track's own key or, on a chromatic track, borrowed from another track (`scale_source`: auto / own / a track); with
no key at all, the pitch classes of the pattern. New notes stay near the pattern's range (`range`) and the previous
note (`max_leap`, `motion`), favouring root / third / fifth (`tone_weights`); before a root or third, with
`transition.probability`, a chromatic approach note.

**art_mutate, art_spice, art_dice** rebuild the generative functions of a well-known keyboard step sequencer
(`generative/art.py`):
- **art_mutate** changes one segment of the pattern (`probability` x a 16-step phrase; on long patterns only the B / C
  phrases, for an A-B-A-C form): `random` of its notes move by the pattern's own material (another of its notes, an
  octave, a fifth, a scale step, a swap), some change length, `polyphony` stacks thirds / fifths, `density` adds or
  removes notes. Cumulative; a longer knob turn changes more. It also rolls a new spice pattern.
- **art_spice** randomizes gate lengths, ratchets and silences by `amount` (0..1), non-destructively: it always works
  from the pattern before spice, the same amount gives the same changes, amount 0 gives the pattern back. Drums too.
- **art_dice** rolls a new spice pattern at the track's last amount; the notes stay.

**Gestures** (`gesture.json`), for any control: an encoder turn ends after `settle_ms`; clockwise runs with intensity =
increments / `units_per_level`; counter-clockwise undoes that many runs of the session (`left`: undo / vary). A
button press runs once (`debounce_ms`: a chord counts once).

**SLOOP's controls**: `knob` (`fm1_sloop\knob.json`: the PHS knob of the LFO page, parked at 64 while in use, put back
to the centre after a pause far from it, the original value back afterwards; the firmware reports knob turns only for
the selected track, so select the armed track on the FM-1) and `keys` (any key; it still sounds, and records with REC
on).

## Notes on SLOOP

- The firmware pushes edits for the **selected** track only (and level/pan/mute of the others): `ostinato_refresh`
  re-reads another track.
- ROOT, SCL, QNT, TRN and CHORD act on the keys as they are played: the steps hold the notes that sound. CHORD (or
  QNT WHITE) on a chromatic track builds in natural minor: that is the track's key.
- Writes go to the device's RAM: save the project on the FM-1 to keep them.

## Development

```
uv run pytest                       # unit tests, the SLOOP backend on its simulator, the device server and the shim
uv run python tools/hw_check.py     # the SLOOP backend on the real FM-1 (stop ostinato-device first)
```

| Path (`src/ostinato/`) | |
| --- | --- |
| `model.py` | The generic model |
| `music/` | `scales.py`, `views.py` (text), `notation.py` (staffs), `notes_io.py` (JSON / MIDI in, fit), `drums.py` (GM voices) |
| `generative/` | `base.py`, `melodic_swapper.py`, `art.py` (art_mutate / spice / dice), `engine.py` (runs, generative mode, gestures), `defaults/` |
| `session.py`, `tools.py` | The session (undo, change log, engine); the generic tools by capability |
| `setup.py`, `config.py` | JSON setups (defaults + yours, checked); the setup folder |
| `backends/base.py`, `backends/__init__.py` | The backend interface and capabilities; the known backends |
| `backends/fm1_sloop/` | `backend.py` (capabilities, steps <-> events), `keys.py`, `controls.py`, `protocol.py`, `link.py`, `device.py` (sync, writes), `state.py`, `render.py` (overview, parameters), `sim.py` + `data/` (simulator) |
| `device_server.py`, `shim.py`, `midi.py`, `owner.py` | `ostinato-device`, `ostinato-mcp`, MIDI ports, the single-instance lock |
| `ui_server.py`, `ui/` | The browser UI: the access gate, the `/api/ui/*` routes; the page (`index.html`, `app.js`, `style.css`) |
