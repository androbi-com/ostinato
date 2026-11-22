"""The virtual device: a sequencer inside this computer that plays with General MIDI sounds, for working without
hardware. The device itself (patterns, clock, MIDI output) is a process of its own (player.py); the backend
(backend.py) reads and writes it as it would a device on a cable."""
