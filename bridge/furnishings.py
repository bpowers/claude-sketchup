"""Loose furniture placed in SketchUp from a list: furnishings/pieces.json drawn into the top-level group FURNISHINGS.

    uv run python bridge/furnishings.py place [--only ID ...] [--list furnishings/pieces.json]
    uv run python bridge/furnishings.py remove ID [ID ...]
    uv run python bridge/furnishings.py list [--list furnishings/pieces.json]

Furniture is in no drawing and no photo, so it takes none of the house's pipeline: it is not in house.json, no build
or sync touches FURNISHINGS, and photo renders never show it. The list says which piece stands where, turned which
way, on which tag; bridge/furnishings.rb draws it (its header says what each command does in SketchUp):

- place: creates the listed pieces FURNISHINGS lacks and brings those still where they were placed to their entries.
  A piece the owner moved, turned or scaled by hand stays where they put it and is named; to have the list's place
  again, remove it and place it.
- remove: takes the named pieces out of FURNISHINGS.
- list: what FURNISHINGS holds, against the list.

A piece (furnishings/README.md has the details):
    {"id": "sofa", "recipe": "sofa", "at": [x, y, z], "rotation": 90, "size": [w, d, h], "params": {...},
     "finish": "fabric_gray", "tag": "furnishings/living", "note": "..."}
or a component, a .skp file (a 3D Warehouse download) in furnishings/components/, at its own size:
    {"id": "lamp", "component": "furnishings/components/lamp.skp", "at": [...], "rotation": 0,
     "tag": "furnishings/living"}

The list is checked here first, and anything it or the command names that is not a piece is refused before anything
is sent: ids are lowercase letters, digits and _, so no command reaches a group but furn/<id> in FURNISHINGS. Each
command is one bridge eval and one undo step in SketchUp, run under the look lock (look.py), since every look shares
the one SketchUp. Finishes are build/palette.json's keys or one of FABRICS; every place sends their colours, so a
material the model lacks is made with the palette's colour.

The exit status is 0 when the command went through, 1 when SketchUp refused or failed it, and 2 when the list or the
command is refused here and nothing is sent.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path

if __package__ in (None, ""):                    # run as a script: the project root, so `look` and `bridge` import
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import look  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RB = ROOT / "bridge" / "furnishings.rb"
LIST = ROOT / "furnishings" / "pieces.json"
COMPONENTS = ROOT / "furnishings" / "components"
PALETTE = ROOT / "build" / "palette.json"
TOP = "FURNISHINGS"                              # furnishings.rb TOP_NAME
PIECE_PREFIX = "furn/"                           # furnishings.rb PIECE_PREFIX
ID_RE = re.compile(r"[a-z][a-z0-9_]*")           # furnishings.rb ID_RE
TAG_RE = re.compile(r"furnishings/[a-z][a-z0-9_]*")   # furnishings.rb TAG_RE
# furnishings.rb RECIPES: each recipe's params. A name ending in _finish is a material key, one in COUNTS a whole
# number in its range, any other a positive number of inches. The defaults live in furnishings.rb alone;
# tests/test_furnishings.py holds the two files to the same names.
RECIPES = {
    "box": set(),
    "table": {"top", "leg", "inset"},
    "chair": {"seat_height", "seat", "leg", "back", "back_drop", "seat_finish"},
    "sofa": {"seat_height", "arm_height", "arm", "back", "back_cushion", "lean", "leg_height", "base", "cushions",
             "arm_left", "arm_right", "leg_finish"},
    "bed": {"frame_height", "mattress", "headboard", "pillows", "bedding_finish"},
    "rug": set(),
}
COUNTS = {"cushions": (1, 6), "pillows": (0, 6), "arm_left": (0, 1), "arm_right": (0, 1)}   # furnishings.rb COUNTS
# furnishings.rb RECIPES: the size [width, depth, height] a recipe draws when a piece gives none. furnishings.rb fills
# it in; this copy is for footprints read here (the tests), and tests/test_furnishings.py holds the two the same.
SIZES = {"box": (36.0, 18.0, 30.0), "table": (60.0, 36.0, 30.0), "chair": (18.0, 20.0, 32.0),
         "sofa": (84.0, 36.0, 32.0), "bed": (60.0, 80.0, 44.0), "rug": (96.0, 60.0, 0.5)}
# Upholstery and bedding: colours a recipe may name beside the house's palette, which holds what the house is built of.
FABRICS = {
    "fabric_gray": {"rgb": [128, 128, 124]},
    "fabric_linen": {"rgb": [214, 205, 186]},
    "fabric_white": {"rgb": [238, 236, 230]},
    "fabric_navy": {"rgb": [44, 56, 86]},
    "fabric_green": {"rgb": [86, 110, 82]},
    "fabric_rust": {"rgb": [160, 86, 56]},
}
LIST_KEYS = {"about", "pieces"}
PIECE_KEYS = {"id", "recipe", "component", "at", "rotation", "size", "params", "finish", "tag", "note"}


class FurnishingsError(ValueError):
    """A list or a command that cannot be sent: nothing reaches SketchUp."""


# ------------------------------------------------------------------ the list

def materials(palette=PALETTE) -> dict:
    """Every finish a piece may name, with its colour: the palette's keys and FABRICS."""
    path = Path(palette)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        raise FurnishingsError(f"no palette at {path}") from None
    except ValueError as e:
        raise FurnishingsError(f"{path} is not JSON: {e}") from None
    colours = {k: {c: v[c] for c in ("rgb", "alpha") if c in v} for k, v in data.items()
               if isinstance(v, dict) and "rgb" in v}
    return {**colours, **FABRICS}


