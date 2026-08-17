const jsonHeaders = { "Content-Type": "application/json" };

export function comfyOutputUrl(baseUrl, outputPath) {
  const parts = outputPath.split("/");
  const filename = parts.pop();
  const params = new URLSearchParams({ filename, subfolder: parts.join("/"), type: "output" });
  return `${baseUrl.replace(/\/$/, "")}/view?${params}`;
}

// The presets the Song workspace offers, and which generation route each takes.
// Nothing may fall through to a default: an unrecognized preset means the markup
// and this module have drifted, and guessing "direct Music 3" would silently hand
// a future SongPlanner variant the wrong bounds and the wrong endpoint.
const SONGPLANNER_PRESETS = new Set(["songplanner-invented", "songplanner-known"]);
const MUSIC_PRESETS = new Set(["balanced", ...SONGPLANNER_PRESETS]);

function assertKnownPreset(preset) {
  if (!MUSIC_PRESETS.has(preset)) {
    throw new Error(`Unknown song preset: ${JSON.stringify(preset)}`);
  }
}

// Pure preset → (endpoint, body) mapping for the Song workspace form. Both
// SongPlanner variants reuse the creative-direction (caption) field as the idea;
// invented never sends lyrics, known sends the Director's lyric sheet with only
// its leading/trailing whitespace trimmed (matching the server's constraint) —
// every interior line, blank line and indent is preserved. Only `balanced` takes
// the direct Music 3 path.
export function musicGenerationPlan(data) {
  assertKnownPreset(data.preset);
  if (data.preset === "songplanner-invented") {
    return {
      endpoint: "songplanner",
      body: { title: data.title, idea: data.caption, duration: Number(data.duration), seed: Number(data.seed) },
    };
  }
  if (data.preset === "songplanner-known") {
    return {
      endpoint: "songplanner",
      body: { title: data.title, idea: data.caption, lyrics: (data.lyrics || "").trim(), duration: Number(data.duration), seed: Number(data.seed) },
    };
  }
  return {
    endpoint: "music",
    body: { title: data.title, caption: data.caption, lyrics: data.lyrics, duration: Number(data.duration), seed: Number(data.seed) },
  };
}

// Pure preset → lyrics/duration/seed field state for the Song workspace form, kept
// here beside musicGenerationPlan so the routing and the form shape cannot drift
// apart. Both SongPlanner variants carry the M3SongPlanner node's real bounds —
// 30–300 s duration and a 32-bit seed — while direct Music 3 keeps its own 4–360 s
// duration and the 64-bit seed its encoder and sampler actually accept. Every bound
// here equals its route model's, asserted by tests/test_frontend_contract.py.
//
// Seed bounds are strings: 18446744073709551615 is not representable as a JS
// number (it rounds to …552000), and an inexact ceiling would refuse or admit
// seeds the route does not. The HTML `max` attribute is a string anyway.
export function musicPresetFieldState(preset) {
  assertKnownPreset(preset);
  const songplanner = SONGPLANNER_PRESETS.has(preset);
  return {
    lyricsVisible: preset !== "songplanner-invented",
    lyricsRequired: preset === "songplanner-known",
    durationMin: songplanner ? 30 : 4,
    durationMax: songplanner ? 300 : 360,
    seedMin: 0,
    seedMax: songplanner ? "4294967295" : "18446744073709551615",
  };
}

// Clamp a raw form value into [minimum, maximum], leaving anything that is not a
// finite number exactly as the Director typed it. `Number("")` is 0, so a cleared
// box must stay cleared rather than silently acquiring the minimum, and NaN must
// not slip through unclamped just because it compares false against both bounds —
// the browser's own validation reports those, and inventing a value would submit
// a number nobody asked for.
export function clampToBounds(raw, minimum, maximum) {
  if (raw === "" || raw === null || raw === undefined) return "";
  const value = Number(raw);
  if (!Number.isFinite(value)) return raw;
  if (minimum !== null && minimum !== undefined && value < Number(minimum)) return minimum;
  if (maximum !== null && maximum !== undefined && value > Number(maximum)) return maximum;
  return raw;
}

// Pure preset + current values → exactly what the Song workspace form should show:
// the lyrics field's visibility/requiredness and each numeric field's bounds and
// (possibly clamped) value. syncMusicVariant is a thin DOM applier over this, so
// the clamp direction and the bound assignment are testable without a browser.
export function musicFormFieldUpdate(preset, current = {}) {
  const fields = musicPresetFieldState(preset);
  return {
    lyricsVisible: fields.lyricsVisible,
    lyricsRequired: fields.lyricsRequired,
    numeric: {
      duration: {
        min: fields.durationMin,
        max: fields.durationMax,
        value: clampToBounds(current.duration, fields.durationMin, fields.durationMax),
      },
      seed: {
        min: fields.seedMin,
        max: fields.seedMax,
        value: clampToBounds(current.seed, fields.seedMin, fields.seedMax),
      },
    },
  };
}

