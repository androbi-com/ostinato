"""ostinato: Claude and other clients working with music devices (today an M-VAVE FM-1 running SLOOP).

ostinato-device (device_server.py) is the one process that talks to the device: it detects which backend the device needs
(ostinato.backends) and runs it with a session (ostinato.session); the generic levels (ostinato.model, ostinato.music, ostinato.generative,
ostinato.tools) work through the backend's capabilities. ostinato-mcp (shim.py) is what Claude starts: a device-independent
stdio shim that forwards to the device server.
"""
__version__ = "0.3.2"
