"""The browser UI of the device server: its page (ostinato/ui/) and a JSON API on the same session as the MCP tools.

Security (the device server listens on 127.0.0.1 only):
- every request: the Host header must be this server's own (127.0.0.1:<port> / localhost:<port>): DNS rebinding;
- /mcp and the shim's /api/status, /api/tools, /api/shutdown: the token as "Authorization: Bearer <token>";
- the page and /api/ui/*: that header, or the session cookie, which /ui/login?key=<token> sets (HttpOnly,
  SameSite=Strict: no other site's page gets it sent) before sending the browser on to the clean address;
- writes with the cookie also need the header "X-Ostinato: 1" and, when the browser sends one, an Origin of this
  server: a page on another site cannot send that header without a CORS preflight, which is never allowed.
`ostinato-device ui` opens the login link in the browser.
"""
from __future__ import annotations

import json
import secrets
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs

import anyio

UI_DIR = Path(__file__).resolve().parent / "ui"
COOKIE = "ostinato_session"
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8"}
# the page's files: path below /ui/ -> its type (only these are served)
STATIC = {p.relative_to(UI_DIR).as_posix(): TYPES[p.suffix] for p in UI_DIR.rglob("*") if p.suffix in TYPES}
SHIM_PATHS = ("/mcp", "/api/status", "/api/tools", "/api/shutdown")


async def _send(send, status: int, body: bytes, ctype: str = "text/plain; charset=utf-8", extra=()) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", ctype.encode()), (b"cache-control", b"no-store"),
                            (b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer"),
                            *extra]})
    await send({"type": "http.response.body", "body": body})


class Gate:
    """ASGI middleware in front of everything (see the module's doc)."""

    def __init__(self, app, token: str, port: int):
        self.app = app
        self.token = token
        self.bearer = f"Bearer {token}".encode()
        self.hosts = {f"127.0.0.1:{port}".encode(), f"localhost:{port}".encode()}
        self.origins = {f"http://127.0.0.1:{port}".encode(), f"http://localhost:{port}".encode()}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        h = dict(scope.get("headers") or [])
        path = scope.get("path", "")
        if h.get(b"host", b"") not in self.hosts:
            return await _send(send, 421, b"ostinato device server: wrong host\n")
        has_bearer = secrets.compare_digest(h.get(b"authorization", b""), self.bearer)
        if path == "/ui/login":  # the link with the key: set the cookie, go to the clean address
            key = (parse_qs(scope.get("query_string", b"").decode()).get("key") or [""])[0]
            if not secrets.compare_digest(key.encode(), self.token.encode()):
                return await _send(send, 403, b"ostinato: this link is not valid (any more). Open the UI with "
                                              b"`ostinato-device ui`.\n")
            cookie = f"{COOKIE}={self.token}; HttpOnly; SameSite=Strict; Path=/"
            return await _send(send, 303, b"", extra=[(b"location", b"/"), (b"set-cookie", cookie.encode())])
        if path.startswith(SHIM_PATHS) or not (path == "/" or path.startswith(("/ui/", "/api/ui/"))):
            if not has_bearer:
                return await _send(send, 401, b"ostinato device server: missing or wrong token\n")
            return await self.app(scope, receive, send)
        # the page and its API: the header, or the cookie
        jar = SimpleCookie()
        try:
            jar.load(h.get(b"cookie", b"").decode())
        except Exception:
            pass
        has_cookie = COOKIE in jar and secrets.compare_digest(jar[COOKIE].value.encode(), self.token.encode())
        if not (has_bearer or has_cookie):
            if path == "/":
                return await _send(send, 401, _NEEDS_LOGIN, "text/html; charset=utf-8")
            return await _send(send, 401, b'{"error": "not logged in: open the UI with `ostinato-device ui`"}',
                               "application/json")
        if scope["method"] not in ("GET", "HEAD") and not has_bearer:
            origin = h.get(b"origin")
            if h.get(b"x-ostinato") != b"1" or (origin is not None and origin not in self.origins):
                return await _send(send, 403, b'{"error": "refused"}', "application/json")
        return await self.app(scope, receive, send)


_NEEDS_LOGIN = b"""<!doctype html><meta charset="utf-8"><title>Ostinato</title>
<body style="font:16px system-ui;margin:3em;max-width:40em">
<h1>Ostinato</h1><p>Open this page with the command</p><pre>ostinato-device ui</pre>
<p>(it opens the link with the device server's key, valid while this device server runs).</p></body>"""


# ---------------------------------------------------------------------- the API