// Pure decision: the `duration` value a pending song import sends to the server.
//
// `pending.decoded` is the AudioBuffer the browser produced for *the file being
// imported*, absent whenever the decode failed. Any other state handed in — most
// importantly a `previous` buffer the app still holds from an earlier song — is
// deliberately ignored, which is the entire point of this function existing.
//
// The server only runs its ffprobe fallback when this value is 0 (app.py:
// `resolved_duration = duration if duration > 0 else _media_duration(target)`), so
// "unknown length" has to arrive as exactly 0. A stale non-zero number instead
// becomes the persisted Song duration — the timing spine every Shot window,
// playback sync and Assembly derives from — and a wrong spine is worse than a
// missing one. Non-finite and non-positive measurements are unknown too.
export function songImportDuration(pending = {}) {
  const duration = Number(pending?.decoded?.duration);
  return Number.isFinite(duration) && duration > 0 ? duration : 0;
}

// True when a rejection is the Song gate refusing an unacknowledged change, which the
// client can recover from by refreshing and asking again -- as opposed to any other
// error, where a refresh would tell the Director nothing new. Keyed on the server's
// own instruction sentence so the two cannot drift apart silently.
export const SONG_REFUSAL_MARKER = "confirm_song_replacement=true";

export function songRefusalMessage(message) {
  return typeof message === "string" && message.includes(SONG_REFUSAL_MARKER);
}

// The single wording for what changing or removing a project's Song costs, shown before
// the import, generate and remove paths send anything. One exported constant because
// three call sites would otherwise drift, and a consequence stated differently in three
// places is a consequence the Director cannot trust.
//
// It names both things that silently stop lining up: Shot windows are absolute seconds
// against the current song, and Assembly synchronization derives from it. It also says
// what is *not* at risk — no shot data is deleted and no window is moved — because a
// Director who fears losing work will avoid the operation instead of understanding it.
// The server states the same consequence in app.py's SONG_REPLACEMENT_CONSEQUENCE.
export const SONG_CHANGE_CONSEQUENCE =
  "Shot windows are absolute seconds against the current song, and Assembly synchronization derives from it. " +
  "No shot data is deleted and no shot window is moved to fit a new song, so every existing shot keeps the timing it has now.";

// Pure mirror of the server's gate (app.py `_require_song_replacement_confirmation`): a
// Song change only needs acknowledgement once the project has both a Song and Shots. A
// first import, and a Shot-less project, stay frictionless because nothing depends on
// the song's timing yet.
export function songChangeNeedsConfirmation(project) {
  return Boolean(project?.song) && Boolean(project?.shots?.length);
}

// The two creative documents a Director reply can replace, and what each is called on
// screen. The server states the same mapping in app.py's DOCUMENT_LABELS; the keys are the
// path segment the restore route accepts, so a rename here 404s rather than mislabelling.
export const DOCUMENT_LABELS = { treatment: "Treatment", style_bible: "Style bible" };

// Every per-document control, in one table: the two element ids the Treatment workspace
// exposes, the project fields they read, and the document tab each pair belongs to. One
// mapping because the alternative — a document's selectors, fields and prose name spelled
// out again at the seed, render, bind and label sites — is exactly how a rename half-lands
// and leaves a control wired to the other document. The field names are the ones the
// restore route derives with `getattr(project, f"{document}_previous")`, so they cannot be
// spelled differently here without the two halves disagreeing about the same slot.
export const DOCUMENT_CONTROLS = {
  treatment: {
    tab: "treatment",
    lock: "#lock-treatment",
    restore: "#restore-treatment",
    lockedField: "treatment_locked",
    previousField: "treatment_previous",
  },
  style_bible: {
    tab: "style",
    lock: "#lock-style",
    restore: "#restore-style",
    lockedField: "style_bible_locked",
    previousField: "style_bible_previous",
  },
};

// One lookup for both tables, throwing rather than returning undefined: a document the
// server has no field for must fail loudly here instead of rendering "undefined" into a
// toast or silently binding a control to nothing.
export function documentControls(document) {
  const control = DOCUMENT_CONTROLS[document];
  if (!control) throw new Error(`Unknown document: ${JSON.stringify(document)}`);
  return control;
}

export function documentLabel(document) {
  const label = DOCUMENT_LABELS[document];
  if (!label) throw new Error(`Unknown document: ${JSON.stringify(document)}`);
  return label;
}

