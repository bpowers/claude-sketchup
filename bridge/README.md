# The SketchUp bridge

SketchUp has no remote API, but it has a Ruby interpreter and a timer, and that is enough. `watcher.rb` runs inside
SketchUp (it calls itself House twin there) and polls `bridge/trigger` four times a second; when a new request appears
it runs it and answers in `out/bridge/<id>.json`. `client.py` is the Python end: it writes a request, waits for its
answer, and returns the result or raises an error carrying the Ruby details. Everything that changes the model (`build`,
`sync`) or looks at it (`render`) goes through here, so a Claude Code session edits the house and the owner sees it in
SketchUp seconds later.

## Install

The bridge needs SketchUp Pro for desktop (its Ruby API) with this project on the same machine. It runs on macOS;
Windows is untested. README.md, Setup, steps 3 to 5, installs it once: one line pasted into SketchUp's Ruby
Console loads this project's `watcher.rb` and calls `HouseTwin::Watcher.install!` and `HouseTwin::Watcher.start`.

`install!` writes `house_twin_loader.rb` into SketchUp's Plugins folder. It records this project's path and loads the
project's own `bridge/watcher.rb` each time SketchUp starts, then starts the watcher a few seconds later, so nothing
needs pasting again. `start` starts it now, and the console prints `[house twin] ok: watching ...`. The watcher serves
one project, the one the line was last pasted from: pasting it from another checkout points SketchUp there instead.
`uv run python bridge/client.py ping` checks the connection and names the project served.

## From the command line

    uv run python bridge/client.py ping
    uv run python bridge/client.py eval 'Sketchup.active_model.title'
    uv run python bridge/client.py build build/fixtures/walls.json --top FIXTURE   # plan a document, draw it whole
    uv run python bridge/client.py sync out/areas/manifest.json [--rebuild]
    uv run python bridge/client.py built                             # what HOUSE was built from
    uv run python bridge/client.py render overlay/requests/x.json [--expect-house house.json]
    uv run python bridge/client.py views [--view NAME]               # overview pictures into out/views/<NAME>/
    uv run python bridge/client.py save [PATH]                       # the model to house.skp, or a copy to PATH

Each prints the result as JSON and exits 1 on an error, with the message (and the Ruby backtrace) on stderr. `build`
and `sync` print a summary of the build's stats, and all of them with `--full`. `look.py` runs the usual sequence
(compile, plan, areas, sync, photos), so day to day the client is mostly for `ping`, `eval`, `views` and `save`.

- `build` draws one document whole, every element in no chain, into the `--top` group, leaving HOUSE alone: it is how
  a fixture is looked at. Without `--top` it draws into HOUSE itself, replacing the synced house until the next sync;
  the house, with its design areas, is drawn by `sync`.
- `sync` makes HOUSE hold exactly what `areasc.py`'s manifest places, building in place: only what changed is built,
  instanced or erased (docs/IR.md, "In SketchUp"). `--rebuild` draws everything again.
- `render` draws one request (docs/IR.md, "Render requests").
- `views` writes four overview pictures of HOUSE to `out/views/today/`: two shaded isometric views, one in the edges
  style, and a plan of the main storey with the roof and ceilings hidden. They show today's house whatever tags the
  owner has on; `--view NAME` shows a view of `zones/areas.star` instead (its tags from `out/areas/manifest.json`),
  in `out/views/NAME/`, so views can be compared side by side.

## From Python

    from bridge.client import Bridge, BridgeError, Refused
    br = Bridge()
    br.ping()                                 # {"protocol", "sketchup", "model", "project", ...}
    br.eval("1 + 1")                          # 2
    stats = br.build("build/fixtures/walls.json", top="FIXTURE")   # Refused, naming each element, before sending
    br.sync("out/areas/manifest.json", rebuild=False)
    br.built()                                # {"ir", "ir_sha256", "manifest_sha256", "build_sha256", ...} or None
    br.render({"out": "out/renders/x.png", ...}, expect_sha256=sha)
    br.views(); br.views("kitchen_island"); br.save()

`Bridge(root=None, *, timeout=1800, pickup_timeout=6, max_pickup=120, activate=True)`: `root` is the project the
watcher serves (this one by default), `timeout` the seconds a request may run, `max_pickup` the seconds to wait for
SketchUp to pick a request up.

- `BridgeError` carries `ruby_class`, `ruby_message`, `backtrace` and, for a build failure, `element_id`.
- `BridgeTimeout` (a BridgeError): nothing picked the request up, or it did not finish in time.
- `Refused` (a BridgeError): the document does not plan (`ir/plan.py`); nothing was sent. `element_ids` names the
  refused elements and `plan` holds the findings.
