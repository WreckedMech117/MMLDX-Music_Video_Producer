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
  pixelsPerSecond: 16,
  playhead: 0,
  dirty: false,
  documentsDirty: false,
  shotsDirty: false,
};

export function selectedAsset() {
  return state.project?.assets.find((asset) => asset.id === state.selectedAssetId) || null;
}

export function selectedShot() {
  return state.project?.shots.find((shot) => shot.id === state.selectedShotId) || null;
}
