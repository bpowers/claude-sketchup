#!/usr/bin/env python3
"""housec: compile the house written in Starlark into house.json, and a design view into a document of its own.

    uv run python housec.py house.star -o house.json
    uv run python housec.py house.star --option kitchen_island -o out/options/kitchen_island.json
    uv run python housec.py house.star --dump-source

house.star loads the zones under zones/ and its last expression is the document (docs/IR.md): today's house. housec
evaluates it, checks the document's frame (ir_version, every element's zone declared) and writes it. What the elements
mean is the planner's to check (ir/plan.py).

Why Starlark: the house wants functions and loops (a wall run, a cabinet run, a dimension chain), but it must stay
hermetic and diffable. Starlark is real code with no I/O, no ambient state and guaranteed termination, so the same
sources always produce the same document.

Modules: load() is resolved here by inlining each dependency once, in dependency order, ahead of the file that loads
it, so the sources keep standard load() syntax. Two consequences are handled here rather than left to discipline:

  * Inlined modules share one namespace, and Starlark rebinds a top-level name silently, so a name defined by two
    modules would quietly shadow one of them. housec fails instead, naming both files.
  * Errors would report positions in the inlined source. Load lines are blanked, not deleted, so every module keeps its
    own line numbers, and positions in an error are mapped back to the file and line they came from.

Design areas (docs/DESIGN.md): zones/areas.star names the areas, in the order their layers apply, and the views, the
named combinations of their alternatives. An alternative is a layer, zones/areas/<area>/<alt>.star, whose last
expression is {"LAYER": {...}}: what it removes from the house the areas before it leave (element ids), what it replaces
(whole elements keeping their id and type) and what it adds. Every area also has `today`, which has no file. An area
that `reads` earlier areas is given what their alternatives export as the predeclared UPSTREAM; a layer that cannot
stand in that context fails with a choice code ("<design> choice.<code>: <words>") and its area then stays as today
(Unavailable). Any other failure is an error wherever it happens.

--option NAME compiles today's house, then the view NAME: its layers composed (compose) and applied to today's document
(apply_option), which gives a whole-house document whose meta.option records the layers, what they remove, replace and
add, the sha256 of the base document and of the layers' inlined sources. house.json is never changed by a view;
areasc.py compiles every alternative for SketchUp.

What a layer loads is evaluated once, frozen, and shared by every layer that loads the same (_eval_layer): a layer is a
few lines over the thousands it shares with the other alternatives of its design.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import re
import sys

import starlark

IR_VERSION = 1
PREDECLARED = ("UPSTREAM",)
VARIANT = "today"                        # meta.variant of today's house; a view's document carries the view's id

AREAS_FILE = os.path.join("zones", "areas.star")
AREAS_DIR = os.path.join("zones", "areas")
TODAY = "today"                          # every area's alternative that changes nothing; it has no file
ID_RE = re.compile(r"[a-z][a-z0-9_]*")   # an area's, an alternative's and a view's id

# A LAYER object's keys and their JSON types; the first five are required.
LAYER_FIELDS = {"id": str, "name": str, "removes": list, "replaces": list, "adds": list,
                "note": str, "sources": list, "later": list, "checks": list, "exports": dict, "drops": dict}
LAYER_REQUIRED = ("id", "name", "removes", "replaces", "adds")
LAYER_DROPS = ("checks", "later")        # what a layer may take out of what the layers before it reported
# A view's composed object (compose): a layer's keys less exports and drops, and the layers it was composed from.
OPTION_FIELDS = {"id": str, "name": str, "removes": list, "replaces": list, "adds": list,
                 "note": str, "sources": list, "later": list, "checks": list, "layers": list}

# A layer saying it cannot stand in the choices it was given, as fail() reports it: "<design> choice.<code>: <words>".
# Only a choice code says so. Any other refusal, a design rule that no longer holds (an aisle too narrow) among them,
# is an error wherever it happens: an alternative that stops working is never quietly shown as today's.
REFUSAL_RE = re.compile(r"^error: fail: ([a-z][a-z0-9_]*) (choice\.[a-z0-9_]+(?:\.[a-z0-9_]+)*): (.*)$", re.M)

LOAD_RE = re.compile(r'''^\s*load\(\s*["']([^"']+)["']\s*(?:,[^)]*)?\)\s*$''', re.M)
DEF_RE = re.compile(r'^def\s+([A-Za-z_]\w*)\s*\(', re.M)
ASSIGN_RE = re.compile(r'^([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s*=(?!=)', re.M)
BEGIN_RE = re.compile(r'^# ---- begin (.+) ----$')


class HousecError(Exception):
    pass


class Unavailable(HousecError):
    """A layer refused by its design's choice code in the context it was given: the alternative cannot stand with what
    the areas it reads have on. `code` is the design's stable code, such as choice.no_peninsula."""

    def __init__(self, area, alt, design, code, words):
        super().__init__(f"{area}/{alt} is not available here: {design} {code}: {words}")
        self.area, self.alt, self.design, self.code, self.words = area, alt, design, code, words