def piece_id(name) -> str:
    """The id a piece's group name furn/<id> carries, from the id or the whole name. Anything else is refused, so a
    command names only furn/* pieces."""
    text = name[len(PIECE_PREFIX):] if isinstance(name, str) and name.startswith(PIECE_PREFIX) else name
    if not isinstance(text, str) or not ID_RE.fullmatch(text):
        raise FurnishingsError(f"{name!r} names no furnishings piece: a piece is furn/<id>, its id lowercase letters, "
                               "digits and _ starting with a letter")
    return text


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def component_path(rel, root=ROOT) -> Path:
    """A component's file: a .skp path relative to the project root, under furnishings/."""
    if not isinstance(rel, str) or not rel.lower().endswith(".skp") or os.path.isabs(rel):
        raise FurnishingsError(f"a component is a .skp file's path relative to the project, as "
                               f"furnishings/components/<name>.skp, got {rel!r}")
    path = (Path(root) / rel).resolve()
    if (Path(root) / "furnishings").resolve() not in path.parents:
        raise FurnishingsError(f"component {rel!r} lies outside furnishings/")
    return path


def check_piece(p, where, finishes, root=ROOT) -> dict:
    """One piece of the list, checked (module doc), as furnishings.rb reads it: a component's path made absolute."""
    if not isinstance(p, dict):
        raise FurnishingsError(f"{where} must be an object")
    pid = p.get("id")
    if not isinstance(pid, str) or not ID_RE.fullmatch(pid):
        raise FurnishingsError(f"{where}: {pid!r} names no furnishings piece: an id is lowercase letters, digits and _ "
                               f"starting with a letter, its group {PIECE_PREFIX}<id>")
    where = f"{where} ({pid})"
    extra = set(p) - PIECE_KEYS
    if extra:
        raise FurnishingsError(f"{where} has unknown keys {', '.join(sorted(extra))}; a piece has "
                               f"{', '.join(sorted(PIECE_KEYS))}")
    if ("recipe" in p) == ("component" in p):
        raise FurnishingsError(f"{where} names a recipe or a component, one of the two")
    tag = p.get("tag")
    if not isinstance(tag, str) or not TAG_RE.fullmatch(tag):
        raise FurnishingsError(f"{where} tag must be furnishings/<room>, got {tag!r}")
    at = p.get("at")
    if not (isinstance(at, list) and len(at) == 3 and all(_number(v) for v in at)):
        raise FurnishingsError(f"{where} at must be three numbers (model inches), got {at!r}")
    rot = p.get("rotation", 0.0)
    if not _number(rot):
        raise FurnishingsError(f"{where} rotation must be a number of degrees, got {rot!r}")
    out = {"id": pid, "at": [float(v) for v in at], "rotation": float(rot), "tag": tag}
    if "component" in p:
        for k in ("size", "params", "finish"):
            if k in p:
                raise FurnishingsError(f"{where} is a component, which keeps its own {k}")
        out["component"] = str(component_path(p["component"], root))
        return out
    recipe = p["recipe"]
    if recipe not in RECIPES:
        raise FurnishingsError(f"{where} names no recipe {recipe!r}; the recipes are {', '.join(sorted(RECIPES))}")
    out["recipe"] = recipe
    if "size" in p:
        size = p["size"]
        if not (isinstance(size, list) and len(size) == 3 and all(_number(v) and v > 0 for v in size)):
            raise FurnishingsError(f"{where} size must be three positive numbers [width, depth, height], got {size!r}")
        out["size"] = [float(v) for v in size]
    if "finish" in p:
        if p["finish"] not in finishes:
            raise FurnishingsError(f"{where} finish must be a palette key or a fabric, got {p['finish']!r}")
        out["finish"] = p["finish"]
    params = p.get("params", {})
    if not isinstance(params, dict):
        raise FurnishingsError(f"{where} params must be an object")
    for k, v in params.items():
        if k not in RECIPES[recipe]:
            raise FurnishingsError(f"{where}: {recipe} takes no param {k!r}; its params are "
                                   f"{', '.join(sorted(RECIPES[recipe])) or 'none'}")
        if k.endswith("_finish"):
            if v not in finishes:
                raise FurnishingsError(f"{where} {k} must be a palette key or a fabric, got {v!r}")
        elif k in COUNTS:
            lo, hi = COUNTS[k]
            if not (_number(v) and v == int(v) and lo <= v <= hi):
                raise FurnishingsError(f"{where} {k} must be a whole number from {lo} to {hi}, got {v!r}")
        elif not (_number(v) and v > 0):
            raise FurnishingsError(f"{where} {k} must be a positive number of inches, got {v!r}")
    out["params"] = dict(params)
    return out


