# Ostinato: Music on your FM-1

**Ostinato** runs as a local server and lets you interact through a browser UI or Claude through MCP with musical sequences reproduced on a MIDI device. The current implementation provides a backend for an M-VAVE FM-1 running the SLOOP firmware.

## Current capabilities

- **Read sequences from the device:** download track data and show it as a compact grid, detailed note list, piano roll, or music staff.
- **Download complete notation:** export a track or the full score as MusicXML for notation software.
- **Upload note data:** write simple JSON notes or a standard MIDI file to a track, replacing or merging with the existing sequence.
- **Inspect musical information:** view tracks and instruments, keys and scales, pattern notes, tempo, parameters, and MIDI port status.
- **Analyze structured sequences:** retrieve per-event start, length, pitches, velocity, articulation, ratchets, and backend-specific data.
- **Mute tracks:** mute or unmute individual SLOOP tracks from the browser UI or through MCP without changing their notes.
- **Edit existing sequences:** run generative algorithms directly on a track, clear a track (all notes, or just one note or drum lane), and undo writes, clears or algorithm runs one step at a time.
- **Generate variations and patterns:** use `random_initializer` to create a scale-constrained sequence, or
  `melodic_swapper`, `art_mutate`, `art_spice`, and `art_dice` to alter melodic, chord, or drum sequences.
- **Arm generative mode:** use supported device controls such as a knob or buttons to vary a track, undo runs, or restore the sequence from before arming.
- **Follow live changes:** refresh device sequences and inspect a change log of device edits, writes, algorithm runs, restores, and connection events.
- **Control and monitor locally:** connect or disconnect the device, monitor its status, and use the browser UI for tracks, staff views, generation, undo, settings, and activity.


## Quick Start

### 1. Install

Clone this repository:

```bash
git clone https://github.com/androbi-com/ostinato.git
cd ostinato
uv sync
```

Requires [uv](https://docs.astral.sh/uv/). Install it if you haven't already, then run `uv sync` to create the local environment and install Ostinato and its dependencies. Repeat `uv sync` after pulling dependency or project changes.

### 2. Connect your FM-1

- Connect your M-VAVE FM-1 to your computer via USB
- Make sure SLOOP firmware is running on the device

### 3. Start the device server

Open a terminal and run:

```bash
uv run ostinato-device
```

You'll see output indicating the device is connected and ready. The server runs at `http://127.0.0.1:5317`.

### 4. Open the browser UI

In another terminal, run:

```bash
uv run ostinato-device ui
```

This opens the device server's web interface in your browser, showing your tracks, the step sequencer, and live edits.

## Using with Claude

To use Ostinato with Claude Desktop you need to configure the MCP server.

### Setup for Claude Desktop

Add this to your `%APPDATA%\Claude\claude_desktop_config.json`:

```json
"mcpServers": {
  "ostinato": {
    "command": "C:\\Users\\<your-username>\\.local\\bin\\uv.exe",
    "args": ["--directory", "C:\\path\\to\\ostinato", "run", "ostinato-mcp"]
  }
}
```

Then restart Claude Desktop. You'll now have access to Ostinato tools.

### Example Usage

Here are some example prompts to try:

**Get device info:**
> "What's the current status of the FM-1? Show me all 4 tracks and their current sequences."

**Show a track as a staff:**
> "Display track 1 as a musical staff with notation."

**Upload a simple melody:**
> "Write these notes to track 1: C4 quarter note, E4 quarter, G4 quarter, C5 quarter"

**List available notes:**
> "What scale is track 2 in? Show me all the available notes and the current key signature."

**Generate variations:**
> "Use the melodic_swapper algorithm to generate a variation of track 1 with medium intensity."

**Check track parameters:**
> "What are the parameters for track 1? Show me the instrument, tempo, and other settings."

**Edit with undo:**
> "Write a new drum pattern to track 4, then undo it and write a different one."

**Clear a track:**
> "Clear track 2." / "Remove every A3 from track 1." / "Take the hi-hats out of the drums."

**Mute a track:**
> "Mute track 2." / "Unmute track 2."

## For More Details

For technical documentation, architecture details, backend information, and advanced configuration, see [README_TECHNICAL.md](README_TECHNICAL.md). The [generative algorithm reference](ALGORITHMS.md) documents all implemented algorithms and their parameters.

## Troubleshooting

**Device not found?**
- Ensure your FM-1 is connected via USB
- Try specifying the MIDI port: `ostinato-device --midi-port "FM-1"` (replace with your device name)

**Using the official SLOOP editor?**
- Release the device in Ostinato before opening the SLOOP editor: use `ostinato_disconnect` in Claude or the Release button in the UI
- Reconnect with `ostinato_connect` or the Reconnect button when you're done

**Device server won't start?**
- Make sure no other program (like SLOOP editor or DAW) is using the FM-1's MIDI port
- Check that [uv](https://docs.astral.sh/uv/) is installed correctly

## Support

For detailed information about tools, capabilities, generative algorithms, and development, see [README_TECHNICAL.md](README_TECHNICAL.md).
