#!/usr/bin/env python3
"""areasc: compile the design areas into every view's document and the manifest SketchUp is synced from.

    uv run python areasc.py house.star [--house house.json] [-o out]

The owner switches the design areas (zones/areas.star, docs/DESIGN.md) one by one in SketchUp, each with its own tags:
<area>/today, <area>/<alt>. Nothing is built per combination. This works out, from the sources alone, which element
shows under which tags, so that whichever alternative of each area is on, SketchUp shows exactly the house that
combination compiles to:

1. The areas are put in groups that stand on their own (independent_groups): areas that read one another, and any two
   whose alternatives reach a common element. Within a group every combination of its alternatives is composed
   (housec.Design.stack, housec.compose) and applied to today's house.json, the other areas as today; no combination
   across groups is, since each of its pieces is a piece of one group alone. So the compile costs the sum of the
   groups' combinations, not the product of every area's alternatives. An alternative that refuses a combination with
   a choice code (housec.REFUSAL_RE) leaves its area as today there, and the manifest says so (`unavailable`); any
   other failure stops the compile.
2. Each combination's document is planned (ir.plan), and each planned element gets a content key: the sha256 of its
   canonical JSON without the layer tags, which say which alternative put it there and draw nothing. The planner plans
   an element from its own record alone, so two placements with one key are one piece of geometry, drawn once and
   instanced. (Were the planner ever to draw one element from another, a key would have to cover both, and two areas
   reaching the two would have to share a group.)
3. A piece's presence is the set of its group's combinations it stands in. Present in all of them, it is the shell,
   under the one empty chain. Otherwise it shows under tag chains (cover): lists of tags, one per area, that between
   them are on in exactly those combinations and never two at once. A piece of today's house lists first the chain
   that holds today's combination, such as ["kitchen/today"]. An element that draws nothing (a space building neither
   floor nor ceiling) is no piece: SketchUp keeps no empty group.

It writes, under the out directory:

    options/<view>.json              each view's whole-house document, as housec.py --option writes it
    areas/documents/<name>.json      each combination's document that introduces a piece of its own
    areas/plans/today.json           today's house planned: what SketchUp draws today's pieces from
    areas/plans/<name>.json          the plans of those combinations' documents
    areas/manifest.json              what build.rb sync reads: the areas' tags, the views, the plans and the pieces

house.json is read, never written: run housec.py first when the house's sources change (look.py does).
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import random
import sys
import time

import housec
from ir import plan as planner

MANIFEST_VERSION = 1
MAX_ORDERS = 24                     # cover() tries every order of up to four areas, and the listed order past that
PROOF_LIMIT = 128                   # proof_combinations(): how many combinations a proof composes at most
AREAS_OUT = "areas"                 # under the out directory


class AreasError(Exception):
    pass


def canonical(obj):
    """The canonical JSON of a value, which content keys and the manifest's sha256 hash."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


# ------------------------------------------------------------------ content keys

def _record(planned, layer_tags):
    """A planned element without the layer tags it carries; with none left, without tags at all, as an element that
    never had any."""
    tags = planned.get("tags")
    if not tags or not any(t in layer_tags for t in tags):
        return planned
    kept = [t for t in tags if t not in layer_tags]
    rest = {k: v for k, v in planned.items() if k != "tags"}
    return {**rest, "tags": kept} if kept else rest


def _draws_nothing(el):
    return el["type"] == "space" and not el.get("build_floor") and not el.get("build_ceiling")


def planned_keys(doc, layer_tags=(), path="the document"):
    """(keys, hollow, plan) for a document: {element id: content key}, the ids of the elements that draw nothing, and
    the document's plan. AreasError where the document does not plan."""
    plan = planner.plan_document(doc, path)
    if not plan.ok:
        raise AreasError(f"{path} does not plan: {plan.message()}")
    layer_tags = set(layer_tags)
    keys = {el["id"]: _sha(canonical(_record(el, layer_tags))) for el in plan.elements}
    hollow = {el["id"] for el in plan.elements if _draws_nothing(el)}
    return keys, hollow, plan


