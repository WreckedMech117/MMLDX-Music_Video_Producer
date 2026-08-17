import { APPLY_DOCUMENTS_CONTROL, DOCUMENT_CONTROLS, PLACEHOLDER_PROMPT, SHOT_EXPANSION_EDIT_BLOCKED, SHOT_EXPANSION_WITHOUT_SHOTS, SONG_CHANGE_CONSEQUENCE, UNSAVED_DOCUMENT_EDITS_CONSEQUENCE, api, batchQueueProgress, batchReadinessBlock, clearDocumentConsent, comfyOutputUrl, documentChangeToast, documentConsent, documentConsentClearedOnLoad, documentLabel, documentLockNotice, documentRestoreAvailable, documentRestoreNotice, documentRestoreRefusal, documentRestoreStaleNotice, documentRestoreTitle, escapeHtml, musicFormFieldUpdate, musicGenerationPlan, queueButtonState, readinessLines, readinessSummary, shotExpansionToast, shotInspectorReadiness, shotPromptCell, songChangeNeedsConfirmation, songImportDuration, songRefusalMessage, threadHtml } from "./api.js";
import { selectedAsset, selectedShot, state } from "./state.js";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
let shotSaveChain = Promise.resolve();
let shotSaveRevision = 0;
// True for the whole of an expansion call, which is what stops a timeline edit made *during* it
// from queueing a whole-list shot save that lands afterwards and reverts every prompt written.
let shotExpansionInFlight = false;
let waveformLoadRevision = 0;
// The plan's readiness as the server last reported it, or null when nothing has been fetched for
// the project on screen. Fetched on project load rather than only at the click, because readiness
// is a cheap GET and a batch the route will certainly refuse must not look submittable until the
// Director has spent the click on it. Held here rather than on `state` because it is not project
// data: it is derived, never saved, and never sent back.
let readinessReport = null;
let readinessLoadRevision = 0;

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

// One gate for every path that changes or removes the project's Song — import, generate,
// remove — matching the `window.confirm` precedent for destructive and expensive actions.
//
// Returns both answers separately on purpose. `proceed` is whether to act at all;
// `confirmed` is whether the Director actually saw and accepted SONG_CHANGE_CONSEQUENCE,
// and that is the only thing sent to the server as confirm_song_replacement. Sending a
// blanket `true` would let a stale local project (shots added elsewhere since this one
// loaded) defeat the server's gate without anyone ever reading the consequence.
function confirmSongChange(question) {
  if (!songChangeNeedsConfirmation(state.project)) return { proceed: true, confirmed: false };
  const confirmed = window.confirm(`${question}\n\n${SONG_CHANGE_CONSEQUENCE}`);
  return { proceed: confirmed, confirmed };
}

// A refusal from the Song gate means the server knows about Shots this client does not,
// so `confirmSongChange` never asked. Without refreshing, every retry re-reads the same
// stale project, sends confirmed=false again, and fails identically -- the Director is
// stuck, staring at a REST instruction they cannot act on. Refresh and let them retry
// for real; any other error is just reported.
async function recoverFromSongRefusal(error) {
  toast(error.message, "error");
  if (!state.project || !songRefusalMessage(error.message)) return;
  try {
    state.project = await api.project(state.project.id);
    renderAll();
    toast("This project has shots on the server. Try again to see what changing the song affects.");
  } catch {
    // Leave the original error standing; a failed refresh is not new information.
  }
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
  // Ahead of the no-project branch, because both are a change of project: consent given in one
  // project must never be inherited by the next one loaded, or a document is replaced in a
  // project the Director was not even looking at when they ticked the box.
  //
  // Gated on the project actually changing, because most of this function's callers are refreshes
  // of the project already on screen -- the queue refresh, both generate paths, multiview, the
  // queue-ready loop -- and unticking the box there revokes consent the Director gave seconds ago
  // in the project they are still looking at, with nothing on screen to explain it.
  if (documentConsentClearedOnLoad(state.project?.id, id)) clearDocumentConsent(applyDocumentsControl());
  // Whatever readiness this client held belongs to the project being left, and the revision bump
  // discards an answer still in flight for it. A readiness report drawn under another project's
  // name would name Shots that are not on screen and count a plan nobody is looking at.
  readinessReport = null;
  readinessLoadRevision += 1;
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
  loadReadiness(id);
}

