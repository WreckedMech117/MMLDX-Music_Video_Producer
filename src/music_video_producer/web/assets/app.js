import { api, comfyOutputUrl } from "./api.js";
import { selectedAsset, selectedShot, state } from "./state.js";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
let shotSaveChain = Promise.resolve();
let shotSaveRevision = 0;
let waveformLoadRevision = 0;

function toast(message, kind = "info") {
  const item = document.createElement("div");
  item.className = `toast ${kind}`;
  item.textContent = message;
  $("#toast-region").append(item);
  setTimeout(() => item.remove(), 4200);
}

function formatTime(seconds = 0, frames = false) {
  const safe = Math.max(0, Number(seconds) || 0);
  const mins = Math.floor(safe / 60);
  const secs = Math.floor(safe % 60);
  if (frames) return `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}:${String(Math.floor((safe % 1) * 24)).padStart(2, "0")}`;
  const millis = Math.floor((safe % 1) * 1000);
  return `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}.${String(millis).padStart(3, "0")}`;
}

function requireProject() {
  if (!state.project) {
    toast("Create or select a project first.", "error");
    return false;
  }
  return true;
}

async function loadHealth() {
  try {
    state.health = await api.health();
    const online = state.health.comfy.online;
    $("#comfy-dot").className = `status-dot ${online ? "online" : "offline"}`;
    $("#comfy-label").textContent = online ? "ComfyUI ready" : "ComfyUI offline";
    $("#llm-state").textContent = state.health.llm.configured ? `LLM · ${state.health.llm.model}` : "LLM not configured";
  } catch (error) {
    $("#comfy-dot").className = "status-dot offline";
    $("#comfy-label").textContent = "App service error";
  }
}

async function loadProjects(selectId = null) {
  state.projects = await api.projects();
  const select = $("#project-select");
  select.innerHTML = `<option value="">No project</option>${state.projects.map((project) => `<option value="${project.id}">${escapeHtml(project.name)}</option>`).join("")}`;
  const next = selectId || state.project?.id || state.projects[0]?.id;
  if (next) {
    select.value = next;
    await loadProject(next);
  } else {
    renderAll();
    $("#project-dialog").showModal();
  }
}

async function loadProject(id) {
  if (!id) {
    state.project = null;
    state.audioBuffer = null;
    renderAll();
    return;
  }
  state.project = await api.project(id);
  state.audioBuffer = null;
  state.selectedAssetId = null;
  state.selectedShotId = state.project.shots[0]?.id || null;
  state.dirty = false;
  state.documentsDirty = false;
  state.shotsDirty = false;
  renderAll();
  loadPersistedWaveform(id);
}

function renderAll() {
  renderSong();
  renderTreatment();
  renderAssets();
  renderTimeline();
  renderJobs();
}

function renderSong() {
  const song = state.project?.song;
  const audio = $("#master-audio");
  const source = songAudioUrl(song);
  if (audio.dataset.source !== source) {
    audio.pause();
    audio.dataset.source = source;
    if (source) audio.src = source;
    else audio.removeAttribute("src");
    audio.load();
  }
  const playable = Boolean(source);
  $("#global-play").disabled = !playable;
  $("#jump-start").disabled = !playable;
  $("#timeline-play").disabled = !playable;
  $("#timeline-start").disabled = !playable;
  $("#song-meta").textContent = song ? `${song.title} · ${formatTime(song.duration)}` : "No song loaded";
  $("#song-title").textContent = song?.title || "Load or generate a song";
  $("#song-source").textContent = song?.source?.toUpperCase() || "EMPTY";
  $("#duration-value").textContent = song?.duration ? formatTime(song.duration) : "—";
  $("#timeline-value").textContent = song?.duration ? `${state.project?.shots.length || 0} shots · ready` : "Waiting for song";
  $("#analyze-song").disabled = true;
  $("#send-treatment").disabled = !song;
  $("#waveform-empty").style.display = state.audioBuffer ? "none" : "grid";
  const duration = song?.duration || 0;
  $("#quarter-time").textContent = duration ? formatTime(duration * .25).slice(0, 5) : "—";
  $("#half-time").textContent = duration ? formatTime(duration * .5).slice(0, 5) : "—";
  $("#three-quarter-time").textContent = duration ? formatTime(duration * .75).slice(0, 5) : "—";
  $("#end-time").textContent = duration ? formatTime(duration).slice(0, 5) : "—";
  if (state.audioBuffer) drawWaveform($("#waveform"), state.audioBuffer, "#d4f75e");
}

