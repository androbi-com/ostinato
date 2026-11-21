"""Hardware check: the SLOOP backend on the real FM-1: read everything, print it through the generic views, then
follow device-side edits.

usage: uv run python tools/hw_check.py [--port SUBSTRING] [--listen SECONDS]

Stop ostinato-device first (only one program should talk to the FM-1). While listening, turn knobs / edit steps / select
tracks on the FM-1 and watch the events arrive.
"""
import argparse
import logging
import sys
import time

from ostinato.backends.fm1_sloop.backend import SloopBackend
from ostinato.midi import list_ports
from ostinato.music import views as V

ap = argparse.ArgumentParser()
ap.add_argument("--port")
ap.add_argument("--listen", type=float, default=30)
ap.add_argument("-v", action="store_true")
args = ap.parse_args()
logging.basicConfig(level=logging.DEBUG if args.v else logging.INFO, stream=sys.stderr)
sys.stdout.reconfigure(encoding="utf-8")

print("MIDI ports:", list_ports())
b = SloopBackend()
t0 = time.monotonic()
b.start(args.port)
while b.connection.not_ready() and time.monotonic() - t0 < 20:
    time.sleep(0.1)
print(f"\nready in {time.monotonic() - t0:.1f} s: {b.connection.status()}\n")
print(b.connection.overview(), "\n")
for t in b.tracks.list_tracks():
    seq = b.reader.read(t.track)
    print(f"Track {t.track} ({seq.kind}): {t.instrument}\n{V.pattern_line(seq)}")
    print(V.view(seq, "grid", b.keys.key(t.track)), "\n")
print(b.parameters.parameters(1, only_changed=True), "\n")
print(b.parameters.parameters(None, only_changed=True), "\n")

print(f"listening {args.listen:.0f} s: change things on the FM-1 ...")
b.changes.subscribe(lambda e: print(f"  {e.kind} track {e.track}: {e.text}"))
time.sleep(args.listen)
print("\nlink stats", b.dev.link.stats if b.dev.link else None)
b.stop()
