"""Note staffs from Sequences: a music21 score -> MusicXML -> verovio SVG -> PNG. Device-independent.

- Melodic / chord parts: each sounding event is a note or chord lasting its length (cut at the next event); muted
  events and gaps are rests. Accent: an accent mark; a "hard" note: a strong accent; a "ghost" note is
  parenthesised; a ratchet (2..4 hits) gets tremolo strokes; a slide a slur to the next note. Key signature and
  spelling from the part's Key (if there is one), the clef from the range used.
- Drum parts: a percussion staff in two voices, hands (stems up) and feet (kick, pedal: stems down), GM voices at
  their usual positions (music.drums); a hit lasts until the voice's next hit or the next beat.
- Several parts loop their own lengths up to the longest one (as they play), at most `max_bars` bars of 4/4.
Optional: a part's name (e.g. the instrument), the key, the tempo. Without them the staff simply has none.

music21 and verovio (C++) are not made for several threads at once, and verovio keeps its fonts per thread: all of
it runs under LOCK, and every toolkit is given the font path (see _render_png).
"""
from __future__ import annotations

import math
import os
import threading
from dataclasses import dataclass
from fractions import Fraction

from ..model import Key, Sequence
from . import drums as D
from . import scales as SC

LOCK = threading.RLock()


@dataclass
class Part:
    title: str  # "T1 ANALOG MONO BASS" (or just "Track 1")
    seq: Sequence
    key: Key | None = None
    drum_names: dict[int, str] | None = None  # the device's names for its drum voices (for the legend)


def warm() -> None:
    """Import music21 and verovio and make one toolkit (a few seconds), under the lock."""
    with LOCK:
        import music21  # noqa: F401
        import verovio

        verovio.toolkit()


def _off(x: Fraction):
    return float(x) if x.denominator == 1 else x


def _key_signature(key: Key | None):
    from music21 import key as m21key

    if key is None or not SC.mode_of(key.scale):
        return None
    letter, alter, _ = SC.spelled(key.root + 60, key)
    try:
        return m21key.Key(letter + {-1: "-", 0: "", 1: "#"}.get(alter, ""), SC.mode_of(key.scale))
    except Exception:
        return None


def key_caption(key: Key | None) -> str | None:
    if key is None or key.scale == "chromatic":
        return None
    return "key " + SC.key_name(key) + (f" ({key.note})" if key.note else "")


def _melodic_part(p: Part, total: Fraction):
    from music21 import articulations, chord, clef, expressions, note, spanner, stream

    part = stream.Part()
    part.partName = p.title
    seq = p.seq
    events = seq.sounding()
    reps = max(1, math.ceil(total / seq.length)) if seq.length > 0 else 1
    prev_slide = None
    slurs = []
    pitches = [n.pitch for e in events for n in e.notes]
    for r in range(reps):
        base = r * seq.length
        for k, e in enumerate(events):
            off = base + e.start
            if off >= total:
                break
            nxt = events[k + 1].start if k + 1 < len(events) else seq.length
            ql = min(e.length, nxt - e.start, total - off)
            if ql <= 0:
                continue
            ns = []
            for n in e.notes:
                x = note.Note(SC.music21_name(n.pitch, p.key))
                if n.level == "ghost":
                    x.noteheadParenthesis = True
                ns.append(x)
            el = ns[0] if len(ns) == 1 else chord.Chord(ns)
            el.quarterLength = ql
            if e.accent:
                el.articulations.append(articulations.Accent())
            if any(n.level == "hard" for n in e.notes):
                el.articulations.append(articulations.StrongAccent())
            rat = max(n.ratchet for n in e.notes)
            if rat > 1:
                trem = expressions.Tremolo()
                trem.numberOfMarks = min(3, rat - 1)
                el.expressions.append(trem)
            if prev_slide is not None:
                slurs.append((prev_slide, el))
            prev_slide = el if e.slide else None
            part.insert(_off(off), el)
        prev_slide = None
    for a, b in slurs:
        part.insert(0, spanner.Slur(a, b))
    if pitches:
        lo, hi = min(pitches), max(pitches)
        mid = (lo + hi) / 2
        c = (clef.Bass8vbClef() if lo < 31 else clef.BassClef()) if mid < 57 else (
            clef.Treble8vaClef() if hi > 88 else clef.TrebleClef())
    else:
        c = clef.TrebleClef()
    part.insert(0, c)
    k = _key_signature(p.key)
    if k is not None:
        part.insert(0, k)
    return part