function songAudioUrl(song = state.project?.song) {
  if (!song?.path || !state.project) return "";
  if (song.source === "imported") {
    const relative = song.path.replace(/^media\//, "");
    return `/api/projects/${state.project.id}/media/${encodeURI(relative)}`;
  }
  const comfyUrl = state.health?.comfy?.url || "http://127.0.0.1:8188";
  return comfyOutputUrl(comfyUrl, song.path);
}

async function loadPersistedWaveform(projectId) {
  const url = songAudioUrl();
  const revision = ++waveformLoadRevision;
  if (!url) return;
  try {
    const response = await fetch(url);
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    const context = new AudioContext();
    const buffer = await context.decodeAudioData(await response.arrayBuffer());
    await context.close();
    if (revision !== waveformLoadRevision || state.project?.id !== projectId) return;
    state.audioBuffer = buffer;
    renderSong();
    renderTimeline();
  } catch {
    // Playback uses the media element even when Web Audio cannot decode the source.
  }
}

async function toggleMasterAudio() {
  const audio = $("#master-audio");
  if (!audio.src || !state.project?.song?.path) return;
  try {
    if (audio.paused) await audio.play();
    else audio.pause();
  } catch (error) {
    toast(`Could not play the master audio: ${error.message}`, "error");
  }
}

function seekMasterAudio(seconds) {
  const audio = $("#master-audio");
  const duration = Number.isFinite(audio.duration) ? audio.duration : projectDuration();
  state.playhead = clamp(seconds, 0, duration || 0);
  if (audio.src) audio.currentTime = state.playhead;
  updateTimelinePlayhead();
}

function syncTransportState() {
  const audio = $("#master-audio");
  const icon = audio.paused ? "▶" : "❚❚";
  $("#global-play").textContent = icon;
  $("#timeline-play").textContent = icon;
}

function renderTreatment() {
  const project = state.project;
  $("#creative-brief").value = project?.creative_brief || "";
  $("#treatment-text").value = project?.treatment || "";
  $("#style-bible").value = project?.style_bible || "";
  const thread = $("#chat-thread");
  if (!project?.messages?.length) {
    thread.innerHTML = `<div class="empty-thread"><strong>Direct the video naturally</strong><p>Describe narrative, energy, references, camera language, what to avoid, and where the performance should feel literal or abstract.</p></div>`;
    return;
  }
  thread.innerHTML = project.messages.map((message) => `<div class="message ${message.role}">${escapeHtml(message.content)}</div>`).join("");
  thread.scrollTop = thread.scrollHeight;
}

function assetImageUrl(asset) {
  if (!asset?.path) return "";
  if (asset.source === "upload") return `/api/projects/${state.project.id}/media/${encodeURI(asset.path.replace(/^media\//, ""))}`;
  const comfyUrl = state.health?.comfy?.url || "http://127.0.0.1:8188";
  return comfyOutputUrl(comfyUrl, asset.path);
}

function renderAssets() {
  const assets = state.project?.assets || [];
  const query = $("#asset-search")?.value?.toLowerCase() || "";
  const filtered = assets.filter((asset) => (state.assetFilter === "all" || asset.kind === state.assetFilter) && (!query || asset.name.toLowerCase().includes(query)));
  const grid = $("#asset-grid");
  if (!filtered.length) {
    grid.innerHTML = `<div class="library-empty"><strong>No matching assets</strong><span>Generate a character or setting with Flux, or upload existing media.</span></div>`;
  } else {
    grid.innerHTML = filtered.map((asset) => {
      const url = assetImageUrl(asset);
      return `<button class="asset-card ${asset.id === state.selectedAssetId ? "selected" : ""}" data-asset-id="${asset.id}" draggable="true"><div class="asset-thumb">${url ? `<img src="${url}" alt="">` : asset.prompt_id ? "RENDERING" : "NO PREVIEW"}</div><footer><strong>${escapeHtml(asset.name)}</strong><span>${asset.kind} · ${asset.source}</span></footer></button>`;
    }).join("");
  }
  $$(".asset-card", grid).forEach((card) => {
    card.addEventListener("click", () => { state.selectedAssetId = card.dataset.assetId; renderAssets(); });
    card.addEventListener("dragstart", (event) => event.dataTransfer.setData("text/asset-id", card.dataset.assetId));
  });
  renderAssetInspector();
}

function renderAssetInspector() {
  const asset = selectedAsset();
  const inspector = $("#asset-inspector");
  if (!asset) {
    inspector.innerHTML = `<span class="eyebrow">Inspector</span><h2>Select an asset</h2><p>Review provenance, generate multiview references, and attach approved assets to shots.</p>`;
    return;
  }
  const url = assetImageUrl(asset);
  const vision = asset.vision ? `<div class="meta-list"><b>Vision summary</b><span>${escapeHtml(asset.vision.summary)}</span><b>Continuity</b><span>${escapeHtml(asset.vision.continuity_cues.join(" · ") || "—")}</span><b>Risks</b><span>${escapeHtml(asset.vision.risks.join(" · ") || "None")}</span></div>` : "";
  inspector.innerHTML = `<span class="eyebrow">${escapeHtml(asset.kind)}</span><h2>${escapeHtml(asset.name)}</h2><div class="asset-preview">${url ? `<img src="${url}" alt="${escapeHtml(asset.name)}">` : "Awaiting output"}</div><div class="meta-list"><b>Source</b><span>${escapeHtml(asset.source)}</span><b>Prompt ID</b><span>${escapeHtml(asset.prompt_id || "—")}</span><b>Created</b><span>${new Date(asset.created_at).toLocaleString()}</span></div>${vision}${asset.prompt ? `<label>Generation prompt<textarea rows="7" readonly>${escapeHtml(asset.prompt)}</textarea></label>` : ""}<button class="quiet-button full" id="analyze-asset" ${asset.path && !["audio"].includes(asset.kind) ? "" : "disabled"}>Inspect with vision model</button>${asset.kind === "character" ? `<button class="primary-button full" id="create-multiview" ${asset.path ? "" : "disabled"}>Create Krea multiview sheet</button>` : ""}<button class="quiet-button full" id="attach-asset" style="margin-top:8px" ${selectedShot() ? "" : "disabled"}>Attach to selected shot</button>`;
  $("#attach-asset")?.addEventListener("click", attachSelectedAsset);
  $("#create-multiview")?.addEventListener("click", createMultiview);
  $("#analyze-asset")?.addEventListener("click", async () => {
    try { state.project = await api.analyzeAsset(state.project.id, asset.id); renderAssets(); toast("Vision inspection saved"); }
    catch (error) { toast(error.message, "error"); }
  });
}

async function createMultiview() {
  const asset = selectedAsset();
  if (!asset || !state.project) return;
  const prompt = `Preserve the exact identity, facial features, body type and wardrobe of this character. Convert the character into a clean four-panel character sheet showing a face close-up, front full body, side full body and back full body view. Consistent neutral lighting and proportions across every view.`;
  try {
    await api.generateMultiview(state.project.id, asset.id, { prompt, seed: 0 });
    await loadProject(state.project.id);
    toast("Krea multiview job queued");
  } catch (error) { toast(error.message, "error"); }
}

function projectDuration() {
  const shotEnd = Math.max(0, ...(state.project?.shots || []).map((shot) => shot.start + shot.duration));
  return Math.max(state.project?.song?.duration || 0, shotEnd, 30);
}

function renderTimeline() {
  const duration = projectDuration();
  const trackWidth = Math.max(900, duration * state.pixelsPerSecond);
  const canvas = $("#timeline-canvas");
  canvas.style.width = `${trackWidth + 90}px`;
  $("#zoom-label").textContent = `${Math.round(state.pixelsPerSecond / 16 * 100)}%`;
  $("#timeline-duration").textContent = state.project?.song ? `${formatTime(state.project.song.duration)} master` : "No master song";
  renderRuler(duration, trackWidth);
  const track = $("#shots-track");
  track.innerHTML = (state.project?.shots || []).map((shot, index) => `<div class="shot-clip ${shot.id === state.selectedShotId ? "selected" : ""}" data-shot-id="${shot.id}" style="left:${shot.start * state.pixelsPerSecond}px;width:${Math.max(40, shot.duration * state.pixelsPerSecond)}px"><span class="resize-handle left"></span><span class="clip-id">SHOT ${String(index + 1).padStart(2, "0")} · ${shot.duration.toFixed(1)}s</span><span class="clip-prompt">${escapeHtml(shot.prompt || "Untitled shot")}</span><span class="resize-handle right"></span></div>`).join("");
  $$(".shot-clip", track).forEach(bindClip);
  renderReferences();
  renderShotInspector();
  if (state.audioBuffer) drawWaveform($("#timeline-waveform"), state.audioBuffer, "#6f7d3d");
  updateTimelinePlayhead();
}

function renderRuler(duration, width) {
  const ruler = $("#timeline-ruler");
  const step = state.pixelsPerSecond < 12 ? 10 : state.pixelsPerSecond > 30 ? 2 : 5;
  const labels = [];
  for (let time = 0; time <= duration; time += step) labels.push(`<span class="ruler-label" style="left:${90 + time * state.pixelsPerSecond}px">${formatTime(time).slice(0, 5)}</span>`);
  ruler.innerHTML = labels.join("");
  ruler.style.backgroundSize = `${state.pixelsPerSecond * step}px 100%`;
}

function renderReferences() {
  const assets = new Map((state.project?.assets || []).map((asset) => [asset.id, asset]));
  const refs = [];
  for (const shot of state.project?.shots || []) {
    shot.asset_ids.forEach((id, index) => {
      const asset = assets.get(id);
      if (asset) refs.push(`<span class="ref-pill" style="left:${shot.start * state.pixelsPerSecond + index * 12}px;width:${Math.max(55, shot.duration * state.pixelsPerSecond - index * 12)}px">${escapeHtml(asset.name)}</span>`);
    });
  }
  $("#refs-track").innerHTML = refs.join("");
}

function bindClip(clip) {
  clip.addEventListener("pointerdown", (event) => {
    const shot = state.project.shots.find((item) => item.id === clip.dataset.shotId);
    state.selectedShotId = shot.id;
    renderTimeline();
    const mode = event.target.classList.contains("left") ? "left" : event.target.classList.contains("right") ? "right" : "move";
    const startX = event.clientX;
    const original = { start: shot.start, duration: shot.duration };
    const move = (moveEvent) => {
      const delta = (moveEvent.clientX - startX) / state.pixelsPerSecond;
      const snapped = Math.round(delta * 4) / 4;
      if (mode === "move") shot.start = Math.max(0, original.start + snapped);
      if (mode === "left") {
        const end = original.start + original.duration;
        shot.start = clamp(original.start + snapped, 0, end - .5);
        shot.duration = end - shot.start;
      }
      if (mode === "right") shot.duration = Math.max(.5, original.duration + snapped);
      state.dirty = true;
      renderTimeline();
    };
    const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); saveShotsSilently(); };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  });
  clip.addEventListener("dragover", (event) => event.preventDefault());
  clip.addEventListener("drop", (event) => {
    event.preventDefault();
    const id = event.dataTransfer.getData("text/asset-id");
    const shot = state.project.shots.find((item) => item.id === clip.dataset.shotId);
    if (id && !shot.asset_ids.includes(id)) shot.asset_ids.push(id);
    saveShotsSilently();
    renderTimeline();
  });
}

