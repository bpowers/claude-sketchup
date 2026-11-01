# claude-sketchup

A house modelled as code, built with Claude Code and drawn in SketchUp Pro. You give Claude your architect's drawings
and photos of the house; it writes the house as Starlark source, and SketchUp redraws the model a few seconds after
each change, so you can talk through the model while you look at it. Photos are matched to the model with solved
cameras, so the model's edges can be laid over each photo to show where it is right and where it is not. Remodel ideas
are design areas: alternatives you switch on and off in SketchUp with its tags, beside the house as it stands today.

It ships with an example cottage, a small made-up house with a synthetic drawing set and photo, so the whole loop and
every tool can be tried before your own house replaces it.

## How it works

```
  sources/plans/*.pdf            sources/photos/*.jpg
  (CAD lines, dimensions)        (picks -> solved cameras)
           |                              |
           v                              |
  house.star, zones/, lib/                |
  the house as Starlark code              |
           |  housec.py                   |
           v                              |
  house.json  (flat list of elements)     |
           |  ir/plan.py checks it,       |
           |  areasc.py adds the          |
           |  design areas                |
           v                              |
  out/areas/manifest.json + plans         |
           |  bridge/client.py writes     |
           |  bridge/trigger              |
           v                              v
  SketchUp: watcher.rb syncs HOUSE  -->  renders laid over the photos
                                         (out/overlays/)
```

