"""`ostinato-mcp`: what Claude starts (stdio). A device-independent shim in front of the ostinato device server.

It knows nothing about devices. The tools and instructions come from the device server
(`/api/tools`, found through `device.json` in the setup folder, with its token); while the device server is not
running, the shim shows the list the device server saved last (`tools.json`), and each call answers how to start
it. Before the device server ever ran there is one tool, ostinato_status, saying the same. When the device server's tool
list changes (a device detected, another backend), the shim tells Claude (notifications/tools/list_changed).

It holds no state and never opens a MIDI port, so any number of them (the Chat tab, each Code session) is fine.
"""
from __future__ import annotations

import json
import logging
import os
import sys

import anyio
import mcp.types as types
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server

from . import __version__

log = logging.getLogger("ostinato.shim")

NOT_RUNNING = ("The ostinato device server is not running. Start it with `ostinato-device` (in a terminal; it stays there "
               "until Ctrl+C), then try again.")
CALL_TIMEOUT = 180.0  # (a full read, a long generative run, a staff)
WATCH_S = 3.0  # how often the shim checks whether the device server's tool list changed

PLACEHOLDER = {
    "name": "ostinato_status",
    "description": "Status of the ostinato device server and the music device connected to it. (The device server "
                   "has not run yet on this computer: its tools appear once it runs.)",
    "inputSchema": {"type": "object", "properties": {}},
}
BASE_INSTRUCTIONS = ("Works with a music device (a synthesizer, a groovebox..., or a virtual one) through the ostinato device server (`ostinato-device`, started by "
                     "the user in a terminal). Start with ostinato_status.")


def _text_error(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=True)


def _read_info() -> dict | None:
    from .device_server import read_info

    return read_info()


def _saved_tools() -> dict | None:
    from .device_server import tools_cache_path

    try:
        return json.loads(tools_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _leaves(e: BaseException):
    """The real errors inside (anyio's task groups wrap them in exception groups)."""
    if isinstance(e, BaseExceptionGroup):
        for x in e.exceptions:
            yield from _leaves(x)
    else:
        yield e


def _leaf(e: BaseException) -> BaseException:
    return next(_leaves(e), e)


def _cannot_connect(e: BaseException) -> bool:
    import httpx2

    return any(isinstance(x, (httpx2.ConnectError, ConnectionRefusedError, ConnectionResetError, httpx2.ConnectTimeout))
               for x in _leaves(e))


async def _api_tools(info: dict) -> dict | None:
    """The running device server's tool list (None: not reachable)."""
    try:
        async with create_mcp_http_client(headers={"Authorization": f"Bearer {info['token']}"}) as http:
            r = await http.get(info["url"] + "/api/tools", timeout=3.0)
            r.raise_for_status()
            return r.json()
    except Exception:
        return None


async def _forward(name: str, arguments: dict | None) -> types.CallToolResult:
    info = _read_info()
    if not info:
        return _text_error(NOT_RUNNING)
    try:
        async with create_mcp_http_client(headers={"Authorization": f"Bearer {info['token']}"}) as http:
            async with streamable_http_client(info["url"] + "/mcp", http_client=http) as (r, w):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    res = await s.call_tool(name, arguments or {}, read_timeout_seconds=CALL_TIMEOUT)
                    if isinstance(res, types.CallToolResult):
                        return res
                    return _text_error(f"unexpected answer from the device server: {type(res).__name__}")
    except Exception as e:
        if _cannot_connect(e):  # (it ended without removing device.json, e.g. killed)
            return _text_error(NOT_RUNNING)
        log.exception("forwarding %s failed", name)
        return _text_error(f"The ostinato device server did not answer {name}: {_leaf(e)!r}")


class Shim:
    def __init__(self):
        saved = _saved_tools()
        self.instructions = (saved or {}).get("instructions") or BASE_INSTRUCTIONS
        self.shown_version = None  # the tools_version of the list Claude was given last
        self.session = None  # (captured from the first request: to send list_changed later)
        self.server = Server("ostinato", version=__version__, instructions=self.instructions,
                             on_list_tools=self.on_list_tools, on_call_tool=self.on_call_tool)

    async def current(self) -> dict:
        """The tool list to show: the running device server's, else the one it saved, else the placeholder."""
        info = _read_info()
        if info:
            live = await _api_tools(info)
            if live and live.get("tools"):
                return live
        saved = _saved_tools()
        if saved and saved.get("tools"):
            return saved
        return {"tools": [PLACEHOLDER], "tools_version": 0}

    async def on_list_tools(self, ctx, params) -> types.ListToolsResult:
        self.session = getattr(ctx, "session", None) or self.session
        data = await self.current()
        self.shown_version = data.get("tools_version")
        return types.ListToolsResult(tools=[types.Tool.model_validate(t) for t in data["tools"]])

    async def on_call_tool(self, ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        self.session = getattr(ctx, "session", None) or self.session
        return await _forward(params.name, params.arguments)

    async def watch(self) -> None:
        """Tell Claude when the device server's tool list changes (e.g. the device was detected after it started)."""
        while True:
            await anyio.sleep(WATCH_S)
            if self.session is None or self.shown_version is None:
                continue
            info = _read_info()
            live = await _api_tools(info) if info else None
            if live and live.get("tools") and live.get("tools_version") != self.shown_version:
                self.shown_version = live.get("tools_version")
                try:
                    await self.session.send_tool_list_changed()
                except Exception:
                    log.debug("could not send tools/list_changed", exc_info=True)

    async def run(self) -> None:
        opts = self.server.create_initialization_options(notification_options=NotificationOptions(tools_changed=True))
        async with stdio_server() as (r, w):
            async with anyio.create_task_group() as tg:
                tg.start_soon(self.watch)
                await self.server.run(r, w, opts)
                tg.cancel_scope.cancel()


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=os.environ.get("OSTINATO_LOG", "WARNING").upper(),
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    anyio.run(Shim().run)


if __name__ == "__main__":
    main()