// Readiness for the project just loaded, fetched rather than computed: this client's copy of the
// plan can be minutes old, and the answer that matters is the one the route will apply. Not
// awaited, for the same reason the waveform decode is not -- the workspace must draw immediately
// -- and guarded by both a revision and the loaded project id, because the selector stays live.
//
// A failure is swallowed on purpose. Readiness here is advance notice, never the gate: the route
// still refuses a blocked submission, so a failed GET must not disable the batch button or put an
// error on screen for something the Director never asked for.
async function loadReadiness(projectId) {
  const revision = ++readinessLoadRevision;
  if (!projectId) return;
  try {
    const report = await api.readiness(projectId);
    if (revision !== readinessLoadRevision || state.project?.id !== projectId) return;
    readinessReport = report;
    renderTimeline();
    renderJobs();
    renderReadiness();
  } catch {
    // Advance notice only; the server remains the gate.
  }
}

function renderAll() {
  renderSong();
  renderTreatment();
  renderAssets();
  renderTimeline();
  renderJobs();
  renderReadiness();
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
  $("#remove-song").disabled = !song;
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
  syncDocumentControls();
  const thread = $("#chat-thread");
  // The whole body -- the empty-thread copy, every bubble, the prose/notice split and every
  // escape -- is `threadHtml`, a pure function the suite executes. This line is the only place
  // any of it becomes DOM, so it is the only thing left here that a test cannot run: assigning
  // to `textContent` instead would print every refusal as literal markup with the block never
  // appearing, so the assignment itself is pinned by tests/test_frontend_contract.py.
  thread.innerHTML = threadHtml(project?.messages);
  thread.scrollTop = thread.scrollHeight;
}

// Both per-document controls, seeded from the project's own fields through the one control
// table: each lock checkbox from its `*_locked` flag, each restore button's enabled state from
// its `*_previous` slot.
//
// Seeding the checkboxes is not cosmetic. An unseeded box reads as unlocked, and the next
// ordinary save PUTs `locked: false` for both — so the client explicitly unlocks on the server
// what the Director locked, defeating the route's "absent means leave it alone" design from
// one layer up.
//
// Deliberately separate from renderTreatment: a failed lock save has to revert these two
// controls to the stored state without also overwriting textareas the Director is still
// typing in.
function syncDocumentControls() {
  for (const [documentKey, control] of Object.entries(DOCUMENT_CONTROLS)) {
    $(control.lock).checked = Boolean(state.project?.[control.lockedField]);
    const available = documentRestoreAvailable(state.project, documentKey);
    const button = $(control.restore);
    button.disabled = !available;
    button.title = documentRestoreTitle(documentKey, available);
  }
}

// The textareas now hold exactly what the server has, so the project is no longer dirty
// against it -- the same bookkeeping saveProject does. Leaving the flags set makes the next
// project switch ask to discard changes that do not exist, which teaches the Director to
// click straight through the one question that protects real unsaved work.
function markDocumentsSaved() {
  state.documentsDirty = false;
  state.dirty = state.shotsDirty;
}

// One gate for every path that lets the server's text overwrite the document editors --
// restore and a Director reply -- matching the `window.confirm` precedent on project switch.
// Recovery captures the *stored* text, so unsaved on-screen edits are the one thing that
// cannot be restored afterwards; discarding them silently is the loss mode this feature
// exists to eliminate.
function confirmDiscardingDocumentEdits(question) {
  if (!state.documentsDirty) return true;
  return window.confirm(`${question}\n\n${UNSAVED_DOCUMENT_EDITS_CONSEQUENCE}`);
}

// The unsaved-edits question, stated for the send that is actually about to happen. Both sends
// re-render the editors from the server and so discard unsaved typing -- that is why the gate
// fires either way -- but only a consented send can *replace* a document, and warning about a
// rewrite that cannot happen deters a send that would write nothing.
function directorSendQuestion(applyDocuments) {
  return applyDocuments
    ? "Send this to the Director? A reply can replace either creative document."
    : "Send this to the Director? No document will be replaced, but the editors are re-rendered from the text stored on the server.";
}

