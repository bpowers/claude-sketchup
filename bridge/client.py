#!/usr/bin/env python3
"""Drive SketchUp through the file bridge (bridge/watcher.rb; protocol in bridge/README.md).

Each call writes one request with a unique id to bridge/trigger, waits for out/bridge/<id>.json, and returns its
result, or raises BridgeError carrying the Ruby class, message, backtrace and the element a build failed on. A
document is planned (ir/plan.py) before anything is sent, so a document that does not plan raises Refused, naming
each refused element, and SketchUp never sees it.

    uv run python bridge/client.py ping
    uv run python bridge/client.py eval 'Sketchup.active_model.title'
    uv run python bridge/client.py build house.json [--top NAME]   # draw a document whole (HOUSE by default)
    uv run python bridge/client.py sync out/areas/manifest.json [--rebuild]
    uv run python bridge/client.py built                          # what HOUSE was built from
    uv run python bridge/client.py render overlay/requests/x.json [--expect-house house.json]
    uv run python bridge/client.py views [--view NAME]            # overview pictures into out/views/<NAME>/
    uv run python bridge/client.py save [PATH]                    # the open model to house.skp, or a copy to PATH

From Python:

    from bridge.client import Bridge
    br = Bridge()
    br.build("house.json")              # build stats; Refused before sending when it does not plan
    br.render({"out": "out/renders/x.png", ...}, expect_sha256=sha)

Requests from any number of processes are serialised with a lock on out/bridge/.lock. On macOS a SketchUp in the
background is throttled by App Nap, its timer slowed until a request sits unread; when nothing picks a request up
within `pickup_timeout` seconds the client brings SketchUp to the front and keeps waiting, up to `max_pickup`.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import plistlib
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:               # run as a script (uv run python bridge/client.py), not as a module
    sys.path.insert(0, str(ROOT))

from ir import plan as irplan  # noqa: E402

PROTOCOL = 1
TOP = "HOUSE"
SHA256_RE = re.compile(r"[0-9a-f]{64}")
PLANS_KEPT = 24                 # the plans build() keeps in out/plans, the most recent
APP_ID_ENV = "TWIN_SKETCHUP_APP_ID"   # the bundle id to activate, where the detected one is wrong


class BridgeError(RuntimeError):
    """SketchUp answered with an error, never answered, or the client refused to send the request."""

    def __init__(self, message: str, reply: dict | None = None):
        self.reply = reply or {}
        err = self.reply.get("error") or {}
        self.ruby_class: str | None = err.get("class")
        self.ruby_message: str = err.get("message", message)
        self.backtrace: list[str] = list(err.get("backtrace") or [])
        self.element_id: str | None = err.get("element_id")
        text = message
        if self.backtrace:
            text += "\nRuby backtrace:\n" + "\n".join("  " + ln for ln in self.backtrace[:12])
        super().__init__(text)


class BridgeTimeout(BridgeError):
    """No reply within the time allowed."""


class Refused(BridgeError):
    """A document that does not plan: nothing was sent. `plan` holds the refusals, `element_ids` the elements."""

    def __init__(self, path, plan: irplan.Plan):
        self.plan = plan
        super().__init__(f"refusing to build {path}: {plan.message()}")
        self.element_ids = [f.element_id for f in plan.refusals]
        self.element_id = self.element_ids[0] if self.element_ids else None


def sha256_file(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def new_id() -> str:
    return f"{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}-{secrets.token_hex(4)}"


def running_app(ps_text: str) -> str | None:
    """The .app bundle of a running SketchUp, from `ps -axo command=` output, or None."""
    m = re.search(r"^(.*?/SketchUp[^/]*\.app)/Contents/MacOS/", ps_text, re.M)
    return m.group(1) if m else None


def bundle_id(app: str | Path) -> str | None:
    try:
        with open(Path(app) / "Contents" / "Info.plist", "rb") as fh:
            return plistlib.load(fh).get("CFBundleIdentifier")
    except (OSError, plistlib.InvalidFileException):
        return None


class Bridge:
    def __init__(self, root: str | Path | None = None, *, timeout: float = 1800.0, pickup_timeout: float = 6.0,
                 max_pickup: float = 120.0, activate: bool = True, poll: float = 0.05, verbose: bool = False):
        self.root = Path(root).resolve() if root else ROOT
        self.trigger = self.root / "bridge" / "trigger"
        self.out = self.root / "out"
        self.replies = self.out / "bridge"
        self.status = self.out / "status.json"
        self.timeout = timeout
        self.pickup_timeout = pickup_timeout
        self.max_pickup = max_pickup
        self.activate = activate
        self.poll = poll
        self.verbose = verbose

    # ---------------------------------------------------------------- commands

    def ping(self) -> dict:
        return self.request("ping")["result"]

    def eval(self, code: str) -> Any:
        """Ruby evaluated inside SketchUp, its value as JSON data (what JSON cannot carry comes back as its inspect
        string; request("eval", code=...)["eval"] always holds the inspect string)."""
        return self.request("eval", code=code).get("result")

    def build(self, ir_path: str | Path, top: str = TOP) -> dict:
        """Draw a house document whole into the top group `top` (HOUSE, or a test's). The document is planned first
        and the plan is what SketchUp is sent; Refused when it does not plan. Returns the build stats, with the plan's
        warnings under "plan_warnings"."""
        path = Path(ir_path).resolve()
        raw = path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as e:
            raise BridgeError(f"refusing to build {path}: not valid JSON: {e}") from e
        plan = irplan.plan_document(doc, str(path))
        if not plan.ok:
            raise Refused(path, plan)
        plans = self.out / "plans"
        plan_path = irplan.write_plan(plan, path, sha, plans / f"{sha[:16]}.json")
        os.utime(plan_path)                     # write_plan leaves an unchanged plan's time; this one is in use now
        others = sorted((p for p in plans.glob("*.json") if p != plan_path), key=lambda p: p.stat().st_mtime,
                        reverse=True)
        for old in others[PLANS_KEPT - 1:]:
            old.unlink(missing_ok=True)
        result = self.request("build", path=str(plan_path), top=top)["result"]
        if not isinstance(result, dict) or result.get("ir_sha256") != sha:
            raise BridgeError(f"SketchUp answered {str(result)[:200]!r}, not the stats of a build of {path} "
                              f"(sha256 {sha[:12]})")
        result["plan_warnings"] = [str(w) for w in plan.warnings]
        return result

    def sync(self, manifest_path: str | Path, rebuild: bool = False) -> dict:
        """Make HOUSE hold what an areas manifest places (areasc.py), changing only what changed unless `rebuild`.
        Refuses a manifest compiled on a house.json other than the one on disk."""
        path = Path(manifest_path).resolve()
        man = json.loads(path.read_text())
        house = Path(man.get("house", {}).get("path", ""))
        if house.is_file() and sha256_file(house) != man["house"].get("sha256"):
            raise BridgeError(f"{house} has changed since {path} was compiled from it; compile again (areasc.py)")
        return self.request("sync", path=str(path), rebuild=bool(rebuild))["result"]

    def built(self) -> dict | None:
        """What HOUSE was built from: {ir, ir_sha256, manifest_sha256, build_sha256, built_at, elements, top}, or None
        when the model has no HOUSE."""
        return self.request("built")["result"]

    def render(self, request: str | Path | dict, expect_sha256: str | None = None) -> dict:
        """Render a request (docs/IR.md "Render requests"), given as a dict or a path to one. A relative "out" is
        resolved against the project root. With expect_sha256, SketchUp refuses unless the top group drawn records
        that house sha. Returns the result (out, width, height, ir, ir_sha256, camera, ...)."""
        if expect_sha256 is not None and not SHA256_RE.fullmatch(str(expect_sha256)):
            raise BridgeError(f"expect_sha256 must be 64 lowercase hex digits, got {expect_sha256!r}")
        req = dict(request) if isinstance(request, dict) else json.loads(Path(request).read_text())
        if isinstance(req.get("out"), str) and not os.path.isabs(req["out"]):
            req["out"] = str(self.root / req["out"])
        fields: dict[str, Any] = {"request": req}
        if expect_sha256 is not None:
            fields["expect_ir_sha256"] = expect_sha256
        return self.request("render", **fields)["result"]

    def views(self, view: str | None = None) -> list:
        """Four overview pictures of HOUSE in out/views/<view>/: today's house, or with `view` a design view of
        zones/areas.star, whose tags are read from out/areas/manifest.json. Returns the files written."""
        areas: list = []
        if view:
            manifest = self.out / "areas" / "manifest.json"
            try:
                views = json.loads(manifest.read_text()).get("views", {})
            except (OSError, ValueError):
                raise BridgeError(f"no readable {manifest}; compile the design areas first (look.py)") from None
            if view not in views:
                raise BridgeError(f"no view {view!r} in {manifest} (views: {', '.join(sorted(views)) or 'none'})")
            areas = list(views[view]["tags"])
        return self.request("views", name=view or "today", areas=areas)["result"]

    def save(self, path: str | Path | None = None) -> dict:
        """With no path, save the open model as the project's house.skp (the watcher refuses when the open model is
        another file); with a path, save a copy there."""
        return self.request("save", path=str(Path(path).resolve()) if path else None)["result"]

    # ---------------------------------------------------------------- protocol

    def request(self, cmd: str, *, timeout: float | None = None, **fields: Any) -> dict:
        """Send one request and return the whole reply (state "ok"); BridgeError on an error reply, BridgeTimeout when
        none comes in time."""
        rid = new_id()
        self.replies.mkdir(parents=True, exist_ok=True)
        reply_path = self.replies / f"{rid}.json"
        with self._lock():
            self._write_trigger(json.dumps({"bridge": PROTOCOL, "id": rid, "cmd": cmd, **fields}))
            self._log(f"sent {cmd} {rid}")
            reply = self._await(rid, cmd, reply_path, self.timeout if timeout is None else timeout)
        with contextlib.suppress(FileNotFoundError):
            reply_path.unlink()
        if reply.get("state") != "ok":
            raise BridgeError(f"SketchUp {cmd} failed: {reply.get('message')}", reply)
        return reply

    @contextlib.contextmanager
    def _lock(self) -> Iterator[None]:
        with open(self.replies / ".lock", "a+") as fh:
            if os.name == "nt":                 # SketchUp also runs on Windows, where there is no flock
                import msvcrt
                while True:
                    try:
                        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                        break
                    except OSError:
                        time.sleep(0.1)
                try:
                    yield
                finally:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def _write_trigger(self, text: str) -> None:
        self.trigger.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.trigger.with_name(f".trigger.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        tmp.write_text(text)
        os.replace(tmp, self.trigger)

    @staticmethod
    def _read_json(path: Path) -> dict | None:
        try:
            with open(path) as fh:
                return json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def _await(self, rid: str, cmd: str, reply_path: Path, timeout: float) -> dict:
        t0 = time.monotonic()
        next_nudge = t0 + self.pickup_timeout
        picked_up = False
        while True:
            reply = self._read_json(reply_path)
            if reply is not None:
                if reply.get("state") != "running":
                    return reply
                if not picked_up:
                    self._log(f"picked up after {time.monotonic() - t0:.1f} s")
                picked_up = True
            now = time.monotonic()
            if not picked_up:
                if now - t0 > self.max_pickup:
                    raise BridgeTimeout(f"request {rid} ({cmd}) was not picked up within {self.max_pickup:g} s. Is "
                                        "SketchUp open with a model window and no dialog up, and does its watcher "
                                        f"serve this project ({self.root})? It serves the folder whose install line "
                                        f"was pasted into SketchUp. Last status: {self._status()}")
                if self.activate and now >= next_nudge:
                    self._nudge(rid, cmd)
                    next_nudge = now + self.pickup_timeout
            if timeout and now - t0 > timeout:
                raise BridgeTimeout(f"request {rid} ({cmd}) did not finish within {timeout:g} s")
            time.sleep(self.poll)

    def _nudge(self, rid: str, cmd: str) -> None:
        """Bring SketchUp to the front (macOS App Nap), or say plainly that it is not running."""
        if sys.platform != "darwin":
            return
        try:
            r = subprocess.run(["ps", "-axo", "command="], capture_output=True, text=True, timeout=10)
            ps = r.stdout if r.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            ps = ""
        app = running_app(ps)
        # only a full process list (ps can be fenced off) says SketchUp is not running
        if app is None and ps.count("\n") > 10:
            raise BridgeTimeout(f"request {rid} ({cmd}) was not picked up, and SketchUp is not running; open it (the "
                                "watcher starts with it once installed, see bridge/README.md)")
        app_id = os.environ.get(APP_ID_ENV) or (bundle_id(app) if app else None)
        scripts = [f'tell application id "{app_id}" to activate'] if app_id else []
        for script in scripts + ['tell application "SketchUp" to activate']:
            self._log(f"no pickup yet; {script}")
            try:
                if subprocess.run(["osascript", "-e", script], capture_output=True, timeout=15).returncode == 0:
                    return
            except (OSError, subprocess.TimeoutExpired):
                continue
        self._log("could not bring SketchUp to the front with osascript")

    def _status(self) -> str:
        st = self._read_json(self.status)
        if st is None:
            return ("none: no watcher has ever answered in this project (out/status.json is missing); paste the "
                    "install line from this folder (README.md, Setup)")
        return f"{st.get('at')} {st.get('state')} {str(st.get('message'))[:160]!r}"

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[bridge] {msg}", file=sys.stderr)


# What `sync` and `build` print of build.rb's stats: what the build did, not every counter it keeps.
SUMMARY_KEYS = ("top", "ir_sha256", "manifest_sha256", "elements", "seconds", "in_place", "built", "instanced", "kept",
                "erased", "placements", "volume_checks", "intersection_pairs", "intersection_pairs_drawn", "phases",
                "warnings", "plan_warnings")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Drive SketchUp through the file bridge.")
    ap.add_argument("--timeout", type=float, default=1800.0, help="seconds to wait for a request to finish")
    ap.add_argument("--max-pickup", type=float, default=120.0, help="seconds to wait for SketchUp to pick it up")
    ap.add_argument("--no-activate", action="store_true", help="never bring SketchUp to the front")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ping")
    sub.add_parser("eval").add_argument("code")
    p = sub.add_parser("build", help="plan a document and draw it whole")
    p.add_argument("path")
    p.add_argument("--top", default=TOP, help="the top group to draw into (default HOUSE)")
    p.add_argument("--full", action="store_true", help="print every field of the build's stats")
    p = sub.add_parser("sync", help="make HOUSE hold what an areas manifest places")
    p.add_argument("path")
    p.add_argument("--rebuild", action="store_true", help="draw everything again rather than in place")
    p.add_argument("--full", action="store_true", help="print every field of the build's stats")
    sub.add_parser("built")
    p = sub.add_parser("render")
    p.add_argument("path")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--expect-sha", help="refuse unless HOUSE records this house sha256")
    g.add_argument("--expect-house", type=Path, help="refuse unless HOUSE was built from this file as it stands")
    sub.add_parser("views", help="overview pictures of HOUSE in out/views/today/, or a design view's").add_argument(
        "--view", help="a view of zones/areas.star (VIEWS) to show instead of today's house")
    sub.add_parser("save").add_argument("path", nargs="?")
    a = ap.parse_args(argv)

    br = Bridge(timeout=a.timeout, max_pickup=a.max_pickup, activate=not a.no_activate, verbose=a.verbose)
    try:
        if a.cmd == "ping":
            out = br.ping()
        elif a.cmd == "eval":
            out = br.eval(a.code)
        elif a.cmd in ("build", "sync"):
            out = br.build(a.path, top=a.top) if a.cmd == "build" else br.sync(a.path, rebuild=a.rebuild)
            out = out if a.full else {k: v for k, v in out.items() if k in SUMMARY_KEYS}
        elif a.cmd == "built":
            out = br.built()
        elif a.cmd == "render":
            out = br.render(a.path, expect_sha256=sha256_file(a.expect_house) if a.expect_house else a.expect_sha)
        elif a.cmd == "views":
            out = br.views(a.view)
        else:
            out = br.save(a.path)
    except (BridgeError, OSError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 1
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
