"""General MIDI: the 128 sounds in their 16 families, the drum kits (GS; a synth without them plays its standard kit)
and the percussion notes."""
from __future__ import annotations

_FAMILIES = {
    "Piano": "Acoustic Grand Piano|Bright Acoustic Piano|Electric Grand Piano|Honky-tonk Piano|Electric Piano 1|"
             "Electric Piano 2|Harpsichord|Clavinet",
    "Chromatic Percussion": "Celesta|Glockenspiel|Music Box|Vibraphone|Marimba|Xylophone|Tubular Bells|Dulcimer",
    "Organ": "Drawbar Organ|Percussive Organ|Rock Organ|Church Organ|Reed Organ|Accordion|Harmonica|Tango Accordion",
    "Guitar": "Nylon Guitar|Steel Guitar|Jazz Guitar|Clean Guitar|Muted Guitar|Overdriven Guitar|Distortion Guitar|"
              "Guitar Harmonics",
    "Bass": "Acoustic Bass|Fingered Bass|Picked Bass|Fretless Bass|Slap Bass 1|Slap Bass 2|Synth Bass 1|Synth Bass 2",
    "Strings": "Violin|Viola|Cello|Contrabass|Tremolo Strings|Pizzicato Strings|Orchestral Harp|Timpani",
    "Ensemble": "String Ensemble 1|String Ensemble 2|Synth Strings 1|Synth Strings 2|Choir Aahs|Voice Oohs|"
                "Synth Voice|Orchestra Hit",
    "Brass": "Trumpet|Trombone|Tuba|Muted Trumpet|French Horn|Brass Section|Synth Brass 1|Synth Brass 2",
    "Reed": "Soprano Sax|Alto Sax|Tenor Sax|Baritone Sax|Oboe|English Horn|Bassoon|Clarinet",
    "Pipe": "Piccolo|Flute|Recorder|Pan Flute|Blown Bottle|Shakuhachi|Whistle|Ocarina",
    "Synth Lead": "Square Lead|Sawtooth Lead|Calliope Lead|Chiff Lead|Charang Lead|Voice Lead|Fifths Lead|Bass + Lead",
    "Synth Pad": "New Age Pad|Warm Pad|Polysynth Pad|Choir Pad|Bowed Pad|Metallic Pad|Halo Pad|Sweep Pad",
    "Synth Effects": "Rain|Soundtrack|Crystal|Atmosphere|Brightness|Goblins|Echoes|Sci-fi",
    "Ethnic": "Sitar|Banjo|Shamisen|Koto|Kalimba|Bagpipe|Fiddle|Shanai",
    "Percussive": "Tinkle Bell|Agogo|Steel Drums|Woodblock|Taiko Drum|Melodic Tom|Synth Drum|Reverse Cymbal",
    "Sound Effects": "Guitar Fret Noise|Breath Noise|Seashore|Bird Tweet|Telephone Ring|Helicopter|Applause|Gunshot",
}
# (family, name), by program number 0..127
SOUNDS: list[tuple[str, str]] = [(f, n) for f, ns in _FAMILIES.items() for n in ns.split("|")]
assert len(SOUNDS) == 128

# (program number, name) of the drum channel's kits
KITS: list[tuple[int, str]] = [(0, "Standard"), (8, "Room"), (16, "Power"), (24, "Electronic"), (25, "TR-808"),
                               (32, "Jazz"), (40, "Brush"), (48, "Orchestra")]

# the percussion notes every GM synth has
DRUMS: dict[int, str] = {
    35: "KICK 2", 36: "KICK", 37: "RIM", 38: "SNARE", 39: "CLAP", 40: "SNARE 2", 41: "LOW FLOOR TOM", 42: "HAT",
    43: "LOW TOM", 44: "PEDAL", 45: "LOW MID TOM", 46: "OPEN HAT", 47: "MID TOM", 48: "HI TOM", 49: "CRASH",
    50: "HIGH TOM", 51: "RIDE", 52: "CHINA", 53: "RIDE BELL", 54: "TAMBOURINE", 55: "SPLASH", 56: "COWBELL",
    57: "CRASH 2", 58: "VIBRASLAP", 59: "RIDE 2", 60: "HI BONGO", 61: "LOW BONGO", 62: "MUTE CONGA", 63: "CONGA",
    64: "LOW CONGA", 65: "HI TIMBALE", 66: "LOW TIMBALE", 67: "HI AGOGO", 68: "LOW AGOGO", 69: "CABASA",
    70: "SHAKER", 71: "SHORT WHISTLE", 72: "LONG WHISTLE", 73: "SHORT GUIRO", 74: "LONG GUIRO", 75: "CLAVES",
    76: "HI WOOD BLOCK", 77: "LOW WOOD BLOCK", 78: "MUTE CUICA", 79: "OPEN CUICA", 80: "MUTE TRIANGLE",
    81: "OPEN TRIANGLE"}
