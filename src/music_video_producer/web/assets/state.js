export const state = {
  health: null,
  projects: [],
  project: null,
  activePanel: "song",
  selectedAssetId: null,
  selectedShotId: null,
  // Which subtab of the Assets panel owns the library area: an `ASSET_TABS` id. Session state,
  // never persisted and never sent -- it is where a Director is looking, not a fact about the
  // project. Was `assetFilter` while the strip only filtered the one grid; it now also decides
  // whether the clips library or the asset grid is on screen at all.
  assetTab: "all",
  waveform: null,
  audioBuffer: null,
  // The candidate import's own measurement, tied to the File it came from. Kept apart
  // from audioBuffer, which loadPersistedWaveform also writes for the stored song.
  pendingImport: null,
  // **One slot for one measurement**, as `GET /timeline/snap-targets` serves it: the whole reply,
  // carrying both halves. `gaps` and `beats` are the seconds a dragged shot edge may land on, as
  // `timeline.py` itself computed them; `measured` and `analysed` say which halves the song has;
  // and `envelope` is the marks the waveform draws, or `null`.
  //
  // **This was two slots, and that was the defect.** `songEnvelope` and `snapTargets` were filled
  // by two reads of two routes with two keys and two silent catches, and nothing held them
  // together: a measurement replaced under an unchanged manifest record moved neither, and a
  // refusal of one while the other landed left the band empty while the drag went on snapping to
  // beats that were no longer on screen. Both were executed in `epic-8-retro-2026-08-24.md` (S4,
  // S5). One slot, filled by one reply, cannot reach either state -- and the band and the drag
  // read the same object, so they cannot describe different states.
  //
  // `null` until a read comes back, which is also what a project with no song and an unreachable
  // route leave behind: no marks and no targets, which is the timeline this application drew
  // before Epic 8. Nothing here branches on *why* a measurement is absent; the rows in the "Snap
  // to" panel say that, from `measured` and `analysed`.
  //
  // Session-scoped and **never persisted**: the route hashes the master to decide whether the
  // measurement still describes it, so a copy in `localStorage` would be a second truth about a
  // file that can be replaced between two page loads. Deliberately **not** on `state.project`
  // either, beside `audioBuffer` and for its reason: the project object is what
  // `PUT /api/projects/{id}` sends back whole, and anything folded into it is written straight
  // into the manifest by the next ordinary save.
  songMeasurement: null,
  pixelsPerSecond: 16,
  playhead: 0,
  dirty: false,
  documentsDirty: false,
  shotsDirty: false,
  // The Song context editors hold typing the server has not seen, saved by their own button
  // through their own route rather than by the project save -- which is why this is a second flag
  // and not `dirty`. It answers a question no other flag answers: whether an incidental renderSong,
  // such as the one the audio element's `loadedmetadata` fires, may re-seed the editors from the
  // stored Song. Folded into `dirty` it would also be cleared by `saveProject`, and the next render
  // would wipe a lyric sheet mid-paste.
  //
  // Both navigation guards read it all the same, through `unsavedWorkPending`: the project-switch
  // question and the `beforeunload` guard ask about this text as well as about `dirty`. It is
  // cleared by a landed save, by an import or removal that makes the stored Song the truth again,
  // and by a project load that actually changes project -- never by a refresh of the project
  // already on screen, which is a queue refresh, not a decision to discard anything.
  songContextDirty: false,
  // The server's answer about the VRAM eject: whether it is on, where that value came from, and
  // what the last attempt did. `null` until the first successful GET, which is what the control
  // renders as "unknown" rather than guessing a default.
  //
  // Not project data and never sent back inside one: this describes the machine's single card,
  // shared by LM Studio and ComfyUI, and a project manifest carrying it would change how someone
  // else's renders behave. It lives beside `health` for that reason, not beside `project`.
  vramEject: null,
  // Live render percentages, `{ targetId: percent }`, rebuilt from every render-status answer by
  // `renderProgressByTarget`. An empty object is the honest starting state: nothing is known
  // until ComfyUI says something, and an absent key is what makes the asset card and the clip
  // draw exactly what they drew before this existed.
  //
  // Deliberately NOT on `state.project`. The project object is what `PUT /api/projects/{id}`
  // sends back whole, so a percentage folded into `project.jobs[].progress` would be saved into
  // the manifest by the next ordinary project write -- the generic full-project PUT is this
  // codebase's repeat offender for exactly that. Kept beside `vramEject`, which is out here for
  // the same reason: it is machine state, not project data.
  renderProgress: {},
  // Which shot ComfyUI is rendering *now* and which are waiting behind it, `{ shotId: phase }`,
  // rebuilt from every render-status answer by `renderPhaseByShot`. Out here for
  // `renderProgress`' reasons exactly -- it is derived from an answer rather than stored on the
  // manifest, and a phase folded into `project.jobs[].status` would be written back by the next
  // ordinary project save.
  //
  // It exists because nothing in this application ever writes `running` onto a Shot: the
  // submission route writes `queued` and the reconciler writes `running` onto the *job*, so
  // `Shot.status` alone cannot tell a clip on the GPU from twenty-five queued behind it. An
  // empty object is the honest starting state, and an absent key makes the clip draw exactly
  // what it drew before this existed.
  renderPhase: {},
  // The Brief's attribution as the last render drew it: the server's ranges, clamped into the
  // text that was on screen, in the order the mirror's marks and their rules were built in. The
  // caret label reads this rather than re-deriving anything, so "which mark is the caret in" and
  // "which stretch is washed" cannot answer differently.
  //
  // Derived, never sent, and deliberately **not** on `state.project`: the project object is what
  // `PUT /api/projects/{id}` sends back whole, and `brief_attribution` is server-owned (AD-45) --
  // a clamped copy folded into it would be a client writing provenance, which is the one thing
  // that route re-adopts the stored value to prevent. Empty is the honest starting state: a
  // Brief nobody has rendered yet has no marks to be inside.
  briefMarks: [],
};

export function selectedAsset() {
  return state.project?.assets.find((asset) => asset.id === state.selectedAssetId) || null;
}

export function selectedShot() {
  return state.project?.shots.find((shot) => shot.id === state.selectedShotId) || null;
}