def load_list(path=LIST, finishes=None, root=ROOT) -> list[dict]:
    """The placement list at path, checked: its pieces as check_piece gives them. FurnishingsError for anything the
    module doc refuses."""
    path = Path(path)
    try:
        doc = json.loads(path.read_text())
    except FileNotFoundError:
        raise FurnishingsError(f"no placement list {path}") from None
    except ValueError as e:
        raise FurnishingsError(f"{path} is not JSON: {e}") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("pieces"), list):
        raise FurnishingsError(f"{path} must hold an object with a list of pieces")
    extra = set(doc) - LIST_KEYS
    if extra:
        raise FurnishingsError(f"{path} has unknown keys {', '.join(sorted(extra))}")
    finishes = materials() if finishes is None else finishes
    pieces = [check_piece(p, f"pieces[{i}]", finishes, root) for i, p in enumerate(doc["pieces"])]
    ids = [p["id"] for p in pieces]
    twice = sorted({i for i in ids if ids.count(i) > 1})
    if twice:
        raise FurnishingsError(f"{path} lists {', '.join(twice)} more than once")
    return pieces


# ------------------------------------------------------------------ the spec

def spec_for(cmd, pieces=(), *, only=(), ids=(), colours=None) -> dict:
    """The spec furnishings.rb runs (its header): place sends the pieces (all, with `only` naming those to place)
    and every finish's colour; remove sends the ids; list sends nothing more."""
    if cmd == "place":
        known = [p["id"] for p in pieces]
        wanted = [piece_id(i) for i in only]
        unknown = [i for i in wanted if i not in known]
        if unknown:
            raise FurnishingsError(f"--only names pieces the list does not: {', '.join(unknown)}")
        gone = [p["component"] for p in pieces if "component" in p and (not wanted or p["id"] in wanted)
                and not Path(p["component"]).is_file()]
        if gone:
            raise FurnishingsError(f"no component file {', '.join(gone)}")
        return {"cmd": "place", "pieces": list(pieces), "only": wanted, "materials": colours or {}}
    if cmd == "remove":
        if not ids:
            raise FurnishingsError("remove needs the ids of the pieces to take out")
        return {"cmd": "remove", "ids": [piece_id(i) for i in ids]}
    if cmd == "list":
        return {"cmd": "list"}
    raise FurnishingsError(f"no command {cmd!r}; the commands are place, remove and list")


def ruby_string(text: str) -> str:
    """text as a Ruby single-quoted literal."""
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def eval_code(spec, rb=RB) -> str:
    """The one eval: this project's furnishings.rb loaded by absolute path and run on the spec's JSON."""
    return f"load {ruby_string(str(Path(rb).resolve()))}; HouseTwin::Furnishings.run({ruby_string(json.dumps(spec))})"


# ------------------------------------------------------------------ the run

def _ids(v) -> str:
    return ", ".join(v) if v else "none"