def _finish(s, total: Fraction):
    """Fill the gaps with rests up to total, cut into 4/4 measures, tie / beam as notation wants."""
    from music21 import meter, note

    s.insert(0, meter.TimeSignature("4/4"))
    s.makeRests(fillGaps=True, inPlace=True, timeRangeFromBarDuration=False)
    end = Fraction(s.highestTime)
    if end < total:
        s.insert(_off(end), note.Rest(quarterLength=total - end))
    out = s.makeNotation()
    _split_rests_at_beats(out)
    _fix_accidentals(out)
    return out


def _split_rests_at_beats(measured) -> None:
    """music21 leaves rests across beats: split a rest at the next beat when it starts off the beat, and an on-beat
    rest into whole beats + the remainder."""
    from music21 import note

    for m in measured.getElementsByClass("Measure"):
        for c in list(m.voices) or [m]:
            for r in list(c.getElementsByClass(note.Rest)):
                off, ql = Fraction(r.offset), Fraction(r.quarterLength)
                pieces = []
                cur, left = off, ql
                while left > 0:
                    take = (Fraction(math.floor(left)) if left >= 1 else left) if cur.denominator == 1 else \
                        min(left, Fraction(math.floor(cur) + 1) - cur)
                    pieces.append((cur, take))
                    cur, left = cur + take, left - take
                if len(pieces) == 1:
                    continue
                c.remove(r)
                for o, q in pieces:
                    c.insert(_off(o), note.Rest(quarterLength=q))


def _fix_accidentals(measured) -> None:
    """Accidentals as a score has them: shown once per measure and pitch, cancelled by naturals."""
    from music21 import key, pitch

    ks = next(iter(measured.recurse().getElementsByClass(key.KeySignature)), None)
    for m in measured.getElementsByClass("Measure"):
        seen: dict[str, int] = {}
        for n in m.recurse().notes:
            for p in getattr(n, "pitches", ()):
                if not isinstance(p, pitch.Pitch) or n.isRest:
                    continue
                k = f"{p.step}{p.octave}"
                alter = int(p.alter)
                ks_alter = int(ks.accidentalByStep(p.step).alter) if ks and ks.accidentalByStep(p.step) else 0
                show = alter != seen.get(k, ks_alter)
                if p.accidental is None and show:
                    p.accidental = pitch.Accidental("natural")
                if p.accidental is not None:
                    p.accidental.displayStatus = show
                seen[k] = alter


def _drum_voice(hits, feet: bool, total: Fraction, min_len: Fraction, stem: str):
    """One drum voice: the hits whose voices are (not) feet, each until the voice's next hit or the next beat."""
    from music21 import articulations, expressions, note, percussion, stream

    v = stream.Stream()
    mine = [(off, [n for n in e.notes if (n.pitch in D.FEET) == feet]) for off, e in hits]
    mine = [(off, ns) for off, ns in mine if ns]
    for k, (off, ns) in enumerate(mine):
        nxt = mine[k + 1][0] if k + 1 < len(mine) else total
        end = min(nxt, Fraction(math.floor(off) + 1), total)
        ql = max(end - off, min(min_len, total - off))
        us = []
        for n in ns:
            pos, head = D.staff(n.pitch)
            u = note.Unpitched(displayName=pos)
            if head != "normal":
                u.notehead = head
            if n.level == "ghost":
                u.noteheadParenthesis = True
            us.append(u)
        el = us[0] if len(us) == 1 else percussion.PercussionChord(us)
        el.quarterLength = ql
        el.stemDirection = stem
        if any(n.level == "hard" for n in ns):
            el.articulations.append(articulations.Accent())
        rat = max(n.ratchet for n in ns)
        if rat > 1:
            trem = expressions.Tremolo()
            trem.numberOfMarks = min(3, rat - 1)
            el.expressions.append(trem)
        v.insert(_off(off), el)
    return v


def _drum_part(p: Part, total: Fraction):
    """The percussion staff: hands (stems up) and feet (stems down) as two voices in each measure."""
    from music21 import clef, instrument, meter, stream

    seq = p.seq
    events = seq.sounding()
    reps = max(1, math.ceil(total / seq.length)) if seq.length > 0 else 1
    hits = [(r * seq.length + e.start, e) for r in range(reps) for e in events if r * seq.length + e.start < total]
    min_len = seq.grid.step if seq.grid else Fraction(1, 4)
    used = {n.pitch for _, e in hits for n in e.notes}
    voices = []
    for feet, stem in ((False, "up"), (True, "down")):
        if any((v in D.FEET) == feet for v in used):
            voices.append(_finish(_drum_voice(hits, feet, total, min_len, stem), total))
    if not voices:
        voices = [_finish(stream.Stream(), total)]
    part = stream.Part()
    part.partName = p.title
    part.insert(0, instrument.Percussion())
    vmeasures = [list(vs.getElementsByClass("Measure")) for vs in voices]
    for mi in range(len(vmeasures[0])):
        m = stream.Measure(number=mi + 1)
        if mi == 0:
            m.insert(0, clef.PercussionClef())
            m.insert(0, meter.TimeSignature("4/4"))
        for vi, ms in enumerate(vmeasures):
            if mi < len(ms):
                voice = stream.Voice(id=str(vi + 1))
                for el in ms[mi].notesAndRests:
                    voice.insert(el.offset, el)
                m.insert(0, voice)
        part.append(m)
    return part