function renderShotInspector() {
  const shot = selectedShot();
  const inspector = $("#shot-inspector");
  if (!shot) {
    inspector.innerHTML = `<span class="eyebrow">Shot inspector</span><h2>No shot selected</h2><p>Add a shot to begin. Shots are rendered independently in H3's reliable 4–15 second range.</p>`;
    return;
  }
  const assets = state.project.assets || [];
  inspector.innerHTML = `<span class="eyebrow">Shot inspector</span><h2>${escapeHtml(shot.prompt?.slice(0, 34) || "Untitled shot")}</h2><span class="shot-status">${shot.status}</span><div class="form-row" style="margin-top:14px"><label>Start<input id="shot-start" type="number" min="0" step=".25" value="${shot.start}"></label><label>Duration<input id="shot-duration" type="number" min=".5" step=".25" value="${shot.duration}"></label></div><label>Generation mode<select id="shot-mode"><option value="reference" ${shot.mode === "reference" ? "selected" : ""}>Reference + audio</option><option value="image" ${shot.mode === "image" ? "selected" : ""}>Image to video</option><option value="text" ${shot.mode === "text" ? "selected" : ""}>Text to video</option></select></label><label>Creative intent<textarea id="shot-prompt" rows="8">${escapeHtml(shot.prompt)}</textarea></label><label>Seed<input id="shot-seed" type="number" min="0" value="${shot.seed}"></label><label>References<select id="shot-asset-select"><option value="">Attach asset…</option>${assets.filter((asset) => !shot.asset_ids.includes(asset.id)).map((asset) => `<option value="${asset.id}">${escapeHtml(asset.name)}</option>`).join("")}</select></label><div class="attached-list">${shot.asset_ids.map((id) => { const asset = assets.find((item) => item.id === id); if (!asset) return ""; const sameKind = shot.asset_ids.map((ref) => assets.find((item) => item.id === ref)).filter((item) => item && (item.kind === asset.kind || (!["video", "audio"].includes(item.kind) && !["video", "audio"].includes(asset.kind)))); const tag = asset.kind === "video" ? "Video" : asset.kind === "audio" ? "Audio" : "Picture"; return `<button class="quiet-button remove-ref" data-id="${id}">${tag} ${sameKind.indexOf(asset) + 1}: ${escapeHtml(asset.name)} ×</button>`; }).join(" ")}</div><label class="check-row"><input id="shot-song-audio" type="checkbox" ${shot.use_song_audio ? "checked" : ""}> Use master song as H3 audio reference</label>${shot.latest_output ? `<button class="quiet-button full" id="analyze-take">Inspect latest take</button>` : ""}<button class="primary-button full" id="compile-shot" style="margin-top:14px">Compile Director data</button>`;
  ["shot-start", "shot-duration", "shot-mode", "shot-prompt", "shot-seed", "shot-song-audio"].forEach((id) => $("#" + id).addEventListener("change", updateShotFromInspector));
  $("#shot-asset-select").addEventListener("change", (event) => { if (event.target.value) { shot.asset_ids.push(event.target.value); saveShotsSilently(); renderTimeline(); } });
  $$(".remove-ref", inspector).forEach((button) => button.addEventListener("click", () => { shot.asset_ids = shot.asset_ids.filter((id) => id !== button.dataset.id); saveShotsSilently(); renderTimeline(); }));
  $("#compile-shot").addEventListener("click", compileSelectedShot);
  $("#analyze-take")?.addEventListener("click", async () => {
    try { state.project = await api.analyzeLatestTake(state.project.id, shot.id); renderTimeline(); toast("Latest take review saved"); }
    catch (error) { toast(error.message, "error"); }
  });
}