def keys(doc, layer_tags=()):
    """{element id: content key} for a document (planned_keys)."""
    return planned_keys(doc, layer_tags)[0]


# ------------------------------------------------------------------ presence, and the tag chains that show it

def cover(present, dims):
    """The chains that show a piece standing in exactly the combinations `present`: a list of {area: alternative}, the
    piece showing where all of one chain's alternatives are on. `present` is a set of tuples, an alternative for each
    of `dims` ([(area, [its alternatives]), ...]). The chains never overlap, so no combination shows a piece twice, and
    an area whose alternative makes no difference to the piece is in none of them: a piece standing in every
    combination has the one empty chain, and one standing in none has no chain. Of the orders of the areas it tries,
    it keeps the one giving the fewest chains."""
    if not present:
        return []
    if not dims:
        return [{}]
    best = None
    orders = list(itertools.permutations(range(len(dims))))
    for order in (orders if len(orders) <= MAX_ORDERS else orders[:1]):
        turned = {tuple(t[i] for i in order) for t in present}
        got = _cover(frozenset(turned), [dims[i] for i in order])
        if best is None or len(got) < len(best):
            best = got
    names = [a for a, _ in dims]
    return [{a: c[a] for a in names if a in c} for c in best]


def _cover(present, dims):
    if not present:
        return []
    if not dims:
        return [{}]
    area, values = dims[0]
    below = {v: frozenset(t[1:] for t in present if t[0] == v) for v in values}
    first = below[values[0]]
    if all(b == first for b in below.values()):
        return _cover(first, dims[1:])            # this area makes no difference to the piece
    return [{area: v, **c} for v in values for c in _cover(below[v], dims[1:])]


def chain_tags(chain, order):
    """A chain as the tags SketchUp nests, in the areas' order."""
    return [housec.layer_tag(a, chain[a]) for a in order if a in chain]


# ------------------------------------------------------------------ the groups of areas that stand on their own

def _combo_name(effective):
    """A combination's name, from the alternatives that change anything: kitchen-island, or kitchen-island+bath-tub."""
    return "+".join(f"{a}-{alt}" for a, alt in effective)


def _compose_group(areas, design, base, base_sha, layer_tags, today):
    """Every combination of the alternatives of `areas`, the other areas as today, composed, applied and keyed: the
    combinations, each one's document (combinations that leave the same layers on share one), what was unavailable
    where, and the elements the group reaches."""
    combos = [{}]
    for a in areas:
        combos = [{**c, a: alt} for c in combos for alt in [housec.TODAY] + design.alts[a]]
    reads = {a["id"]: a["reads"] for a in design.areas}
    docs, combo_doc, unavailable, named = {(): today}, [], {}, set()
    for combo in combos:
        try:
            layers, missing = design.stack(combo)
        except housec.HousecError as e:
            said = ", ".join(f"{a}/{alt}" for a, alt in combo.items())
            raise AreasError(f"the combination {said}: {e}") from None
        for area, why in missing.items():
            where = {r: combo[r] for r in reads[area]}
            unavailable[(area, combo[area], canonical(where))] = {
                "area": area, "alt": combo[area], "combination": where, "code": why.code, "words": why.words}
        effective = tuple((a, alt) for a, alt, _ in layers)
        if effective not in docs:
            name = _combo_name(effective)
            try:
                doc = housec.apply_option(base, housec.compose(layers, name, name), name, base_sha)
                keys_, hollow, plan = planned_keys(doc, layer_tags, f"the combination {name}")
            except (housec.HousecError, AreasError) as e:
                raise AreasError(f"the combination {name}: {e}") from None
            docs[effective] = {"name": name, "doc": doc, "keys": keys_, "hollow": hollow, "plan": plan,
                               "index": {e["id"]: i for i, e in enumerate(doc["elements"])}}
            for _, _, spec in layers:
                named.update(spec["removes"], (e["id"] for e in spec["replaces"]), (e["id"] for e in spec["adds"]))
        combo_doc.append(effective)
    # what the group reaches: every element a layer names, and every one whose key is not today's somewhere
    touched = set(named)
    for d in docs.values():
        touched |= set(d["keys"]) ^ set(today["keys"])
        touched |= {i for i, k in d["keys"].items() if today["keys"].get(i) != k}
    return {"areas": list(areas), "combos": combos, "combo_doc": combo_doc, "docs": docs,
            "unavailable": unavailable, "touched": touched}