// Pure: is anything actually recoverable for this document? The restore button's enabled
// state is the answer to that question and nothing else — an always-enabled button offers a
// restore the server refuses with 409, and the client then misreads its own bad offer as
// stale state and "refreshes" a project that was never stale.
export function documentRestoreAvailable(project, document) {
  const previous = project?.[documentControls(document).previousField];
  return typeof previous === "string" && previous.trim().length > 0;
}

// What the restore button says it will do, in the two states it has. Named from
// DOCUMENT_LABELS so the tooltip, the toast and the markup's label are one spelling.
export function documentRestoreTitle(document, available) {
  const label = documentLabel(document);
  return available
    ? `Swap ${label} back to the version kept before the last applied replacement; no Director call is made`
    : `No previous version of ${label} is kept yet; one is kept when a Director reply replaces it`;
}

// Toggling a lock is a change to how the *next* Director reply behaves, so the toast has to
// confirm that change rather than report a generic save — "Project saved" tells the Director
// nothing about whether the document is now protected.
export const DOCUMENT_LOCK_SET_NOTICE =
  "{document} is locked: a Director reply will not replace it, and no previous version is recorded for it.";
export const DOCUMENT_LOCK_CLEARED_NOTICE =
  "{document} is unlocked: a Director reply may replace it, keeping the previous version for restore.";

export function documentLockNotice(document, locked) {
  const wording = locked ? DOCUMENT_LOCK_SET_NOTICE : DOCUMENT_LOCK_CLEARED_NOTICE;
  return wording.replace("{document}", documentLabel(document));
}

// What to say after a refused restore has been recovered from by refreshing. The refreshed
// project decides which sentence is true: the refusal only means *this client* was stale, so
// claiming no kept version exists would contradict the very state just fetched — and would
// tell the Director to stop trying when one more click would work.
export function documentRestoreStaleNotice(document, available) {
  const label = documentLabel(document);
  return available
    ? `${label} does have a kept version on the server; this project has been refreshed, so the restore can be tried again.`
    : `No kept version of ${label} exists on the server; this project has been refreshed.`;
}

// What a Director reply actually did to the documents, computed from the project before and
// after the call. The reply itself states which documents changed, were locked, or were
// rejected; the toast is the most prominent feedback there is, so it must not assert an
// update that may not have happened — a locked document, a rejected candidate and an
// identical rewrite all leave the text exactly as it was.
export const DOCUMENT_CHANGE_TOAST =
  "{documents} replaced by this reply; existing shots preserved and the previous version kept.";
export const DOCUMENT_UNCHANGED_TOAST =
  "The Director replied and no document changed; the reply says what it proposed and why it was not applied.";

export function documentChangeToast(before, after) {
  const changed = Object.keys(DOCUMENT_LABELS).filter(
    (document) => (before?.[document] ?? "") !== (after?.[document] ?? ""),
  );
  if (!changed.length) return DOCUMENT_UNCHANGED_TOAST;
  return DOCUMENT_CHANGE_TOAST.replace("{documents}", changed.map(documentLabel).join(" and "));
}

// The consequence of letting the server's text overwrite the editors, stated before any path
// does it. Recovery captures the *stored* text, so unsaved on-screen edits are the one thing
// this feature cannot bring back — discarding them silently would reintroduce the exact loss
// mode it exists to eliminate.
export const UNSAVED_DOCUMENT_EDITS_CONSEQUENCE =
  "The document editors have unsaved edits. Continuing replaces their text with the version stored on the " +
  "server, and only stored text is ever kept as a recoverable version, so unsaved edits cannot be restored " +
  "afterwards. Cancel and save the document first to keep them.";

// The one wording for a restore, mirroring app.py's DOCUMENT_RESTORE_NOTICE so the toast
// the Director reads is the same sentence the thread records. It says the swap is
// symmetric on purpose: single-slot recovery nobody dares use is not recovery.
export const DOCUMENT_RESTORE_NOTICE =
  "{document} was restored to the version kept before the last applied replacement. " +
  "No Director call was made. The text that was replaced is now the kept version, so " +
  "restoring again swaps back.";

export function documentRestoreNotice(document) {
  return DOCUMENT_RESTORE_NOTICE.replace("{document}", documentLabel(document));
}

// True when a rejection is the restore route refusing because no version was ever kept.
// The controls are disabled when the loaded project has an empty slot, so a refusal means
// this client is looking at stale state -- the same recovery shape as SONG_REFUSAL_MARKER.
// Keyed on a phrase from the server's own refusal so the two cannot drift apart silently.
export const DOCUMENT_RESTORE_REFUSAL_MARKER = "nothing to restore";