function updateShotFromInspector() {
  const shot = selectedShot();
  shot.start = Math.max(0, Number($("#shot-start").value));
  shot.duration = Math.max(.5, Number($("#shot-duration").value));
  shot.mode = $("#shot-mode").value;
  shot.prompt = $("#shot-prompt").value;
  shot.seed = Math.max(0, Number($("#shot-seed").value));
  shot.use_song_audio = $("#shot-song-audio").checked;
  state.dirty = true;
  saveShotsSilently();
  renderTimeline();
}

async function compileSelectedShot() {
  const shot = selectedShot();
  if (!shot) return;
  try {
    const result = await api.compileTimeline(state.project.id, { window_start: shot.start, window_duration: shot.duration, fps: 24 });
    const warnings = result.warnings.length ? ` · ${result.warnings.join(" ")}` : "";
    toast(`Director data ready: ${result.requested_frames} frames → ${result.aligned_frames} aligned${warnings}`);
  } catch (error) { toast(error.message, "error"); }
}

function renderJobs() {
  const jobs = state.project?.jobs || [];
  const list = $("#job-list");
  const queueable = (state.project?.shots || []).filter((shot) => shot.status === "ready");
  $("#queue-ready").disabled = queueable.length === 0;
  $("#queue-ready").title = queueable.length
    ? `Queue ${queueable.length} reviewed H3 shot${queueable.length === 1 ? "" : "s"}`
    : "Mark a shot ready to queue H3";
  if (!jobs.length) { list.innerHTML = `<div class="queue-empty">No render jobs for this project.</div>`; return; }
  list.innerHTML = [...jobs].reverse().map((job) => `<div class="job-row" data-job-id="${job.id}"><span class="job-kind">${job.kind}</span><span>${escapeHtml(job.target_id || "—")}</span><span class="job-status ${job.status}">${job.status}</span><span>${job.seed}</span><span>${job.output_files?.[0] ? escapeHtml(job.output_files[0]) : job.error ? escapeHtml(job.error) : "—"}</span></div>`).join("");
}