# ------------------------------------------------------------------ modules

def top_level_names(src):
    names = [m.group(1) for m in DEF_RE.finditer(src)]
    for m in ASSIGN_RE.finditer(src):
        names.extend(n.strip() for n in m.group(1).split(","))
    return names


def resolve(path, root, seen, stack, owners):
    """The source of `path` (relative to `root`) with its load() dependencies inlined ahead of it, each once."""
    real = os.path.normpath(os.path.join(root, path))
    if real in stack:
        chain = " -> ".join(os.path.relpath(p, root) for p in stack + [real])
        raise HousecError(f"circular load: {chain}")
    if real in seen:
        return ""
    if not os.path.exists(real):
        origin = os.path.relpath(stack[-1], root) if stack else "the command line"
        raise HousecError(f"load target not found: {path} (from {origin})")
    seen.add(real)
    with open(real) as f:
        src = f.read()
    rel = os.path.relpath(real, root)
    out = [resolve(dep, root, seen, stack + [real], owners) for dep in LOAD_RE.findall(src)]
    body = LOAD_RE.sub(lambda m: "\n" * m.group(0).count("\n"), src)
    for name in top_level_names(body):
        prev = owners.get(name)
        if prev is not None and prev != rel:
            raise HousecError(
                f"top-level name {name!r} is defined in both {prev} and {rel}. Inlined modules share one namespace "
                f"and Starlark rebinds silently, so rename one of them (a zone's names usually carry its prefix).")
        owners[name] = rel
    out.append(f"# ---- begin {rel} ----\n{body}\n")
    return "\n".join(o for o in out if o)


def inline(entry, root=None):
    """The entry's source with its loads inlined. Load paths resolve against `root`, the entry's directory by default;
    a layer under zones/areas/ loads lib/ and zones/ modules from the house's directory."""
    entry = os.path.abspath(entry)
    root = os.path.abspath(root) if root else (os.path.dirname(entry) or ".")
    owners = {name: "<predeclared by housec>" for name in PREDECLARED}
    return resolve(os.path.relpath(entry, root), root, set(), [], owners)


def line_map(src):
    """A function from a line number of the inlined source to (module, its own line number), or None."""
    starts = []
    for i, line in enumerate(src.split("\n"), start=1):
        m = BEGIN_RE.match(line)
        if m:
            starts.append((i, m.group(1)))

    def lookup(n):
        best = None
        for start, rel in starts:
            if start < n:
                best = (rel, n - start)
        return best
    return lookup


def remap_positions(text, name, src):
    """An error message about the inlined source `src` (evaluated as `name`), its positions put back in the modules
    they came from: "name:123" becomes "zones/kitchen.star:40", and so do the line numbers in the gutter of a snippet
    the message quotes from `name`."""
    lookup = line_map(src)

    def sub(m):
        hit = lookup(int(m.group(1)))
        return f"{hit[0]}:{hit[1]}" if hit else m.group(0)
    out, quoting = [], False
    for line in text.split("\n"):
        at = re.match(r"\s*--> (.+?):\d+", line)
        if at:
            quoting = at.group(1) == name
        gutter = re.match(r"(\s*)(\d+)( \|.*)$", line) if quoting else None
        hit = gutter and lookup(int(gutter.group(2)))
        if hit:
            line = f"{gutter.group(1)}{hit[1]}{gutter.group(3)}"
        out.append(re.sub(re.escape(name) + r":(\d+)", sub, line))
    return "\n".join(out)


