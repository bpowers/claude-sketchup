"""The owner's review page: every solved photo with the model's edges over it, in one self-contained HTML file.

    uv run python -m overlay.gallery [--out out/gallery.html] [--title TITLE] [--notes NOTES.md] [--width 1400]

One card per camera in overlay/cameras, grouped by the zone the camera stands in (outside last), titled by the first
of the picks file's notes. Each card stacks the photo under its clean overlay (out/overlays/<stem>_clean.png, from
overlay.compose --clean) with a fade slider, and says the solve's rms and held-out checks. A card says so when the
camera was solved on another house.json than the one standing, or its overlay predates its camera. --notes puts a
short Markdown file (headings, paragraphs, bullets, **bold**, `code`) at the top: what changed, what to look at.
Images are embedded as JPEG at most --width px wide, so the page is one file to open, send or publish.
"""
from __future__ import annotations

import argparse
import base64
import datetime as _dt
import hashlib
import html
import io
import json
import re
from pathlib import Path
from typing import Sequence

from PIL import Image

from . import paths

CSS = """
:root{--bg:#f6f5f2;--card:#fff;--ink:#1c1c1e;--muted:#6b6b70;--line:#dedcd6;--accent:#b0127a;--warn:#9a5b00;
--code:#f0eee9}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#141416;--card:#1d1d20;--ink:#ececef;
--muted:#9c9ca3;--line:#2e2e33;--accent:#ff5fcf;--warn:#f0b35a;--code:#26262b}}
:root[data-theme="dark"]{--bg:#141416;--card:#1d1d20;--ink:#ececef;--muted:#9c9ca3;--line:#2e2e33;--accent:#ff5fcf;
--warn:#f0b35a;--code:#26262b}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
sans-serif}
main{max-width:1120px;margin:0 auto;padding:24px 16px 64px;overflow-wrap:break-word}
h1{font-size:26px;line-height:1.25;margin:0 0 4px}
.sub{color:var(--muted);margin:0 0 20px;max-width:70ch}
.lede{max-width:70ch;margin:0 0 28px}
.lede ul{padding-left:20px}
code{background:var(--code);padding:1px 4px;border-radius:4px}
nav{display:flex;flex-wrap:wrap;gap:6px 18px;margin:0 0 8px}
nav a{color:var(--accent)}
h2.group{font-size:21px;margin:36px 0 12px;padding-bottom:6px;border-bottom:1px solid var(--line)}
.sheet{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:0 0 24px;overflow:hidden}
.head{display:flex;flex-wrap:wrap;gap:6px 18px;align-items:baseline;justify-content:space-between;padding:12px 16px}
.head h3{font-size:19px;margin:0}
.meta{display:flex;flex-wrap:wrap;gap:2px 12px;color:var(--muted);font:13px ui-monospace,SFMono-Regular,Menlo,monospace}
.stale{color:var(--warn);font-weight:600}
.stack{position:relative}
.stack img{display:block;width:100%;height:auto}
.stack .lines{position:absolute;inset:0;height:100%;opacity:var(--blend,1)}
.controls{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:10px;align-items:center;padding:10px 16px 14px}
.controls input{width:100%;margin:0;accent-color:var(--accent)}
.controls button{font:inherit;font-size:14px;padding:4px 10px;border:1px solid var(--line);border-radius:6px;
background:var(--card);color:var(--ink);cursor:pointer}
.missing{margin:0;padding:10px 16px 14px;color:var(--warn)}
"""

SCRIPT = """
document.querySelectorAll(".match").forEach(function (fig) {
  var stack = fig.querySelector(".stack"), range = fig.querySelector("input");
  function apply(v) { stack.style.setProperty("--blend", String(v / 100)); }
  range.addEventListener("input", function () { apply(Number(range.value)); });
  fig.querySelectorAll("button").forEach(function (b) {
    b.addEventListener("click", function () { range.value = b.dataset.set; apply(Number(b.dataset.set)); });
  });
});
"""


def _jpeg(im: Image.Image, width: int, quality: int) -> tuple[str, tuple[int, int]]:
    im = im.convert("RGB")
    if im.width > width:
        im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode(), im.size


def _inline(s: str) -> str:
    s = html.escape(s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)


def markdown_html(text: str) -> str:
    """Headings, paragraphs and bullets: enough for a short note to the owner."""
    out, para, items = [], [], []

    def flush() -> None:
        if para:
            out.append("<p>" + _inline(" ".join(para)) + "</p>")
            para.clear()
        if items:
            out.append("<ul>" + "".join(f"<li>{_inline(i)}</li>" for i in items) + "</ul>")
            items.clear()

    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            flush()
            out.append(f"<h2>{_inline(s.lstrip('#').strip())}</h2>")
        elif s.startswith(("- ", "* ")):
            if para:
                flush()
            items.append(s[2:])
        elif not s:
            flush()
        else:
            if items:
                flush()
            para.append(s)
    flush()
    return "\n".join(out)