def _coupled(g, h):
    """Why the groups g and h (_compose_group) cannot be composed apart, in words, or None when they can: they reach a
    common element. Otherwise no layer of one names an element the other's do, and each element is planned from its
    own record, so every piece of a combination of the two together is a piece of one of them alone."""
    both = g["touched"] & h["touched"]
    if both:
        return f"both reach {', '.join(sorted(both)[:3])}{' ...' if len(both) > 3 else ''}"
    return None


def independent_groups(design, compose):
    """The design's areas in groups that stand on their own, each composed by `compose` (a function of a group's area
    ids): first the areas that read one another, then any two groups _coupled finds a reason to join, joined and
    composed again, until none is left."""
    order = [a["id"] for a in design.areas]
    parent = {a: a for a in order}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    for a in design.areas:
        for r in a["reads"]:
            parent[find(a["id"])] = find(r)
    done = {}
    while True:
        sets = {}
        for a in order:
            sets.setdefault(find(a), []).append(a)
        groups = []
        for s in sets.values():
            if tuple(s) not in done:
                done[tuple(s)] = compose(s)
            groups.append(done[tuple(s)])
        joined = False
        for i, g in enumerate(groups):
            for h in groups[i + 1:]:
                if find(g["areas"][0]) != find(h["areas"][0]) and _coupled(g, h):
                    parent[find(g["areas"][0])] = find(h["areas"][0])
                    joined = True
        if not joined:
            return groups


def proof_combinations(design, groups, limit=PROOF_LIMIT, seed=0):
    """The combinations a proof holds the manifest to (tests/test_areas.py), each naming every area: all of them where
    there are no more than `limit`. Past that, every combination of each group (`groups`: lists of area ids) with the
    other areas as today, which is what the compile composed, then combinations across groups, which it did not, drawn
    by `seed` up to `limit`: the ones that would show a dependence between groups that _coupled missed."""
    every = design.combinations()
    if len(every) <= limit:
        return every
    today = every[0]
    own = [c for c in every if sum(1 for g in groups if any(c[a] != today[a] for a in g)) <= 1]
    rest = [c for c in every if c not in own]
    random.Random(seed).shuffle(rest)
    return own + rest[:max(0, limit - len(own))]


# ------------------------------------------------------------------ the compile