// The composer's consent control, or null when the markup does not carry it. The reading and
// the clearing are pure functions in api.js so they can be executed rather than only grepped;
// this is the one place that turns the selector into an element.
function applyDocumentsControl() {
  return $(APPLY_DOCUMENTS_CONTROL);
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
  // An unprompted clip says so, rather than borrowing the "Untitled shot" fallback a real prompt
  // of that name would also render. Three independent signals, because state is never carried by
  // colour alone: the flag text in place of the prompt, the dashed border `.no-prompt` gets, and
  // the clip's accessible name, which is the only one a screen reader announces.
  //
  // Every one of those comes out of `shotPromptCell`, which is executed by the contract tests.
  // The ternaries used to live in this template, where swapping their arms rendered the flag onto
  // every written clip and the unprompted one empty with the whole suite still green.
  track.innerHTML = (state.project?.shots || []).map((shot, index) => {
    const cell = shotPromptCell(shot);
    return `<div class="shot-clip ${cell.className} ${shot.id === state.selectedShotId ? "selected" : ""}" data-shot-id="${shot.id}" title="${escapeHtml(cell.label)}" aria-label="${escapeHtml(cell.label)}" style="left:${shot.start * state.pixelsPerSecond}px;width:${Math.max(40, shot.duration * state.pixelsPerSecond)}px"><span class="resize-handle left"></span><span class="clip-id">SHOT ${String(index + 1).padStart(2, "0")} · ${shot.duration.toFixed(1)}s</span><span class="clip-prompt">${escapeHtml(cell.text)}</span><span class="resize-handle right"></span></div>`;
  }).join("");
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
  // The refusal sends the Director here -- "Write a prompt in the shot inspector" -- so the panel
  // has to show which Shot is blocked and why, rather than looking like an ordinary shot with an
  // empty box. The sameness lines are the other half: a near-duplicate pair is only something the
  // Director can differentiate or accept deliberately if it is named where its prompt is edited.
  const readiness = shotInspectorReadiness(readinessReport, shot);
  const readinessHtml = readiness.blocked || readiness.sameness.length
    ? `<div class="shot-readiness ${readiness.blocked ? "blocked" : "sameness"}">${readiness.blocked ? `<strong>${escapeHtml(readiness.flag)}</strong><p>${escapeHtml(readiness.help)}</p>` : ""}${readiness.sameness.map((line) => `<p>${escapeHtml(line.text)}</p>`).join("")}</div>`
    : "";
  inspector.innerHTML = `<span class="eyebrow">Shot inspector</span><h2>${escapeHtml(shot.prompt?.slice(0, 34) || "Untitled shot")}</h2><span class="shot-status">${shot.status}</span>${readinessHtml}<div class="form-row" style="margin-top:14px"><label>Start<input id="shot-start" type="number" min="0" step=".25" value="${shot.start}"></label><label>Duration<input id="shot-duration" type="number" min=".5" step=".25" value="${shot.duration}"></label></div><label>Generation mode<select id="shot-mode"><option value="reference" ${shot.mode === "reference" ? "selected" : ""}>Reference + audio</option><option value="image" ${shot.mode === "image" ? "selected" : ""}>Image to video</option><option value="text" ${shot.mode === "text" ? "selected" : ""}>Text to video</option></select></label><label>Creative intent<textarea id="shot-prompt" rows="8">${escapeHtml(shot.prompt)}</textarea></label><label>Seed<input id="shot-seed" type="number" min="0" value="${shot.seed}"></label><label>References<select id="shot-asset-select"><option value="">Attach asset…</option>${assets.filter((asset) => !shot.asset_ids.includes(asset.id)).map((asset) => `<option value="${asset.id}">${escapeHtml(asset.name)}</option>`).join("")}</select></label><div class="attached-list">${shot.asset_ids.map((id) => { const asset = assets.find((item) => item.id === id); if (!asset) return ""; const sameKind = shot.asset_ids.map((ref) => assets.find((item) => item.id === ref)).filter((item) => item && (item.kind === asset.kind || (!["video", "audio"].includes(item.kind) && !["video", "audio"].includes(asset.kind)))); const tag = asset.kind === "video" ? "Video" : asset.kind === "audio" ? "Audio" : "Picture"; return `<button class="quiet-button remove-ref" data-id="${id}">${tag} ${sameKind.indexOf(asset) + 1}: ${escapeHtml(asset.name)} ×</button>`; }).join(" ")}</div><label class="check-row"><input id="shot-song-audio" type="checkbox" ${shot.use_song_audio ? "checked" : ""}> Use master song as H3 audio reference</label>${shot.latest_output ? `<button class="quiet-button full" id="analyze-take">Inspect latest take</button>` : ""}<button class="primary-button full" id="compile-shot" style="margin-top:14px">Compile Director data</button>`;
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
  // Both reasons the button can be off, decided in one place: nothing to queue, and a batch the
  // route will certainly refuse. The second was invisible until the click -- the button was
  // enabled purely from the ready count -- so a Director spent the click to be told no.
  const queue = queueButtonState(readinessReport, queueable);
  $("#queue-ready").disabled = queue.disabled;
  $("#queue-ready").title = queue.title;
  if (!jobs.length) { list.innerHTML = `<div class="queue-empty">No render jobs for this project.</div>`; return; }
  list.innerHTML = [...jobs].reverse().map((job) => `<div class="job-row" data-job-id="${job.id}"><span class="job-kind">${job.kind}</span><span>${escapeHtml(job.target_id || "—")}</span><span class="job-status ${job.status}">${job.status}</span><span>${job.seed}</span><span>${job.output_files?.[0] ? escapeHtml(job.output_files[0]) : job.error ? escapeHtml(job.error) : "—"}</span></div>`).join("");
}