def _module(upstream):
    module = starlark.Module()
    module["UPSTREAM"] = upstream or {}
    return module


def _eval(src, name, upstream=None):
    """Evaluate inlined Starlark source and return the value of its last expression. `upstream` is the predeclared
    UPSTREAM: for a layer, what the areas it reads export, by area id; {} for anything else."""
    try:
        return starlark.eval(_module(upstream), starlark.parse(name, src), starlark.Globals.standard())
    except starlark.StarlarkError as e:
        raise HousecError("evaluation failed\n" + remap_positions(str(e), name, src)) from None


def evaluate(entry):
    """(today's document as house.star's last expression gives it, the inlined source)."""
    src = inline(entry)
    return _eval(src, os.path.basename(entry)), src


# A layer's own load() lines, each with the names it takes: `load("zones/areas/kitchen.star", "kitchen_layer")`.
LAYER_LOAD_RE = re.compile(r'''^[ \t]*load\(\s*["'][^"']+["']\s*((?:,[^)]*)?)\)''', re.M)
_LOADS = {}                              # {sha256 of a layer's inlined loads: (the frozen module, its source, label)}


def _eval_layer(src, rel, original, upstream):
    """Evaluate a layer's inlined source and return its last expression's value, as _eval does, with what it loads
    evaluated once for every layer that loads the same.

    The inlined source is cut at the layer's own module: everything before it is evaluated once into a frozen module
    (kept by its text, so a source a test has rewritten is its own), and the layer's module is evaluated alone, loading
    its names from that. `original` is the layer file's own text, whose load lines say which names it takes."""
    mark = f"# ---- begin {rel} ----\n"
    cut = src.rfind(mark)
    names = "".join(m.group(1) for m in LAYER_LOAD_RE.finditer(original))
    first = LAYER_LOAD_RE.search(original)
    if cut < 0 or not names or first is None:
        return _eval(src, rel, upstream)
    loads, body = src[:cut], src[cut + len(mark):]
    key = hashlib.sha256(loads.encode()).hexdigest()
    label = f"{rel}#loads"
    if key not in _LOADS:
        module = _module({})
        try:
            starlark.eval(module, starlark.parse(label, loads), starlark.Globals.standard())
        except starlark.StarlarkError as e:
            raise HousecError("evaluation failed\n" + remap_positions(str(e), label, loads)) from None
        _LOADS[key] = (module.freeze(), loads, label)
    frozen, loads_src, loads_label = _LOADS[key]
    # the layer's own lines keep their numbers: its names are loaded on the line its first load() stood on
    lines = body.split("\n")
    at = original.count("\n", 0, first.start())
    if lines[at].strip():
        return _eval(src, rel, upstream)
    lines[at] = f'load("loads"{names})'.replace("\n", " ")
    try:
        return starlark.eval(_module(upstream), starlark.parse(rel, "\n".join(lines)), starlark.Globals.standard(),
                             starlark.FileLoader(lambda _: frozen))
    except starlark.StarlarkError as e:
        raise HousecError("evaluation failed\n" + remap_positions(str(e), loads_label, loads_src)) from None


# ------------------------------------------------------------------ design areas