def compile_areas(entry, house_json, out_dir, echo=print):
    """Steps 1 to 3 of the module doc. Returns the manifest, written to <out_dir>/areas/manifest.json with the
    documents and plans it names, and every view's document under <out_dir>/options/. Nothing is written until
    everything has compiled, the manifest last."""
    t0 = time.monotonic()
    with open(house_json, "rb") as fh:
        raw = fh.read()
    base = json.loads(raw)
    base_sha = hashlib.sha256(raw).hexdigest()
    if housec.document_text(base).encode() != raw:
        raise AreasError(f"{house_json} is not a document housec wrote; compile it first (housec.py house.star -o "
                         "house.json)")
    design = housec.Design(entry)
    order = [a["id"] for a in design.areas]
    dims = [(a, [housec.TODAY] + design.alts[a]) for a in order]
    layer_tags = {housec.layer_tag(a, alt) for a, alts in dims for alt in alts}
    today_tags = {housec.layer_tag(a, housec.TODAY) for a in order}

    # 1 and 2: the groups of areas that stand on their own, each group's combinations composed and keyed
    try:
        today_keys, today_hollow, today_plan = planned_keys(base, layer_tags, house_json)
    except AreasError as e:
        raise AreasError(f"today's house: {e}") from None
    today = {"name": housec.TODAY, "doc": base, "keys": today_keys, "hollow": today_hollow, "plan": today_plan,
             "index": {e["id"]: i for i, e in enumerate(base["elements"])}}
    groups = independent_groups(
        design, lambda areas: _compose_group(areas, design, base, base_sha, layer_tags, today))

    # an alternative that can stand nowhere is a broken one, not an unavailable one
    unavailable = {k: u for g in groups for k, u in g["unavailable"].items()}
    for g in groups:
        for a in g["areas"]:
            for alt in design.alts[a]:
                if all(c[a] != alt or (a, alt) not in e for c, e in zip(g["combos"], g["combo_doc"])):
                    why = "; ".join(f"{u['code']}: {u['words']}" for u in unavailable.values()
                                    if (u["area"], u["alt"]) == (a, alt))
                    raise AreasError(f"{a}/{alt} can stand in no combination at all ({why})")

    # 3: presence, a group at a time: the combinations of its areas each piece stands in, and the first document that
    # holds it. A piece that is today's in every combination of a group is none of that group's; one that is in
    # every group is the shell.
    pieces, used, switched = [], [], set()
    for g in groups:
        gdims = [(a, alts) for a, alts in dims if a in g["areas"]]
        tuples = [tuple(c[a] for a, _ in gdims) for c in g["combos"]]
        presence, first = {}, {}
        for n, effective in enumerate(g["combo_doc"]):
            for eid, key in g["docs"][effective]["keys"].items():
                presence.setdefault((eid, key), set()).add(tuples[n])
                first.setdefault((eid, key), effective)
        covers = {}
        for (eid, key), present in presence.items():
            of_today = today_keys.get(eid) == key
            if of_today and len(present) == len(tuples):
                continue                           # the group's alternatives all leave it as it is today
            if eid in today_keys:
                switched.add(eid)
            d = g["docs"][first[(eid, key)]]
            if eid in d["hollow"]:
                continue
            frozen = frozenset(present)
            if frozen not in covers:
                covers[frozen] = [chain_tags(c, order) for c in cover(frozen, gdims)]
            if not of_today and d["name"] not in used:
                used.append(d["name"])
            el = d["doc"]["elements"][d["index"][eid]]
            rank = today["index"].get(eid, len(base["elements"]) + d["index"][eid])
            pieces.append((rank, {"id": eid, "key": key, "plan": housec.TODAY if of_today else d["name"],
                                  "zone": el["zone"], "type": el["type"],
                                  "chains": sorted(covers[frozen],
                                                   key=lambda c: (not all(t in today_tags for t in c), c))}))
    for e in base["elements"]:                     # the shell: what no group's alternative changes
        if e["id"] not in switched and e["id"] not in today_hollow:
            pieces.append((today["index"][e["id"]], {"id": e["id"], "key": today_keys[e["id"]], "plan": housec.TODAY,
                                                     "zone": e["zone"], "type": e["type"], "chains": [[]]}))
    pieces = [p for _, p in sorted(pieces, key=lambda rp: (rp[0], rp[1]["id"], rp[1]["key"]))]
    docs = {d["name"]: d for g in groups for d in g["docs"].values() if d["name"] in used}

    # the documents, plans and views, all written once everything has compiled
    areas_dir = os.path.join(out_dir, AREAS_OUT)
    plans = {housec.TODAY: os.path.abspath(os.path.join(areas_dir, "plans", housec.TODAY + ".json"))}
    writes, plan_writes = [], [(today_plan, os.path.abspath(house_json), base_sha, plans[housec.TODAY])]
    for name, d in docs.items():
        text = housec.document_text(d["doc"])
        doc_path = os.path.abspath(os.path.join(areas_dir, "documents", name + ".json"))
        plans[name] = os.path.abspath(os.path.join(areas_dir, "plans", name + ".json"))
        writes.append((doc_path, text))
        plan_writes.append((d["plan"], doc_path, _sha(text), plans[name]))
    views = {}
    for vid in sorted(design.views):
        try:
            spec = housec.evaluate_option(entry, vid, design=design)
            doc = housec.apply_option(base, spec, vid, base_sha, housec.option_source_sha256(entry, vid, design))
            housec.check_document(doc, vid)
        except housec.HousecError as e:
            raise AreasError(f"view {vid}: {e}") from None
        writes.append((os.path.join(out_dir, "options", vid + ".json"), housec.document_text(doc)))
        choose = design.views[vid]["choose"]
        views[vid] = {"name": design.views[vid]["name"],
                      "tags": [housec.layer_tag(a, choose.get(a, housec.TODAY)) for a in order]}

    manifest = {
        "manifest": MANIFEST_VERSION,
        "house": {"path": os.path.abspath(house_json), "sha256": base_sha},
        "areas": [{"id": a["id"], "name": a["name"], "tags": [housec.layer_tag(a["id"], alt) for alt in alts],
                   "today": housec.layer_tag(a["id"], housec.TODAY)} for a, (_, alts) in zip(design.areas, dims)],
        "views": views,
        "plans": plans,
        "pieces": pieces,
        "unavailable": [u for _, u in sorted(unavailable.items())],
        "location": today_plan.location,
    }
    manifest["sha256"] = _sha(canonical(manifest))
    for path, text in writes:
        _write(path, text)
    for plan, ir_path, sha, path in plan_writes:
        planner.write_plan(plan, ir_path, sha, path)
    _prune(os.path.join(areas_dir, "documents"), {name + ".json" for name in docs})
    _prune(os.path.join(areas_dir, "plans"), {os.path.basename(p) for p in plans.values()})
    manifest_path = os.path.join(areas_dir, "manifest.json")
    _write(manifest_path, json.dumps(manifest, indent=1) + "\n")

    shell = sum(1 for p in pieces if p["chains"] == [[]])
    said = " + ".join(" x ".join(f"{1 + len(design.alts[a])} {a}" for a in g["areas"]) for g in groups) or "no areas"
    echo(f"wrote {manifest_path}: {sum(len(g['combos']) for g in groups)} combinations in {len(groups)} "
         f"group{'' if len(groups) == 1 else 's'} ({said}); {shell} of today's {len(base['elements'])} elements are "
         f"the shell, {len(switched)} switch with an area; {sum(1 for p in pieces if p['plan'] != housec.TODAY)} "
         f"pieces of the alternatives' own; {sum(len(p['chains']) for p in pieces)} placements; {len(views)} views; "
         f"{time.monotonic() - t0:.1f}s")
    for u in manifest["unavailable"]:
        where = ", ".join(f"{a}/{alt}" for a, alt in sorted(u["combination"].items())) or "anything"
        echo(f"  {u['area']}/{u['alt']} shows as {u['area']}/{housec.TODAY} with {where}: {u['code']}: {u['words']}")
    return manifest


def _write(path, text):
    """Write text to path unless the file already holds it, so an unchanged file keeps its time stamp."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path) as fh:
            if fh.read() == text:
                return
    except OSError:
        pass
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _prune(directory, keep):
    """Remove the .json files in `directory` that the manifest no longer names."""
    if os.path.isdir(directory):
        for f in os.listdir(directory):
            if f.endswith(".json") and f not in keep:
                os.remove(os.path.join(directory, f))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("entry", help="house.star")
    ap.add_argument("--house", default=None, help="today's compiled document (default: house.json beside the entry)")
    ap.add_argument("-o", "--out", default=None, help="the out directory (default: out beside the entry)")
    a = ap.parse_args(argv)
    root = os.path.dirname(os.path.abspath(a.entry))
    try:
        compile_areas(a.entry, a.house or os.path.join(root, "house.json"), a.out or os.path.join(root, "out"))
    except (housec.HousecError, AreasError) as e:
        sys.exit(f"areasc: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