// The whole report, above the button that acts on it: the counts, every Shot that blocks, and
// every near-duplicate pair. The warnings half reached no surface at all before this -- the batch
// check reads only the blocking ids and the compile toast prints the timeline's frame warnings --
// so the sameness the server computes was invisible to the Director it was computed for.
//
// Every line is the server's own sentence, prefixed with which half it came from, so the two kinds
// are told apart by words rather than only by the colour of a list marker.
function renderReadiness() {
  const region = $("#plan-readiness");
  if (!region) return;
  const lines = readinessLines(readinessReport);
  region.classList.toggle("blocked", lines.some((line) => line.kind === "blocking"));
  region.innerHTML = `<strong>${escapeHtml(readinessSummary(readinessReport))}</strong>${lines.length ? `<ul>${lines.map((line) => `<li class="${line.kind}">${escapeHtml(line.text)}</li>`).join("")}</ul>` : ""}`;
}

// `notice` so a lock toggle can confirm what it actually changed instead of reporting a
// generic save. Returns whether the save reached the server, which is the only way the lock
// handler can tell that the checkbox in front of the Director is now a lock the server does
// not have.
async function saveProject(notice = "Project saved") {
  if (!requireProject()) return false;
  const documents = {
    creative_brief: $("#creative-brief").value,
    treatment: $("#treatment-text").value,
    style_bible: $("#style-bible").value,
    // Sent on every document save, never omitted: the route reads an absent lock as "leave
    // it alone", so omitting them would make the checkboxes purely decorative.
    treatment_locked: $("#lock-treatment").checked,
    style_bible_locked: $("#lock-style").checked,
  };
  try {
    state.project = await api.saveDocuments(state.project.id, documents);
    markDocumentsSaved();
    toast(notice);
    return true;
  }
  catch (error) { toast(error.message, "error"); return false; }
}

// Recovery, without the model that caused the problem: this sends no message and never
// reaches the Director. The reply is the whole project, so re-rendering shows the restored
// text, the swapped recovery slot, and the system line the thread now carries.
// `documentKey`, not `document`: this module reaches for the DOM global constantly, and a
// parameter shadowing it here would break the next edit in a thoroughly confusing way.
async function restoreDocument(documentKey) {
  if (!requireProject()) return;
  // The restored text lands in the textarea, so anything typed and unsaved is gone -- and it
  // was never captured, because only stored text becomes a kept version.
  if (!confirmDiscardingDocumentEdits(`Restore ${documentLabel(documentKey)} from the version kept on the server?`)) return;
  try {
    state.project = await api.restoreDocument(state.project.id, documentKey);
    markDocumentsSaved();
    renderAll();
    toast(documentRestoreNotice(documentKey));
  } catch (error) {
    toast(error.message, "error");
    // The controls are disabled unless a version is kept, so this refusal means the loaded
    // project is stale. Refresh, exactly as a Song refusal does, or every retry fails
    // identically against the same stale state.
    if (!documentRestoreRefusal(error.message) || !state.project) return;
    try {
      state.project = await api.project(state.project.id);
      markDocumentsSaved();
      renderAll();
      // Which sentence is true is decided by the project just fetched, never assumed: the
      // refusal only proves this client was stale, so a refreshed project that does hold a
      // kept version must not be reported as having none.
      toast(documentRestoreStaleNotice(documentKey, documentRestoreAvailable(state.project, documentKey)));
    } catch {
      // Leave the original error standing; a failed refresh is not new information.
    }
  }
}