class Design:
    """The design areas beside a house entry: zones/areas.star and the layers under zones/areas/.

    areas    [{"id", "name", "reads": [area ids before it]}, ...], in the order the layers apply
    alts     {area id: [its alternatives' ids, sorted]}, the files under zones/areas/<area>/; `today` is not listed
    views    {view id: {"name", "choose": {area id: alternative id}}}

    Layers are evaluated on demand and once each: a layer is a function of its sources and of what the areas it reads
    export, so one evaluation serves every combination that gives it the same UPSTREAM. `rewrite`, when given, maps a
    layer's inlined source to the source evaluated in its place, which is how a test moves one of a design's numbers.
    An entry with no zones/areas.star has no areas and no views."""

    def __init__(self, entry, rewrite=None):
        self.entry = os.path.abspath(entry)
        self.root = os.path.dirname(self.entry) or "."
        self.rewrite = rewrite
        self._layers, self._sources, self._originals = {}, {}, {}
        self.areas, self.alts, self.views = [], {}, {}
        path = os.path.join(self.root, AREAS_FILE)
        if not os.path.exists(path):
            return
        value = _eval(inline(path, self.root), AREAS_FILE)
        if not isinstance(value, dict) or set(value) != {"AREAS", "VIEWS"}:
            raise HousecError(f'{AREAS_FILE} must end with the expression {{"AREAS": [...], "VIEWS": {{...}}}}')
        if not isinstance(value["AREAS"], list) or not isinstance(value["VIEWS"], dict):
            raise HousecError(f"{AREAS_FILE}: AREAS must be a list and VIEWS an object")
        problems, seen = [], []
        for a in value["AREAS"]:
            aid = a.get("id") if isinstance(a, dict) else None
            if not isinstance(aid, str) or not ID_RE.fullmatch(aid):
                problems.append(f"an area's id is lowercase letters, digits and _, starting with a letter; got {aid!r}")
                continue
            if sorted(set(a) - {"id", "name", "reads"}) or not isinstance(a.get("name"), str):
                problems.append(f"area {aid} has a name, and may name the areas it reads; got keys {sorted(a)}")
            reads = a.get("reads", [])
            if aid in seen:
                problems.append(f"area {aid} is listed twice")
            if not isinstance(reads, list) or [r for r in reads if r not in seen]:
                problems.append(f"area {aid} reads {reads!r}: an area reads only areas listed before it")
                reads = []
            seen.append(aid)
            self.areas.append({"id": aid, "name": a.get("name", ""), "reads": list(reads)})
            d = os.path.join(self.root, AREAS_DIR, aid)
            self.alts[aid] = sorted(f[:-5] for f in os.listdir(d) if f.endswith(".star")) if os.path.isdir(d) else []
            for alt in self.alts[aid]:
                if alt == TODAY or not ID_RE.fullmatch(alt):
                    problems.append(f"{AREAS_DIR}/{aid}/{alt}.star: an alternative's id is lowercase letters, digits "
                                    f"and _, starting with a letter, and `{TODAY}` is the one with no file")
        for vid, v in value["VIEWS"].items():
            if not isinstance(vid, str) or not ID_RE.fullmatch(vid):
                problems.append(f"a view's id is lowercase letters, digits and _, starting with a letter; got {vid!r}")
                continue
            if not isinstance(v, dict) or set(v) != {"name", "choose"} or not isinstance(v["name"], str) \
                    or not isinstance(v["choose"], dict):
                problems.append(f'view {vid} must be {{"name": "...", "choose": {{area: alternative}}}}')
                continue
            for aid, alt in v["choose"].items():
                if aid not in self.alts:
                    problems.append(f"view {vid} chooses for {aid!r}, which is no area ({', '.join(seen)})")
                elif alt != TODAY and alt not in self.alts[aid]:
                    problems.append(f"view {vid} chooses {aid}/{alt}, but there is no {AREAS_DIR}/{aid}/{alt}.star "
                                    f"(alternatives: {', '.join(self.alts[aid]) or 'none'})")
            self.views[vid] = {"name": v["name"], "choose": dict(v["choose"])}
        if problems:
            raise HousecError(f"{AREAS_FILE}:\n  - " + "\n  - ".join(problems))

    def source(self, area, alt):
        """(the inlined source of zones/areas/<area>/<alt>.star as it is evaluated, its path relative to the house's
        directory). Inlining reads the layer's file and the modules it loads; it evaluates nothing."""
        key = (area, alt)
        if key not in self._sources:
            path = os.path.join(self.root, AREAS_DIR, area, alt + ".star")
            rel = os.path.relpath(path, self.root)
            if area not in self.alts or alt not in self.alts[area]:
                have = ", ".join(self.alts.get(area, [])) or "none"
                raise HousecError(f"no alternative {area}/{alt}: {rel} does not exist (alternatives: {have})")
            src = inline(path, self.root)
            with open(path) as fh:
                self._originals[key] = fh.read()
            self._sources[key] = (self.rewrite(src) if self.rewrite else src, rel)
        return self._sources[key]

    def layer(self, area, alt, upstream):
        """The LAYER object of <area>/<alt> given `upstream`, what the areas it reads export ({area id: {"alt": its
        alternative, ...its exports}}). Raises Unavailable where the layer's design refuses that context with a choice
        code (REFUSAL_RE), and HousecError for anything else that goes wrong."""
        key = (area, alt, json.dumps(upstream, sort_keys=True))
        if key not in self._layers:
            src, rel = self.source(area, alt)
            try:
                value = _eval_layer(src, rel, self._originals[(area, alt)], upstream)
            except HousecError as e:
                m = REFUSAL_RE.search(str(e))
                self._layers[key] = Unavailable(area, alt, m.group(1), m.group(2), m.group(3)) if m else e
            else:
                if not isinstance(value, dict) or set(value) != {"LAYER"}:
                    self._layers[key] = HousecError(f'{rel} must end with the expression {{"LAYER": {{...}}}}')
                else:
                    self._layers[key] = _check_layer(value["LAYER"], area, alt, rel)
        got = self._layers[key]
        if isinstance(got, Exception):
            raise got
        return got

    def stack(self, choose, strict=False):
        """The layers of the combination `choose` ({area id: alternative id}, `today` for an area it does not name), in
        the areas' order: ([(area, alt, LAYER object), ...] for the areas that change anything, {area: Unavailable} for
        each alternative that cannot stand in this combination, whose area then stays as today). With strict, such an
        alternative is an error: a view names a combination that works."""
        unknown = sorted(set(choose) - set(self.alts))
        if unknown:
            raise HousecError(f"no area {', '.join(unknown)} (areas: {', '.join(a['id'] for a in self.areas)})")
        layers, missing, exports = [], {}, {}
        for a in self.areas:
            aid, alt = a["id"], choose.get(a["id"], TODAY)
            exports[aid] = {"alt": TODAY}
            if alt == TODAY:
                continue
            try:
                spec = self.layer(aid, alt, {r: exports[r] for r in a["reads"]})
            except Unavailable as e:
                if strict:
                    raise HousecError(str(e)) from None
                missing[aid] = e
                continue
            exports[aid] = {**spec.get("exports", {}), "alt": alt}
            layers.append((aid, alt, spec))
        return layers, missing

    def combinations(self):
        """Every combination of the areas' alternatives, `today` first in each: [{area id: alternative id}, ...] with
        every area named, the first one today's house."""
        out = [{}]
        for a in self.areas:
            out = [{**c, a["id"]: alt} for c in out for alt in [TODAY] + self.alts[a["id"]]]
        return out