def card(stem: str, doc: dict, sha_now: str | None, width: int, quality: int) -> str:
    notes = doc.get("notes") or []
    name = notes[0] if notes else stem
    r, ck = doc.get("residual_px") or {}, doc.get("check_residual_px") or {}
    meta = [f"<span>{html.escape(stem)}</span>",
            f"<span>rms {r.get('rms', 0):.2f} px over {r.get('n', 0)} picks</span>",
            f"<span>checks rms {ck['rms']:.2f} px, max {ck['max']:.2f} px over {ck['n']}</span>" if ck.get("n")
            else "<span>no held-out checks</span>"]
    solved = (doc.get("house") or {}).get("sha256")
    if solved and sha_now and solved != sha_now:
        meta.append(f'<span class="stale">solved on an older house.json ({html.escape(solved[:12])})</span>')
    clean = paths.OVERLAYS / f"{stem}_clean.png"
    missing = []
    try:
        photo = Image.fromarray(paths.load_rgb(paths.resolve_photo(doc.get("photo") or stem)))   # as overlays see it
    except FileNotFoundError:
        photo = None
    if photo is None:
        figure = ""
        missing.append(f"The photo {html.escape(str(doc.get('photo')))} was not found.")
    else:
        src, (w, h) = _jpeg(photo, width, quality)
        if clean.exists():
            if clean.stat().st_mtime < (paths.CAMERAS / f"{stem}.json").stat().st_mtime:
                missing.append("This overlay predates the camera's latest solve; render and compose it again.")
            with Image.open(clean) as im:
                lines, _ = _jpeg(im, width, quality)
            figure = f"""<figure class="match" style="margin:0">
  <div class="stack"><img src="{src}" width="{w}" height="{h}" alt="Photo {html.escape(stem)}">
   <img class="lines" src="{lines}" width="{w}" height="{h}"
    alt="Photo {html.escape(stem)} with the model's edges"></div>
  <div class="controls"><button type="button" data-set="0">Photo</button>
   <input type="range" min="0" max="100" value="100" aria-label="Fade between the photo and the model's edges">
   <button type="button" data-set="100">Model</button></div>
 </figure>"""
        else:
            figure = (f'<div class="stack"><img src="{src}" width="{w}" height="{h}" '
                      f'alt="Photo {html.escape(stem)}"></div>')
            missing.append("No model overlay for this photo yet (render it, then compose --clean).")
    return (f'<section class="sheet" id="{html.escape(stem)}"><div class="head"><h3>{html.escape(name)}</h3>'
            f'<div class="meta">{"".join(meta)}</div></div>{figure}'
            + "".join(f'<p class="missing">{m}</p>' for m in missing) + "</section>")


def build(out: Path, *, title: str = "Model on photos", notes: str = "", width: int = 1400, quality: int = 80) -> dict:
    sha_now = hashlib.sha256(paths.HOUSE.read_bytes()).hexdigest() if paths.HOUSE.exists() else None
    groups: dict[str, list[str]] = {}
    for p in sorted(paths.CAMERAS.glob("*.json")):
        doc = json.loads(p.read_text())
        zone = (doc.get("eye_space") or {}).get("zone") or "outside"
        groups.setdefault(zone, []).append(card(p.stem, doc, sha_now, width, quality))
    order = sorted(groups, key=lambda z: (z == "outside", z))
    nav = " ".join(f'<a href="#g-{html.escape(z)}">{html.escape(z.capitalize())} ({len(groups[z])})</a>' for z in order)
    body = "\n".join(f'<h2 class="group" id="g-{html.escape(z)}">{html.escape(z.capitalize())}</h2>\n'
                     + "\n".join(groups[z]) for z in order)
    n = sum(len(v) for v in groups.values())
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}</style></head>
<body><main>
<h1>{html.escape(title)}</h1>
<p class="sub">{n} photos with the model's edges drawn over them; made {_dt.datetime.now():%Y-%m-%d %H:%M}. Drag a
slider to fade between the model and the plain photo. rms is how far the camera's reference points miss, in pixels;
checks are points held out of the camera's fit.</p>
<section class="lede">{markdown_html(notes)}</section>
<nav>{nav}</nav>
{body or '<p>No solved photos yet.</p>'}
</main><script>{SCRIPT}</script></body></html>
"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page)
    return {"page": out, "cards": n, "bytes": out.stat().st_size}


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m overlay.gallery",
                                 description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=paths.OUT / "gallery.html")
    ap.add_argument("--title", default="Model on photos")
    ap.add_argument("--notes", type=Path, help="a short Markdown note for the top of the page")
    ap.add_argument("--width", type=int, default=1400, help="the widest image embedded, px")
    ap.add_argument("--quality", type=int, default=80, help="JPEG quality")
    a = ap.parse_args(argv)
    res = build(a.out, title=a.title, notes=a.notes.read_text() if a.notes else "", width=a.width, quality=a.quality)
    print(f"wrote {paths.to_root(res['page'])}: {res['cards']} photos, {res['bytes'] / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