// Write a prompt onto every unlocked shot from the Treatment, the Style bible and the shot
// windows. It queues nothing: no render is submitted, no shot status changes, and the prompts land
// in the shot inspector where they stay editable -- which is why the button says so.
//
// Silent shot saves are shut out for the whole call, in two halves, because either one alone still
// loses the prompts. Awaiting the pending chain drains the saves queued *before* the click -- a
// drag followed immediately by a press would otherwise let a save carrying the old prompts land
// after the expansion wrote the new ones -- and it is also what makes the server build the model's
// input from the windows currently on screen. The in-flight flag covers the rest: the call takes
// multiple seconds, and a drag *during* it would queue exactly the same stale whole-list save,
// which awaiting something queued earlier cannot prevent. Edits made then are refused out loud
// rather than dropped.
//
// The response also re-renders the document editors, so it is gated on the same unsaved-edits
// question every other server-overwrites-the-editors path asks, and clears the dirty flags after
// the editors have been rendered from the server -- exactly as a restore and a Director reply do.
//
// The reply is the whole project, so the timeline and the inspector are re-rendered from it rather
// than patched locally, and the toast is read out of the reply rather than diffed.
async function expandShotPrompts() {
  if (!requireProject()) return;
  if (!state.project.shots.length) return toast(SHOT_EXPANSION_WITHOUT_SHOTS, "error");
  if (!state.health?.llm?.configured) return toast("Configure MVP_LLM_BASE_URL and MVP_LLM_MODEL to expand shots into prompts.", "error");
  if (!confirmDiscardingDocumentEdits("Expand shots into prompts? No document is replaced, but the whole project comes back, so the editors are re-rendered from the text stored on the server.")) return;
  // The id this call is sent for, captured before any await. `state.project` is rebound by the
  // response and the project selector stays live throughout, so without this a result for project
  // A can be written over project B and drawn -- A's shots and A's documents -- under B's name.
  const projectId = state.project.id;
  const button = $("#expand-shot-prompts");
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Expanding…";
  shotExpansionInFlight = true;
  try {
    await shotSaveChain;
    const expanded = await api.expandShots(projectId);
    // The Director switched projects while the model was thinking. The prompts are written and
    // saved on the server, so nothing is lost by dropping this reply -- loading that project again
    // shows them -- whereas applying it here would show one project's work under another's name.
    if (state.project?.id !== projectId) return;
    state.project = expanded;
    markDocumentsSaved();
    renderAll();
    // An expansion writes prompts onto Shots, so the report this client holds describes the plan
    // as it was before the call -- including blocks it has just resolved.
    loadReadiness(projectId);
    toast(shotExpansionToast(state.project));
  } catch (error) { toast(error.message, "error"); }
  finally { shotExpansionInFlight = false; button.disabled = false; button.textContent = label; }
}