def _check_layer(spec, area, alt, rel):
    """A LAYER object as its file gave it, or the HousecError naming what is wrong with its shape."""
    if not isinstance(spec, dict):
        return HousecError(f"{rel}: LAYER must be an object, got {type(spec).__name__}")
    problems = [f"unknown key {k!r} (keys: {', '.join(LAYER_FIELDS)})" for k in sorted(set(spec) - set(LAYER_FIELDS))]
    problems += [f"missing key {k!r}" for k in LAYER_REQUIRED if k not in spec]
    problems += [f"{k} must be a {t.__name__}, got {type(spec[k]).__name__}"
                 for k, t in LAYER_FIELDS.items() if k in spec and not isinstance(spec[k], t)]
    if not problems and spec["id"] != alt:
        problems.append(f"its id is {spec['id']!r}, but it lives in {rel}")
    drops = spec.get("drops")
    for k, v in (drops.items() if isinstance(drops, dict) else []):
        if k not in LAYER_DROPS or not isinstance(v, list):
            problems.append(f"drops.{k} must be one of {', '.join(LAYER_DROPS)} and a list")
    if isinstance(spec.get("exports"), dict) and "alt" in spec["exports"]:
        problems.append("exports.alt is the alternative's id, which housec gives the areas that read it")
    return HousecError(f"layer {area}/{alt}: " + "; ".join(problems)) if problems else spec


def layer_tag(area, alt):
    """The tag the elements a layer replaces and adds carry, and the SketchUp tag that shows them: <area>/<alt>."""
    return f"{area}/{alt}"


def _tagged(el, tag):
    """A copy of an element carrying `tag` among its tags."""
    tags = list(el.get("tags", []))
    if tag not in tags:
        tags.append(tag)
    return {**el, "tags": tags}


def _code(line):
    """A check line's code, its first field ("<code> <value> <op> <limit> ok")."""
    return line.split(" ")[0] if isinstance(line, str) else line