async function saveProject() {
  if (!requireProject()) return;
  const documents = {
    creative_brief: $("#creative-brief").value,
    treatment: $("#treatment-text").value,
    style_bible: $("#style-bible").value,
  };
  try {
    state.project = await api.saveDocuments(state.project.id, documents);
    state.documentsDirty = false;
    state.dirty = state.shotsDirty;
    toast("Project saved");
  }
  catch (error) { toast(error.message, "error"); }
}

function saveShotsSilently() {
  if (!state.project) return Promise.resolve();
  const projectId = state.project.id;
  const shots = structuredClone(state.project.shots);
  const revision = ++shotSaveRevision;
  state.shotsDirty = true;
  state.dirty = true;
  shotSaveChain = shotSaveChain
    .then(() => api.saveShots(projectId, shots))
    .then(() => {
      if (revision === shotSaveRevision) {
        state.shotsDirty = false;
        state.dirty = state.documentsDirty;
      }
    })
    .catch((error) => { toast(error.message, "error"); });
  return shotSaveChain;
}

async function decodeAudio(file) {
  const context = new AudioContext();
  const buffer = await context.decodeAudioData(await file.arrayBuffer());
  await context.close();
  state.audioBuffer = buffer;
  return buffer;
}

function drawWaveform(canvas, buffer, color) {
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(rect.width * dpr));
  canvas.height = Math.max(1, Math.floor(rect.height * dpr));
  const context = canvas.getContext("2d");
  context.scale(dpr, dpr);
  const width = rect.width;
  const height = rect.height;
  const data = buffer.getChannelData(0);
  const step = Math.max(1, Math.floor(data.length / width));
  context.clearRect(0, 0, width, height);
  context.strokeStyle = color;
  context.lineWidth = 1;
  context.beginPath();
  for (let x = 0; x < width; x += 1) {
    let min = 1, max = -1;
    const offset = x * step;
    for (let index = 0; index < step; index += 1) { const value = data[offset + index] || 0; min = Math.min(min, value); max = Math.max(max, value); }
    context.moveTo(x, (1 + min) * height / 2);
    context.lineTo(x, (1 + max) * height / 2);
  }
  context.stroke();
}