export function documentRestoreRefusal(message) {
  return typeof message === "string" && message.includes(DOCUMENT_RESTORE_REFUSAL_MARKER);
}

// FastAPI reports handler failures as a plain `detail` string but validation
// failures (422) as a list of {loc, msg, type} objects, which would otherwise
// reach the Director as "[object Object]". Render both into readable text.
export function errorMessage(payload, response) {
  const detail = payload?.detail ?? payload?.message;
  if (detail === null || detail === undefined) return `${response.status} ${response.statusText}`;
  const readable = (item) => {
    if (typeof item === "string") return item;
    if (!item || typeof item !== "object") return String(item);
    const field = Array.isArray(item.loc) ? item.loc.filter((part) => part !== "body").join(".") : "";
    const message = item.msg || item.message || item.type || JSON.stringify(item);
    return field ? `${field}: ${message}` : message;
  };
  const rendered = Array.isArray(detail) ? detail.map(readable).join("; ") : readable(detail);
  return rendered || `${response.status} ${response.statusText}`;
}

export async function request(path, options = {}) {
  const response = await fetch(path, options);
  let payload = null;
  const type = response.headers.get("content-type") || "";
  if (type.includes("application/json")) payload = await response.json();
  if (!response.ok) throw new Error(errorMessage(payload, response));
  return payload;
}

export const api = {
  health: () => request("/api/health"),
  projects: () => request("/api/projects"),
  project: (id) => request(`/api/projects/${id}`),
  createProject: (name) => request("/api/projects", { method: "POST", headers: jsonHeaders, body: JSON.stringify({ name }) }),
  saveProject: (project) => request(`/api/projects/${project.id}`, { method: "PUT", headers: jsonHeaders, body: JSON.stringify(project) }),
  saveDocuments: (id, documents) => request(`/api/projects/${id}/documents`, { method: "PUT", headers: jsonHeaders, body: JSON.stringify(documents) }),
  // Recovery must not depend on the model that caused the problem, so this is its own
  // route and carries no message: nothing here reaches the Director.
  restoreDocument: (id, document) => request(`/api/projects/${id}/documents/${document}/restore`, { method: "POST" }),
  saveShots: (id, shots) => request(`/api/projects/${id}/shots`, { method: "PUT", headers: jsonHeaders, body: JSON.stringify({ shots }) }),
  uploadSong: (id, data) => request(`/api/projects/${id}/songs/upload`, { method: "POST", body: data }),
  // The flag is the Director's acknowledgement, so it is passed through rather than
  // hardcoded: a caller that never showed SONG_CHANGE_CONSEQUENCE must not claim it did.
  removeSong: (id, confirmed = false) => request(`/api/projects/${id}/song?confirm_song_replacement=${confirmed ? "true" : "false"}`, { method: "DELETE" }),
  uploadAsset: (id, data) => request(`/api/projects/${id}/assets/upload`, { method: "POST", body: data }),
  generateMusic: (id, body) => request(`/api/projects/${id}/generate/music`, { method: "POST", headers: jsonHeaders, body: JSON.stringify(body) }),
  generateSongPlanner: (id, body) => request(`/api/projects/${id}/generate/songplanner`, { method: "POST", headers: jsonHeaders, body: JSON.stringify(body) }),
  generateFlux: (id, body) => request(`/api/projects/${id}/generate/flux`, { method: "POST", headers: jsonHeaders, body: JSON.stringify(body) }),
  generateMultiview: (projectId, assetId, body) => request(`/api/projects/${projectId}/assets/${assetId}/multiview`, { method: "POST", headers: jsonHeaders, body: JSON.stringify(body) }),
  analyzeAsset: (projectId, assetId) => request(`/api/projects/${projectId}/assets/${assetId}/analyze`, { method: "POST" }),
  analyzeLatestTake: (projectId, shotId) => request(`/api/projects/${projectId}/shots/${shotId}/analyze-latest`, { method: "POST" }),
  compileTimeline: (id, body) => request(`/api/projects/${id}/timeline/compile`, { method: "POST", headers: jsonHeaders, body: JSON.stringify(body) }),
  generateH3: (projectId, shotId, body = {}) => request(`/api/projects/${projectId}/shots/${shotId}/generate/h3`, { method: "POST", headers: jsonHeaders, body: JSON.stringify(body) }),
  directorChat: (id, body) => request(`/api/projects/${id}/director/chat`, { method: "POST", headers: jsonHeaders, body: JSON.stringify(body) }),
  job: (projectId, jobId) => request(`/api/projects/${projectId}/jobs/${jobId}`),
  workflows: () => request("/api/workflows"),
};
