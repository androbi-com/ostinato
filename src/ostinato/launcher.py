# SPDX-License-Identifier: GPL-3.0-only
"""One program for everything (the Windows bundle's `ostinato-cli.exe`; `ostinato` in a Python install):

    ostinato                       the device server (as `ostinato-device`)
    ostinato run|status|stop|ui    as `ostinato-device <command>` (and its options)
    ostinato mcp                   the MCP shim Claude starts (as `ostinato-mcp`)
    ostinato claude-config add|remove|show
                                   put Ostinato into Claude Desktop's configuration, or take it out
"""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "run"
    if cmd == "mcp":
        from .shim import main as shim_main

        shim_main()
        return
    if cmd == "claude-config":
        from .claude_config import main as cc_main

        sys.exit(cc_main(argv[1:]))
    if cmd == "virtual-player":  # (the virtual device's process: the device server starts it)
        from .backends.virtual_gm.player import main as player_main

        sys.exit(player_main(argv[1:]))
    if cmd in ("-h", "--help", "help"):
        print(__doc__.strip())
        return
    from .device_server import main as device_main

    device_main(argv)


if __name__ == "__main__":
    main()