- **Starlark** is a small, Python-like language with no I/O, so the same sources always give the same house. Every
  number is a named constant with a comment saying where it came from (a drawing's dimension, a photo, your word), and
  dimension strings must add up to their overall or the compile fails.
- **house.json** is the whole house as data, in inches. `docs/IR.md` defines it.
- **The planner** (`ir/plan.py`) checks every element before anything reaches SketchUp, and names the element when
  something is wrong.
- **The bridge**: SketchUp has no remote API, so `bridge/watcher.rb` runs inside SketchUp and polls a trigger file
  four times a second. The model is synced in place: only what changed is redrawn, which keeps an edit to seconds.
- **Drawings** are read as exact CAD vectors where the PDF keeps them, and the model's plan is drawn over each sheet
  to check it (`docs/DRAWINGS.md`). **Photos** get solved cameras, and SketchUp renders the model through each one
  (`docs/PHOTOS.md`).

## What you need

- A Mac with **SketchUp Pro** for desktop. The bridge needs SketchUp's Ruby API, which the web and iPad versions lack.
- **[uv](https://docs.astral.sh/uv/)**, which installs Python and the dependencies.
- **[Claude Code](https://claude.com/claude-code)**.
- **Ruby 3.2 or later** first on the PATH, only for the Ruby tests; without it they skip. macOS ships Ruby 2.6, so
  install Homebrew's and put it first:

      brew install ruby
      echo 'export PATH="$(brew --prefix ruby)/bin:$PATH"' >> ~/.zshrc     # then open a new terminal

It runs on macOS with SketchUp Pro 2026. Other SketchUp versions and Windows are untested: the code has
Windows branches, but they have never run.

## Setup

1. **Copy this folder** to where you keep projects, without anything generated (`.venv/`, `out/`, caches), and make
   it a git repository. Keep it out of iCloud Drive's Desktop and Documents sync, which leaves conflict copies
   ("name 2") inside `.git` and `out/`.

       rsync -a --exclude=.git --exclude-from=claude-sketchup/.gitignore claude-sketchup/ my-house/
       cd my-house && git init && git add -A && git commit -m "Start from claude-sketchup"

2. **Install the dependencies and check them** (no SketchUp needed):

       uv sync
       uv run python look.py --compile-only     # compiles the example cottage
       uv run pytest -q                         # about 15 seconds

3. **Install the bridge**, the small Ruby program inside SketchUp that the tools talk to (it calls itself House twin
   there). Open SketchUp with a new, empty model. Open **Extensions > Developer > Ruby Console** and paste this line,
   with this folder's absolute path (`pwd` prints it):

       load "/path/to/my-house/bridge/watcher.rb"; HouseTwin::Watcher.install!; HouseTwin::Watcher.start

   The console says `[house twin] ok: watching ...`. From then on SketchUp starts the watcher itself each time it
   opens, for this folder: pasting the line from another folder moves it there.

4. **Check the connection**, from a terminal in this folder:

       uv run python bridge/client.py ping

   It prints SketchUp's version, the open model and the project the watcher serves.

5. **Keep macOS from throttling SketchUp** in the background (App Nap), which otherwise delays each request until the
   client brings SketchUp to the front. Quit SketchUp, then run this, putting in your SketchUp's bundle id if it
   differs (`osascript -e 'id of app "SketchUp"'` prints it):

       defaults write com.sketchup.SketchUp.2026 NSAppSleepDisabled -bool YES

6. **Draw the cottage.** With SketchUp open again:

       uv run python bridge/smoke.py              # optional live check of the builder, a few seconds
       uv run python look.py living-01            # draws the cottage into the group HOUSE, then matches its photo
       uv run python bridge/furnishings.py place  # its furniture, into the group FURNISHINGS
       uv run python bridge/client.py save        # saves the model as house.skp in this folder

   `out/overlays/living-01_overlay.png` then shows the model's edges over the cottage's (synthetic) photo.

   `house.skp` is not committed. The house in it is rebuilt from the sources; what only the file keeps is what you do
   by hand (furniture you move, tags you switch).

In SketchUp the house is one group, `HOUSE`. Its tags switch parts of it: `zone/<zone>` (hide `zone/roof` to look into
the rooms), `part/ceiling`, `part/glass`, `part/trim`, and one tag per design alternative such as `kitchen/island`.
**Extensions > House twin** switches each design area's alternatives. Loose furniture lives in the group `FURNISHINGS`.

`.claude/settings.json` lets Claude run the loop's commands (`look.py`, the drawing and photo tools, the tests) without
asking each time; anything else, such as a Ruby `eval` in SketchUp, still asks.

## Working with Claude

1. Put the architect's PDFs in `sources/plans/` and photos of the house in `sources/photos/`: real-estate listing
   photos are ideal, as the original files, uncropped.
2. Open `house.skp` in SketchUp, start Claude Code in this folder, and say something like:

   > This is my house at 12 Elm Street. The drawings are in sources/plans and photos in sources/photos. We want to
   > redo the kitchen. Let's start a new house.

3. Claude follows `CLAUDE.md`: it looks through the sources, asks you a few questions (your goal and which rooms come
   first, which way is north, the floors, what changed since the drawings), fills in `docs/HOUSE.md`, then replaces
   the cottage with your house, shell first, and checks it against the drawings and photos.
4. Watch SketchUp as it works. Speak in your own words and directions ("the window on the street side", "make the
   island six inches longer"), and answer its "what is this?" questions: those are the cheap, decisive facts.

Things worth knowing:

- Changes you make by hand inside `HOUSE` are lost whenever a sync redraws that part; tell Claude instead. Your tag
  choices, your camera, furniture you move and your own groups outside `HOUSE` are kept.
- Each request freezes SketchUp briefly while it runs, so Claude asks before anything that would hold it longer than
  about ten seconds. Say "SketchUp is mine for a while" and it sends nothing until you hand SketchUp back.
- `uv run python -m overlay.gallery` writes `out/gallery.html`, a page of every matched photo with the model's edges
  over it, and `uv run python bridge/client.py views --view kitchen_island` four overview pictures of a design
  alternative in `out/views/kitchen_island/`: both are for when you are away from SketchUp.

## The everyday loop

    uv run python look.py                  # compile, check, sync SketchUp: seconds
    uv run python look.py living-01        # the same, then re-match that photo: out/overlays/living-01_overlay.png
    uv run python look.py --compile-only   # compile and check without SketchUp

`CLAUDE.md` maps the repository and says how the work is done; `docs/` holds this house's brief (`HOUSE.md`) and the
guides to the house document, drawings, photos and design areas.

## Troubleshooting

- **`look.py` says SketchUp did not answer**, or `ping` times out: is SketchUp open with a model window, and no dialog
  open? Was the install line (Setup, step 3) last pasted from this folder? `bridge/README.md`, Troubleshooting, goes
  through the causes.
- **A compile error** names the Starlark file and line; **a refusal** names the element. Claude reads both.
- **`WARNING the sync changed the owner's state`**: a sync changed a tag or a group you had set. Tell Claude what you
  see; the sync is not meant to.
- **The cottage's tags are still listed** after your house replaced it: Window > Model Info > Statistics > Purge Unused.
- **A rebuild is slow with Enscape open**: every entity added or erased costs far more while a renderer watches the
  model. Day-to-day syncs touch only what changed; a change to `build/build.rb` redraws everything once.
- **Claude asks permission for every command**: start it from this folder, so it reads `.claude/settings.json`.
