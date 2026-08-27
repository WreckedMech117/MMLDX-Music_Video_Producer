"""The routes that used to be nested functions inside `create_app`, split by resource.

`create_app` builds one `RouterContext` -- the seven injected dependencies and the handful of
helpers built from them -- and hands the same instance to each module's `register`, which
declares its routes on the application with the same decorators it used inside the factory.
Sixty of the seventy-six routes live here now; of the sixteen left, fifteen are held by tests
named below and the sixteenth is `index`, which serves the workspace's `index.html` from inside
the block that mounts the static assets. `create_app` went from 7,882 lines and 94 nested
definitions to 4,836 and 64 when the split landed, and to 3,542 and 44 when the file-scoped
source guards were widened and the fifteen routes they were pinning followed.

**Why `register(app)` and not `APIRouter` + `include_router`.** The router object is the
idiomatic FastAPI answer and it was built first; it is unusable in this application. On
FastAPI 0.141.1 `include_router` no longer copies routes onto the application -- it appends one
lazy `_IncludedRouter` wrapper, and `app.routes` stops being a flat list of routes. Three of
this repository's guards walk `app.routes` off a live `create_app()`: `MANIFEST_WRITE_GUARDS`
(every saving route is classified), `JOB_RECORDED_FIELDS` (every route a job record can arrive
on is enumerated), and the readiness gate (every route reaching `comfy.submit` asks readiness
first). Under `include_router` all three stopped seeing sixty routes and the suite failed
loudly. There is no public way to flatten an included router on this version. Registering on
the real application keeps that list flat and keeps every route object identical to the one
the decorator built before the move.

The modules import `app.py` back, for the request models, refusal sentences and helpers their
routes are written against. That is why `create_app` imports *them* from inside its own body:
a module-scope import in both directions is a cycle, and this is the direction that can be
deferred, because nothing needs a route module until `create_app` runs.

The import at the bottom of this file is load-bearing, not decoration. It makes the cycle
resolvable from *either* end: reaching a route module first -- `import
music_video_producer.routes.shots` on a cold interpreter -- pulls `app.py` in through this
package before that module starts executing, so `create_app`'s own import of it finds it
unstarted and loads it cleanly. Without this line that import fails with a partially
initialised module, which is a trap set for whoever writes the first script that imports one
route module on its own.

## The source guards were widened, and fifteen routes followed

The split left thirty-one routes in `app.py`, in two classes. The first was held by tests that
read `app.py` **as a file** -- counting occurrences in it, slicing it between markers, parsing
it. The Director's ruling on that class: *the scope was the defect*. Every one of those guards
claims something about this application -- "every write path for an asset's name", "the one
writer of approval", "the reader that drains stderr the safe way" -- and every one of them was
written when a filename happened to be a synonym for the application. It no longer is, and a
new write path added in `routes/assets.py` sailed straight past the count that exists to catch
it.

So the guards now scan `src/music_video_producer/` (see `tests/package_source.py`, which is the
one implementation of that scan and explains why comments and docstrings come out of it first).
They are strictly stronger than they were, each was shown to fail on a violation planted in a
module it did not previously read, and the fifteen routes moved as a consequence rather than as
the goal:

* `replace_project`, `rename_asset`, `upload_asset`, `generate_flux`, `generate_multiview`,
  `edit_asset`, `fill_assets` -- the `Asset(`/`asset.name = ` enumeration counts over the
  package now, and subtracts `models.py`'s own `class Asset(` by name rather than by exempting
  a file.
* `approve_take`, `unapprove_take` -- the approval scans always walked the package; they keyed
  their answer on `Path.name`, which is not an identity here (`timeline.py` and
  `routes/timeline.py` both exist, and a shared filename silently overwrites an entry). They
  key on the module's path within the package now.
* `cancel_open_jobs`, `cancel_job`, `read_job` -- the cancel guard finds `cancel_open_jobs` by
  name across the package instead of cutting `create_app`'s source between `async def` and the
  next `@app.`, so a body ends where the body ends. `read_job` moves with the pair to keep
  `cancel_job` registered first: whichever of them comes first decides the `Allow` header
  `/jobs/{job_id}` answers a 405 with, and this application has always answered `DELETE`.
* `read_song_envelope` -- the decorator is one literal and is looked for package-wide.
* `read_timeline_snap_targets` -- all three of its assertions: the text slice and the `ast`
  walk both go through `function_source`/`function_ast`, and the path check now requires
  exactly one module to name the path and *that* module to declare it with a `GET` and the
  response model.
* `expand_shot_prompt` -- the "every writer of an expansion records its map" scan parses every
  module. This one is the clearest case for the widening: moving the route would not have
  failed the old test, it would have stopped covering it. Run the scan over `app.py` alone
  today and this route is missing from the answer.

## The fifteen routes still in `app.py`, and what holds each there

All fifteen are held the same way: a test monkeypatches a module-level name in
`music_video_producer.app`'s namespace. A route resolves such a name against the globals of the
module it is *defined* in, so a route declared here would read `routes/<file>.py`'s binding and
never see the patch. This class is out of scope by the Director's decision; each becomes
movable when its test reaches the name through the endpoint's own module instead.

* `generate_h3` -- `build_h3_director_payload`, `build_h3_reference_payload`,
  `numbered_references`, `readiness_report`. One of those tests patches "every module that
  binds the name" and lists them; a move adds an eighth module to a list a test writes out.
* `generate_batch`, `render_again`, `mark_shot_ready`, `mark_shot_draft` and their
  `_set_shot_commitment` helper -- the batch route is a fan-out over `generate_h3` and calls
  it, and the other three, by name.
* `render_shot_preview` and its `take_measurement`, `export_geometry` and `preview_side`
  helpers -- `build_effect_stages`, `trim_args`, `probe_take_args`.
* `assemble_project` -- `trim_args` and `concat_args`, patched at four sites in
  `test_assembly_route.py`. **The split's own notes had this route in the source-text class,
  named only by the `run_tool` slice and the `offset=` literal; it is in both, and the
  namespace half is the one that decides.** Moved with the other fourteen it took thirteen
  tests down with it, which is how this was found. Its two source guards are widened all the
  same -- `run_tool` is `create_app`'s shared helper and stays regardless of where the route
  lives, and the offset expression is now looked for across the package.
* `compile_timeline`, `read_readiness` -- `readiness_report`.
* `fill_section_looks` -- `plan_fingerprint`.
* `lay_out_timeline`, `line_up_timeline`, `fill_in_timeline`, `populate_timeline` --
  `lay_out_shots`, `line_up_shots`, `fill_in_shots`, `plan_fingerprint`, `window_fingerprint`.
* `clean_shot_prompts` -- `window_fingerprint`, `citation_fingerprint`, `plan_fingerprint`.

That last group is the reason `timeline.py` holds two routes. Five of the seven `/timeline/*`
routes are in the list above, and the file is kept rather than folded away so that the shape
the split was asked for is visible and the debt has somewhere to be paid back into.
"""

from __future__ import annotations

from .. import app as _app  # noqa: F401