function saveShotsSilently() {
  if (!state.project) return Promise.resolve();
  // Refused, not queued: this save carries the whole shot list as it was before the expansion, so
  // landing it afterwards reverts every prompt just written while the success toast is still on
  // screen. Said out loud because the edit really is not saved and the response re-renders the
  // timeline over it.
  if (shotExpansionInFlight) {
    toast(SHOT_EXPANSION_EDIT_BLOCKED, "error");
    return Promise.resolve();
  }
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
        // Only for the save that settled the burst: a prompt edited in the inspector changes what
        // blocks and what is a near-duplicate, and a report from before the edit would keep
        // reporting a block the Director has just fixed.
        loadReadiness(projectId);
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
  // Wrapped rather than passed directly: saveProject's first argument is the toast wording,
  // and handing it a click event would report an Event object as the confirmation.
  $("#save-project").addEventListener("click", () => saveProject());
  $("#save-treatment").addEventListener("click", () => saveProject());
  // Bound from the one control table, so a document's selectors are never spelled out again
  // here -- a crossed pair would otherwise wire one document's button to the other's slot.
  for (const [documentKey, control] of Object.entries(DOCUMENT_CONTROLS)) {
    $(control.restore).addEventListener("click", () => restoreDocument(documentKey));
    // A lock change persists immediately through the ordinary document save, which also
    // carries whatever is in the textareas -- so nothing typed is discarded, and a lock the
    // Director set is in effect before the next reply rather than waiting on a separate save.
    $(control.lock).addEventListener("change", async (event) => {
      if (await saveProject(documentLockNotice(documentKey, event.currentTarget.checked))) return;
      // The save never landed, so the server's lock is still whatever it was; a checkbox left
      // showing the attempted state claims a protection that does not exist. Only the controls
      // are reverted -- reverting the textareas too would discard unsaved typing.
      syncDocumentControls();
    });
  }
  ["creative-brief", "treatment-text", "style-bible"].forEach((id) => $("#" + id).addEventListener("input", () => { state.documentsDirty = true; state.dirty = true; }));
  $("#song-file").addEventListener("change", async (event) => {
    const file = event.target.files[0];
    if (!file) return;
    $("#import-title").value ||= file.name.replace(/\.[^.]+$/, "");
    // The chosen file's measurement is recorded against the file itself, not left in
    // state.audioBuffer. That field has a second writer -- loadPersistedWaveform, an
    // un-awaited decode of the *stored* song -- which could land after this handler and
    // hand the import the previous song's length, persisting a wrong timing spine: the
    // exact regression this path exists to prevent. Bumping the revision also cancels
    // any persisted decode still in flight.
    waveformLoadRevision += 1;
    let decoded = null;
    try { decoded = await decodeAudio(file); }
    catch {
      state.pendingImport = { file, decoded: null };
      state.audioBuffer = null;
      renderSong();
      $("#duration-value").textContent = "—";
      toast("The browser could not decode this audio file; its length will be measured on the server.", "error");
      // The song still loaded in the project keeps its waveform; an unrelated failed
      // decode of a candidate file should not blank the display until a reload.
      if (state.project?.song?.path) loadPersistedWaveform(state.project.id);
      return;
    }
    // Rendering is outside the catch on purpose: a throw from renderSong or the
    // waveform draw is not a decode failure, and reporting it as "undecodable file"
    // would discard a perfectly good buffer.
    state.pendingImport = { file, decoded };
    renderSong();
    $("#duration-value").textContent = formatTime(decoded.duration);
  });
  $("#import-song").addEventListener("click", async () => {
    if (!requireProject()) return;
    const file = $("#song-file").files[0];
    if (!file) return toast("Choose a WAV, FLAC, or MP3 file.", "error");
    const change = confirmSongChange("Replace this project's song with the imported file?");
    if (!change.proceed) return;
    const form = new FormData();
    form.append("file", file); form.append("title", $("#import-title").value || file.name);
    // Only this file's own measurement counts. Anything decoded for another file --
    // or for the project's stored song -- is not this import's length.
    form.append("duration", songImportDuration(state.pendingImport?.file === file ? state.pendingImport : null));
    form.append("confirm_song_replacement", String(change.confirmed));
    try { state.project = await api.uploadSong(state.project.id, form); state.pendingImport = null; renderAll(); toast("Song imported"); }
    catch (error) { await recoverFromSongRefusal(error); }
  });
  $("#remove-song").addEventListener("click", async () => {
    if (!requireProject()) return;
    if (!state.project.song) return toast("This project has no song to remove.", "error");
    const change = confirmSongChange("Remove the song from this project?");
    if (!change.proceed) return;
    try {
      state.project = await api.removeSong(state.project.id, change.confirmed);
      // Cancels any persisted decode still in flight, so a removed song's waveform
      // cannot reappear and be read as current.
      waveformLoadRevision += 1;
      state.audioBuffer = null;
      renderAll();
      toast("Song removed from the project; the audio file was left on disk");
    }
    catch (error) { await recoverFromSongRefusal(error); }
  });
  const musicForm = $("#music-form");
  // Thin DOM applier: every decision (which bounds, which way to clamp) lives in
  // musicFormFieldUpdate, which is unit-tested without a browser.
  const syncMusicVariant = () => {
    const update = musicFormFieldUpdate(musicForm.elements.preset.value, {
      duration: musicForm.elements.duration.value,
      seed: musicForm.elements.seed.value,
    });
    const lyricsField = musicForm.elements.lyrics;
    lyricsField.closest("label").style.display = update.lyricsVisible ? "" : "none";
    lyricsField.disabled = !update.lyricsVisible;
    lyricsField.required = update.lyricsRequired;
    for (const [name, bounds] of Object.entries(update.numeric)) {
      const field = musicForm.elements[name];
      field.min = bounds.min;
      field.max = bounds.max;
      field.value = bounds.value;
    }
  };
  syncMusicVariant();
  musicForm.elements.preset.addEventListener("change", syncMusicVariant);
  musicForm.addEventListener("submit", async (event) => {
    event.preventDefault(); if (!requireProject()) return;
    const data = Object.fromEntries(new FormData(event.currentTarget));
    const plan = musicGenerationPlan(data);
    if (data.preset === "songplanner-known" && !plan.body.lyrics) {
      return toast("Paste the lyric sheet, or switch to the invented-lyrics preset.", "error");
    }
    // Both generate routes assign the new Song at submit time, before any audio exists,
    // so the consequence is asked here rather than when the job completes. This is a
    // separate question from the GPU-cost confirm below, which is about render time.
    const change = confirmSongChange("Queue song generation? It replaces this project's song as soon as the job is submitted.");
    if (!change.proceed) return;
    plan.body.confirm_song_replacement = change.confirmed;
    try {
      if (plan.endpoint === "songplanner") {
        if (!window.confirm("Queue SongPlanner generation? It loads the 12B Gemma-3 planner plus the Music 3 stack and can use significant GPU time.")) return;
        await api.generateSongPlanner(state.project.id, plan.body);
        toast("SongPlanner job queued");
      } else {
        await api.generateMusic(state.project.id, plan.body);
        toast("Music 3 job queued");
      }
      await loadProject(state.project.id);
    }
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
  $$(".document-tabs button").forEach((button) => button.addEventListener("click", () => {
    $$(".document-tabs button").forEach((item) => item.classList.toggle("active", item === button));
    $$(".document-editor").forEach((editor) => editor.classList.toggle("active", editor.dataset.docPanel === button.dataset.doc));
    // Lock and restore each belong to one document. The Creative brief has neither -- it is
    // never replaced by a Director reply -- so controls left visible there offer actions that
    // cannot apply to the text on screen.
    $$("[data-doc-controls]").forEach((group) => group.classList.toggle("active", group.dataset.docControls === button.dataset.doc));
  }));
  $("#chat-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!requireProject()) return;
    const field = event.currentTarget.elements.message;
    const message = field.value.trim();
    if (!message) return;
    if (!state.health?.llm?.configured) return toast("Configure MVP_LLM_BASE_URL and MVP_LLM_MODEL to enable conversational planning.", "error");
    // Read from the control, never hardcoded: it is the Director's consent for this turn, and a
    // fixed `true` here would reinstate the unrequested rewrite the flag exists to stop, while a
    // fixed `false` would make the control decorative.
    const applyDocuments = documentConsent(applyDocumentsControl());
    // The whole project comes back, so the editors are re-rendered from the server whether or
    // not anything changed. Ask before that discards typing that was never stored and therefore
    // was never recoverable -- and ask about the send that is actually happening, since only a
    // consented one can replace a document.
    if (!confirmDiscardingDocumentEdits(directorSendQuestion(applyDocuments))) return;
    const before = state.project;
    const button = event.currentTarget.querySelector("button[type=submit]");
    button.disabled = true; button.textContent = "Directing…";
    try {
      state.project = await api.directorChat(state.project.id, { message, apply_shots: false, apply_documents: applyDocuments });
      field.value = "";
      markDocumentsSaved();
      renderAll();
      // Derived from what the documents actually are now, not asserted. A locked document, a
      // candidate the guard rejected and an identical rewrite all leave the text as it was,
      // and the most prominent feedback on screen must not contradict the reply itself. The
      // consent is passed in because a declined turn's "nothing changed" has a different cause
      // -- and a different remedy -- from a lock or a rejection.
      toast(documentChangeToast(before, state.project, applyDocuments));
    } catch (error) { toast(error.message, "error"); }
    // The consent this turn carried is spent, so the next turn starts from a decline again.
    finally { clearDocumentConsent(applyDocumentsControl()); button.disabled = false; button.textContent = "Send to Director"; }
  });
  $("#send-treatment").addEventListener("click", () => document.querySelector('[data-panel="treatment"]').click());
  $("#expand-shot-prompts").addEventListener("click", expandShotPrompts);
  $("#add-shot").addEventListener("click", () => {
    if (!requireProject()) return;
    const shots = state.project.shots;
    const start = shots.length ? Math.max(...shots.map((shot) => shot.start + shot.duration)) : 0;
    // The placeholder comes from the one constant the readiness rule reads, because the server
    // blocks exactly this string: a second spelling here would create Shots the timeline draws as
    // prompted and the route then refuses.
    const shot = { id: `shot_${crypto.randomUUID().replaceAll("-", "").slice(0, 12)}`, start, duration: Math.min(5, Math.max(.5, projectDuration() - start)), prompt: PLACEHOLDER_PROMPT, mode: "reference", asset_ids: [], seed: 0, status: "draft", prompt_id: "", approved_output: "", locked: false };
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
    if (!requireProject()) return;
    const shots = state.project.shots.filter((shot) => shot.status === "ready");
    if (!shots.length) return;
    // The project this batch belongs to, captured before any await. The selector stays live while
    // the readiness GET is in flight, so without this the Shot ids collected from project A would
    // be submitted against project B -- renders queued for a plan nobody asked about, on a project
    // whose readiness was never checked. Same guard the expansion handler carries.
    const projectId = state.project.id;
    const button = $("#queue-ready");
    button.disabled = true;
    // How much of the batch the server has already accepted, so a failure partway can say so.
    let queued = 0;
    try {
      // Before the loop, never inside it. The route refuses a blocked Shot per submission, so a
      // refusal discovered mid-loop would leave every earlier Shot already queued and burning GPU
      // minutes on a plan that is about to be edited and resubmitted -- a half-submitted batch,
      // which is the one outcome this check exists to make impossible. Either all of it goes or
      // none of it does.
      //
      // Asked of the server rather than computed here: this client's copy of the project can be
      // minutes old, and the readiness that matters is the one the route will apply.
      const report = await api.readiness(projectId);
      if (state.project?.id !== projectId) return;
      readinessReport = report;
      // Only the Shots actually being queued block it -- decided by `batchReadinessBlock`, which is
      // executed by the contract tests. A blocked draft elsewhere in the plan is not this batch's
      // problem, and refusing over one would make the button unusable for a plan the Director is
      // still writing, which is every plan most of the time.
      const block = batchReadinessBlock(report, shots.map((shot) => shot.id));
      if (block.refused) { toast(block.message, "error"); return; }
      // After the check, so the Director is never asked to accept a GPU cost for a batch that
      // was never going to be sent.
      if (!window.confirm(`Queue ${shots.length} reviewed H3 shot${shots.length === 1 ? "" : "s"}? Reference shots use MiniMax Ultra and can use significant GPU time.`)) return;
      for (const shot of shots) { await api.generateH3(projectId, shot.id); queued += 1; }
      toast(`${shots.length} H3 shot${shots.length === 1 ? "" : "s"} queued`);
      if (state.project?.id === projectId) await loadProject(projectId);
    } catch (error) {
      // What already queued is spending GPU minutes right now. The refusal on its own reads as
      // "nothing happened", and a Director who believes that edits the plan and submits the whole
      // batch again -- on top of the half that is already rendering.
      toast(`${error.message} ${batchQueueProgress(queued, shots.length)}`, "error");
      if (queued && state.project?.id === projectId) await loadProject(projectId);
    }
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