function updateTimelinePlayhead() {
  const left = 90 + state.playhead * state.pixelsPerSecond;
  $("#timeline-playhead").style.left = `${left}px`;
  $("#timeline-time").textContent = formatTime(state.playhead, true);
  $("#global-time").textContent = formatTime(state.playhead);
  const duration = Number.isFinite($("#master-audio").duration)
    ? $("#master-audio").duration
    : projectDuration();
  $("#song-playhead").style.left = `${duration ? (state.playhead / duration) * 100 : 0}%`;
}

function attachSelectedAsset() {
  const asset = selectedAsset();
  const shot = selectedShot();
  if (!asset || !shot) return;
  if (!shot.asset_ids.includes(asset.id)) shot.asset_ids.push(asset.id);
  saveShotsSilently();
  renderTimeline();
  toast(`${asset.name} attached to shot`);
}

function escapeHtml(value = "") {
  return String(value).replace(/[&<>"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[char]));
}

function bindEvents() {
  $$(".rail-item").forEach((button) => button.addEventListener("click", () => {
    state.activePanel = button.dataset.panel;
    $$(".rail-item").forEach((item) => item.classList.toggle("active", item === button));
    $$(".panel").forEach((panel) => panel.classList.toggle("active", panel.id === `panel-${state.activePanel}`));
    if (state.activePanel === "timeline") requestAnimationFrame(renderTimeline);
  }));
  $("#project-select").addEventListener("change", async (event) => {
    const previousId = state.project?.id || "";
    if (state.dirty && !window.confirm("Discard unsaved changes and switch projects?")) {
      event.target.value = previousId;
      return;
    }
    try { await loadProject(event.target.value); }
    catch (error) { event.target.value = previousId; toast(error.message, "error"); }
  });
  $("#new-project").addEventListener("click", () => $("#project-dialog").showModal());
  $("#project-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const name = new FormData(form).get("name").trim();
    if (!name) return;
    try { const project = await api.createProject(name); $("#project-dialog").close(); form.reset(); await loadProjects(project.id); toast("Project created"); }
    catch (error) { toast(error.message, "error"); }
  });
  $("#save-project").addEventListener("click", saveProject);
  $("#save-treatment").addEventListener("click", saveProject);
  ["creative-brief", "treatment-text", "style-bible"].forEach((id) => $("#" + id).addEventListener("input", () => { state.documentsDirty = true; state.dirty = true; }));
  $("#song-file").addEventListener("change", async (event) => {
    const file = event.target.files[0];
    if (!file) return;
    $("#import-title").value ||= file.name.replace(/\.[^.]+$/, "");
    try { const buffer = await decodeAudio(file); $("#duration-value").textContent = formatTime(buffer.duration); renderSong(); }
    catch { toast("The browser could not decode this audio file.", "error"); }
  });
  $("#import-song").addEventListener("click", async () => {
    if (!requireProject()) return;
    const file = $("#song-file").files[0];
    if (!file) return toast("Choose a WAV, FLAC, or MP3 file.", "error");
    const form = new FormData();
    form.append("file", file); form.append("title", $("#import-title").value || file.name); form.append("duration", state.audioBuffer?.duration || 0);
    try { state.project = await api.uploadSong(state.project.id, form); renderAll(); toast("Song imported"); }
    catch (error) { toast(error.message, "error"); }
  });
  $("#music-form").addEventListener("submit", async (event) => {
    event.preventDefault(); if (!requireProject()) return;
    const data = Object.fromEntries(new FormData(event.currentTarget));
    const body = { title: data.title, caption: data.caption, lyrics: data.lyrics, duration: Number(data.duration), seed: Number(data.seed) };
    try { await api.generateMusic(state.project.id, body); await loadProject(state.project.id); toast("Music 3 job queued"); }
    catch (error) { toast(error.message, "error"); }
  });
  $("#flux-form").addEventListener("submit", async (event) => {
    event.preventDefault(); if (!requireProject()) return;
    const data = Object.fromEntries(new FormData(event.currentTarget));
    const [width, height] = data.aspect.split("x").map(Number);
    try { await api.generateFlux(state.project.id, { name: data.name, kind: data.kind, prompt: data.prompt, width, height, steps: Number(data.steps), guidance: Number(data.guidance), seed: Number(data.seed) }); await loadProject(state.project.id); state.activePanel = "assets"; toast("Flux image job queued"); }
    catch (error) { toast(error.message, "error"); }
  });
  $("#upload-asset-button").addEventListener("click", () => $("#asset-file").click());
  $("#asset-file").addEventListener("change", async (event) => {
    if (!requireProject()) return;
    const file = event.target.files[0]; if (!file) return;
    const name = window.prompt("Asset name", file.name.replace(/\.[^.]+$/, "")); if (!name) return;
    const kind = file.type.startsWith("video") ? "video" : file.type.startsWith("audio") ? "audio" : "image";
    const form = new FormData(); form.append("file", file); form.append("name", name); form.append("kind", kind);
    try { state.project = await api.uploadAsset(state.project.id, form); renderAssets(); toast("Asset added"); }
    catch (error) { toast(error.message, "error"); }
    event.target.value = "";
  });
  $$("#asset-filters button").forEach((button) => button.addEventListener("click", () => { state.assetFilter = button.dataset.filter; $$("#asset-filters button").forEach((item) => item.classList.toggle("active", item === button)); renderAssets(); }));
  $("#asset-search").addEventListener("input", renderAssets);
  $$(".document-tabs button").forEach((button) => button.addEventListener("click", () => { $$(".document-tabs button").forEach((item) => item.classList.toggle("active", item === button)); $$(".document-editor").forEach((editor) => editor.classList.toggle("active", editor.dataset.docPanel === button.dataset.doc)); }));
  $("#chat-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!requireProject()) return;
    const field = event.currentTarget.elements.message;
    const message = field.value.trim();
    if (!message) return;
    if (!state.health?.llm?.configured) return toast("Configure MVP_LLM_BASE_URL and MVP_LLM_MODEL to enable conversational planning.", "error");
    const button = event.currentTarget.querySelector("button[type=submit]");
    button.disabled = true; button.textContent = "Directing…";
    try {
      state.project = await api.directorChat(state.project.id, { message, apply_shots: false });
      field.value = "";
      renderAll();
      toast("Treatment updated; existing shots preserved");
    } catch (error) { toast(error.message, "error"); }
    finally { button.disabled = false; button.textContent = "Send to Director"; }
  });
  $("#send-treatment").addEventListener("click", () => document.querySelector('[data-panel="treatment"]').click());
  $("#apply-shot-plan").addEventListener("click", () => toast("Save the treatment, then add or edit shots directly in the Director timeline."));
  $("#add-shot").addEventListener("click", () => {
    if (!requireProject()) return;
    const shots = state.project.shots;
    const start = shots.length ? Math.max(...shots.map((shot) => shot.start + shot.duration)) : 0;
    const shot = { id: `shot_${crypto.randomUUID().replaceAll("-", "").slice(0, 12)}`, start, duration: Math.min(5, Math.max(.5, projectDuration() - start)), prompt: "New shot", mode: "reference", asset_ids: [], seed: 0, status: "draft", prompt_id: "", approved_output: "", locked: false };
    shots.push(shot); state.selectedShotId = shot.id; saveShotsSilently(); renderTimeline();
  });
  $("#duplicate-shot").addEventListener("click", () => { const shot = selectedShot(); if (!shot) return; const copy = structuredClone(shot); copy.id = `shot_${crypto.randomUUID().replaceAll("-", "").slice(0, 12)}`; copy.start = shot.start + shot.duration; copy.status = "draft"; state.project.shots.push(copy); state.selectedShotId = copy.id; saveShotsSilently(); renderTimeline(); });
  $("#delete-shot").addEventListener("click", () => { const shot = selectedShot(); if (!shot) return; state.project.shots = state.project.shots.filter((item) => item.id !== shot.id); state.selectedShotId = state.project.shots[0]?.id || null; saveShotsSilently(); renderTimeline(); });
  $("#split-shot").addEventListener("click", () => { const shot = selectedShot(); if (!shot || shot.duration < 1) return; const half = shot.duration / 2; const copy = structuredClone(shot); copy.id = `shot_${crypto.randomUUID().replaceAll("-", "").slice(0, 12)}`; copy.start = shot.start + half; copy.duration = half; shot.duration = half; state.project.shots.push(copy); saveShotsSilently(); renderTimeline(); });
  $("#zoom-in").addEventListener("click", () => { state.pixelsPerSecond = Math.min(64, state.pixelsPerSecond * 1.25); renderTimeline(); });
  $("#zoom-out").addEventListener("click", () => { state.pixelsPerSecond = Math.max(6, state.pixelsPerSecond / 1.25); renderTimeline(); });
  $("#timeline-canvas").addEventListener("pointerdown", (event) => {
    if (event.target.closest(".shot-clip")) return;
    const rect = $("#timeline-canvas").getBoundingClientRect();
    seekMasterAudio((event.clientX - rect.left - 90) / state.pixelsPerSecond);
  });
  $("#waveform").addEventListener("pointerdown", (event) => {
    const rect = event.currentTarget.getBoundingClientRect();
    const audio = $("#master-audio");
    const duration = Number.isFinite(audio.duration) ? audio.duration : projectDuration();
    seekMasterAudio(((event.clientX - rect.left) / rect.width) * duration);
  });
  $("#global-play").addEventListener("click", toggleMasterAudio);
  $("#timeline-play").addEventListener("click", toggleMasterAudio);
  $("#jump-start").addEventListener("click", () => seekMasterAudio(0));
  $("#timeline-start").addEventListener("click", () => seekMasterAudio(0));
  const masterAudio = $("#master-audio");
  masterAudio.addEventListener("timeupdate", () => {
    state.playhead = masterAudio.currentTime;
    updateTimelinePlayhead();
  });
  masterAudio.addEventListener("play", syncTransportState);
  masterAudio.addEventListener("pause", syncTransportState);
  masterAudio.addEventListener("ended", syncTransportState);
  masterAudio.addEventListener("loadedmetadata", () => {
    if (state.project?.song && !state.project.song.duration && Number.isFinite(masterAudio.duration)) {
      state.project.song.duration = masterAudio.duration;
      renderSong();
      renderTimeline();
    }
  });
  $("#refresh-jobs").addEventListener("click", refreshJobs);
  $("#cancel-project")?.addEventListener("click", () => $("#project-dialog").close());
  $("#queue-ready").addEventListener("click", async () => {
    const shots = state.project.shots.filter((shot) => shot.status === "ready");
    if (!shots.length) return;
    if (!window.confirm(`Queue ${shots.length} reviewed H3 shot${shots.length === 1 ? "" : "s"}? Reference shots use MiniMax Ultra and can use significant GPU time.`)) return;
    const button = $("#queue-ready");
    button.disabled = true;
    try {
      for (const shot of shots) await api.generateH3(state.project.id, shot.id);
      await loadProject(state.project.id);
      toast(`${shots.length} H3 shot${shots.length === 1 ? "" : "s"} queued`);
    } catch (error) { toast(error.message, "error"); }
    finally { renderJobs(); }
  });
  window.addEventListener("resize", () => { if (state.audioBuffer) { renderSong(); renderTimeline(); } });
  window.addEventListener("beforeunload", (event) => { if (state.dirty) event.preventDefault(); });
}

async function refreshJobs() {
  if (!state.project) return;
  try {
    const results = await Promise.allSettled(state.project.jobs.filter((job) => !["complete", "error", "cancelled"].includes(job.status)).map((job) => api.job(state.project.id, job.id)));
    await loadProject(state.project.id);
    const failed = results.filter((result) => result.status === "rejected").length;
    toast(failed ? `Queue refreshed with ${failed} downstream error${failed === 1 ? "" : "s"}` : "Queue refreshed", failed ? "error" : "info");
  } catch (error) { toast(error.message, "error"); }
}

async function init() {
  bindEvents();
  await Promise.all([loadHealth(), api.workflows().catch(() => [])]);
  try { await loadProjects(); } catch (error) { toast(error.message, "error"); }
}

init();
