"""What a route module is allowed to reach, named once.

**This is the decision the split turns on.** Every route in this application used to be a
nested function inside `create_app`, closing over whatever that factory happened to have in
scope -- seven injected dependencies and a dozen helpers built from them, none of it written
down anywhere, none of it enumerable, and all of it in reach of every route whether the route
had any business with it or not. Moving routes into modules does not by itself change that; it
only decides where the same closure lives.

So the closure is replaced by a value. `create_app` fills this in once and hands the same
instance to all seven `register` calls, each of which unpacks the fields it needs into plain
locals before declaring its routes. Three properties follow, and they are why this shape was
chosen over the alternatives:

* **The set is enumerable.** What a route may reach is this class's field list and nothing
  else. A route that wants something new cannot quietly reach further up a scope chain; it has
  to add a field here, in front of whoever reads this file next. That is the whole difference
  between a contract and a closure.
* **Route bodies did not change.** Each field is unpacked into a local of the same name, so a
  route that said `store.save(project)` inside `create_app` still says exactly that -- which
  matters beyond taste, because `MANIFEST_WRITE_GUARDS` classifies routes by reading their
  source for that call. The diff for a moved route is the move.
* **Nothing happens per request.** These are plain closure variables, not FastAPI
  dependencies. `Depends` was the other candidate and is the more idiomatic shape, but it puts
  a resolution step in front of all eighty-one routes, rewrites all eighty-one signatures,
  and -- because this application builds a fresh `create_app()` per test, hundreds of times in
  one process, each with its own injected doubles -- would need per-application
  `dependency_overrides` to keep those doubles apart. That is a behaviour change wearing a
  refactor's clothes.

**Adding a field is the deliberate act.** ~~Eleven~~ **Sixteen** of the fields below are helpers
`create_app` builds once and shares; there is still exactly one of each, passed rather than
rebuilt, which is the single-implementation rule this repository enforces everywhere else. A
route that finds it needs a ~~twelfth~~ **seventeenth** should ask whether the helper belongs to one
resource -- in which case it belongs in that module, not here. The eleventh,
`song_envelope_report`, is the test of that question: it was added when the envelope route and
the timeline's snap-targets read both left the factory, and it is here rather than in either
module because *both* of them read it and neither owns it.

*Five added 2026-08-29 by story 11.5*, and they pass the same test: `take_measurement`,
`preview_assembly`, `preview_envelope`, `preview_into_cache` and `preview_side`
are read by the Shot preview, which
`app.py` holds, and by the boundary preview, which `routes/shots.py` holds. Neither route owns
them, and the alternative -- a second copy of AD-29's "the export's own geometry" rule in the
second module -- is precisely the drift this file exists to make visible.

Frozen, because none of it is state. The mutable state this application keeps lives on
`app.state` -- the live-render progress map, the preview registry, the set of running exports
-- and is reached *through* the `app` field, which each module also declares its routes with.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI

from ..comfy import ProgressTracker
from ..config import Settings
from ..store import ProjectStore
from ..workflows import WorkflowCatalog


@dataclass(frozen=True)
class RouterContext:
    """Everything `create_app` builds that a route declared outside it still needs."""

    #: The application, for two things and no others: the decorator each route is declared
    #: with, and `app.state` -- the live-render progress map, the preview registry, the set of
    #: running exports, the discovered looks and the eject control's source.
    app: FastAPI
    #: The seven injected dependencies, in the order `create_app` takes them. The five typed
    #: `Any` are injected as doubles by every suite in this repository and must stay that way.
    settings: Settings
    store: ProjectStore
    comfy: Any
    director: Any
    ejector: Any
    preferences: Any
    transcriber: Any
    #: The workflow catalogue, read by the workflows route.
    catalog: WorkflowCatalog
    #: Live render percentages, filled by the ComfyUI progress socket, read by the poll.
    render_progress: ProgressTracker
    #: Whether `MVP_LLM_EJECT_BEFORE_RENDER` was explicitly configured, which is what tells the
    #: eject control the difference between "configured to True" and "defaulted to True".
    eject_pinned_by_environment: bool
    #: The shared helpers, each a closure over `store`, `settings` or both.
    get_project: Callable[..., Any]
    get_project_for_update: Callable[..., Any]
    settle_unsubmitted_jobs: Callable[..., Any]
    record_submission: Callable[..., Any]
    resolve_asset_path: Callable[..., Any]
    resolve_song_path: Callable[..., Any]
    analyze_song_for_project: Callable[..., Any]
    analyze_a_landed_song: Callable[..., Any]
    #: The song measurement's read-time report, shared by the envelope route and the
    #: timeline's snap-targets read -- two resources, one computation, which is why it is
    #: here and not private to either module.
    song_envelope_report: Callable[..., Any]
    discovered_looks: Callable[..., Any]
    run_tool: Callable[..., Any]
    #: The five preview helpers, shared by the Shot preview -- pinned in `app.py` by the tests
    #: that monkeypatch its neighbours -- and the boundary preview, which is a new route and
    #: therefore lives in `routes/shots.py`. They are here for `song_envelope_report`'s reason
    #: and it is the same reason: **two resources read them and neither owns them**. A preview
    #: geometry re-derived in the second module would be AD-29 stated twice, and the second copy
    #: is the one that stops following the export.
    take_measurement: Callable[..., Any]
    preview_assembly: Callable[..., Any]
    preview_envelope: Callable[..., Any]
    preview_into_cache: Callable[..., Any]
    preview_side: Callable[..., Any]
