# Units, dimension chains and the little arithmetic Starlark lacks.
#
# The model works in decimal inches. Drawings are dimensioned in feet and inches, so a dimension is written the way the
# drawing writes it and parsed here: inches("12'-6 1/2\"") is 150.5.
#
# chain() is the important one. A drawing states a run of dimensions and an overall. If a number was misread, the run
# does not sum to the overall, so chain() checks the closure and fails the build: a misread number becomes an error
# instead of a crooked wall.

FT = 12.0

def inches(v):
    """A dimension in inches: a number (already inches) or a feet-inches string such as 12'-6 1/2", 6'-8", 3 1/2" or
    7'."""
    if type(v) == "int" or type(v) == "float":
        return float(v)
    if type(v) != "string":
        fail("inches: expected a number or a feet-inches string, got %r" % (v,))
    s = v.strip().replace('"', "").replace("''", "'")
    neg = s.startswith("-")
    if neg:
        s = s[1:].strip()
    total = 0.0
    rest = s
    if "'" in s:
        parts = s.split("'", 1)
        if parts[0].strip():
            total += float(parts[0].strip()) * FT
        rest = parts[1].strip()
    rest = rest.lstrip("-").strip()
    for tok in rest.split(" "):
        tok = tok.strip()
        if not tok:
            continue
        if "/" in tok:
            nd = tok.split("/")
            if len(nd) != 2 or float(nd[1]) == 0.0:
                fail("inches: %r has a bad fraction %r" % (v, tok))
            total += float(nd[0]) / float(nd[1])
        else:
            total += float(tok)
    return -total if neg else total

_SIXTEENTHS = ["", "1/16", "1/8", "3/16", "1/4", "5/16", "3/8", "7/16",
               "1/2", "9/16", "5/8", "11/16", "3/4", "13/16", "7/8", "15/16"]

def fmt(v):
    """Inches as a feet-inches string rounded to 1/16 in, for messages: fmt(150.5) is 12'-6 1/2"."""
    neg = v < 0
    v = -v if neg else v
    six = int(v * 16.0 + 0.5)
    f = six // (12 * 16)
    six = six - f * 12 * 16
    i = six // 16
    frac = _SIXTEENTHS[six - i * 16]
    s = "%d'-%d" % (f, i)
    if frac:
        s += " " + frac
    s += "\""
    return "-" + s if neg else s

def total(values):
    """The sum of a list of numbers (Starlark has no sum())."""
    t = 0.0
    for v in values:
        t += v
    return t

def sqrt(x):
    """Starlark has no ** operator and no math module; Newton's method with a fixed count is exact to double precision
    for the sizes a house has."""
    if x <= 0.0:
        return 0.0
    g = x if x > 1.0 else 1.0
    for _i in range(40):
        g = 0.5 * (g + x / g)
    return g

def chain(dims, start = 0.0, closes = None, tol = 0.03125, name = "chain"):
    """A run of dimensions as cumulative stations along an axis.

    dims    the dimensions, each a number, a feet-inches string or a [label, dimension] pair. One of them may be the
            string "FILL": a span the drawing marks EQUAL or leaves undimensioned, solved from the closure rather than
            guessed.
    start   the station of the first tick
    closes  the stated overall: the run must sum to it within tol, or the build fails naming the chain
    Returns {"stations": [...], "spans": [{"label", "a", "b", "len"}], "total", "filled": the FILL span or None,
             "name"}.
    """
    fill_at = -1
    vals = []
    labels = []
    for i, d in enumerate(dims):
        label = None
        val = d
        if type(d) == "tuple" or type(d) == "list":
            label = d[0]
            val = d[1]
        labels.append(label)
        if val == "FILL":
            if fill_at >= 0:
                fail("%s: only one FILL span is allowed in a chain" % name)
            fill_at = i
            vals.append(0.0)
        else:
            vals.append(inches(val))
    filled = None
    if fill_at >= 0:
        if closes == None:
            fail("%s: a FILL span needs a stated overall to solve against" % name)
        filled = inches(closes) - total(vals)
        if filled < 0:
            fail("%s: the stated dimensions already exceed the overall by %s" % (name, fmt(-filled)))
        vals[fill_at] = filled
    stations = [start]
    spans = []
    x = start
    for i in range(len(vals)):
        spans.append({"label": labels[i], "a": x, "b": x + vals[i], "len": vals[i]})
        x += vals[i]
        stations.append(x)
    run = x - start
    if closes != None:
        diff = run - inches(closes)
        if diff > tol or diff < -tol:
            fail("%s does not close: the run sums to %s but the overall is %s (off by %s)" % (
                name, fmt(run), fmt(inches(closes)), fmt(diff)))
    return {"stations": stations, "spans": spans, "total": run, "filled": filled, "name": name}

def equal_bays(count, clear, bar, jamb = 0.0):
    """A run of `count` equal lights in the clear width `clear`, between jambs `jamb` wide and separated by bars `bar`
    wide, as a drawing says "4 LIGHTS EQUAL": {"glass", "bar", "jamb", "bays": [{"i", "a", "b", "w"}]}, positions from
    the run's start."""
    if type(count) != "int" or count < 1:
        fail("equal_bays: count must be a positive integer, got %r" % (count,))
    b = inches(bar)
    j = inches(jamb)
    glass = (inches(clear) - 2.0 * j - b * (count - 1)) / count
    if glass <= 0.0:
        fail("equal_bays: %d lights leave no glass in %s" % (count, fmt(inches(clear))))
    out = []
    x = j
    for i in range(count):
        out.append({"i": i, "a": x, "b": x + glass, "w": glass})
        x += glass + b
    return {"glass": glass, "bar": b, "jamb": j, "bays": out}

def solve_glass(fixed, groups, closes):
    """The common glass width a chain implies when the drawing says EQUAL instead of giving it: `fixed` the dimensions
    it does state, `groups` the equal runs as [light count, bar width] pairs, `closes` the overall."""
    n = 0
    bars = 0.0
    for g in groups:
        n += g[0]
        bars += (g[0] - 1) * inches(g[1])
    if n == 0:
        fail("solve_glass: no equal runs given")
    return (inches(closes) - total([inches(f) for f in fixed]) - bars) / n
