const jsonHeaders = { "Content-Type": "application/json" };

export function comfyOutputUrl(baseUrl, outputPath) {
  const parts = outputPath.split("/");
  const filename = parts.pop();
  const params = new URLSearchParams({ filename, subfolder: parts.join("/"), type: "output" });
  return `${baseUrl.replace(/\/$/, "")}/view?${params}`;
}

// Pure preset → (endpoint, body) mapping for the Song workspace form. The invented
// SongPlanner variant reuses the creative-direction (caption) field as the idea and
// never sends lyrics; every other preset takes the direct Music 3 path.
export function musicGenerationPlan(data) {
  if (data.preset === "songplanner-invented") {
    return {
      endpoint: "songplanner",
      body: { title: data.title, idea: data.caption, duration: Number(data.duration), seed: Number(data.seed) },
    };
  }
  return {
    endpoint: "music",
    body: { title: data.title, caption: data.caption, lyrics: data.lyrics, duration: Number(data.duration), seed: Number(data.seed) },
  };
}

export async function request(path, options = {}) {
  const response = await fetch(path, options);
  let payload = null;
  const type = response.headers.get("content-type") || "";
  if (type.includes("application/json")) payload = await response.json();
  if (!response.ok) {
    const message = payload?.detail || payload?.message || `${response.status} ${response.statusText}`;
    throw new Error(message);
  }
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
