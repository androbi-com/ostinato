"""General MIDI drum voices: names, the staff positions of a drum-kit staff, and which are played by the feet."""
from __future__ import annotations

GM_NAMES = {35: "kick 2", 36: "kick", 37: "rim", 38: "snare", 39: "clap", 40: "snare 2", 41: "low floor tom",
            42: "closed hat", 43: "low tom", 44: "pedal hat", 45: "low mid tom", 46: "open hat", 47: "mid tom",
            48: "hi tom", 49: "crash", 50: "high tom", 51: "ride", 52: "china", 53: "ride bell", 54: "tambourine",
            55: "splash", 56: "cowbell", 57: "crash 2", 59: "ride 2", 60: "hi bongo", 61: "low bongo",
            62: "mute conga", 63: "conga", 64: "low conga", 69: "cabasa", 70: "shaker", 75: "claves", 76: "wood block"}

# GM voice -> (position on the percussion staff, notehead): the usual drum-kit notation
STAFF = {
    35: ("E4", "normal"), 36: ("F4", "normal"), 37: ("C5", "x"), 38: ("C5", "normal"), 39: ("D5", "x"),
    40: ("B4", "normal"), 41: ("G4", "normal"), 43: ("A4", "normal"), 45: ("B4", "normal"), 47: ("D5", "normal"),
    48: ("E5", "normal"), 50: ("E5", "normal"), 42: ("G5", "x"), 46: ("G5", "circle-x"), 44: ("D4", "x"),
    49: ("A5", "x"), 57: ("A5", "x"), 55: ("A5", "x"), 52: ("A5", "x"), 51: ("F5", "x"), 59: ("F5", "x"),
    53: ("F5", "diamond"), 56: ("B5", "triangle"), 54: ("B5", "x"), 69: ("C6", "x"), 70: ("C6", "x"),
    60: ("E5", "normal"), 61: ("D5", "normal"), 62: ("G4", "x"), 63: ("G4", "normal"), 64: ("F4", "normal"),
    75: ("B5", "x"), 76: ("B5", "normal"),
}
FEET = {35, 36, 44}  # played with the feet: the lower voice (stems down)


def name(gm: int) -> str:
    return GM_NAMES.get(gm, f"drum {gm}")


def staff(gm: int) -> tuple[str, str]:
    return STAFF.get(gm, ("B5", "x"))


def legend(voices: list[int], names: dict[int, str] | None = None) -> str:
    """"kick F4, snare C5, closed hat G5 (x)" (names: the device's own names, if it has them)."""
    out = []
    for gm in voices:
        pos, head = staff(gm)
        out.append(f"{(names or {}).get(gm, name(gm)).lower()} {pos}" + (f" ({head})" if head != "normal" else ""))
    return ", ".join(out)