def compose(layers, name, title):
    """The object of the combination whose `layers` (Design.stack) apply one after another: what it removes from today's
    house, replaces in it and adds to it, each replaced and added element carrying its layer's tag, and the layers'
    notes, sources, later lines and checks merged in order (a later check of the same code takes the earlier one's
    place, and `drops` take out what no longer holds). HousecError names what does not compose: a layer adding an id an
    earlier one added, or replacing or removing an id an earlier layer removed."""
    removes, replaces, adds = [], {}, {}
    checks, later, sources, notes, problems = {}, [], [], [], []
    for area, alt, spec in layers:
        tag = layer_tag(area, alt)
        for rid in spec["removes"]:
            if not isinstance(rid, str):
                problems.append(f"{tag} removes {rid!r}, which is no element id")
            elif rid in adds:
                del adds[rid]
            elif rid in removes:
                problems.append(f"{tag} removes {rid}, which a layer before it already removed")
            else:
                replaces.pop(rid, None)
                removes.append(rid)
        for r in spec["replaces"]:
            rid = r.get("id") if isinstance(r, dict) else None
            if not isinstance(rid, str):
                problems.append(f"{tag} replaces an element with no id: {str(r)[:80]}")
            elif rid in removes:
                problems.append(f"{tag} replaces {rid}, which a layer before it removed")
            elif rid in adds:
                if r.get("type") != adds[rid].get("type"):
                    problems.append(f"{tag} replaces the {adds[rid].get('type')} {rid} with a {r.get('type')}; a "
                                    "replacement keeps its type")
                adds[rid] = _tagged(r, tag)
            else:
                replaces[rid] = _tagged(r, tag)
        for a in spec["adds"]:
            aid = a.get("id") if isinstance(a, dict) else None
            if not isinstance(aid, str):
                problems.append(f"{tag} adds an element with no id: {str(a)[:80]}")
            elif aid in adds or aid in replaces or aid in removes:
                problems.append(f"{tag} adds {aid}, an id a layer before it already uses")
            else:
                adds[aid] = _tagged(a, tag)
        drops = spec.get("drops", {})
        for k in drops.get("checks", []):
            checks.pop(k, None)
        later = [w for w in later if w not in drops.get("later", [])]
        for line in spec.get("checks", []):
            checks[_code(line)] = line
        later += [w for w in spec.get("later", []) if w not in later]
        sources += [w for w in spec.get("sources", []) if w not in sources]
        if spec.get("note"):
            notes.append(spec["note"])
    if problems:
        raise HousecError(f"{name}: the layers do not compose:\n  - " + "\n  - ".join(problems))
    return {"id": name, "name": title, "note": "; ".join(notes), "sources": sources,
            "removes": removes, "replaces": list(replaces.values()), "adds": list(adds.values()),
            "later": later, "checks": list(checks.values()),
            "layers": [{"area": area, "alt": alt} for area, alt, _ in layers]}


def option_names(entry):
    """The views beside a house entry (zones/areas.star VIEWS), sorted."""
    return sorted(Design(entry).views)


def _view(design, name):
    if not ID_RE.fullmatch(name or ""):
        raise HousecError(f"view names are lowercase letters, digits and _, starting with a letter; got {name!r}")
    if name not in design.views:
        raise HousecError(f"no view {name!r} in {AREAS_FILE} (views: {', '.join(sorted(design.views)) or 'none'})")
    return design.views[name]


def option_source(entry, name, design=None):
    """The text a view's meta.option.source_sha256 hashes: what it chooses, the areas in their order with what each
    reads, and the inlined source of each layer it chooses. Nothing but zones/areas.star is evaluated, so a reader can
    tell whether a compiled view still says what the sources do in milliseconds."""
    design = design or Design(entry)
    view = _view(design, name)
    parts = [f"# ---- view {name}: {json.dumps(view['choose'], sort_keys=True)} ----\n"
             f"# ---- areas: {json.dumps([[a['id'], a['reads']] for a in design.areas])} ----\n"]
    for a in design.areas:
        alt = view["choose"].get(a["id"], TODAY)
        if alt != TODAY:
            parts.append(design.source(a["id"], alt)[0])
    return "\n".join(parts)


def option_source_sha256(entry, name, design=None):
    return hashlib.sha256(option_source(entry, name, design).encode()).hexdigest()