def _seq_json(session, track: int) -> dict:
    from .music import drums as D
    from .music import scales as SC

    seq = session.read(track)
    key = session.key(track) if seq.kind != "drums" else None
    nm = SC.namer(key)
    names = None
    if session.backend.writer is not None and seq.kind == "drums":
        names = session.backend.writer.limits(track).drum_voices
    step = seq.grid.step if seq.grid else None
    evs = []
    for e in sorted(seq.events, key=lambda e: e.start):
        evs.append({
            "start": float(e.start), "length": float(e.length),
            "step": int(e.start / step) if step else None, "steps": float(e.length / step) if step else None,
            "notes": [{"pitch": n.pitch, "name": (names or {}).get(n.pitch, D.name(n.pitch)) if seq.kind == "drums"
                       else nm(n.pitch), "velocity": n.velocity, "level": n.level, "ratchet": n.ratchet}
                      for n in e.notes],
            "accent": e.accent, "slide": e.slide, "muted": e.muted, "offset": float(e.offset),
            "extras": {k: v for k, v in e.extras.items() if k != "step"},
        })
    voices = sorted({n["pitch"] for e in evs for n in e["notes"]})
    m = seq.meter
    lim = session.backend.writer.limits(track) if session.backend.writer is not None else None
    return {"track": track, "kind": seq.kind, "length": float(seq.length),
            "meter": {"name": str(m), "bar": float(m.bar), "beat": float(m.beat)} if m else None,
            "signature": session.signature_setting(track),
            "limits": {"grids": list(lim.grids), "max_steps": lim.max_steps} if lim else None,
            "instrument": session.instrument(track),
            "grid": {"steps": seq.grid.steps, "step": float(seq.grid.step), "name": seq.grid.name} if seq.grid else None,
            "key": SC.key_name(key) if key and key.scale != "chromatic" else None,
            "key_note": key.note if key else None,
            "voices": [{"pitch": v, "name": (names or {}).get(v, D.name(v)) if seq.kind == "drums" else nm(v)}
                       for v in voices],
            "events": evs}


def _setup_classes(manager) -> list:
    from . import generative as G
    from .generative.engine import GestureSetup

    out = [G.REGISTRY[n] for n in G.names()] + [GestureSetup]
    if manager.backend is not None:
        out += manager.backend.setups()
    return out


def _setup_id(cls) -> str:
    return f"{cls.folder}:{cls.name}"


