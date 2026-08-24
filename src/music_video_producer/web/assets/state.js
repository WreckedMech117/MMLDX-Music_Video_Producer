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
  // The Song Envelope this browser has read, or `null`. Session-scoped and never persisted: it
  // is a measurement the server holds in a sidecar, fetched once per project and song file on
  // the load path, and a copy kept in `localStorage` would be a second truth about a file that
  // can be replaced between two page loads.
  //
  // `null` until a read comes back `present: true`, which is the same nothing an absent, stale,
  // unreadable or never-taken measurement leaves behind -- naming *which* absence it was is the
  // endpoint's job, and nothing on this side branches on it or even counts them. Deliberately NOT
  // on `state.project`: the project object is what `PUT /api/projects/{id}` sends back whole, and
  // over a megabyte of arrays folded into it would be written straight into the manifest by the
  // next ordinary save. It sits beside `audioBuffer`, which is out here for the same reason.
  songEnvelope: null,
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
};

export function selectedAsset() {
  return state.project?.assets.find((asset) => asset.id === state.selectedAssetId) || null;
}

export function selectedShot() {
  return state.project?.shots.find((shot) => shot.id === state.selectedShotId) || null;
}