def build_score(parts: list[Part], tempo: float | None = None, max_bars: int = 8):
    """-> (music21 Score, remarks for the caption); score.ostinato_bars = the number of bars."""
    with LOCK:
        from music21 import metadata, stream

        total = max((p.seq.length for p in parts), default=Fraction(4))
        total = min(total, Fraction(4 * max_bars))
        total = Fraction(math.ceil(total * 4), 4) or Fraction(4)
        score = stream.Score()
        score.metadata = metadata.Metadata()
        score.metadata.title = ""
        for p in parts:
            part = _drum_part(p, total) if p.seq.kind == "drums" else _finish(_melodic_part(p, total), total)
            score.insert(0, part)
        remarks = [f"Tempo {tempo:g} BPM, 4/4" if tempo else "4/4"]
        for p in parts:
            g = p.seq.grid
            r = f"{p.title}: {float(p.seq.length):g} beats" + (f" ({g.steps} steps of {g.name or g.step})" if g else "")
            kc = key_caption(p.key) if p.seq.kind != "drums" else None
            if kc:
                r += f", {kc}"
            if p.seq.kind == "drums":
                voices = sorted({n.pitch for e in p.seq.sounding() for n in e.notes})
                if voices:
                    r += f". Staff positions: {D.legend(voices, p.drum_names)}"
            remarks.append(r)
        if any(p.seq.length > 4 * max_bars for p in parts):
            remarks.append(f"(cut at {max_bars} bars)")
        score.ostinato_bars = math.ceil(total / 4)
        return score, remarks


def to_musicxml(score) -> str:
    with LOCK:
        from music21.musicxml.m21ToXml import GeneralObjectExporter

        return GeneralObjectExporter(score).parse().decode("utf-8")


def render_png(musicxml: str, bars: int = 1, width_px: int | None = None, scale: int = 45) -> list[bytes]:
    """MusicXML -> verovio SVG page(s) -> PNG bytes (white background). Width: about 380 px a bar plus the part
    names, at most 1500 px (then the systems wrap)."""
    import resvg_py

    with LOCK:
        return [bytes(resvg_py.svg_to_bytes(svg_string=svg, background="white"))
                for svg in _render_svg(musicxml, bars, width_px, scale)]


def render_svg(musicxml: str, bars: int = 1, width_px: int | None = None, scale: int = 45) -> list[str]:
    """MusicXML -> verovio SVG page(s) (for a browser: no background, scales with its box)."""
    with LOCK:
        return _render_svg(musicxml, bars, width_px, scale)


def _render_svg(musicxml: str, bars: int, width_px: int | None, scale: int) -> list[str]:
    import verovio

    if width_px is None:
        width_px = min(1500, 260 + 380 * max(1, bars))
    tk = verovio.toolkit()
    # verovio keeps its fonts per thread: the package points only the importing thread at them, so a toolkit made in
    # any other thread (every tool call in the device server) has none and refuses all music, without a log line
    tk.setResourcePath(os.path.join(os.path.dirname(verovio.__file__), "data"))
    tk.setOptions({"pageWidth": int(width_px * 100 / scale), "pageHeight": 60000, "adjustPageHeight": True,
                   "scale": scale, "pageMarginTop": 40, "pageMarginBottom": 40, "pageMarginLeft": 60,
                   "pageMarginRight": 60, "header": "none", "footer": "none", "breaks": "auto",
                   "spacingSystem": 12, "minLastJustification": 0})
    if not tk.loadData(musicxml):
        vlog = (tk.getLog() or "").strip().replace("\n", " | ")[:400]
        raise RuntimeError(f"verovio could not read the MusicXML ({len(musicxml)} chars; verovio: {vlog or 'no log'})")
    return [tk.renderToSVG(page) for page in range(1, tk.getPageCount() + 1)]