def evaluate_option(entry, name, rewrite=None, design=None):
    """The composed object of the view <name>: its layers evaluated in the areas' order, each given what the areas it
    reads export, and composed. A view names a combination that works, so a layer that refuses its context is an error
    here."""
    design = design or Design(entry, rewrite)
    view = _view(design, name)
    layers, _ = design.stack(view["choose"], strict=True)
    return compose(layers, name, view["name"])


# ------------------------------------------------------------------ documents

def document_text(doc):
    """A document as housec writes it: the bytes meta.option.base.sha256 and the manifest's house sha256 hash."""
    return json.dumps(doc, indent=1) + "\n"


def _ids_of(el):
    """An element's id and its openings' ids, which share one namespace."""
    return [el.get("id")] + [o.get("id") for o in el.get("openings", []) if isinstance(o, dict)]


def apply_option(base, spec, name, base_sha, source_sha=None):
    """Today's document `base` with the composed object `spec` applied: a new document, `base` untouched. Today's
    elements stay in today's order, the removed ones gone and the replaced ones in their place, then the added ones.
    HousecError names every way it does not apply: an unknown or missing key, a removed or replaced id today's house
    lacks, a replacement that changes an element's type, an added id today's house already uses, an id named twice."""
    if not isinstance(spec, dict):
        raise HousecError(f"option {name}: it must be an object, got {type(spec).__name__}")
    problems = [f"unknown key {k!r} (keys: {', '.join(OPTION_FIELDS)})" for k in sorted(set(spec) - set(OPTION_FIELDS))]
    problems += [f"missing key {k!r}" for k in (*LAYER_REQUIRED, "layers") if k not in spec]
    problems += [f"{k} must be a {t.__name__}, got {type(spec[k]).__name__}"
                 for k, t in OPTION_FIELDS.items() if k in spec and not isinstance(spec[k], t)]
    if problems:
        raise HousecError(f"option {name}: " + "; ".join(problems))
    if spec["id"] != name:
        problems.append(f"its id is {spec['id']!r}, but it is applied as {name}")
    elements = base["elements"]
    index = {e["id"]: e for e in elements}
    base_ids = {i for e in elements for i in _ids_of(e)}
    gone, swap, added = set(), {}, set()
    for rid in spec["removes"]:
        if not isinstance(rid, str) or rid not in index:
            problems.append(f"removes {rid!r}, which is not an element of today's house")
        elif rid in gone:
            problems.append(f"removes {rid} twice")
        gone.add(rid)
    for r in spec["replaces"]:
        rid = r.get("id") if isinstance(r, dict) else None
        if rid not in index:
            problems.append(f"replaces {rid!r}, which is not an element of today's house")
        elif rid in gone:
            problems.append(f"both removes and replaces {rid}")
        elif rid in swap:
            problems.append(f"replaces {rid} twice")
        elif r.get("type") != index[rid]["type"]:
            problems.append(f"replaces the {index[rid]['type']} {rid} with a {r.get('type')}; a replacement keeps its "
                            "type (remove it and add a new element instead)")
        else:
            swap[rid] = r
    for a in spec["adds"]:
        aid = a.get("id") if isinstance(a, dict) else None
        if not isinstance(aid, str):
            problems.append(f"adds an element with no id: {str(a)[:80]}")
        elif aid in base_ids:
            problems.append(f"adds {aid}, an id today's house already uses; replace that element instead")
        elif aid in added:
            problems.append(f"adds {aid} twice")
        added.add(aid)
    if not problems:
        out = [swap.get(e["id"], e) for e in elements if e["id"] not in gone] + list(spec["adds"])
        owner = {}
        for e in out:
            for i in _ids_of(e):
                if i in owner:
                    problems.append(f"id {i} is used by both {owner[i]} and {e['id']} in the view's document")
                owner[i] = e["id"]
    if problems:
        raise HousecError(f"option {name} does not apply to today's house:\n  - " + "\n  - ".join(problems))
    option = {"id": name, "name": spec["name"], "note": spec.get("note", ""),
              "base": {"variant": base["meta"].get("variant"), "sha256": base_sha, "elements": len(elements)},
              "layers": [dict(x) for x in spec["layers"]],
              "removes": list(spec["removes"]), "replaces": [r["id"] for r in spec["replaces"]],
              "adds": [a["id"] for a in spec["adds"]], "sources": list(spec.get("sources", [])),
              "later": list(spec.get("later", [])), "checks": list(spec.get("checks", []))}
    if source_sha is not None:                   # a caller that did not hash the sources records no digest at all
        option["source_sha256"] = source_sha
    meta = {"project": base["meta"].get("project"), "variant": name, "generated_by": "housec", "option": option}
    doc = {"ir_version": IR_VERSION, "meta": meta, "zones": base["zones"], "datums": base["datums"]}
    if base.get("location") is not None:
        doc["location"] = base["location"]
    doc["elements"] = out
    return doc


