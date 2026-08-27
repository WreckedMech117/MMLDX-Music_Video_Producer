"""The routes that used to be nested functions inside `create_app`, split by resource.

`create_app` builds one `RouterContext` -- the seven injected dependencies and the handful of
helpers built from them -- and hands the same instance to each module's `register`, which
declares its routes on the application with the same decorators it used inside the factory.
Forty-five of the seventy-six routes live here now; `create_app` went from 7,882 lines and 94
nested definitions to 4,836 and 64.

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

## The thirty-one routes still in `app.py`, and what holds each there

Not taste, and not the work running out. Each of these is held by a test that reaches into
`app.py` *as a file or as a namespace*, in a way that a move would break or -- worse -- would
silently stop enforcing. The rule applied was the Director's: if a test has to change for the
split to pass, the split is wrong, so the split moved instead.

**Held by tests that read `app.py`'s source text.** These count occurrences or slice the file
between markers, and both are keyed to the filename:

* `replace_project`, `rename_asset`, `upload_asset`, `generate_flux`, `generate_multiview`,
  `edit_asset`, `fill_assets` -- `tests/test_api.py::test_every_write_path_for_an_assets_name_is_enumerated`
  requires exactly two `asset.name = ` assignments and five `Asset(` constructions *in
  `app.py`*. Moving any one of them changes a count.
* `approve_take`, `unapprove_take` -- `test_the_approve_route_is_the_one_writer_of_approval`
  and its window-snapshot twin scan the package and assert `{"app.py": 2}` and `{"app.py": 4}`.
  The dictionary is keyed by filename, so the pair cannot leave.
* `cancel_open_jobs` and `cancel_job` --
  `test_the_whole_queue_cancel_is_the_per_job_route_and_not_a_second_settle_path` takes
  `inspect.getsource(create_app)` and splits it on `async def cancel_open_jobs` and then on
  the next `\n    @app.`. `cancel_job` follows because `cancel_open_jobs` calls it by name.
* `assemble_project` and its `run_tool` helper --
  `test_assembly_route.py::test_the_export_reader_drains_stderr_concurrently_with_the_progress_stream`
  uses the same idiom on `run_tool`, and `test_frontend_contract.py` requires
  `offset=shot.latest_take_lead + shot.trim_nudge` to appear in `app.py`.
* `read_song_envelope` -- `test_frontend_contract.py` asserts the literal
  `@app.get("/api/projects/{project_id}/song/envelope")` is in `app.py`, decorator and all.
* `read_timeline_snap_targets` -- three assertions: a text slice from
  `def read_timeline_snap_targets(` to the next `\n    @app.`, an `ast` walk of `app.py` for a
  `FunctionDef` of that name, and a substring check on the path string.
* `expand_shot_prompt` -- `test_every_writer_of_an_expansion_records_the_map_it_was_written_against`
  parses `app.py` alone. Moving this route would not fail that test; it would quietly stop
  covering it, which is worse.
* `read_job` -- not a source test. It shares `/jobs/{job_id}` with `cancel_job`, and the first
  route whose path matches decides the `Allow` header on a 405. Registered with the others it
  would answer `Allow: GET` where this application has always answered `Allow: DELETE`.

**Held by tests that monkeypatch `music_video_producer.app`'s namespace.** A route resolves a
module-level name against the globals of the module it is *defined* in, so a route declared
here would read `routes/<file>.py`'s binding and never see the patch:

* `generate_h3` -- `build_h3_director_payload`, `build_h3_reference_payload`,
  `numbered_references`, `readiness_report`. One of those tests patches "every module that
  binds the name" and lists them; a move adds an eighth module to a list a test writes out.
* `generate_batch`, `render_again`, `mark_shot_ready`, `mark_shot_draft` and their
  `_set_shot_commitment` helper -- the batch route is a fan-out over `generate_h3` and calls
  it, and the other three, by name.
* `render_shot_preview` and its `take_measurement`, `export_geometry` and `preview_side`
  helpers -- `build_effect_stages`, `trim_args`, `probe_take_args`.
* `compile_timeline`, `read_readiness` -- `readiness_report`.
* `fill_section_looks` -- `plan_fingerprint`.
* `lay_out_timeline`, `line_up_timeline`, `fill_in_timeline`, `populate_timeline` --
  `lay_out_shots`, `line_up_shots`, `fill_in_shots`, `plan_fingerprint`, `window_fingerprint`.
* `clean_shot_prompts` -- `window_fingerprint`, `citation_fingerprint`, `plan_fingerprint`.

That last group is the reason `timeline.py` holds one route. Six of the seven `/timeline/*`
routes are in the list above, and the file is kept rather than folded away so that the shape
the split was asked for is visible and the debt has somewhere to be paid back into.

Every one of these becomes movable the moment its test is rewritten to reach the route by the
routing table or by the endpoint's own module rather than by `app.py`'s name -- which is a
change to a test, and therefore the Director's to approve, not a refactor's to make.
"""

from __future__ import annotations

from .. import app as _app  # noqa: F401
