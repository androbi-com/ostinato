# Ostinato: technical notes

Ostinato lets Claude (Desktop and Code) and a browser page read and write music on a device over USB-MIDI: today an
**M-VAVE FM-1 running SLOOP** ([isod89/sloop-fm1](https://github.com/isod89/sloop-fm1)). Everything musical
(sequences, scales, staffs, note import, generative algorithms, gestures, undo, saved states) is generic; only getting
things out of a device and into it is device-specific, in a **backend** that offers what it can as **capabilities**.

## How it fits together

```
 Claude Desktop chat --stdio--> ostinato-mcp (shim) --+
 Claude Code session --stdio--> ostinato-mcp (shim) --+--HTTP 127.0.0.1:5317--> ostinato-device --USB MIDI--> FM-1
 browser (ostinato-device ui) ------------------------+
```

Inside `ostinato-device`:

```
 tools (generic)       ostinato_show_track, ostinato_write_notes, ostinato_generate, ostinato_save_state ...
 ui_server (generic)   the browser page and /api/ui/*
       |               both offer only what the backend's capabilities allow; + the backend's own extra tools
 session (generic)     the active backend, undo (Sequence snapshots), change log, generative engine, saved states
       |
 music (generic)       Sequence model, scales and spelling, text views, staffs, JSON / MIDI in, fitting to a
       |               device's limits, generative algorithms, gestures (turn = vary, turn back = undo)
 backends              fm1_sloop (FM-1 hardware with SLOOP firmware) . later: others (e.g. a DAW)
```

- **`ostinato-device`** is the device server: the only program that talks to the device. You start it yourself and
  it runs until you stop it. It finds the FM-1's MIDI port, asks each backend whether the firmware is its own, and
  runs that backend with a session; with nothing found it keeps looking every 3 s. It serves the tools over MCP at
  `http://127.0.0.1:5317/mcp` and the browser page, on this computer only; every request needs the token it writes
  to `device.json` in the setup folder at start (Host and Origin are checked too, so web pages cannot reach it). A
  new token is made at every start.
- **`ostinato-mcp`** is what Claude starts: a stdio shim that knows nothing about devices. It gets the tools from the
  device server (and shows the list it saved last, `tools.json`, while it is not running), forwards each call, and
  tells Claude when the list changes. Claude Code does not refresh a running session's tools on that notice (tested):
  after the very first start of `ostinato-device`, after an update that adds tools, or when the backend changes,
  restart Claude once.

## The generic model (`ostinato/model.py`)

- **Sequence**: a list of events, not a grid. An event has a start and a length in beats, one or more notes (pitch,
  velocity, level ghost/soft/normal/hard, ratchet), accent, slide, muted, a timing offset, and `extras`: the
  backend's own per-event data (SLOOP: fill condition, parameter locks), carried through untouched. A step sequencer
  reports an optional **Grid** (step length, number of steps). Kind: melodic, chords, drums; drum notes are General
  MIDI voices (36 kick, 38 snare, 42 closed hat...), mapped by each backend to its own sounds.
- **TrackInfo** (how many tracks and which kind: the backend says; SLOOP has 4), **Key** (root, generic scale name),
  **WriteLimits** (what a writer takes: step lengths, steps, notes at once, which articulations), **WriteReport**,
  **MixValue** (a level or pan on the device's own scale, with its text and, for a level in dB, where 0 dB is and dB
  per unit), **DeviceEvent**, **ControlEvent** (an encoder turn or a button press).

## Backends and capabilities (`ostinato/backends/base.py`)

A backend implements only what it can; the levels above check and degrade (no instrument name: a staff without part
names; no key: no key signature, sharps; no writer: no writing tools; no transport: no play button).

| Capability | Interface | What it gives | SLOOP |
| --- | --- | --- | --- |
| `connection` | Connection | connect / disconnect / status / overview (always) | yes |
| `tracks` | Tracks | the tracks and their kind | 4 (3 synth, drums) |
| `reader` / `writer` | SequenceReader / SequenceWriter | a track's Sequence; write one within the device's WriteLimits | yes (steps <-> events) |
| `keys` | KeyInfo | a track's key; the device's own key settings; the scales it offers | yes |
| `instruments` | InstrumentInfo | a track's sound name | engine + preset, drum kit |
| `tempo` | TempoInfo | tempo, time signature | yes |
| `changes` | ChangeFeed | notices of edits made on the device | yes (pushes) |
| `parameters` | ParameterInfo | named parameters with values | yes |
| `controls` | ControlInputs | knobs / buttons usable as triggers (generative mode) | the PHS knob, the keys |
| `mixer` | TrackMixer | a track's mute, level and pan (as MixValues) | yes |
| `sounds` | SoundSelector | the sounds a track can take, grouped (e.g. by engine); set one by name | factory presets, kits |
| `states` | StateStore | the device's whole state as JSON-able data, written back by part; optionally an exact copy | yes |
| `transport` | TransportControl | play / stop / toggle, and whether it plays | protocol 12+ |

An external MIDI controller would be another implementation of `controls` (interface only for now). A hardware
backend also has `probe` (found on the MIDI ports); one that cannot be found that way (e.g. a DAW) would be chosen by
configuration. Adding a backend: a package `ostinato/backends/<name>/` with a `Backend` subclass, added to `BACKENDS`
in `ostinato/backends/__init__.py`.

## Install and run

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+ (uv provides it). `uv sync` in the checkout; again after
pulling changes. In `%APPDATA%\Claude\claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`):

```json
"mcpServers": {
  "ostinato": {
    "command": "C:\\Users\\<you>\\.local\\bin\\uv.exe",
    "args": ["--directory", "C:\\path\\to\\ostinato", "run", "ostinato-mcp"]
  }
}
```

Start the device server in a terminal (Ctrl+C stops it and releases the device):

```
uv run --directory C:\path\to\ostinato ostinato-device
```

| Command | |
| --- | --- |
| `ostinato-device` | Run the device server here |
| `ostinato-device status` / `stop` | Is it running (which backend, connected?) / stop it from anywhere |
| `ostinato-device ui` | Open the browser UI of the running device server |
| `ostinato-device --port 5318` | Another HTTP port (or `OSTINATO_HTTP_PORT`) |
| `ostinato-device --midi-port Felucca` | Only MIDI ports whose name contains this (or `OSTINATO_MIDI_PORT`) |
| `ostinato-device --simulate [sloop]` | A simulated FM-1 with SLOOP (no hardware; or `OSTINATO_SIMULATE`) |

Other environment variables: `OSTINATO_CONFIG_DIR` (the setup folder, default `%APPDATA%\ostinato`, elsewhere
`~/.config/ostinato`), `OSTINATO_LOG` (log level), and for SLOOP `SLOOP_SELECT_FOR_DESC` (`1`, default: may select
another track for a moment to read its engine's parameter names).

The setup folder holds `device.json` / `device.lock` (while the device server runs), `tools.json` (its last tool
list), `generators\` (the algorithms' setups and `gesture.json`), `states\` (saved states) and one folder per backend
(`fm1_sloop\knob.json`).

**One program talking to the device at a time.** The FM-1 answers one editor request at a time (with Windows MIDI
Services several programs can open its port, but each receives every reply). Before using the official SLOOP web
editor, release it with `ostinato_disconnect`; `ostinato_connect` takes it back (or Release / Reconnect in the UI).

If the checkout is moved or renamed, uv's launchers in `.venv\Scripts` still point at the old path ("Failed to
canonicalize script path"): `uv sync --reinstall`, or `uv run python -m pytest` meanwhile.

## Browser UI

`ostinato-device ui` opens the device server's page in your browser. It shows:

- **Header**: the backend, firmware and port, **▶ Play / ■ Stop** (with the `transport` capability; it follows the
  device's own PLAY button), the tempo, and **Release** / **Reconnect** for the device.
- **Tracks**: each track as a step roll (or a drum grid) or as a staff, live: edits on the device, writes from
  Claude and generative runs show up within a second. Per track: the **sound menu** (presets grouped by engine; kits
  on the drum track), **Mute**, **Level** and **Pan** sliders (sent when let go; double-click Pan to centre it), an
  algorithm and **Vary** (one run), **Undo**, **Clear** (asks first), and generative mode (**Arm** with a control
  such as the knob; **Disarm** keeps the notes, **Restore** goes back).
- **States**: save the device now (name, note, with or without the exact copy); the saved states (Load, Delete); the
  loaded state with its comparison with the device, track and part checkboxes, the exact-copy option and **Apply to
  the device** (asks first).
- **Settings**: a form for every setup (the algorithms, `gesture.json`, the backend's controls such as the knob),
  generated from the defaults files and their `_ui` hints. **Save** writes your file in the setup folder (checked:
  a bad value falls back to its default, and the card says so); **Reset to defaults** removes it.
- **Activity**: the change log (device edits, writes, runs, undos, sounds, mix, states, play / stop, connection).

The page is for this computer only. The server listens on 127.0.0.1 and checks the `Host` header (against DNS
rebinding). `ostinato-device ui` opens `/ui/login?key=<token>` (the token from `device.json`), which sets an
HttpOnly, SameSite=Strict cookie; the page and `/api/ui/*` need that cookie (or the bearer token), and every write
also needs the `X-Ostinato: 1` header and a same-origin `Origin`. The shim's routes (`/mcp`, `/api/status`...)
accept the bearer token only. After a restart of the device server the token changes: `ostinato-device ui` again.

## Tools

All named `ostinato_*`; tracks are numbered from 1, notes with 60 = C4.

| Tool | Needs | What it does |
| --- | --- | --- |
| `ostinato_status`, `ostinato_connect`, `ostinato_disconnect`, `ostinato_list_midi_ports` | (the device server) | Status and overview (incl. playback); take / release the device; MIDI ports |
| `ostinato_show_track`, `ostinato_show_all_tracks` | reader | Text: `grid`, `list` (every detail), `roll` (piano roll); drums as a voice x step grid |
| `ostinato_show_staff`, `ostinato_export_musicxml` | reader | Notation as an image; the same score as MusicXML |
| `ostinato_track_data` | reader | A track's Sequence as structured data |
| `ostinato_refresh`, `ostinato_changes` | reader | Re-read from the device; the change log |
| `ostinato_scales` | keys | Key, scale notes, the device's key settings, notes outside the scale; the scales it offers |
| `ostinato_parameters` | parameters | A track's parameters or the globals |
| `ostinato_mute_track` | mixer | Mute or unmute one track (its notes stay) |
| `ostinato_set_level` | mixer | Level (`-12dB`, `75%`, `off`, a number; `relative` for `+3dB` / `-10%`) and pan (`center`, `L30`, `R50`, `-30%`); without values: the mix of every track |
| `ostinato_instruments`, `ostinato_set_instrument` | sounds | The sounds a track can take (by engine; kits on drums); set one by name (`RHODES`, `DIGITAL / RHODES`, `909`), with suggestions for near misses |
| `ostinato_play` | transport | `play`, `stop`, `toggle` (as PLAY), `status` |
| `ostinato_write_notes`, `ostinato_write_midi` | writer | JSON notes / a .mid file onto a track, fitted to the device's limits (what was adapted is reported) |
| `ostinato_clear_track` | writer | Remove all notes (length and step length kept, or `keep_length=false`: one bar of 1/16), or only one note (`note_only="A3"`) / one drum lane (`"HAT"`) |
| `ostinato_undo` | writer | Undo the last write to a track (notes: writes, clears, runs, applied states; up to 30) |
| `ostinato_generators`, `ostinato_generate` | writer | The algorithms and their setups; run one now |
| `ostinato_generative_mode` | writer + controls | Arm a track: a control on the device drives the algorithm |
| `ostinato_save_state`, `ostinato_list_states`, `ostinato_load_state`, `ostinato_show_state`, `ostinato_delete_state` | reader | Saved states: see below |
| `ostinato_apply_state` | reader + writer | Put the loaded state on the device, whole or in part |
| `ostinato_link_info` | (SLOOP's own) | Diagnostics of the MIDI link |

A JSON note: `{"note": "E4" | 64 | ["C4","E4","G4"], "start": 1, "length": 2, "accent", "slide", "velocity",
"level", "ratchet"}`; drums `{"lane": "KICK" | GM note, ...}`; positions in steps of the track's grid or in beats
(`unit`), where the grid is chosen to hold every position (the current one, else the finest that does).

What can and cannot be undone: `ostinato_undo` restores a track's **notes** (Sequence snapshots). Sound, level, pan,
mute and play are not undoable: their tools report the previous value, so it can be set again. A clear is refused
while generative mode is armed on the track.

## Saved states (`ostinato/states.py`, `backends/fm1_sloop/states.py`)

The device's storage is small, so the whole state goes to a file on the computer: `<setup folder>\states\<name>.json`
(a name), or any `.json` path.

```
ostinato_state  1                      the format
name, note, saved_at, device {backend, title, firmware}, tempo
tracks   [ {track, name, kind, instrument, key, level, pan, muted, sequence} ]   generic: any device
backend  { ... }                       the backend's own data (StateStore.capture)
exact    {format, length, base64} | null   the device's own copy of its project
```

- **Save** re-reads the device first (the firmware pushes only the selected track's edits).
- **Load** only reads the file into the session (`Session.loaded`): `ostinato_load_state` shows what it holds and
  compares it with the device per track (sound, level, pan, mute, notes); `ostinato_show_state` shows its notes.
- **Apply** writes only what differs, by `tracks` and `parts`:

  | Part | SLOOP |
  | --- | --- |
  | `notes` | the steps / drum steps, LEN / DIV / SWG / GATE, parameter locks, nudges, fill conditions |
  | `sound` | the engine and preset (PRESET, when they differ), then every other track parameter (sound, arp, key) |
  | `mix` | level, pan, mute (the drum track's level is the global drum level) |
  | `globals` | the global settings (not the ones that are actions: ENG, LOAD, SAVE, SET, CLRSQ, INIT, NEW, SECT), the selected track |

  It works while the song plays. The device's state is saved as `before apply` first (pass `backup=False` to
  `Session.apply_state` to skip), and the notes of each changed track go on its undo stack. If the backend data does
  not fit the device (another firmware layout: steps, parameter or global counts), only the generic part is used:
  the notes (fitted as any write) and the sounds by name. A state from another kind of device is refused.
- **Exact**: `exact=True` puts back the device's own copy instead, every track and everything at once; refused while
  the song plays (checked before the backup is taken).

Not in a state: SLOOP's song sections and song order (flash slots and settings), user samples, FM6 patch banks, user
presets; solo is saved for reference only (nothing can set it from outside).

## Generative algorithms and gestures (`ostinato/generative/`)

One module per algorithm (a `Generator` with `@register`) and its defaults in `generative/defaults/<name>.json`, also
the schema. Your setup: `%APPDATA%\ostinato\generators\<name>.json` (`ostinato_generators create_config` writes it):
a missing or broken file means the defaults, a wrong value its default, and the tools say which. All parameters:
[ALGORITHMS.md](ALGORITHMS.md).

**melodic_swapper** swaps 1-3 notes per run (`notes_per_trigger`; times `1 + intensity_gain x (intensity - 1)` for a
longer turn) for new notes of the scale, keeping start, length, articulations and the device's extras. The scale is
the track's own key or, on a chromatic track, borrowed from another track (`scale_source`: auto / own / a track);
with no key at all, the pitch classes of the pattern. New notes stay near the pattern's range (`range`) and the
previous note (`max_leap`, `motion`), favouring root / third / fifth (`tone_weights`); before a root or third, with
`transition.probability`, a chromatic approach note.

**random_initializer** replaces a sequence with random notes on its existing grid. `density` controls how many
steps receive notes, `note_range` and `length_steps` constrain pitch and gate lengths, and `max_leap` shapes the
line. Notes stay in the track's scale or a borrowed scale (`scale_source`); chord tracks can stack an in-scale third
or fifth (`polyphony`).

**art_mutate, art_spice, art_dice** rebuild the generative functions of a well-known keyboard step sequencer
(`generative/art.py`):
- **art_mutate** changes one segment of the pattern (`probability` x a 16-step phrase; on long patterns only the B /
  C phrases, for an A-B-A-C form): `random` of its notes move by the pattern's own material (another of its notes, an
  octave, a fifth, a scale step, a swap), some change length, `polyphony` stacks thirds / fifths, `density` adds or
  removes notes. Cumulative; a longer knob turn changes more. It also rolls a new spice pattern.
- **art_spice** randomizes gate lengths, ratchets and silences by `amount` (0..1), non-destructively: it always works
  from the pattern before spice, the same amount gives the same changes, amount 0 gives the pattern back. Drums too.
- **art_dice** rolls a new spice pattern at the track's last amount; the notes stay.

**Gestures** (`gesture.json`), for any control: an encoder turn ends after `settle_ms`; clockwise runs with intensity
= increments / `units_per_level`; counter-clockwise undoes that many runs of the session (`left`: undo / vary). A
button press runs once (`debounce_ms`: a chord counts once).

**SLOOP's controls**: `knob` (`fm1_sloop\knob.json`: the PHS knob of the LFO page, parked at 64 while in use, put
back to the centre after a pause far from it, the original value back afterwards; the firmware reports knob turns
only for the selected track, so select the armed track on the FM-1) and `keys` (any key; it still sounds, and
records with REC on).

## Notes on SLOOP

The editor protocol (SysEx `F0 7D 46 4C <cmd> ... F7`, every data byte 7 bit) is documented in SLOOP's
`web/EDITOR_PROTOCOL.md`; `backends/fm1_sloop/protocol.py` ports the web editor's own encoders and parsers.

- **Pushes**: the firmware pushes edits for the **selected** track only (and level / pan / mute of the others, with
  `WATCH 3`): `ostinato_refresh` re-reads another track. Our own writes are never echoed.
- **Keys**: ROOT, SCL, QNT, TRN and CHORD act on the keys as they are played: the steps hold the notes that sound.
  CHORD (or QNT WHITE) on a chromatic track builds in natural minor: that is the track's key.
- **Sounds**: `PRESET` (cmd 8) loads a factory preset onto the **selected** track only: Ostinato selects the target
  for a moment and puts the selection back. It replaces the sound, sends and arp, not the pattern, mix or key; a
  later reload of the same preset brings its defaults, not your tweaks. The preset names come from `NAMES` (cmd 10)
  at connect. The drum kit is the drum track's `P_E0` (enum KIT), written with `TRACK_PARAM`.
- **Mix**: level through `TRACK_MIX` (cmd 28) on any track without selecting it. Synth level is `P_LEVEL` (0..127:
  112 = 0 dB, 0.5 dB a step, 0 = OFF); the drum track's level is the global drum level (`G_DRLVL`, GLO > DRUMS LEVEL,
  0..127, no dB scale; its own `P_LEVEL` is unused). Pan is `P_PAN` (-64..63).
- **Playback**: `LIVE` (cmd 77, protocol 12): op 1 with 0 stop / 1 play / 2 toggle; op 0 returns the live state,
  whose first byte has bit 0 = playing (bit 2 song mode). Play / stop is not pushed: the keeper thread asks every
  0.5 s, which also keeps `WATCH` alive in place of PING. In song mode with an empty section, play is refused.
- **Saved states**: locks are written one at a time (`LOCK_SET`, 24 a track), nudges (`MICRO_SET`) and fill
  conditions (`FILL_SET`) one step at a time, globals with `SET` scope 1. The exact copy is backup object 0 (the
  working project, a 3816-byte `project_t` on 2.8): `BK_LIST`, then `BK_GET` in pieces of 256 bytes, checked against
  the listed CRC-32; written back with `BK_PUT` begin / data / commit, loaded at once, only while stopped (rc 3).
  Needs protocol 6+ (SLOOP 2.3).
- **RAM**: every write goes to the device's RAM: save the project on the FM-1 (or a state in Ostinato) to keep it.
- **Threads**: the device's push worker logs into the session while holding the device's lock, so the session's log
  has a lock of its own (never take the session's write lock inside a device callback).

## Development

```
uv run pytest                       # unit tests, the SLOOP backend on its simulator, the device server and the shim
uv run python tools/hw_check.py     # the SLOOP backend on the real FM-1 (stop ostinato-device first)
```

The simulator (`backends/fm1_sloop/sim.py`) is seeded from the SLOOP web editor's own mock device
(`data/editor_mock.json`, dumped by `tools/dump_editor_mock.mjs`) and models the writes, pushes, `PRESET`,
`TRACK_MIX`, `LIVE`, locks / nudges / fills, globals and the backup objects (its exact copy is its own JSON, not a
`project_t`). `--simulate` runs the whole device server on it.

| Path (`src/ostinato/`) | |
| --- | --- |
| `model.py` | The generic model |
| `music/` | `scales.py`, `views.py` (text), `notation.py` (staffs), `notes_io.py` (JSON / MIDI in, fit), `drums.py` (GM voices) |
| `generative/` | `base.py`, `melodic_swapper.py`, `random_initializer.py`, `art.py` (art_mutate / spice / dice), `engine.py` (runs, generative mode, gestures), `defaults/` |
| `session.py`, `tools.py` | The session (undo, change log, engine, mix, sounds, playback, saved states); the generic tools by capability |
| `states.py` | Saved-state files: format, Sequence <-> JSON, list / read / write / delete |
| `setup.py`, `config.py` | JSON setups (defaults + yours, checked); the setup folder |
| `backends/base.py`, `backends/__init__.py` | The backend interface and capabilities; the known backends |
| `backends/fm1_sloop/` | `backend.py` (capabilities, steps <-> events), `states.py` (capture / apply), `keys.py`, `controls.py`, `protocol.py`, `link.py`, `device.py` (sync, writes, presets, mix, LIVE, backup objects), `state.py`, `render.py` (overview, parameters), `sim.py` + `data/` (simulator) |
| `device_server.py`, `shim.py`, `midi.py`, `owner.py` | `ostinato-device`, `ostinato-mcp`, MIDI ports, the single-instance lock |
| `ui_server.py`, `ui/` | The browser UI: the access gate, the `/api/ui/*` routes; the page (`index.html`, `app.js`, `style.css`) |

| Tests (`tests/`) | |
| --- | --- |
| `test_music.py` | Scales and spelling, notes in, fitting to a device's limits, text views, staffs |
| `test_generative.py`, `test_art.py` | The algorithms and gestures; the session on SLOOP (undo, clear, sounds, mix) |
| `test_protocol.py`, `test_backend_sloop.py`, `test_sync.py` | SLOOP: encoders / parsers, the capabilities on the simulator, pushes |
| `test_states.py` | Saved states: save, load, compare, apply whole / in part / exactly, undo |
| `test_daemon.py`, `test_owner.py`, `test_ui.py` | The device server and shim, the single-instance lock, the page's routes |
