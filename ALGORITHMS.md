# Generative Algorithms

Ostinato currently provides six registered generative algorithms. Algorithm setups are JSON files in
`%APPDATA%\ostinato\generators\` on Windows (or `~/.config/ostinato/generators/` on other platforms). Set
`OSTINATO_CONFIG_DIR` to use a different setup folder.

Missing or invalid setup files use the shipped defaults. Values with the wrong JSON type or out of range fall back
to a default or are clamped; the UI and `ostinato_generators` report validation notes. Keys beginning with `_` are
documentation fields, not parameters.

They work on every device, the virtual one included. Run an algorithm with **Vary** / **Initialize** on the page or with
`ostinato_generate`, or, on a device with controls (the FM-1), arm it on a track with `ostinato_generative_mode`. The list
starts with the two that replace the pattern. Direct runs and
knob/button runs write a result back to the track and remain undoable. The optional `seed` parameter makes a
randomized result repeatable; `null` makes the run vary.

## `melodic_swapper`

**Track kinds:** melodic and chord tracks.

Swaps selected notes in a melodic line for scale-based alternatives while keeping event timing, length,
articulation, velocity, levels, ratchets, and backend-specific data. Chord events can change the highest note or be
skipped, depending on `chords`.

| Parameter | Default | Description / accepted values |
|---|---:|---|
| `notes_per_trigger` | `{"1": 0.6, "2": 0.3, "3": 0.1}` | Open table of note counts to probabilities. Each key is a count and is normalised with the other weights; the result is capped at the eligible notes. |
| `intensity_gain` | `0.5` | Range `0..4`. Multiplies the drawn note count by `1 + intensity_gain * (intensity - 1)`. Direct runs use intensity 1. |
| `scale_source` | `"auto"` | `"own"` uses the track's own scale, `"auto"` uses it or borrows the first usable scale from another track, and an integer uses that track number. |
| `fallback_scale` | `"pattern"` | When no usable scale exists, `"pattern"` infers a scale from at least three pitch classes in the pattern, or `"chromatic"` is used. |
| `keep_first_note` | `true` | Never changes the pattern's first sounding event. |
| `chords` | `"top"` | `"top"` changes the highest note of a chord; `"skip"` leaves chord events unchanged. |
| `avoid_repeat` | `true` | Prevents a selected note from being replaced with the same pitch. |
| `range.below` | `2` | Range `0..24`: semitones below the pattern's lowest pitch at which new notes may be chosen. |
| `range.above` | `3` | Range `0..24`: semitones above the pattern's highest pitch at which new notes may be chosen. |
| `max_leap` | `7` | Range `1..24`: maximum interval in semitones from the preceding note. |
| `motion.step` | `0.55` | Weight for intervals of 1-2 semitones from the preceding note. |
| `motion.skip` | `0.3` | Weight for intervals of 3-4 semitones. |
| `motion.leap` | `0.15` | Weight for intervals of 5 or more semitones. |
| `tone_weights.root` | `1.5` | Relative weight for root notes. |
| `tone_weights.third` | `1.3` | Relative weight for scale thirds. |
| `tone_weights.fifth` | `1.2` | Relative weight for scale fifths. |
| `tone_weights.other` | `1.0` | Relative weight for other scale tones. |
| `transition.probability` | `0.25` | Range `0..1`: chance to turn a selected note into a chromatic approach. |
| `transition.targets` | `["root", "third"]` | Targets are `root`, `third`, and/or `fifth`; the note is approaching the following line note. |
| `transition.approach.below` | `0.6` | Relative weight for placing the approach one semitone below the target. |
| `transition.approach.above` | `0.4` | Relative weight for placing it one semitone above the target. |
| `seed` | `null` | `null` for a fresh random sequence; otherwise a number for repeatable results. |

## `random_initializer`

**Track kinds:** melodic and chord tracks.

Replaces the sequence's events with random notes on the existing step grid. The generated pitches stay within the
configured MIDI range and in the selected track or borrowed scale.

| Parameter | Default | Description / accepted values |
|---|---:|---|
| `density` | `0.5` | Range `0..1`: share of sequence steps that receive a note. `0` creates an empty pattern. |
| `intensity_gain` | `0.1` | Range `0..1`: density added for each intensity level after the first in generative mode. |
| `scale_source` | `"auto"` | `"own"`, `"auto"`, or an integer track number for a borrowed scale. |
| `fallback_scale` | `"chromatic"` | `"pattern"` infers a scale from existing notes when possible; `"chromatic"` allows all notes when no scale is available. |
| `note_range.low` | `60` | Range `0..127`: lowest generated MIDI pitch (inclusive). |
| `note_range.high` | `78` | Range `0..127`: highest generated MIDI pitch (inclusive). |
| `max_leap` | `12` | Range `1..127`: preferred largest interval from the preceding note, when the scale and range allow it. |
| `avoid_repeat` | `true` | Avoids immediately repeating a pitch when another nearby in-scale pitch is available. |
| `length_steps.min` | `1` | Range `1..32`: shortest generated note length in grid steps. |
| `length_steps.max` | `2` | Range `1..32`: longest generated note length in grid steps. Notes are shortened before the next occupied step. |
| `polyphony` | `0` | Range `0..1`: chance for a chord track event to add an in-scale third or fifth. |
| `seed` | `null` | `null` for a fresh random sequence; otherwise a number for repeatable results. |

## `initialize_fotf`

**Track kinds:** drum tracks.

Replaces the pattern with a simple four on the floor: the bass drum on every beat of the track's time signature
(quarters; dotted quarters in 6/8, 9/8 and 12/8; a 3/8 bar is one beat), nothing else. The pattern's length and grid
stay. Every bar starts with a kick. It has no parameters.

## `art_mutate`

**Track kinds:** melodic and chord tracks.

Mutates a segment of a pattern and keeps its musical identity. Runs are cumulative: each run changes the result of
the previous run, and normal undo restores the previous result.

For patterns longer than `phrase_steps`, the algorithm chooses a phrase segment. With `phrases: "abac"`, it selects
only the even-numbered phrases (B and C) where an A-B-A-C pattern is detected. With `"all"`, any phrase can be
selected.

| Parameter | Default | Description / accepted values |
|---|---:|---|
| `probability` | `0.5` | Range `0..1`: segment size as a share of one phrase. |
| `random` | `0.4` | Range `0..1`: share of notes in the segment that change. |
| `polyphony` | `0.1` | Range `0..1`: probability that a changed note gains a third or fifth. |
| `density` | `null` | Range `0..1` or `null`: target share of segment steps holding notes. `null` does not add or remove notes. |
| `length` | `0.25` | Range `0..1`: probability that a changed note also gets a new length. |
| `operations.pattern_note` | `0.35` | Weight for choosing another pitch already used by the pattern. |
| `operations.octave` | `0.2` | Weight for shifting a pitch an octave up or down. |
| `operations.fifth` | `0.15` | Weight for shifting a pitch by an in-scale fifth up or fourth down. |
| `operations.step` | `0.15` | Weight for moving one scale step up or down. |
| `operations.swap` | `0.15` | Weight for swapping notes with another event in the segment. |
| `intensity_gain` | `0.6` | Range `0..4`: widens both the segment and changed-note share by `1 + gain * (intensity - 1)`. |
| `phrase_steps` | `16` | Range `2..64`: steps per phrase. |
| `phrases` | `"abac"` | `"abac"` targets B/C phrases in long patterns; `"all"` allows every phrase. |
| `scale_source` | `"auto"` | `"own"`, `"auto"`, or an integer track number for scale-based operations. |
| `fallback_scale` | `"pattern"` | `"pattern"` infers pitch classes from the pattern; `"chromatic"` otherwise uses all pitches. |
| `range.below` | `12` | Range `0..24`: semitones below the pattern minimum. |
| `range.above` | `12` | Range `0..24`: semitones above the pattern maximum. |
| `new_spice` | `true` | Rolls a new spice pattern after mutation. Existing spice is reapplied on top of the mutation. |
| `keep_first_note` | `true` | Never changes the pattern's first sounding event. |
| `seed` | `null` | `null` for a fresh random sequence; otherwise a number for repeatable results. |

## `art_spice`

**Track kinds:** melodic, chord, and drum tracks.

Applies deterministic gate, ratchet, and silence variations over a saved pre-spice pattern. It is non-destructive:
amount `0` restores the base pattern, larger amounts retain smaller-amount changes and add more, and the same seed
and amount reproduce the same result. The base is updated when the track changes outside spice.

| Parameter | Default | Description / accepted values |
|---|---:|---|
| `amount` | `0.4` | Range `0..1`: amount of spice applied. |
| `amount_per_level` | `0.15` | Range `0..1`: extra amount added for each encoder intensity level. |
| `gate.share` | `1.0` | Range `0..1`: share of eligible notes receiving a changed gate at full amount. |
| `gate.short` | `0.5` | Relative weight for a one-step short gate. |
| `gate.long` | `0.5` | Relative weight for a longer gate tail. |
| `max_tail_steps` | `4` | Range `2..64`: maximum long-gate length in steps, never beyond the next note. |
| `ratchet.share` | `0.35` | Range `0..1`: share of eligible notes receiving ratchets at full amount. |
| `ratchet.max` | `3` | Range `2..8`: maximum ratchet hits. |
| `silence.share` | `0.3` | Range `0..1`: share of eligible notes silenced at full amount. |
| `keep_first_note` | `true` | Never spices the pattern's first event. |
| `seed` | `null` | `null` creates a seed for the track; otherwise a number makes spice repeatable. |

Spice changes event length and ratchet values and can remove events. It does not change the underlying notes used
for the returned pitch sequence.

## `art_dice`

**Track kinds:** melodic, chord, and drum tracks.

Rerolls the spice seed without changing the pattern's pitches or explicitly mutating notes. It uses
`art_spice`'s gate, ratchet, silence, and tail settings.

| Parameter | Default | Description / accepted values |
|---|---:|---|
| `amount` | `null` | Range `0..1` or `null`: spice amount for the rolled pattern. `null` uses the track's last spice amount, then `art_spice.amount`. |
| `seed` | `null` | `null` produces a fresh spice pattern; otherwise a number makes repeated runs repeatable. |

## Generative mode gestures

(Only on a device with controls: the FM-1, not the virtual device.) The `gesture` setup controls how a device control
drives any armed algorithm. The setup is stored in
`generators/gesture.json`.

| Parameter | Default | Description / accepted values |
|---|---:|---|
| `settle_ms` | `300` | Range `50..3000`: idle time after encoder movement before the turn is processed. |
| `units_per_level` | `4` | Range `1..32`: encoder increments needed for one intensity level. |
| `max_level` | `8` | Range `1..32`: maximum intensity or undo depth for one turn. |
| `left` | `"undo"` | `"undo"` makes a counter-clockwise turn undo runs; `"vary"` runs the algorithm as a clockwise turn does. |
| `debounce_ms` | `150` | Range `0..2000`: button presses within this interval count as one chord/press. |

For encoders, clockwise movement calculates intensity as `ceil(abs(delta) / units_per_level)`, capped by
`max_level`. With `left: "undo"`, counter-clockwise movement steps back through undoable runs. For buttons, each
debounced press runs at intensity 1.

Disarming keeps the current pattern. Disarming with restore enabled returns the exact sequence captured when the
track was armed.