- `build` checks that SketchUp built the bytes it planned (the result's `ir_sha256`), and `sync` refuses a manifest
  compiled on a `house.json` other than the one on disk.
- Requests from any number of processes are serialised with a lock on `out/bridge/.lock`, since there is one trigger.

## The protocol

The trigger holds one JSON object, written atomically (a temporary file renamed over it):

    {"bridge": 1, "id": "20261031T141500-123-ab12cd34", "cmd": "ping"}
    {"bridge": 1, "id": "...", "cmd": "eval", "code": "Sketchup.version"}
    {"bridge": 1, "id": "...", "cmd": "build", "path": "/abs/out/plans/<sha>.json", "top": "HOUSE"}
    {"bridge": 1, "id": "...", "cmd": "sync", "path": "/abs/out/areas/manifest.json", "rebuild": false}
    {"bridge": 1, "id": "...", "cmd": "built"}
    {"bridge": 1, "id": "...", "cmd": "render", "request": {...}, "expect_ir_sha256": "..."}
    {"bridge": 1, "id": "...", "cmd": "views", "name": "kitchen_island", "areas": ["kitchen/island"]}
    {"bridge": 1, "id": "...", "cmd": "save", "path": null}

- `id` matches `[A-Za-z0-9._-]{1,128}`. `build` takes a plan file (the client plans the document and writes the plan
  under `out/plans/`), never a house.json. `render` takes the request inline as `request`, or as a file in `path`.
- `save` with a null path saves the open model as `house.skp` in the project root: an untitled model becomes that
  file, and a model opened from it is saved in place. It refuses when the open model is another file, rather than
  write over the project's; with a path it saves a copy there.
- The reply is written atomically to `out/bridge/<id>.json`, first `{"state": "running"}` when the request is picked
  up, then `"ok"` or `"error"`, and mirrored to `out/status.json`:

      {"state": "ok", "id": "...", "cmd": "eval", "protocol": 1, "message": "eval ok", "at": "...",
       "seconds": 0.002, "sketchup": "26.0.000", "model": "house", "result": 2, "eval": "2"}

      {"state": "error", "id": "...", "cmd": "sync", "protocol": 1,
       "message": "HouseTwin::BuildError: living.wall.front: the wall body has volume ...",
       "error": {"class": "HouseTwin::BuildError", "message": "...", "backtrace": ["..."],
                 "element_id": "living.wall.front"}}

- `result` is the command's value reduced to JSON (points and lengths become numbers, anything else its inspect
  string); `eval` also returns the raw inspect string. Eval requests share one top-level binding, so a local variable
  one sets is there for the next.
- A request is new when the trigger's modification time changes, or, within three seconds of the last request, when
  its id does (a filesystem with coarse time stamps can give two requests one time).
- A request written while SketchUp is still starting is answered when the watcher starts, if it is less than ten
  minutes old; a request already picked up (`out/bridge/last_id` records the last one) is never run twice.
- Replies older than a day are pruned.

## Hot reload

`build/build.rb` and `build/render.rb` are loaded again by every command that uses them, so an edit to either takes
effect on the next request. The watcher itself reloads in place, keeping its timer:

    uv run python bridge/client.py eval "load '$PWD/bridge/watcher.rb'; HouseTwin::Watcher.running?"

A changed polling period takes effect when the watcher is started again (`HouseTwin::Watcher.start`). Run `install!`
again only to point SketchUp at another checkout.

## App Nap (macOS)

macOS slows the timers of an app in the background, so a request can sit unread while SketchUp is behind the terminal.
When nothing picks a request up within `pickup_timeout` seconds, the client brings SketchUp to the front with
`osascript` (the running SketchUp's own bundle id, read from its Info.plist; `TWIN_SKETCHUP_APP_ID` overrides it) and
keeps waiting, up to `max_pickup`. If SketchUp is not running at all it says so at once. README.md, Setup, step 5,
stops the throttling for good with one `defaults write`.

## The live check

    uv run python bridge/smoke.py [--only walls roof] [--keep]

A few seconds in the open SketchUp: it builds every fixture in `build/fixtures/` into the throwaway top group `SMOKE`
(never HOUSE), checks each build's stats (every element drawn, every solid's volume check passed, faces there), checks
that `refused.json` is refused before anything is sent, renders one edges picture to `out/smoke/sample_edges.png`, and
erases `SMOKE` with the tags and tag folders its builds made that the model did not have. It refuses to start unless the
bridge answers ping. It leaves HOUSE, FURNISHINGS, the camera and the owner's tags as they were, but each build is a
step on the owner's undo stack, so ask the owner before running it while they work. Run it after changing build.rb or
render.rb; the fast tests (`uv run pytest tests/test_ruby.py`) cover only what runs without SketchUp.

## Troubleshooting

- **`ping` times out ("not picked up").** Is SketchUp open, with a model window? The Ruby Console shows
  `[house twin]` lines when the watcher starts and for every request. In the console, `HouseTwin::Watcher.running?`
  says whether it polls, and `HouseTwin::Watcher.start` starts it. If `HouseTwin` is not defined, the loader did not
  run: paste the install line again (README.md, Setup, step 3). The timeout message ends with this project's last
  status (`out/status.json`, the watcher's last word here).
- **The watcher serves another checkout.** It reads only its own project's `bridge/trigger`, so requests from this
  folder go unanswered and time out, and the last status shown is missing or old. This is the likely cause after the
  project was copied or moved. In the Ruby Console, `HouseTwin::Watcher::ROOT` names the folder it serves; paste the
  install line from this folder to move it here.
- **Requests hang while SketchUp shows a dialog.** A modal dialog (a save prompt, Model Info, an extension's window,
  a Ruby error box) stops SketchUp's timer, and so the watcher. Close it and the request is picked up.
- **"a group or component is open for editing".** A build or render needs every group closed; close it in SketchUp
  (Edit > Close Group) and send again.
- **A build failed.** The error names the element (`element_id`) and the operation was aborted, so the previous HOUSE
  is as it was. A refusal from the planner names the element before anything was sent; an error from SketchUp itself
  (a face it would not make, a volume that disagrees with the plan) comes back with the Ruby backtrace.
- **An error from inside SketchUp that the reply does not explain.** The Ruby Console prints every error with its
  backtrace. `uv run python bridge/client.py eval '...'` runs Ruby there to look at the model: for example
  `HouseTwin::Build.identity` or `HouseTwin::Build.find("living.wall.front").entities.length`.
- **Windows** is untested. The client and the look lock have Windows code paths (with no App Nap to work around, the
  client just waits), but they have never run.
