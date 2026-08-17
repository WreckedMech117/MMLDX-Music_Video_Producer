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
  saveShots: (id, shots) => request(`/api/projects/${id}/shots`, { method: "PUT", headers: jsonHeaders, body: JSON.stringify({ shots }) }),
  uploadSong: (id, data) => request(`/api/projects/${id}/songs/upload`, { method: "POST", body: data }),
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