def register(mcp, manager) -> None:
    """Add the page and /api/ui/* to the device server's app (`manager`: its DeviceManager)."""
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response

    async def run(fn, *a):
        return await anyio.to_thread.run_sync(lambda: fn(*a))

    def session_or_error():
        s = manager.session
        if s is None:
            return None, JSONResponse({"error": "no device detected yet"}, status_code=409)
        e = s.not_ready()
        if e:
            return None, JSONResponse({"error": e}, status_code=409)
        return s, None

    async def body(request: Request) -> dict:
        try:
            d = await request.json()
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def track_arg(request: Request, s) -> int:
        t = int(request.path_params["track"])
        n = len(s.tracks())
        if not 1 <= t <= n:
            raise ValueError(f"track must be 1..{n}")
        return t

    # ---- the page
    @mcp.custom_route("/", methods=["GET"])
    async def page(request: Request):
        return Response((UI_DIR / "index.html").read_bytes(), media_type=STATIC["index.html"])

    @mcp.custom_route("/ui/{name:path}", methods=["GET"])
    async def static(request: Request):
        name = request.path_params["name"]
        if name not in STATIC:
            return Response("not found", status_code=404)
        return Response((UI_DIR / name).read_bytes(), media_type=STATIC[name])

    # ---- state
    @mcp.custom_route("/api/ui/state", methods=["GET"])
    async def state(request: Request):
        from . import __version__
        from .music import meter as MT

        m = manager
        s, cls = m.session, m.backend_cls
        st = m.status()
        out = {"version": __version__, "status": st, "backend": cls.title if cls else None,
               "capabilities": st.get("capabilities", []), "log_version": 0, "tracks": [], "tempo": None,
               "tempo_range": None, "tempo_note": None, "signatures": list(MT.CHOICES),
               "armed": [], "controls": [], "algorithms": [], "playing": None, "groove": None}
        if s is not None and s is m.session:  # (else the device was changed just now: the next poll has it)
            out["log_version"] = s.version
            # (not connected: as without a device; the last known state is Claude's to ask for)
            if st.get("connected") and not s.not_ready():
                out["playing"] = s.playing()

                def collect():
                    from . import generative as G

                    tracks = []
                    for t in s.tracks():
                        k = s.key(t.track) if t.kind != "drums" else None
                        from .music import scales as SC

                        tracks.append({"track": t.track, "name": t.name, "kind": t.kind, "instrument": t.instrument,
                                       "key": SC.key_name(k) if k and k.scale != "chromatic" else None,
                                       "undo": s.undo_depth(t.track),
                                       "muted": s.track_muted(t.track) if s.backend.mixer else None,
                                       "level": _mix_json(s.track_level(t.track)) if s.backend.mixer else None,
                                       "pan": _mix_json(s.track_pan(t.track)) if s.backend.mixer else None})
                    eng = s.engine
                    armed = [{"track": a.track, "algorithm": a.algorithm, "control": a.source, "kind": a.kind,
                              "runs": a.runs, "depth": a.depth, "last": a.last, "warnings": a.warnings}
                             for a in (eng.armed.values() if eng else [])]
                    algos = [{"name": n, "description": G.REGISTRY[n].description, "kinds": list(G.REGISTRY[n].kinds),
                              "initializes": G.REGISTRY[n].initializes}
                             for n in G.names()]
                    ctrls = ([{"name": n, "kind": k, "description": d} for n, k, d in s.backend.controls.sources()]
                             if s.backend.controls else [])
                    r = s.tempo_range()
                    groove = None
                    if s.groove_supported():
                        grid, why = s.groove_grid()
                        g, use, grids = s.groove(), s.groove_use(), s.grids()
                        groove = {"groove": _groove_json(g), "blocked": why,
                                  "grid": {"steps": grid.steps, "step": float(grid.step), "name": grid.name}
                                  if grid else None,
                                  "use": use.to_dict(), "playing": s.groove_tracks(),
                                  "fits_all": bool(g) and g.fits_all(list(grids.values())),
                                  "lengths": [{"track": t, "steps": gr.steps if gr else None,
                                               "step": float(gr.step) if gr else None, "name": gr.name if gr else None,
                                               "beats": float(gr.steps * gr.step) if gr else None,
                                               "fits": bool(g) and g.fits(gr)} for t, gr in grids.items()]}
                        from .music import grooves as GR

                        groove["preset"] = g.name if g is not None and g.name in GR.BY_NAME else None
                        groove["presets"] = [{"name": p.name, "label": p.label, "description": p.description,
                                              "source": p.source, "fits": fits, "compat": compat}
                                             for p, fits, compat in s.groove_presets()]
                    return {"tracks": tracks, "groove": groove, "armed": armed, "algorithms": algos, "controls": ctrls,
                            "tempo": s.tempo(), "tempo_note": s.tempo_note(),
                            "tempo_range": {"min": r[0], "max": r[1], "step": r[2]} if r else None}
                out.update(await run(collect))
        return JSONResponse(out)

    @mcp.custom_route("/api/ui/log", methods=["GET"])
    async def log(request: Request):
        s = manager.session
        if s is None:
            return JSONResponse({"version": 0, "events": []})
        since = int(request.query_params.get("since", 0))
        evs = s.events_since(since)[-200:]
        return JSONResponse({"version": s.version, "events": [
            {"version": e.version, "at": e.at, "kind": e.kind, "track": e.track, "source": e.source, "text": e.text}
            for e in evs]})

    @mcp.custom_route("/api/ui/track/{track}", methods=["GET"])
    async def track(request: Request):
        s, err = session_or_error()
        if err:
            return err
        try:
            return JSONResponse(await run(_seq_json, s, track_arg(request, s)))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @mcp.custom_route("/api/ui/track/{track}/staff.svg", methods=["GET"])
    async def staff(request: Request):
        from .music import notation as N

        s, err = session_or_error()
        if err:
            return err

        def make(t):
            seq = s.read(t)
            inst = s.instrument(t)
            names = s.backend.writer.limits(t).drum_voices if s.backend.writer and seq.kind == "drums" else None
            sc, _ = N.build_score([N.Part(f"T{t} {inst}" if inst else f"Track {t}", seq,
                                          s.key(t) if seq.kind != "drums" else None, names)], s.tempo(), 8)
            return N.render_svg(N.to_musicxml(sc), sc.ostinato_bars, 1100, 40)[0]
        try:
            svg = await run(make, track_arg(request, s))
        except Exception as e:
            return Response(f"could not draw the staff: {e!r}", status_code=500)
        return Response(svg, media_type="image/svg+xml")

    # ---- actions
    @mcp.custom_route("/api/ui/connect", methods=["POST"])
    async def connect(request: Request):
        d = await body(request)
        return JSONResponse({"text": await run(manager.connect, d.get("port") or None)})

    @mcp.custom_route("/api/ui/virtual", methods=["POST"])
    async def use_virtual(request: Request):
        """Use the virtual device."""
        try:
            await run(manager.use_virtual)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"text": "Using the virtual device (General MIDI sounds)"})

    @mcp.custom_route("/api/ui/disconnect", methods=["POST"])
    async def disconnect(request: Request):
        return JSONResponse({"text": await run(manager.disconnect)})

    def needs(s, cap: str):
        if getattr(s.backend, cap) is None:
            raise ValueError(f"this device cannot do that ({cap})")

    @mcp.custom_route("/api/ui/track/{track}/generate", methods=["POST"])
    async def generate(request: Request):
        s, err = session_or_error()
        if err:
            return err
        d = await body(request)
        try:
            needs(s, "writer")
            t = track_arg(request, s)
            r = await run(s.engine.run_once, t, d.get("algorithm") or "melodic_swapper", d.get("options") or None)
            return JSONResponse({"text": f"{r['algorithm']}: " + "; ".join(r["changes"]), "scale": r["scale"]})
        except (KeyError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @mcp.custom_route("/api/ui/track/{track}/mute", methods=["POST"])
    async def mute(request: Request):
        s, err = session_or_error()
        if err:
            return err
        d = await body(request)
        try:
            needs(s, "mixer")
            t = track_arg(request, s)
            muted = await run(s.set_track_muted, t, bool(d.get("muted", True)))
            return JSONResponse({"muted": muted, "text": f"track {t} {'muted' if muted else 'unmuted'}"})
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @mcp.custom_route("/api/ui/track/{track}/sounds", methods=["GET"])
    async def sounds(request: Request):
        s, err = session_or_error()
        if err:
            return err
        try:
            needs(s, "sounds")
            t = track_arg(request, s)
            choices = await run(s.sounds, t)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"track": t, "current": s.instrument(t),
                             "sounds": [{"group": g, "name": n} for g, n in choices]})

    @mcp.custom_route("/api/ui/track/{track}/sound", methods=["POST"])
    async def set_sound(request: Request):
        s, err = session_or_error()
        if err:
            return err
        d = await body(request)
        try:
            needs(s, "sounds")
            t = track_arg(request, s)
            before, now = await run(s.set_sound, t, str(d.get("name") or ""), d.get("group") or None)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"instrument": now, "text": f"track {t}: {now}" + (f" (was {before})" if before else "")})

    @mcp.custom_route("/api/ui/transport", methods=["POST"])
    async def transport(request: Request):
        """{"action": "play" | "stop" | "toggle"}"""
        s, err = session_or_error()
        if err:
            return err
        d = await body(request)
        try:
            now = await run(s.set_playing, str(d.get("action") or "toggle"))
        except (ValueError, RuntimeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"playing": now, "text": "playing" if now else "stopped"})

    @mcp.custom_route("/api/ui/tempo", methods=["POST"])
    async def tempo(request: Request):
        """{"bpm": n, "relative": bool}"""
        s, err = session_or_error()
        if err:
            return err
        d = await body(request)
        try:
            needs(s, "tempo_control")
            _, now = await run(s.set_tempo, d.get("bpm"), bool(d.get("relative")))
        except (ValueError, RuntimeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"tempo": now, "text": f"tempo {now:g} BPM"})

    @mcp.custom_route("/api/ui/track/{track}/mix", methods=["POST"])
    async def mix(request: Request):
        """{"level": n, "relative": bool} and / or {"pan": n}: numbers on the device's scale, or text ("-6dB")."""
        s, err = session_or_error()
        if err:
            return err
        d = await body(request)
        try:
            needs(s, "mixer")
            t = track_arg(request, s)
            out = []
            if d.get("level") is not None:
                _, a = await run(s.set_track_level, t, d["level"], bool(d.get("relative")))
                out.append(f"level {a.text}")
            if d.get("pan") is not None:
                _, a = await run(s.set_track_pan, t, d["pan"])
                out.append(f"pan {a.text}")
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"text": f"track {t}: " + ", ".join(out) if out else "nothing to change"})

    @mcp.custom_route("/api/ui/groove", methods=["POST"])
    async def groove(request: Request):
        """{"values": [n, ...], "res": 64} (per step, in 1/res of a step), or {"straight": true}: every track's."""
        s, err = session_or_error()
        if err:
            return err
        d = await body(request)
        try:
            res = int(d.get("res") or 64)
            values = d.get("values")
            if d.get("straight") and s.groove() is not None:
                after = await run(s.set_groove_straight)
                return JSONResponse({"groove": _groove_json(after), "text": "every step at 0%"})
            if d.get("custom"):
                after = await run(s.set_groove_custom)
                return JSONResponse({"groove": _groove_json(after), "text": "custom groove"})
            if d.get("preset"):
                _, after = await run(s.set_groove_preset, str(d["preset"]))
                return JSONResponse({"groove": _groove_json(after), "text": "groove preset set"})
            if d.get("straight") or not values:
                percents = None
            elif isinstance(values, list) and all(isinstance(v, int) and not isinstance(v, bool) for v in values):
                percents = [v * 100 / res for v in values]
            else:
                raise ValueError("values: whole numbers (1/res of a step)")
            _, after = await run(s.set_groove, percents, res)
        except (ValueError, RuntimeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"groove": _groove_json(after), "text": "groove set" if after else "straight"})

    @mcp.custom_route("/api/ui/groove/use", methods=["POST"])
    async def groove_use(request: Request):
        """{"all": bool} and / or {"track": n, "on": bool}: which tracks play the groove; {"none": true}: none (the
        groove is kept)."""
        s, err = session_or_error()
        if err:
            return err
        d = await body(request)
        try:
            all_tracks = bool(d["all"]) if "all" in d else None
            tracks = {int(d["track"]): bool(d.get("on"))} if "track" in d else None
            if d.get("none"):
                all_tracks, tracks = False, {t.track: False for t in s.tracks()}
            use = await run(s.set_groove_use, all_tracks, tracks)
        except (ValueError, RuntimeError, TypeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"use": use.to_dict(), "text": "groove on all tracks" if use.all else
                             "groove on " + (", ".join(f"track {t}" for t in use.tracks) or "no track")})

    @mcp.custom_route("/api/ui/track/{track}/undo", methods=["POST"])
    async def undo(request: Request):
        s, err = session_or_error()
        if err:
            return err
        try:
            needs(s, "writer")
            label = await run(s.undo, track_arg(request, s))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"text": f"undid {label}" if label else "nothing to undo"})

    @mcp.custom_route("/api/ui/track/{track}/signature", methods=["POST"])
    async def signature(request: Request):
        """{"signature": "auto" | "6/8" ...}"""
        s, err = session_or_error()
        if err:
            return err
        d = await body(request)
        try:
            t = track_arg(request, s)
            _, now = await run(s.set_signature, t, str(d.get("signature") or ""))
        except (ValueError, OSError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"signature": s.signature_setting(t), "meter": str(now), "text": f"track {t}: {now}"})

    @mcp.custom_route("/api/ui/track/{track}/length", methods=["POST"])
    async def length(request: Request):
        """{"steps": n} and / or {"div": "1/16"}"""
        s, err = session_or_error()
        if err:
            return err
        d = await body(request)
        try:
            needs(s, "writer")
            t = track_arg(request, s)
            _, removed = await run(s.set_length, t, d.get("steps"), d.get("div") or None)
            g = (await run(s.read, t)).grid
        except (ValueError, RuntimeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"text": f"track {t}: {g.steps} steps of {g.name}"
                             + (f" ({removed} note(s) past the end removed: Undo brings them back)" if removed else "")})

    @mcp.custom_route("/api/ui/track/{track}/clear", methods=["POST"])
    async def clear(request: Request):
        s, err = session_or_error()
        if err:
            return err
        d = await body(request)
        try:
            needs(s, "writer")
            rep, removed, what = await run(s.clear, track_arg(request, s), bool(d.get("keep_length", True)),
                                           d.get("note_only") or None)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"text": f"cleared {what}: {removed} note(s) removed"})

    @mcp.custom_route("/api/ui/track/{track}/arm", methods=["POST"])
    async def arm(request: Request):
        s, err = session_or_error()
        if err:
            return err
        d = await body(request)
        try:
            needs(s, "controls")
            a = await run(s.engine.arm, track_arg(request, s), d.get("algorithm") or "melodic_swapper",
                          d.get("control") or None, d.get("options") or None)
            return JSONResponse({"text": f"armed: {a.algorithm} on {a.source}", "warnings": a.warnings})
        except (KeyError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @mcp.custom_route("/api/ui/track/{track}/disarm", methods=["POST"])
    async def disarm(request: Request):
        s, err = session_or_error()
        if err:
            return err
        d = await body(request)
        try:
            a = await run(s.engine.disarm, track_arg(request, s), bool(d.get("restore")))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"text": "not armed" if a is None else
                             f"disarmed after {a.runs} run(s)" + (", restored" if d.get("restore") else "")})

    # ---- settings
    # ---- saved states
    def loaded_json(s) -> dict | None:
        x = s.loaded
        if x is None:
            return None
        out = {"name": x.name, "path": str(x.path), "summary": x.summary(), "exact": x.exact is not None,
               "tracks": [t["track"] for t in x.data.get("tracks", [])], "compare": None,
               "parts": list(s.backend.states.parts) if s.backend.states else ["notes", "sound"]}
        if not s.not_ready():
            try:
                out["compare"] = s.compare_state()
            except ValueError as e:
                out["compare"] = [str(e)]
        return out

    @mcp.custom_route("/api/ui/states", methods=["GET"])
    async def states_list(request: Request):
        from . import states as ST

        s = manager.session
        xs = await run(ST.list_states)
        return JSONResponse({"dir": str(ST.states_dir()), "states": xs,
                             "loaded": await run(loaded_json, s) if s is not None else None})

    @mcp.custom_route("/api/ui/states/{op}", methods=["POST"])
    async def states_op(request: Request):
        from . import states as ST

        s, err = session_or_error()
        if err:
            return err
        d = await body(request)
        op = request.path_params["op"]
        try:
            if op == "save":
                if s.not_ready():
                    raise ValueError(s.not_ready())
                x, w = await run(s.save_state, str(d.get("name") or ""), d.get("note") or None,
                                 bool(d.get("exact", True)), bool(d.get("overwrite")))
                return JSONResponse({"text": f"saved {x.name!r}" + (f" ({'; '.join(w)})" if w else "")})
            if op == "load":
                x = await run(s.load_state, str(d.get("name") or ""))
                return JSONResponse({"text": f"loaded {x.name!r} (not applied)", "loaded": await run(loaded_json, s)})
            if op == "apply":
                needs(s, "writer")
                out = await run(s.apply_state, d.get("tracks") or None, d.get("parts") or None, bool(d.get("exact")))
                return JSONResponse({"text": "applied: " + "; ".join(out), "lines": out})
            if op == "delete":
                p = await run(ST.delete, str(d.get("name") or ""))
                return JSONResponse({"text": f"deleted {p.name}"})
        except (ST.StateError, ValueError, OSError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"error": f"no such operation: {op}"}, status_code=404)

    @mcp.custom_route("/api/ui/setups", methods=["GET"])
    async def setups(request: Request):
        def collect():
            out = []
            for cls in _setup_classes(manager):
                d = cls.describe()
                d["id"] = _setup_id(cls)
                d["title"] = {"generators:gesture": "Gestures (generative mode)",
                              "virtual_gm:device": "Virtual device"}.get(d["id"], cls.name.replace("_", " "))
                d["group"] = ("Algorithms" if d["id"].startswith("generators:") and d["id"] != "generators:gesture"
                              else "Generative mode" if d["id"] == "generators:gesture" else "Device")
                out.append(d)
            return out
        return JSONResponse({"setups": await run(collect)})

    def find_setup(sid: str):
        return next((c for c in _setup_classes(manager) if _setup_id(c) == sid), None)

    @mcp.custom_route("/api/ui/setups/{sid}", methods=["PUT", "DELETE"])
    async def setup_save(request: Request):
        cls = find_setup(request.path_params["sid"])
        if cls is None:
            return JSONResponse({"error": "no such setup"}, status_code=404)
        if request.method == "DELETE":
            conf = await run(cls.reset)
        else:
            d = await body(request)
            conf = await run(cls.save, d.get("values") or {})
        return JSONResponse({"values": conf.values, "notes": conf.notes, "source": conf.source,
                             "exists": conf.path.exists()})


def _groove_json(g) -> dict | None:
    return {"values": list(g.values), "res": g.res, "step": float(g.step)} if g else None


def _mix_json(m) -> dict | None:
    if m is None:
        return None
    return {"value": m.value, "min": m.min, "max": m.max, "text": m.text, "db": m.to_db(), "db_zero": m.db_zero,
            "db_step": m.db_step}


def ui_url(info: dict) -> str:
    return f"{info['url']}/ui/login?key={info['token']}"