def check_document(doc, variant=VARIANT):
    """The document's frame, which every consumer relies on: ir_version, its top-level keys, meta.variant, and every
    element's zone declared in "zones". What the elements mean is the planner's to check."""
    if not isinstance(doc, dict):
        raise HousecError("house.star must end with an expression producing the document")
    if doc.get("ir_version") != IR_VERSION:
        raise HousecError(f"the document says ir_version {doc.get('ir_version')!r}; housec emits {IR_VERSION}")
    missing = [k for k in ("meta", "zones", "datums", "elements") if k not in doc]
    if missing:
        raise HousecError(f"the document is missing top-level keys {missing}")
    if not isinstance(doc["meta"], dict) or doc["meta"].get("variant") != variant:
        got = doc["meta"].get("variant") if isinstance(doc["meta"], dict) else None
        raise HousecError(f"meta.variant is {got!r} but housec was asked for {variant!r}")
    if not isinstance(doc["zones"], dict) or not isinstance(doc["elements"], list):
        raise HousecError("zones must be an object and elements a list")
    bad = [f"{e.get('id')} ({e.get('zone')!r})" for e in doc["elements"]
           if not isinstance(e, dict) or e.get("zone") not in doc["zones"]]
    if bad:
        raise HousecError(f"elements in zones the document does not declare (zones: {', '.join(doc['zones'])}): "
                          + ", ".join(bad[:8]) + (f" and {len(bad) - 8} more" if len(bad) > 8 else ""))


def summary(doc):
    """One line: the elements by type and by src, the openings, and for a view what it changes."""
    types = collections.Counter(e["type"] for e in doc["elements"])
    srcs = collections.Counter(e.get("src") for e in doc["elements"])
    n_open = sum(len(e.get("openings", [])) for e in doc["elements"])
    out = (f"{len(doc['elements'])} elements ({', '.join(f'{k} {v}' for k, v in sorted(types.items()))}); "
           f"{n_open} openings; src: {', '.join(f'{k} {v}' for k, v in sorted(srcs.items()))}")
    opt = doc["meta"].get("option")
    if opt:
        out += (f"; view {opt['id']}: removes {len(opt['removes'])}, replaces {len(opt['replaces'])}, adds "
                f"{len(opt['adds'])} on {opt['base']['variant']} {opt['base']['sha256'][:12]}")
    return out


def compile_house(entry, option=None):
    """Today's document, or with `option` the view's whole-house document."""
    doc, _ = evaluate(entry)
    check_document(doc)
    if option:
        spec = evaluate_option(entry, option)
        doc = apply_option(doc, spec, option, hashlib.sha256(document_text(doc).encode()).hexdigest(),
                           option_source_sha256(entry, option))
        check_document(doc, option)
    return doc


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("entry", help="house.star")
    ap.add_argument("-o", "--out", default="-", help="where to write the document (default: stdout)")
    ap.add_argument("--option", metavar="VIEW", help="write the view VIEW of zones/areas.star instead of today's house")
    ap.add_argument("--dump-source", action="store_true", help="print the inlined source instead of evaluating it")
    a = ap.parse_args(argv)
    try:
        if a.dump_source:
            print(inline(a.entry))
            return 0
        doc = compile_house(a.entry, a.option)
    except HousecError as e:
        sys.exit(f"housec: {e}")
    text = document_text(doc)
    if a.out == "-":
        print(text, end="")
        return 0
    d = os.path.dirname(a.out)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = f"{a.out}.tmp-{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, a.out)
    print(f"wrote {a.out}: {summary(doc)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