def report(cmd, result, pieces=()) -> list[str]:
    """What SketchUp did, in words."""
    if not isinstance(result, dict):
        return [f"== furnishings.rb answered {str(result)[:200]!r}"]
    lines = []
    if cmd == "place":
        if result.get("furnishings_created"):
            lines.append(f"== {TOP} created: a top-level group of its own, which no build touches")
        for key, words in (("created", "created"), ("updated", "brought to the list"), ("unchanged", "as listed")):
            lines.append(f"== {words}: {_ids(result.get(key))}")
        for key, words in (("moved", "moved by hand in SketchUp, left where they are (remove and place one to "
                                     "have the list's place again)"),
                           ("copies", "copied by hand (two or more furn/<id> of one id), left as they are"),
                           ("foreign", "a furn/<id> this list did not place, left as it is"),
                           ("not_listed", f"in {TOP} but not in the list, left as they are (remove takes them out)"),
                           ("tags_created", "tags created, shown"),
                           ("materials_created", "materials created from the palette")):
            if result.get(key):
                lines.append(f"== {words}: {_ids(result[key])}")
        if result.get("furnishings_moved"):
            lines.append(f"== WARNING {TOP} has been moved by hand: its pieces stand in its frame, off the house's")
    elif cmd == "remove":
        lines.append(f"== removed: {_ids(result.get('removed'))}")
        if result.get("not_found"):
            lines.append(f"== not in {TOP}: {_ids(result['not_found'])}")
    else:
        if not result.get("furnishings"):
            return [f"== SketchUp holds no {TOP} group (place makes it)"]
        known = {p["id"] for p in pieces}
        rows = result.get("entities") or []
        lines.append(f"== {TOP}: {len(rows)} groups and components" + (", hidden" if result.get("hidden") else "")
                     + (f", {result['loose']} loose entities" if result.get("loose") else "")
                     + (", moved by hand off the house's frame" if result.get("moved") else ""))
        for r in rows:
            if "id" not in r:
                said = "not a piece"
            else:
                said = {None: "not placed by the list", True: "moved by hand", False: "as placed"}[r.get("moved")]
                if r["id"] not in known:
                    said += ", not in the list"
            lines.append(f"   {r.get('name') or '(unnamed)'}  {r.get('kind')}  {r.get('tag')} "
                         f"({'shown' if r.get('tag_visible') else 'hidden'})  {said}")
        absent = sorted(known - {r["id"] for r in rows if "id" in r})
        if absent:
            lines.append(f"== in the list, not in SketchUp: {_ids(absent)}")
    return lines


def run(cmd, *, only=(), ids=(), list_path=LIST, palette=PALETTE, who="someone", bridge=None, echo=print,
        lock_path=None) -> int:
    """The command (module doc); returns the exit status. Raises FurnishingsError before anything is sent."""
    pieces, colours = [], None
    if cmd in ("place", "list"):
        colours = materials(palette)
        pieces = load_list(list_path, set(colours))
    spec = spec_for(cmd, pieces, only=only, ids=ids, colours=colours)
    if bridge is None:
        from bridge.client import Bridge
        bridge = Bridge(ROOT)
    try:
        with look.lock(who, lock_path):
            result = bridge.eval(eval_code(spec))
    except Exception as e:                       # noqa: BLE001 - SketchUp's refusal, reported in its words
        echo("== FAILED:")
        echo(f"   {(str(e).splitlines() or [type(e).__name__])[0]}")
        return 1
    for line in report(cmd, result, pieces):
        echo(line)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="furnishings.py", description=__doc__.splitlines()[0])
    ap.add_argument("--who", default=os.environ.get("USER", "someone"), help="shown to anyone waiting for the lock")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("place", help="create the listed pieces SketchUp lacks and bring those not moved by hand to "
                                     "the list")
    p.add_argument("--only", nargs="+", default=[], metavar="ID", help="only these pieces")
    p.add_argument("--list", type=Path, default=LIST, help="the placement list (default: furnishings/pieces.json)")
    p = sub.add_parser("remove", help="take pieces out of FURNISHINGS")
    p.add_argument("ids", nargs="+", metavar="ID")
    p = sub.add_parser("list", help="what FURNISHINGS holds, against the list")
    p.add_argument("--list", type=Path, default=LIST, help="the placement list (default: furnishings/pieces.json)")
    a = ap.parse_args(argv)
    try:
        return run(a.cmd, only=getattr(a, "only", ()), ids=getattr(a, "ids", ()),
                   list_path=getattr(a, "list", LIST), who=a.who)
    except FurnishingsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
