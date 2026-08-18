export const state = {
  health: null,
  projects: [],
  project: null,
  activePanel: "song",
  selectedAssetId: null,
  selectedShotId: null,
  assetFilter: "all",
  waveform: null,
  audioBuffer: null,
  // The candidate import's own measurement, tied to the File it came from. Kept apart
  // from audioBuffer, which loadPersistedWaveform also writes for the stored song.
  pendingImport: null,
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
};

export function selectedAsset() {
  return state.project?.assets.find((asset) => asset.id === state.selectedAssetId) || null;
}

export function selectedShot() {
  return state.project?.shots.find((shot) => shot.id === state.selectedShotId) || null;
}
