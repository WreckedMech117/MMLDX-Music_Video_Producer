import { APPLY_DOCUMENTS_CONTROL, ASSET_ROLE_LABELS, ASSISTANT_FILL_ALL_CONTROL, ASSISTANT_FILL_CONTROL, ASSISTANT_EDIT_BLOCKED, ASSISTANT_PREFILL_CONTROL, ASSISTANT_WITHOUT_REQUEST, CITATION_MISSING_LABEL, EXPAND_ALL_PROMPTS_CONTROL, EXPAND_ALL_PROMPTS_WITHOUT_SHOTS, DOCUMENT_CONTROLS, PLACEHOLDER_PROMPT, RENDER_POLL_INTERVAL_MS, SHOT_EXPANSION_EDIT_BLOCKED, SHOT_EXPANSION_WITHOUT_SHOTS, SHOT_MODES, SINGING_STATES, SONG_CHANGE_CONSEQUENCE, SONG_CONTEXT_CONTROLS, SONG_CONTEXT_COUNTS, UNSAVED_DOCUMENT_EDITS_CONSEQUENCE, VRAM_EJECT_CONTROL, VRAM_EJECT_NOTE, api, applyRenderStatus, approvalControl, approvalNotice, assistantControl, assistantFillAllControl, assistantToast, clearDocumentConsent, comfyOutputUrl, documentChangeToast, documentConsent, documentConsentClearedOnLoad, documentLabel, documentLockNotice, documentRestoreAvailable, documentRestoreNotice, documentRestoreRefusal, documentRestoreStaleNotice, documentRestoreTitle, escapeHtml, expandAllPromptsControl, expandAllPromptsToast, expandPromptControl, expandPromptToast, expansionReport, hasActiveRenderJobs, markReadyControl, markReadyNotice, aiModPlan, multiviewPlan, musicFormFieldUpdate, musicGenerationPlan, generateAllPlan, batchReportToast, prefillControl, readinessLines, readinessSummary, reconcileShotCitations, renderAgainControl, renderAgainNotice, renderSettledToast, resolveShotMode, shotCitations, shotExpansionToast, shotLabel, shotInspectorReadiness, shotModeOptionLabel, shotPromptCell, shotSpecificationProblems, shotTakeUrl, songChangeNeedsConfirmation, songContextClearing, songContextClearingQuestion, songContextCount, songContextEditable, songContextFields, songContextRestoreAvailable, songContextRestoreNotice, songContextRestoreRefusal, songContextRestoreTitle, songContextSeedClearedOnLoad, songEncoderCeiling, songImportDuration, songRefusalMessage, threadHtml, unsavedWorkPending, unsavedWorkQuestion, vramEjectAvailable, vramEjectChecked, vramEjectNote, vramEjectTitle, vramEjectToast } from "./api.js";
import { ASSEMBLE_RUNNING, assemblyControl, effectiveOffset, latestAssemblyExport, monitorState, takeAudioControl, trimNudgeControl } from "./api.js";
import { selectedAsset, selectedShot, state } from "./state.js";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
let shotSaveChain = Promise.resolve();
let shotSaveRevision = 0;
// Which automated shot write is in flight, or "" for none. It is what stops a timeline edit made
// *during* one from queueing a whole-list shot save that lands afterwards and reverts everything
// just written -- every prompt, for an expansion; every mode, prompt and citation, for an assistant
// fill. A string rather than a boolean because the two are the same protection with two different
// things to say about it, and a refusal that names the wrong one is a refusal nobody can act on.
let shotWriteInFlight = "";
let waveformLoadRevision = 0;
// The plan's readiness as the server last reported it, or null when nothing has been fetched for
// the project on screen. Fetched on project load rather than only at the click, because readiness
// is a cheap GET and a batch the route will certainly refuse must not look submittable until the
// Director has spent the click on it. Held here rather than on `state` because it is not project
// data: it is derived, never saved, and never sent back.
let readinessReport = null;
let readinessLoadRevision = 0;
// The last H3 expansion this client was refused, as `{shotId, problems, prompt}`, or null. Held
// here for `readinessReport`'s reason -- it is derived, never saved and never sent back -- and
// keyed to its shot, because a report drawn under a different shot's intent would be a false claim
// about the panel it sits in. Cleared the moment an expansion is applied: the answer that failed
// stops being the last thing that happened to that shot.
let lastExpansionReport = null;
// The render poll's timer handle, or 0 while no poll is scheduled. AD-1's transport decision in
// one pair of facts: the interval exists exactly while the loaded project has jobs whose answer
// still lives on ComfyUI, and an idle project holds 0 here and issues no request at all.
let renderPollTimer = 0;
// Whether a poll answer is currently awaited. One tick at a time: a slow answer must not stack a
// second request behind it, and — because ticks are therefore serialized — every snapshot is
// applied in the order the server produced it.
let renderPollInFlight = false;

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

// The VRAM eject setting and what the last attempt did. Its own route rather than a field on
// health, because this is refreshed after every project load -- which is every submission path's
// last step -- and health probes ComfyUI over HTTP on the way through.
//
// A failure leaves whatever was last known on screen and repaints nothing. Before the first
// successful answer that is `null`, which renders as "unknown" with the control disabled: the
// server owns this value, and a box drawn from a guess would report a machine-wide setting the
// renders are not honouring. After that, a transient blip must not blank a setting that has not
// changed. Nothing here is a gate, so nothing here is worth a toast.
async function loadVramEject() {
  try {
    state.vramEject = await api.vramEject();
  } catch {
    // Keep the last known answer; the server remains the authority on the next call.
  }
  renderVramEject();
}

// Painted from `state.vramEject` and from nothing else -- no default, and never from what the
// Director last clicked. That is what makes the environment case honest: with
// MVP_LLM_EJECT_BEFORE_RENDER=0 the server answers `enabled: false` and the box is drawn
// unticked, rather than showing a default the application is not honouring.
function renderVramEject() {
  const status = state.vramEject;
  const control = $(VRAM_EJECT_CONTROL);
  const note = $(VRAM_EJECT_NOTE);
  control.disabled = !vramEjectAvailable(status);
  control.checked = vramEjectChecked(status);
  note.textContent = vramEjectNote(status);
  note.title = vramEjectTitle(status);
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
  // Ahead of the no-project branch too, and for the same reason: the Song context editors are
  // seeded from the project on screen, so a sheet left dirty from the project being left would
  // otherwise stay in the boxes under the next project's name -- or under no project at all.
  //
  // Gated on the project actually changing, like the consent above it. Clearing the flag lets the
  // renderAll below re-seed both boxes from the stored Song, and most callers here are refreshes of
  // the project already on screen -- the queue refresh, both generate paths, multiview, the
  // queue-ready loop -- where that silently deletes a sheet the Director is part-way through
  // pasting. A real switch asks first, through unsavedWorkPending on the selector.
  if (songContextSeedClearedOnLoad(state.project?.id, id)) state.songContextDirty = false;
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
  // Refreshed here rather than in each submission handler, because every path that queues a
  // render reloads the project immediately afterwards -- the queue-ready loop, both generate
  // forms, the shot renders, the queue refresh. One call here therefore reports what the eject
  // did after every submission, and no submission handler needs to know this control exists.
  //
  // A refresh, never a reset. The `apply_documents` consent above is cleared on a project change
  // because it is consent for one turn; this is a standing property of the machine, and clearing
  // it on a project load would silently re-enable an eject the Director turned off.
  loadVramEject();
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

// Exported for the executed frontend contract: tests/test_frontend_contract.py boots this module
// against a stub DOM and calls this, to prove the render path really seeds and enables the Song
// context editors. `renderSongContext` is reached from here and from nowhere else, so a grep for
// the call passes just as happily on a function nothing ever runs; the export lets a test run the
// render and read what came out of it.
export function renderSong() {
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
  renderSongContext();
  const duration = song?.duration || 0;
  $("#quarter-time").textContent = duration ? formatTime(duration * .25).slice(0, 5) : "—";
  $("#half-time").textContent = duration ? formatTime(duration * .5).slice(0, 5) : "—";
  $("#three-quarter-time").textContent = duration ? formatTime(duration * .75).slice(0, 5) : "—";
  $("#end-time").textContent = duration ? formatTime(duration).slice(0, 5) : "—";
  if (state.audioBuffer) drawWaveform($("#waveform"), state.audioBuffer, "#d4f75e");
}

// The loaded song's lyric sheet and style description, seeded from the stored Song.
//
// Not seeded while the Director is typing into them. renderSong runs on far more than a project
// load -- the audio element's `loadedmetadata` alone fires it -- and re-seeding then would silently
// delete a lyric sheet mid-paste. The dirty flag is cleared by a project load and by a successful
// save, which are exactly the two moments the stored text is the truth again.
//
// The controls follow the Song rather than the project: with no song loaded there is nothing to
// describe and the route would 404, so the whole block is disabled instead of offering a save that
// cannot land.
function renderSongContext() {
  const song = state.project?.song;
  const editable = songContextEditable(state.project);
  const lyrics = $("#song-lyrics");
  const style = $("#song-style");
  if (!state.songContextDirty) {
    lyrics.value = song?.lyrics || "";
    style.value = song?.caption || "";
  }
  lyrics.disabled = !editable;
  style.disabled = !editable;
  $("#save-song-context").disabled = !editable;
  // Each restore follows its own field's slot, never a shared flag and never a constant: a save
  // that changed only the lyrics leaves the style description with nothing kept, and offering a
  // restore there is an offer the route refuses with a 422 the Director did nothing to earn.
  // Gated on `editable` as well, because a project with no song has no slots to read at all.
  for (const [field, control] of Object.entries(SONG_CONTEXT_CONTROLS)) {
    const available = editable && songContextRestoreAvailable(song, field);
    const button = $(control.restore);
    button.disabled = !available;
    button.title = songContextRestoreTitle(field, available);
  }
  renderSongContextCounts();
}

// Recovery for one context field: swap the box back to the version kept before the last save that
// changed it. Nothing is sent but the field name -- the kept text lives on the server, and a client
// that supplied it would be inventing the thing it claims to be restoring.
//
// The response re-seeds the editors, so anything typed and unsaved is discarded by it. That text was
// never captured, because only *stored* text becomes a kept version, so the question is asked first
// -- the same gate `restoreDocument` puts in front of the identical loss.
async function restoreSongContext(field) {
  if (!requireProject()) return;
  if (state.songContextDirty && !window.confirm(songContextRestoreQuestion(field))) return;
  try {
    state.project = await api.restoreSongContext(state.project.id, field);
    // Cleared only once the server has answered, and before the render, so the boxes are re-seeded
    // from the restored Song rather than left holding the text that was just swapped out.
    state.songContextDirty = false;
    renderSong();
    toast(songContextRestoreNotice(field));
  } catch (error) {
    toast(error.message, "error");
    // The buttons are disabled unless a version is kept, so this refusal means the loaded project
    // is stale. Refresh, exactly as a document restore and a Song refusal do, or every retry fails
    // identically against the same stale state.
    if (!songContextRestoreRefusal(error.message) || !state.project) return;
    try {
      state.project = await api.project(state.project.id);
      state.songContextDirty = false;
      renderSong();
    } catch {
      // Leave the original error standing; a failed refresh is not new information.
    }
  }
}

// The unsaved-work question for a restore, which is the one thing a restore destroys.
function songContextRestoreQuestion(field) {
  return `Restore the ${SONG_CONTEXT_CONTROLS[field].label.toLowerCase()} from the version kept on the server?\n\nWhat is in the box now is unsaved, so it is not the kept version and it is discarded.`;
}

// How much of its bound the text in each of the four song-context boxes uses, written where the
// Director can read it before spending a click. The boxes carry no `maxlength` -- it truncated a
// paste silently, so an oversized sheet was quietly shortened in the browser while the identical
// text sent to the route came back as a 422 naming its length -- so this count and the route's
// refusal are now the only two things that say anything about the bound, and they say the same
// thing. Every decision is in `songContextCount`, which is executed without a browser.
function renderSongContextCounts() {
  for (const control of SONG_CONTEXT_COUNTS) {
    const counted = songContextCount($(control.field).value, control.limit);
    const element = $(control.count);
    element.textContent = counted.label;
    element.classList.toggle("over", counted.over);
  }
}

async function saveSongContext() {
  if (!requireProject()) return;
  if (!state.project.song) return toast("This project has no song to describe yet.", "error");
  // The same pure mapping the import uses, so the style box lands on `caption` in both places and
  // one crossed assignment cannot exist on only one of the two paths.
  const context = songContextFields($("#song-lyrics").value, $("#song-style").value);
  // The route assigns both fields from the body, so a save with an empty box deletes what is
  // stored -- and a Song, unlike the two creative documents, keeps no previous version to restore.
  // Asked only for that: replacing existing text with nothing. Replacing it with different text is
  // typing, and a question on every save teaches the Director to click through this one.
  const cleared = songContextClearing(state.project.song, context);
  if (cleared.length && !window.confirm(songContextClearingQuestion(cleared))) return;
  try {
    state.project = await api.saveSongContext(state.project.id, context);
    // Cleared only after the server has answered: until then the text on screen is the only copy.
    state.songContextDirty = false;
    renderSong();
    toast("Song context saved; the audio, its length and its provenance were not touched");
  } catch (error) { toast(error.message, "error"); }
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
  // The Monitor follows the clock's transport, not only its position: pausing the master
  // must freeze the picture in the same event, or the video free-runs past the playhead.
  syncMonitor();
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

// Assistant ProducerBot's three controls, repainted from the current selection and the current
// project. Exported for the executed frontend contract, on `renderShotInspector`'s precedent: the
// frozen matrix says the prefill control must be "absent or shut, not a silent no-op" with nothing
// selected, and a test that only read this source could not tell a control that is shut from one
// whose handler happens to return early.
//
// Every state is decided by the pure functions in api.js and applied here and nowhere else -- this
// assigns `disabled` and `title` and nothing more, exactly as the timeline clip applies
// `shotPromptCell`. It runs from renderTimeline, which is what every selection change and every
// project load already goes through, so a shot selected on the timeline repaints the composer.
export function syncAssistantControls() {
  const shot = selectedShot();
  const prefill = prefillControl(state.project, shot);
  $(ASSISTANT_PREFILL_CONTROL).disabled = prefill.disabled;
  $(ASSISTANT_PREFILL_CONTROL).title = prefill.title;
  const single = assistantControl(state.project, shot);
  $(ASSISTANT_FILL_CONTROL).disabled = single.disabled;
  $(ASSISTANT_FILL_CONTROL).title = single.title;
  const bulk = assistantFillAllControl(state.project);
  $(ASSISTANT_FILL_ALL_CONTROL).disabled = bulk.disabled;
  $(ASSISTANT_FILL_ALL_CONTROL).title = bulk.title;
}

// One assistant turn, for whichever control was pressed. The shot ids come from the decision that
// drew the button rather than being re-derived here: re-deriving would be a second copy of the
// rule, and the copy that decided the enabled state is the one the Director actually pressed.
//
// The reply is the whole project, so the timeline, the inspector, the thread and the queue button
// all redraw from it, and the toast is read out of the reply rather than diffed -- a re-fill that
// lands the same mode and the same citations is indistinguishable from a turn where every call was
// refused, and the toast must not claim the first when the reply says the second.
//
// Silent shot saves are shut out for the whole call in the same two halves the expansion uses, and
// through the same flag: awaiting the pending chain drains the saves queued before the click, and
// the in-flight flag refuses the ones a drag would queue during it. A save landing afterwards would
// carry the shot list from before the fill and revert every mode, prompt and citation just written.
async function runAssistantFill(control, shotIds) {
  if (!requireProject()) return;
  if (!shotIds.length) return;
  if (!state.health?.llm?.configured) return toast("Configure MVP_LLM_BASE_URL and MVP_LLM_MODEL to use Assistant ProducerBot.", "error");
  const field = $("#chat-form").elements.message;
  const message = field.value.trim();
  if (!message) return toast(ASSISTANT_WITHOUT_REQUEST, "error");
  // The id this call is sent for, captured before any await: `state.project` is rebound by the
  // response and the project selector stays live throughout.
  const projectId = state.project.id;
  const label = control.textContent;
  control.disabled = true;
  control.textContent = "Filling…";
  shotWriteInFlight = "assistant";
  try {
    await shotSaveChain;
    const filled = await api.assistantFill(projectId, { message, shot_ids: shotIds });
    // The Director switched projects while the model was thinking. The shots are written and saved
    // on the server, so nothing is lost by dropping this reply, whereas applying it here would show
    // one project's work under another's name.
    if (state.project?.id !== projectId) return;
    state.project = filled;
    field.value = "";
    renderAll();
    // A fill writes prompts onto shots, so the report this client holds describes the plan as it
    // was before the call -- including blocks it has just resolved.
    loadReadiness(projectId);
    toast(assistantToast(state.project));
  } catch (error) { toast(error.message, "error"); }
  finally { shotWriteInFlight = ""; control.textContent = label; syncAssistantControls(); }
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

// Exported for the same reason `renderSong` and `renderShotInspector` are: it decides
// whether a control exists at all, and a decision like that has to be executed by a test
// rather than read out of the template string it lives in.
export function renderAssetInspector() {
  const asset = selectedAsset();
  const inspector = $("#asset-inspector");
  if (!asset) {
    inspector.innerHTML = `<span class="eyebrow">Inspector</span><h2>Select an asset</h2><p>Review provenance, generate multiview references, and attach approved assets to shots.</p>`;
    return;
  }
  const url = assetImageUrl(asset);
  const vision = asset.vision ? `<div class="meta-list"><b>Vision summary</b><span>${escapeHtml(asset.vision.summary)}</span><b>Continuity</b><span>${escapeHtml(asset.vision.continuity_cues.join(" · ") || "—")}</span><b>Risks</b><span>${escapeHtml(asset.vision.risks.join(" · ") || "None")}</span></div>` : "";
  // One source for "is this promotable" — the same function the click reads to pick the
  // template — so the button and what it sends can never disagree about the asset's kind.
  const promotion = multiviewPlan(asset);
  // AI Mod, decided by `aiModPlan` (contract-tested) exactly as promotion is decided by
  // `multiviewPlan`: shown for anything image-kinded, shut until the image exists.
  const mod = aiModPlan(asset);
  inspector.innerHTML = `<span class="eyebrow">${escapeHtml(asset.kind)}</span><h2>${escapeHtml(asset.name)}</h2><div class="asset-preview">${url ? `<img src="${url}" alt="${escapeHtml(asset.name)}">` : "Awaiting output"}</div><div class="meta-list"><b>Source</b><span>${escapeHtml(asset.source)}</span><b>Prompt ID</b><span>${escapeHtml(asset.prompt_id || "—")}</span><b>Created</b><span>${new Date(asset.created_at).toLocaleString()}</span></div>${vision}${asset.prompt ? `<label>Generation prompt<textarea rows="7" readonly>${escapeHtml(asset.prompt)}</textarea></label>` : ""}<button class="quiet-button full" id="analyze-asset" ${asset.path && !["audio"].includes(asset.kind) ? "" : "disabled"}>Inspect with vision model</button>${promotion ? `<button class="primary-button full" id="create-multiview" ${promotion.ready ? "" : "disabled"}>Create Krea multiview sheet</button>` : ""}${mod ? `<button class="primary-button full" id="ai-mod-asset" ${mod.ready ? "" : "disabled"} title="Prompt an image edit. A new asset is produced beside this one — keep it, delete it to reject, or mod it again. The source is never changed.">AI Mod (image edit)</button>` : ""}<button class="quiet-button full" id="attach-asset" style="margin-top:8px" ${selectedShot() ? "" : "disabled"}>Attach to selected shot</button>`;
  $("#attach-asset")?.addEventListener("click", attachSelectedAsset);
  $("#create-multiview")?.addEventListener("click", createMultiview);
  $("#ai-mod-asset")?.addEventListener("click", aiModAsset);
  $("#analyze-asset")?.addEventListener("click", async () => {
    try { state.project = await api.analyzeAsset(state.project.id, asset.id); renderAssets(); toast("Vision inspection saved"); }
    catch (error) { toast(error.message, "error"); }
  });
}

async function createMultiview() {
  const asset = selectedAsset();
  if (!asset || !state.project) return;
  // The template is chosen by the asset's kind, not by the one sentence this used to hold:
  // a prop gets the object template, a character gets the character one. Re-checked at the
  // click rather than trusted from render time, because the selection can move under a
  // refresh between the two.
  const promotion = multiviewPlan(asset);
  if (!promotion) return;
  const prompt = promotion.prompt;
  // Shut while its own request is in flight — a generation control, so it gets the generation
  // controls' protection against the second click a silent submission invites. Optional-chained
  // because the reload rebuilds the inspector, so the element may be gone by the finally.
  const projectId = state.project.id;
  const button = $("#create-multiview");
  if (button) button.disabled = true;
  try {
    await api.generateMultiview(projectId, asset.id, { prompt, seed: 0 });
    toast("Krea multiview job queued");
    if (state.project?.id === projectId) await loadProject(projectId);
  } catch (error) { toast(error.message, "error"); }
  finally {
    const control = $("#create-multiview");
    if (control) control.disabled = false;
  }
}

async function aiModAsset() {
  const asset = selectedAsset();
  if (!asset || !state.project) return;
  if (!aiModPlan(asset)?.ready) return;
  // `window.prompt` matches the house `window.confirm` idiom for one-value asks. A plain
  // sentence is enough — the server wraps it in the workflow's own prompting form — and
  // a full structured prompt (starting with subject_definitions:) travels verbatim.
  const instruction = window.prompt(
    `AI Mod — ${asset.name}\n\nDescribe the edit: what should change, and what must stay.\n(GPU render; a new asset appears beside this one.)`,
  );
  if (instruction === null || !instruction.trim()) return;
  const useTurbo = window.confirm(
    "Use the turbo bundle (8 steps — faster, the evidenced turbo export)?\nCancel uses the default 20-step bundle.",
  );
  const projectId = state.project.id;
  const button = $("#ai-mod-asset");
  if (button) button.disabled = true;
  try {
    await api.editAsset(projectId, asset.id, {
      instruction: instruction.trim(),
      profile: useTurbo ? "turbo" : "default",
      seed: 0,
    });
    toast("AI Mod queued — a new asset will land beside the source");
    if (state.project?.id === projectId) await loadProject(projectId);
  } catch (error) { toast(error.message, "error"); }
  finally {
    const control = $("#ai-mod-asset");
    if (control) control.disabled = false;
  }
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
  // Assistant ProducerBot's controls live in the composer, two panels away, and their state is
  // decided by the shot selection this function owns. Repainted from here because every selection
  // change, every project load and every reply already goes through it -- wiring them to the click
  // handler instead would leave them stale after a load, a delete or a lock set elsewhere.
  syncAssistantControls();
  // The whole-plan H3 sweep lives beside the pass-one expansion in the Director workspace, and its
  // only question is whether this plan has any shots -- which is a thing this function owns.
  syncExpansionControls();
  if (state.audioBuffer) drawWaveform($("#timeline-waveform"), state.audioBuffer, "#6f7d3d");
  renderAssembly();
  updateTimelinePlayhead();
}

// Whether an assemble request is currently open, and the last multi-line refusal the server
// answered one with. Both module state for `readinessReport`'s reason -- derived, never saved,
// never sent back -- and the report is cleared by the next attempt or a successful export,
// because the plan it described stops being the plan on screen.
let assemblyInFlight = false;
let assemblyRefusalReport = "";

function renderAssembly() {
  const bar = $("#assembly-bar");
  if (!bar) return;
  if (!state.project) { bar.innerHTML = ""; return; }
  const control = assemblyControl(state.project);
  const disabled = control.disabled || assemblyInFlight;
  const label = assemblyInFlight ? ASSEMBLE_RUNNING : control.label;
  const exported = latestAssemblyExport(state.project);
  // The refusal report is the server's own words, one reason per line -- rendered whole
  // because rationing it is exactly what the comprehensive 422 exists to prevent.
  const report = assemblyRefusalReport
    ? `<div class="assembly-report">${assemblyRefusalReport.split("\n").map((line) => `<div>${escapeHtml(line)}</div>`).join("")}</div>`
    : "";
  const player = exported
    ? `<div class="assembly-export"><span class="eyebrow">Latest export</span><video controls preload="metadata" src="${exported.url}"></video><a href="${exported.url}" target="_blank" rel="noopener">${escapeHtml(exported.path)}</a></div>`
    : "";
  bar.innerHTML = `<div class="assembly-controls"><span class="eyebrow">Assembly</span><button class="primary-button" id="assemble-button" ${disabled ? "disabled" : ""} title="${escapeHtml(control.title)}">${escapeHtml(label)}</button><span class="assembly-reason">${escapeHtml(control.reason)}</span></div>${report}${player}`;
  const button = $("#assemble-button", bar);
  if (button) {
    button.addEventListener("click", async () => {
      if (assemblyInFlight) return;
      assemblyInFlight = true;
      assemblyRefusalReport = "";
      renderAssembly();
      try {
        const result = await api.assemble(state.project.id);
        // The reply is the settled job plus measurements, not the project -- re-fetch so the
        // job list, the export reader and every other panel redraw from one server truth.
        state.project = await api.project(state.project.id);
        toast(`Assembled ${result.clip_count} shots into ${result.export} (${result.duration_seconds.toFixed(2)}s)`);
      } catch (error) {
        assemblyRefusalReport = String(error?.message || error);
        toast("Assembly refused — see the report under the button", "error");
      } finally {
        assemblyInFlight = false;
        renderJobs();
        renderTimeline();
      }
    });
  }
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
    shotCitations(shot).forEach((citation, index) => {
      const asset = assets.get(citation.asset_id);
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
    // Only when the pointer actually moved the shot. A plain selection click is not an edit, and
    // saving one sent the whole shot list back for nothing: the reply to that write reloaded
    // readiness, and the reply to *that* rebuilt the inspector a second time, long after the click
    // looked finished. Comparing against `original` rather than tracking a flag, because a drag
    // that returns to where it started has also changed nothing.
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      if (shot.start !== original.start || shot.duration !== original.duration) saveShotsSilently();
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  });
  clip.addEventListener("dragover", (event) => event.preventDefault());
  clip.addEventListener("drop", (event) => {
    event.preventDefault();
    const id = event.dataTransfer.getData("text/asset-id");
    const shot = state.project.shots.find((item) => item.id === clip.dataset.shotId);
    if (id && !shotCitations(shot).some((citation) => citation.asset_id === id)) {
      shot.citations = [...shotCitations(shot), { asset_id: id, role: "reference", order: shotCitations(shot).length }];
      reconcileShotCitations(shot);
    }
    saveShotsSilently();
    renderTimeline();
  });
}

// This panel is rebuilt by replies nobody awaited -- a readiness report landing after a shot save
// calls `renderTimeline`, which lands here. The rebuild is right: readiness decides the blocked
// flag and the sameness lines, and a panel left alone would keep reporting a block the Director
// has just fixed. So the Director's place is carried across it rather than the rebuild skipped.
// Without this, the caret and every character typed since the last `change` vanished under the
// answer to a request they never made.
//
// Only the focused control, only when it is inside this panel, and only when the panel is being
// redrawn for the same shot -- a rebuild that follows a selection change must show the new shot's
// stored text rather than the previous one's uncommitted edit, which is what the stamp is for.
function captureInspectorEdit(inspector, shotId) {
  const active = document.activeElement;
  if (!active?.id || inspector.dataset?.shotId !== shotId || !inspector.contains?.(active)) return null;
  return { id: active.id, value: active.value, start: active.selectionStart, end: active.selectionEnd };
}

function restoreInspectorEdit(inspector, place) {
  const element = place && $("#" + place.id, inspector);
  if (!element?.focus) return;
  if (place.value !== undefined && element.value !== place.value) element.value = place.value;
  element.focus();
  // `selectionStart` is null on a number input and `setSelectionRange` throws there, so the caret
  // is only restored where the browser reports one.
  if (typeof place.start === "number" && element.setSelectionRange) element.setSelectionRange(place.start, place.end);
}

// The mode select's options. "Not declared" is a real, selectable value rather than a placeholder:
// it is what every shot saved before modes were declarable carries, and a Director who declared a
// mode by accident has to be able to take it back. It names the mode the shot resolves to, because
// "not declared" on its own tells the Director nothing about what pressing render would do.
//
// A mode with no adapter is offered and labelled as such. Hiding it would make the plan unable to
// express a section the Director is really planning; offering it unlabelled would be the one thing
// this must never do -- a mode that looks renderable and is not.
//
// Every option's sentence is `shotModeOptionLabel`, which reads the workflow name off the mode
// table `models.SHOT_MODE_SPECS` mirrors into SHOT_MODES: a renderable mode names the MiniMax
// graph it renders through, and a planned one says so honestly. Nothing about that wording is
// decided in this template.
function shotModeOptions(shot) {
  const declared = SHOT_MODES.some((entry) => entry.value === shot.mode) ? shot.mode : "";
  const resolved = SHOT_MODES.find((entry) => entry.value === resolveShotMode(shot));
  const auto = `<option value="" ${declared ? "" : "selected"}>Not declared — renders as ${escapeHtml(resolved ? resolved.label : "")}</option>`;
  return auto + SHOT_MODES.map((entry) => `<option value="${entry.value}" ${declared === entry.value ? "selected" : ""}>${escapeHtml(shotModeOptionLabel(entry))}</option>`).join("");
}

// One row per citation: what it is, what role it plays in *this* shot, and a way to remove it.
//
// Every role is offered on every row, not only the roles this shot's mode declares. A Director
// re-pointing a shot from references to first/middle/last does it one control at a time, and a
// select that hid `middle` until the mode was already right would make the order of those two
// clicks matter. What the mode declares is reported by `shotSpecificationProblems` instead.
//
// A citation whose asset is gone renders as a row saying so, rather than as nothing. The list used
// to skip it silently, which meant a shot could look like it had dropped an attachment it was in
// fact still sending -- and the route refuses that shot with `Unknown reference asset`, a refusal
// the Director had no way to see coming.
function shotCitationRows(shot, assets) {
  const cited = shotCitations(shot);
  const numbering = { picture: 0, video: 0, audio: 0 };
  return cited.map((citation) => {
    const asset = assets.find((item) => item.id === citation.asset_id);
    let tag = CITATION_MISSING_LABEL;
    if (asset) {
      const kind = asset.kind === "video" ? "video" : asset.kind === "audio" ? "audio" : "picture";
      numbering[kind] += 1;
      tag = `${kind === "video" ? "Video" : kind === "audio" ? "Audio" : "Picture"} ${numbering[kind]}`;
    }
    const roles = Object.entries(ASSET_ROLE_LABELS).map(([role, label]) => `<option value="${role}" ${citation.role === role ? "selected" : ""}>${escapeHtml(label)}</option>`).join("");
    return `<div class="citation-row${asset ? "" : " citation-missing"}"><span class="citation-name">${escapeHtml(tag)}: ${escapeHtml(asset ? asset.name : citation.asset_id)}</span><select class="citation-role" data-id="${citation.asset_id}">${roles}</select><button class="quiet-button remove-ref" data-id="${citation.asset_id}">×</button></div>`;
  }).join("");
}

// Exported for the executed frontend contract, on the `renderSong` precedent: the render-again
// control is drawn, enabled and bound in here, and a test that only read this source could not
// tell a control that is bound to the purpose-built route from one bound to the generic shots
// write -- which is the whole distinction the action exists to make. The harness in
// tests/test_frontend_contract.py boots this module, calls this, and reads what came out.
export function renderShotInspector() {
  const shot = selectedShot();
  const inspector = $("#shot-inspector");
  if (!shot) {
    inspector.innerHTML = `<span class="eyebrow">Shot inspector</span><h2>No shot selected</h2><p>Add a shot to begin. Shots are rendered independently in H3's reliable 4–15 second range.</p>`;
    if (inspector.dataset) inspector.dataset.shotId = "";
    return;
  }
  const place = captureInspectorEdit(inspector, shot.id);
  const assets = state.project.assets || [];
  // The refusal sends the Director here -- "Write a prompt in the shot inspector" -- so the panel
  // has to show which Shot is blocked and why, rather than looking like an ordinary shot with an
  // empty box. The sameness lines are the other half: a near-duplicate pair is only something the
  // Director can differentiate or accept deliberately if it is named where its prompt is edited.
  const readiness = shotInspectorReadiness(readinessReport, shot);
  // Every citation this shot holds, in the one shape the rest of this panel reads. Derived once and
  // before the template, because the attach select filters against it and the rows number against
  // it, and two independent derivations of "what does this shot cite" is how a select starts
  // offering an asset the list below already shows.
  const cited = shotCitations(shot);
  // What this shot is missing or carrying wrongly for its mode, in the server's own sentences.
  // Drawn where the mode is chosen, because that is where it becomes wrong and where it is fixed --
  // and it is a report rather than a gate here for the same reason it is one on the server: a
  // first/middle/last section laid out before its adapter exists is real planning work.
  const specification = shotSpecificationProblems(shot);
  const specificationHtml = specification.length
    ? `<div class="shot-readiness sameness">${specification.map((line) => `<p>${escapeHtml(line)}</p>`).join("")}</div>`
    : "";
  // Whether this shot may be re-opened, and why not when it may not -- decided by
  // `renderAgainControl`, which the contract tests execute for every status and every refusal.
  // Nothing about that decision is re-made in the template below: it applies `shown`, `disabled`
  // and `title` and nothing else, exactly as the clip applies `shotPromptCell`.
  const again = renderAgainControl(shot);
  const againHtml = again.shown
    ? `<button class="quiet-button full" id="render-again" ${again.disabled ? "disabled" : ""} title="${escapeHtml(again.title)}">${escapeHtml(again.label)}</button>${again.reason ? `<p class="control-reason">${escapeHtml(again.reason)}</p>` : ""}`
    : "";
  // AD-5's re-render mark, offered wherever render-again is: flag now, resubmit the whole
  // flagged set later from the queue panel. The flag is the Director's own; nothing infers
  // it, and only a successful resubmission (or this toggle) clears it.
  const flagHtml = again.shown
    ? `<button class="quiet-button full" id="flag-shot" title="${shot.flagged ? "Clear this shot's re-render flag." : "Mark this shot for re-rendering; Re-queue flagged in the render queue resubmits every flagged shot as one batch."}">${shot.flagged ? "Unflag re-render" : "Flag for re-render"}</button>`
    : "";
  // Whether this shot may be committed to the queue or taken back out, decided by
  // `markReadyControl`, which the contract tests execute for every status and every refusal. This
  // is the control the primary journey was missing: `status` defaults to `draft`, the queue button
  // submits only what reads `ready`, and nothing in the interface ever wrote it.
  //
  // The two controls are complementary rather than alternatives -- their status lists partition the
  // vocabulary between them -- so exactly one of them is drawn for any shot, and the template does
  // not choose between them.
  const mark = markReadyControl(shot);
  const markHtml = mark.shown
    ? `<button class="quiet-button full" id="mark-ready" ${mark.disabled ? "disabled" : ""} title="${escapeHtml(mark.title)}">${escapeHtml(mark.label)}</button>${mark.reason ? `<p class="control-reason">${escapeHtml(mark.reason)}</p>` : ""}`
    : "";
  // The take player. Drawn from the Shot's own `latest_output` and from nothing else -- never
  // from whether some element exists -- for `updateShotFromInspector`'s recorded reason: the
  // template decided from the shot, so everything about the take decides from the shot, and the
  // two cannot disagree. The URL carries ids only; the server resolves the file from its own
  // manifest, which is what makes the approval below a decision about evidence rather than about
  // a path this client claimed. Range requests make the scrub bar work, and the server side of
  // that is pinned by its own test.
  const takeHtml = shot.latest_output
    ? `<video id="take-player" class="take-player" controls preload="metadata" src="${escapeHtml(shotTakeUrl(state.project.id, shot.id, shot.latest_output))}"></video>`
    : "";
  // The trim nudge: which slice of the over-rendered take fills the window. Decided by
  // `trimNudgeControl` (contract-tested); frame-stepped here because a frame is the unit
  // the cut actually moves in. Editable on an approved shot by design -- it selects a
  // slice of the approved file; the file itself stays immovable.
  // The acceptance flag: whether this take's own audio joins the mix -- previewed by the
  // Monitor and mixed by assembly through the same field. Decided by `takeAudioControl`.
  const takeAudio = takeAudioControl(shot);
  const takeAudioHtml = takeAudio.shown
    ? `<label class="mix-take-audio-line" title="Accept this take's own audio into the video: the Monitor plays it over the master song and assembly mixes the same slice under the song. Off means only the master song comes through for this clip."><input type="checkbox" id="mix-take-audio" ${takeAudio.checked ? "checked" : ""}> Mix this take's audio under the song</label>`
    : "";
  const nudgeState = trimNudgeControl(shot);
  const nudgeHtml = nudgeState.shown
    ? `<div class="trim-nudge" id="trim-nudge"><span class="control-label" title="The take is rendered longer than the shot's window. The offset is where in the take the window starts: the recorded sync lead plus your nudge. The Monitor previews the same slice assembly will cut.">Trim nudge</span><button class="quiet-button" id="nudge-back" title="One frame earlier">−1f</button><span id="nudge-value">${escapeHtml(nudgeState.nudge.toFixed(3))}s</span><button class="quiet-button" id="nudge-forward" title="One frame later">+1f</button><button class="quiet-button" id="nudge-reset" title="Back to the recorded sync lead" ${nudgeState.nudge === 0 ? "disabled" : ""}>Reset</button><span class="control-reason">cut at ${escapeHtml(nudgeState.offset.toFixed(3))}s into the take (lead ${escapeHtml(nudgeState.lead.toFixed(3))}s)</span></div>`
    : "";
  // Whether this shot's take may be approved or the approval cleared, decided by
  // `approvalControl`, which the contract tests execute for every state. Nothing about that
  // decision is re-made in the template below: it applies `shown`, `disabled` and `title` and
  // nothing else, exactly as the render-again control does.
  const approval = approvalControl(shot);
  const approvalHtml = approval.shown
    ? `<button class="quiet-button full" id="approve-take" ${approval.disabled ? "disabled" : ""} title="${escapeHtml(approval.title)}">${escapeHtml(approval.label)}</button>${approval.reason ? `<p class="control-reason">${escapeHtml(approval.reason)}</p>` : ""}`
    : "";
  // The H3 expansion, drawn under the creative intent it was written from. Decided by
  // `expandPromptControl`, which the contract tests execute for every state and every refusal;
  // nothing about that decision is re-made here.
  //
  // The textarea appears only when this shot has an expansion, and its presence is therefore the
  // panel's answer to "is this shot expanded". An always-empty box on every unexpanded shot would
  // be a second prompt field competing with the intent for the Director's attention, in a panel
  // whose whole difficulty is that `prompt` and `h3_prompt` are not the same thing. It is editable
  // because the frozen block says both fields are independently editable, and it saves through the
  // ordinary shots write like every other field here.
  const expand = expandPromptControl(shot);
  const report = expansionReport(lastExpansionReport, shot);
  const expandHtml = expand.shown
    ? `${shot.h3_prompt ? `<label>H3 structured prompt<textarea id="shot-h3-prompt" rows="10">${escapeHtml(shot.h3_prompt)}</textarea></label>` : ""}<button class="quiet-button full" id="expand-prompt" ${expand.disabled ? "disabled" : ""} title="${escapeHtml(expand.title)}">${escapeHtml(expand.label)}</button>${expand.reason ? `<p class="control-reason">${escapeHtml(expand.reason)}</p>` : ""}${report.shown ? `<div class="shot-readiness blocked" id="expansion-report"><strong>${escapeHtml(report.title)}</strong>${report.problems.map((line) => `<p>${escapeHtml(line)}</p>`).join("")}${report.prompt ? `<textarea rows="8" readonly>${escapeHtml(report.prompt)}</textarea>` : ""}</div>` : ""}`
    : "";
  const readinessHtml = readiness.blocked || readiness.sameness.length
    ? `<div class="shot-readiness ${readiness.blocked ? "blocked" : "sameness"}">${readiness.blocked ? `<strong>${escapeHtml(readiness.flag)}</strong><p>${escapeHtml(readiness.help)}</p>` : ""}${readiness.sameness.map((line) => `<p>${escapeHtml(line.text)}</p>`).join("")}</div>`
    : "";
  inspector.innerHTML = `<span class="eyebrow">Shot inspector</span><h2>${escapeHtml(shot.prompt?.slice(0, 34) || "Untitled shot")}</h2><span class="shot-status">${shot.status}</span>${readinessHtml}<div class="form-row" style="margin-top:14px"><label>Start<input id="shot-start" type="number" min="0" step=".25" value="${shot.start}"></label><label>Duration<input id="shot-duration" type="number" min=".5" step=".25" value="${shot.duration}"></label></div><label>Generation mode<select id="shot-mode">${shotModeOptions(shot)}</select></label>${specificationHtml}<label>Performance<select id="shot-singing">${SINGING_STATES.map((entry) => `<option value="${entry.value}" ${(shot.singing || "unknown") === entry.value ? "selected" : ""}>${escapeHtml(entry.label)}</option>`).join("")}</select></label><label>Creative intent<textarea id="shot-prompt" rows="8">${escapeHtml(shot.prompt)}</textarea></label>${expandHtml}<label>Seed<input id="shot-seed" type="number" min="0" value="${shot.seed}"></label><label>Cited assets<select id="shot-asset-select"><option value="">Attach asset…</option>${assets.filter((asset) => !cited.some((citation) => citation.asset_id === asset.id)).map((asset) => `<option value="${asset.id}">${escapeHtml(asset.name)}</option>`).join("")}</select></label><div class="attached-list">${shotCitationRows(shot, assets)}</div><label class="check-row"><input id="shot-song-audio" type="checkbox" ${shot.use_song_audio ? "checked" : ""}> Use master song as H3 audio reference</label>${takeHtml}${takeAudioHtml}${nudgeHtml}${shot.latest_output ? `<button class="quiet-button full" id="analyze-take">Inspect latest take</button>` : ""}${approvalHtml}${markHtml}${againHtml}${flagHtml}<button class="primary-button full" id="compile-shot" style="margin-top:14px">Compile Director data</button>`;
  if (inspector.dataset) inspector.dataset.shotId = shot.id;
  restoreInspectorEdit(inspector, place);
  ["shot-start", "shot-duration", "shot-mode", "shot-singing", "shot-prompt", "shot-seed", "shot-song-audio"].forEach((id) => $("#" + id).addEventListener("change", updateShotFromInspector));
  // Bound separately and optionally: the H3 box is drawn only for a shot that has an expansion, so
  // adding it to the list above would throw on every shot that does not.
  $("#shot-h3-prompt")?.addEventListener("change", updateShotFromInspector);
  // Attach, re-role and remove all go through `reconcileShotCitations`, which is the client half of
  // the model's own reconciliation. Writing `citations` without it would leave this client drawing a
  // stale `asset_ids` until the next full project load -- the shots write deliberately does not
  // adopt its own reply -- and writing `asset_ids` without it would lose the role.
  $("#shot-asset-select").addEventListener("change", (event) => {
    if (!event.target.value) return;
    shot.citations = [...cited, { asset_id: event.target.value, role: "reference", order: cited.length }];
    reconcileShotCitations(shot);
    saveShotsSilently(); renderTimeline();
  });
  $$(".citation-role", inspector).forEach((select) => select.addEventListener("change", () => {
    shot.citations = shotCitations(shot).map((citation) => citation.asset_id === select.dataset.id ? { ...citation, role: select.value } : citation);
    reconcileShotCitations(shot);
    saveShotsSilently(); renderTimeline();
  }));
  $$(".remove-ref", inspector).forEach((button) => button.addEventListener("click", () => {
    shot.citations = shotCitations(shot).filter((citation) => citation.asset_id !== button.dataset.id);
    reconcileShotCitations(shot);
    saveShotsSilently(); renderTimeline();
  }));
  $("#compile-shot").addEventListener("click", compileSelectedShot);
  $("#analyze-take")?.addEventListener("click", async () => {
    try { state.project = await api.analyzeLatestTake(state.project.id, shot.id); renderTimeline(); toast("Latest take review saved"); }
    catch (error) { toast(error.message, "error"); }
  });
  // Its own route, and it sends no body. The shots write would have done this too -- it is what
  // had to be used by hand on 2026-08-18 -- but that route takes the whole shot list, so a request
  // meaning "let me render this one again" would also reassert every prompt, window and lock this
  // client happens to be holding. There is nothing here for a stale client to overwrite with.
  //
  // The reply is the whole project, so the status, the timeline and the queue button all redraw
  // from it. `renderJobs` as well as `renderTimeline`, because the re-opened shot has just become
  // queueable and the batch button is disabled off exactly that count.
  //
  // The toast is the previous take's fate, in the server's own sentence. A bare "re-opened" would
  // leave the Director believing the application is keeping both takes, which it is not.
  // The step the primary journey did not have. Its own route in each direction, and neither sends
  // a body -- the shots write is the generic full-project one, and it is the only thing that could
  // have done this before, which is exactly why nobody could: a request meaning "render this one"
  // would have reasserted every prompt, window and lock this client is holding.
  //
  // The direction comes off the decision that drew the button rather than from `shot.status` read
  // again here. Re-deriving it would be a second copy of the rule, and the copy that decided the
  // label is the one the Director actually pressed.
  //
  // The reply is the whole project, so the status chip, the timeline and the queue button all
  // redraw from it. `renderJobs` as well as `renderTimeline`, because the shot has just entered or
  // left the set the batch button is enabled off.
  //
  // The toast says what did *not* happen -- nothing rendered, nothing spent, nothing deleted -- in
  // the server's own sentence, because the belief a silent state change leaves in place is that
  // this button started a render.
  $("#mark-ready")?.addEventListener("click", async () => {
    if (!requireProject()) return;
    const projectId = state.project.id;
    try {
      const project = mark.action === "draft"
        ? await api.markShotDraft(projectId, shot.id)
        : await api.markShotReady(projectId, shot.id);
      if (state.project?.id !== projectId) return;
      state.project = project;
      renderTimeline();
      renderJobs();
      toast(markReadyNotice(project, shot.id, mark.action));
    } catch (error) { toast(error.message, "error"); }
  });
  // Pass two for one shot. Its own route and no body, for the reason `render-again` records: the
  // shots write is the generic whole-list one, and a request meaning "expand this one" must not
  // also reassert every prompt, window and lock this client happens to be holding.
  //
  // Silent shot saves are shut out for the call in the same two halves the whole-plan expansion
  // uses, and through the same flag. Both are needed: awaiting the pending chain drains the saves
  // queued *before* the click, and the flag refuses the ones a drag would queue during it -- either
  // would otherwise land a shot list from before the expansion and revert what was just written.
  //
  // The reply carries whether it was applied, so the toast is read off that rather than diffed. A
  // refused answer is not thrown away: it goes into `lastExpansionReport`, keyed to this shot, and is
  // drawn under the intent by the re-render below.
  $("#expand-prompt")?.addEventListener("click", async () => {
    if (!requireProject()) return;
    if (!state.health?.llm?.configured) return toast("Configure MVP_LLM_BASE_URL and MVP_LLM_MODEL to expand a shot's prompt.", "error");
    const projectId = state.project.id;
    const button = $("#expand-prompt");
    const label = button.textContent;
    button.disabled = true;
    button.textContent = "Expanding…";
    shotWriteInFlight = "expansion";
    try {
      await shotSaveChain;
      const result = await api.expandShotPrompt(projectId, shot.id);
      // The Director switched projects while the model was thinking. The expansion is saved on the
      // server, so nothing is lost by dropping this reply, whereas applying it here would show one
      // project's work under another's name.
      if (state.project?.id !== projectId) return;
      const shotLabelText = shotLabel(result.project, shot.id);
      lastExpansionReport = result.applied ? null : { shotId: shot.id, problems: result.problems, prompt: result.prompt };
      state.project = result.project;
      renderTimeline();
      toast(expandPromptToast(result, shotLabelText), result.applied ? "info" : "error");
    } catch (error) { toast(error.message, "error"); }
    finally { shotWriteInFlight = ""; button.disabled = false; button.textContent = label; }
  });
  $("#flag-shot")?.addEventListener("click", () => {
    shot.flagged = !shot.flagged;
    saveShotsSilently();
    renderTimeline();
    renderJobs();
  });
  $("#render-again")?.addEventListener("click", async () => {
    if (!requireProject()) return;
    const projectId = state.project.id;
    try {
      const project = await api.renderAgain(projectId, shot.id);
      if (state.project?.id !== projectId) return;
      state.project = project;
      renderTimeline();
      renderJobs();
      toast(renderAgainNotice(project, shot.id));
    } catch (error) { toast(error.message, "error"); }
  });
  // FR-21's two directions, through their own bodyless routes -- emphatically not the generic
  // shots write, which would let a stale client reassert every prompt, window and lock in the
  // plan under a click that meant "I approve this take". The direction comes off the decision
  // that drew the button rather than from the shot read again here, for `mark-ready`'s reason: a
  // re-derivation is a second copy of the rule, and the copy that decided the label is the one
  // the Director actually pressed.
  //
  // The reply is the whole project, so the status chip, the timeline and both refusable controls
  // beside this one redraw from it -- approving is exactly what flips render-again to its
  // disabled arm. The toast is the server's own sentence about the consequence, because the
  // belief a silent chip change leaves in place is that nothing else moved.
  // The nudge moves in frames and is floored at the recorded lead — the cut can never
  // reach before the take begins; the far end is the server's refusal, with the numbers,
  // because only it measures the take. Saved through the ordinary silent shot save and
  // redrawn, so the Monitor shows the new slice in the same gesture that chose it.
  const applyNudge = (nudge) => {
    shot.trim_nudge = Math.max(nudgeState.minNudge, Math.round(nudge * 24) / 24);
    saveShotsSilently();
    renderTimeline();
  };
  $("#mix-take-audio")?.addEventListener("change", (event) => {
    shot.mix_take_audio = event.target.checked;
    saveShotsSilently();
    renderTimeline();
  });
  $("#nudge-back")?.addEventListener("click", () => applyNudge(nudgeState.nudge - 1 / 24));
  $("#nudge-forward")?.addEventListener("click", () => applyNudge(nudgeState.nudge + 1 / 24));
  $("#nudge-reset")?.addEventListener("click", () => applyNudge(0));
  $("#approve-take")?.addEventListener("click", async () => {
    if (!requireProject()) return;
    const projectId = state.project.id;
    try {
      const project = approval.action === "unapprove"
        ? await api.unapproveTake(projectId, shot.id)
        : await api.approveTake(projectId, shot.id);
      if (state.project?.id !== projectId) return;
      state.project = project;
      renderTimeline();
      toast(approvalNotice(project, shot.id, approval.action));
    } catch (error) { toast(error.message, "error"); }
  });
}

function updateShotFromInspector() {
  const shot = selectedShot();
  shot.start = Math.max(0, Number($("#shot-start").value));
  shot.duration = Math.max(.5, Number($("#shot-duration").value));
  // `null` rather than `""`: an empty select means the Director has not declared a mode, and the
  // model's field is `ShotMode | None` precisely so that "undeclared" is representable. Sending ""
  // would be a validation error, and defaulting it to any mode here would be this client making the
  // declaration on the Director's behalf -- the exact thing the mode exists to stop.
  shot.mode = $("#shot-mode").value || null;
  // Nothing infers this and nothing may. An unset select reads `unknown`, which is not "not
  // singing": the enhancer moves lip position, so a value nobody chose is worse than no value.
  shot.singing = $("#shot-singing").value;
  shot.prompt = $("#shot-prompt").value;
  // Read back only when this shot has an expansion, which is exactly when the box was drawn.
  //
  // The condition is the shot's own field and deliberately not "is the element on the page": the
  // template decided from the shot, so the read-back decides from the shot, and the two cannot
  // disagree. Reading unconditionally is the mutation that matters, and its consequence is worse
  // than a blank: the panel is rebuilt with `innerHTML` while the previous shot's box may still be
  // reachable, so an unrelated edit -- a seed, a checkbox -- would copy ONE SHOT'S EXPANSION ONTO
  // ANOTHER through the whole-list save. `h3_prompt` is the one field this client otherwise carries
  // round-trip without ever touching, so nothing on screen would show it until a render submitted
  // the wrong document.
  if (shot.h3_prompt?.trim()) shot.h3_prompt = $("#shot-h3-prompt").value;
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

// Start or stop the render poll from what is on screen right now. Called from `renderJobs`
// because every path that changes the job list already goes through it -- a project load, every
// submission's reload, a poll tick that settled something -- so the timer follows the jobs
// without any submission handler knowing polling exists. The decision itself is
// `hasActiveRenderJobs`, the executed mirror of the server's own "is anything open" predicate.
//
// Exported for the executed frontend contract, on `renderShotInspector`'s precedent: whether a
// timer is scheduled at all, and stood down when the last job settles, is invisible to a source
// read and is the whole difference between this fix and the silence it replaces.
export function syncRenderPolling() {
  const wanted = Boolean(state.project) && hasActiveRenderJobs(state.project);
  if (wanted && !renderPollTimer) {
    renderPollTimer = setInterval(pollRenderStatus, RENDER_POLL_INTERVAL_MS);
  } else if (!wanted && renderPollTimer) {
    clearInterval(renderPollTimer);
    renderPollTimer = 0;
  }
}

// One reconciliation tick: ask the app's own API what ComfyUI did, patch the project in place,
// and repaint only the surfaces something actually reached. Exported for the executed frontend
// contract, on `renderShotInspector`'s precedent: what has to be provable is that a completed job
// lands on the asset grid, the clips and the queue panel without a click, and that an idle or
// guarded tick sends nothing -- none of which a source read can tell from a loop that never runs.
//
// Three refusals before the request, each load-bearing:
// * no project — nothing to ask about;
// * a tick already in flight — a slow answer must not stack requests behind it;
// * `shotWriteInFlight` — an expansion or assistant fill holds a read-to-save window open
//   (docs/LLM-DIRECTOR.md's known cost), and this loop must neither reload the project under it
//   nor patch shots it is about to rewrite. The tick is skipped, not queued: the next one is at
//   most two seconds away.
//
// The patch is `applyRenderStatus`, never `loadProject`: a reload every two seconds is the
// editor-wiping defect this file keeps having to fix, so only render-facing fields move and the
// inspector rebuild (when a shot really changed) goes through the same focus-preserving
// `renderTimeline` path every other unawaited reply already uses. Failures are silent by design:
// the server answers 200 with `comfy_online: false` while ComfyUI is down, and an unreachable app
// server must not become a toast every two seconds.
export async function pollRenderStatus() {
  if (!state.project || renderPollInFlight || shotWriteInFlight) return;
  // The timer is stood down when the last job settles, but a tick already dispatched -- or a
  // job settled between ticks by the manual refresh -- must refuse on its own: an idle project
  // generates zero requests is the contract, not a usually-true consequence of the timer.
  if (!hasActiveRenderJobs(state.project)) {
    syncRenderPolling();
    return;
  }
  const projectId = state.project.id;
  renderPollInFlight = true;
  try {
    const report = await api.renderStatus(projectId);
    // The Director switched projects, or a guarded write began, while this answer was in
    // flight. Dropping it loses nothing: the reconciliation is saved on the server.
    if (state.project?.id !== projectId || shotWriteInFlight) return;
    const changed = applyRenderStatus(state.project, report);
    if (changed.assets) renderAssets();
    if (changed.song) renderSong();
    if (changed.shots) renderTimeline();
    // `renderJobs` re-runs `syncRenderPolling`, which is how the loop stops itself on the tick
    // that settles the last open job. The explicit call covers a tick that changed nothing
    // visible but should still stand the timer down -- a job settled by the manual refresh.
    if (changed.jobs) renderJobs();
    else syncRenderPolling();
    for (const job of changed.settled) {
      toast(renderSettledToast(state.project, job), job.status === "error" ? "error" : "info");
    }
  } catch {
    // Quiet on purpose; the next tick asks again, and a dead app server is its own banner.
  } finally {
    renderPollInFlight = false;
  }
}

function renderJobs() {
  const jobs = state.project?.jobs || [];
  const list = $("#job-list");
  // The poll follows the job list, and this is the one place every version of that list passes.
  syncRenderPolling();
  // Generate All's whole state, decided by one contract-tested function: the count the
  // confirmation will name (Replace Existing widening it to settled unprotected shots),
  // and the readiness heads-up as a warning rather than a gate -- the server-side batch
  // (FR-4) skips a blocked shot by name and submits the rest.
  const plan = generateAllPlan(state.project, readinessReport, Boolean($("#replace-existing")?.checked));
  $("#queue-ready").disabled = plan.disabled;
  $("#queue-ready").title = plan.title;
  const flagged = (state.project?.shots || []).filter((shot) => shot.flagged);
  const flaggedButton = $("#queue-flagged");
  if (flaggedButton) {
    flaggedButton.hidden = !flagged.length;
    flaggedButton.textContent = `Re-queue flagged (${flagged.length})`;
  }
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
  shotWriteInFlight = "expansion";
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
  finally { shotWriteInFlight = ""; button.disabled = false; button.textContent = label; }
}

// Pass two over the whole plan: one model call per shot, on the server, each judged on its own.
// Deliberately the same shape as `expandShotPrompts` above rather than a new one -- the project id
// captured before the await, the pending save chain drained, the in-flight flag held for the whole
// call, the reply adopted only if the Director is still looking at the project it answers -- because
// it is the same hazard for longer. This call is N model calls rather than one, so every window it
// leaves open is open N times as long.
//
// The reply is the whole project, so the document editors are re-rendered from it and the same
// unsaved-edits gate every other server-overwrites-the-editors path asks is asked here. One
// question and not two: what pressing this costs -- one model call per shot -- is on the button's
// own hover text before the click, where a Director deciding whether to press it can read it,
// rather than in a dialog that fires after they already have.
//
// The toast is read out of the reply, not diffed off the shots: `h3_prompt` is not drawn on the
// timeline at all, so there is nothing on screen for a diff to be checked against.
async function expandPlanPrompts() {
  if (!requireProject()) return;
  if (!state.project.shots.length) return toast(EXPAND_ALL_PROMPTS_WITHOUT_SHOTS, "error");
  if (!state.health?.llm?.configured) return toast("Configure MVP_LLM_BASE_URL and MVP_LLM_MODEL to expand shots into H3 prompts.", "error");
  if (!confirmDiscardingDocumentEdits(`Expand all ${state.project.shots.length} shot(s) into H3 prompts? That is one model call per shot and takes a while. No creative intent is overwritten, but the whole project comes back, so the editors are re-rendered from the text stored on the server.`)) return;
  const projectId = state.project.id;
  const button = $(EXPAND_ALL_PROMPTS_CONTROL);
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Expanding…";
  shotWriteInFlight = "expansion";
  try {
    await shotSaveChain;
    const expanded = await api.expandPlanPrompts(projectId);
    if (state.project?.id !== projectId) return;
    state.project = expanded;
    // The report the panel is holding describes a single-shot call from before this sweep, and the
    // sweep has just answered for that shot again.
    lastExpansionReport = null;
    markDocumentsSaved();
    renderAll();
    toast(expandAllPromptsToast(state.project));
  } catch (error) { toast(error.message, "error"); }
  finally { shotWriteInFlight = ""; button.disabled = false; button.textContent = label; syncExpansionControls(); }
}

// The sweep control's state, repainted from the project on screen. Its own function rather than a
// line inside `syncAssistantControls` because the two answer different questions -- that one is
// about the shot *selection*, this one about whether the plan has any shots at all -- and it is
// called from `renderTimeline` for the same reason: every selection change, project load and reply
// already goes through it.
export function syncExpansionControls() {
  const control = $(EXPAND_ALL_PROMPTS_CONTROL);
  if (!control) return;
  const sweep = expandAllPromptsControl(state.project);
  control.disabled = sweep.disabled;
  control.title = sweep.title;
}

function saveShotsSilently() {
  if (!state.project) return Promise.resolve();
  // Refused, not queued: this save carries the whole shot list as it was before the write, so
  // landing it afterwards reverts everything just written while the success toast is still on
  // screen. Said out loud because the edit really is not saved and the response re-renders the
  // timeline over it, and named for whichever write is running, because the two have different
  // remedies.
  if (shotWriteInFlight) {
    toast(shotWriteInFlight === "assistant" ? ASSISTANT_EDIT_BLOCKED : SHOT_EXPANSION_EDIT_BLOCKED, "error");
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
  syncMonitor();
}

// How far the Monitor's video may drift from the master clock before it is snapped back.
// Two frames: below the boundary-switch granularity, above the cost of re-seeking a video
// element every timeupdate for jitter nobody can see.
const MONITOR_DRIFT_SECONDS = 2 / 24;

// The take URL currently loaded in the Monitor's video element, so a playhead move inside
// one shot seeks instead of reloading, and a move across shots swaps the source once.
let monitorLoadedUrl = "";
// The two line mutes from the Director's workflow: audition controls, session-only by
// design -- never persisted, never touching the manifest. Muting the song never stops
// the clock; the element keeps driving the playhead with its output silenced.
let songLineMuted = false;
let videoLineMuted = false;

function syncMonitor() {
  const video = $("#monitor-video");
  if (!video || !state.project) return;
  const frame = $("#timeline-monitor");
  const audio = $("#master-audio");
  audio.muted = songLineMuted;
  // One decision function owns what this moment shows -- the same offset rule assembly
  // cuts by, so the preview and the export cannot disagree about which slice plays.
  const view = monitorState(state.project, state.playhead);
  if (videoLineMuted) {
    frame.classList.remove("showing-take");
    $("#monitor-overlay").textContent = "Video line muted";
    if (!video.paused) video.pause();
    return;
  }
  if (view.kind !== "take") {
    frame.classList.remove("showing-take");
    $("#monitor-overlay").textContent = view.label;
    if (!video.paused) video.pause();
    return;
  }
  frame.classList.add("showing-take");
  // The acceptance flag, previewed: an accepted clip's own audio plays over the master,
  // exactly the mix assembly writes.
  video.muted = view.muted;
  const url = shotTakeUrl(state.project.id, view.shot.id, view.shot.latest_output);
  if (url !== monitorLoadedUrl) {
    monitorLoadedUrl = url;
    video.src = url;
  }
  if (Math.abs(video.currentTime - view.takeTime) > MONITOR_DRIFT_SECONDS) {
    video.currentTime = view.takeTime;
  }
  // The master audio element is the clock; the video is a view of it. Muted playback of a
  // muted element is allowed to autoplay everywhere, so play() here cannot be refused for
  // the reason unmuted media is.
  if (audio.paused) {
    if (!video.paused) video.pause();
  } else if (video.paused) {
    video.play().catch(() => {});
  }
}

function attachSelectedAsset() {
  const asset = selectedAsset();
  const shot = selectedShot();
  if (!asset || !shot) return;
  if (!shotCitations(shot).some((citation) => citation.asset_id === asset.id)) {
    shot.citations = [...shotCitations(shot), { asset_id: asset.id, role: "reference", order: shotCitations(shot).length }];
    reconcileShotCitations(shot);
  }
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
    // Every kind of unsaved work, not only what the project save covers: the switch re-seeds the
    // Song context editors from the project being loaded, so an unsaved lyric sheet is discarded
    // here exactly as an unsaved document edit is, and it used to go without a question.
    if (unsavedWorkPending(state) && !window.confirm(unsavedWorkQuestion("Discard unsaved changes and switch projects?", state))) {
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
    // The import block's own two fields, never the Music 3 form's lyrics textarea: that one is
    // generation input for a song being written, these describe a song that already exists.
    const context = songContextFields($("#import-lyrics").value, $("#import-style").value);
    form.append("lyrics", context.lyrics); form.append("caption", context.caption);
    // Only this file's own measurement counts. Anything decoded for another file --
    // or for the project's stored song -- is not this import's length.
    form.append("duration", songImportDuration(state.pendingImport?.file === file ? state.pendingImport : null));
    form.append("confirm_song_replacement", String(change.confirmed));
    try {
      state.project = await api.uploadSong(state.project.id, form);
      state.pendingImport = null;
      // The stored Song is now the truth about its own context, so the editors below re-seed from
      // it, and the import boxes are emptied: a sheet left sitting in them would be sent again by
      // the next import of a different track.
      state.songContextDirty = false;
      $("#import-lyrics").value = ""; $("#import-style").value = "";
      renderAll();
      toast("Song imported");
    }
    catch (error) { await recoverFromSongRefusal(error); }
  });
  $("#save-song-context").addEventListener("click", saveSongContext);
  // Both restores route through the one function rather than reimplementing the call, and they are
  // bound from the one control table so a field's selector is never respelled at the bind site.
  for (const [field, control] of Object.entries(SONG_CONTEXT_CONTROLS)) {
    $(control.restore).addEventListener("click", () => restoreSongContext(field));
  }
  // Typing marks the editors dirty so no incidental re-render overwrites them; a landed save, an
  // import, a removal and a load that actually changes project are the only things that clear it.
  ["song-lyrics", "song-style"].forEach((id) => $("#" + id).addEventListener("input", () => { state.songContextDirty = true; }));
  // Every bounded box keeps its own counter current, the import block's included: those two are not
  // seeded from anything, so a render is not what puts a number under them.
  SONG_CONTEXT_COUNTS.forEach((control) => $(control.field).addEventListener("input", renderSongContextCounts));
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
      // The song those two editors described is gone, so what is in them describes nothing; left
      // dirty they would sit there disabled, showing context for a song this project no longer has.
      state.songContextDirty = false;
      renderAll();
      toast("Song removed from the project; the audio file was left on disk");
    }
    catch (error) { await recoverFromSongRefusal(error); }
  });
  const musicForm = $("#music-form");
  // The encoder ceiling the two duration fields multiply out to, redrawn from `songEncoderCeiling`
  // on every keystroke in either box. Neither field bounds the other -- the schema bounds their
  // product, so the product is what is shown -- and this line is the only thing that makes that
  // product visible before the submit that would otherwise learn it from a 422.
  const syncMusicCeiling = () => {
    const ceiling = songEncoderCeiling(
      musicForm.elements.duration.value,
      musicForm.elements.duration_headroom.value,
    );
    const note = $("#music-ceiling");
    note.textContent = ceiling.text;
    note.classList.toggle("over", ceiling.exceeds);
  };
  // Thin DOM applier: every decision (which bounds, which way to clamp, which controls a preset
  // even has) lives in musicFormFieldUpdate, which is unit-tested without a browser.
  const syncMusicVariant = () => {
    const update = musicFormFieldUpdate(musicForm.elements.preset.value, {
      duration: musicForm.elements.duration.value,
      duration_headroom: musicForm.elements.duration_headroom.value,
      seed: musicForm.elements.seed.value,
    });
    const lyricsField = musicForm.elements.lyrics;
    lyricsField.closest("label").style.display = update.lyricsVisible ? "" : "none";
    lyricsField.disabled = !update.lyricsVisible;
    lyricsField.required = update.lyricsRequired;
    // The whole block -- box, ceiling readout and the note naming the two node inputs -- goes
    // together, and the box is disabled with it: `required` on a hidden field would block the
    // direct Music 3 submit with a validation message pointing at a control nobody can see.
    $("#music-headroom-field").style.display = update.headroomVisible ? "" : "none";
    musicForm.elements.duration_headroom.disabled = !update.headroomVisible;
    for (const [name, bounds] of Object.entries(update.numeric)) {
      const field = musicForm.elements[name];
      field.min = bounds.min;
      field.max = bounds.max;
      field.value = bounds.value;
    }
    syncMusicCeiling();
  };
  syncMusicVariant();
  musicForm.elements.preset.addEventListener("change", syncMusicVariant);
  musicForm.elements.duration.addEventListener("input", syncMusicCeiling);
  musicForm.elements.duration_headroom.addEventListener("input", syncMusicCeiling);
  musicForm.addEventListener("submit", async (event) => {
    event.preventDefault(); if (!requireProject()) return;
    const data = Object.fromEntries(new FormData(event.currentTarget));
    const plan = musicGenerationPlan(data);
    if (data.preset === "songplanner-known" && !plan.body.lyrics) {
      return toast("Paste the lyric sheet, or switch to the invented-lyrics preset.", "error");
    }
    // Refused here, before the replacement question and before the GPU-cost question, for the same
    // reason the server refuses before `comfy.submit`: the product of the duration and the headroom
    // leaving the encoder's schema range is arithmetic the browser already has every number for,
    // and spending two confirmations to be told it by a 422 is a worse way to find out. Same
    // sentence the readout beside the fields is already showing, and the same numbers the route
    // would name. Nothing is clamped -- the Director is told which of their two numbers to move.
    if (plan.endpoint === "songplanner") {
      const ceiling = songEncoderCeiling(plan.body.duration, plan.body.duration_headroom);
      if (ceiling.refusal) return toast(ceiling.refusal, "error");
    }
    // Both generate routes assign the new Song at submit time, before any audio exists,
    // so the consequence is asked here rather than when the job completes. This is a
    // separate question from the GPU-cost confirm below, which is about render time.
    const change = confirmSongChange("Queue song generation? It replaces this project's song as soon as the job is submitted.");
    if (!change.proceed) return;
    plan.body.confirm_song_replacement = change.confirmed;
    // Shut while its own request is in flight — the same protection the Flux form carries, for
    // the same reason: a song generation with no visible consequence invites the second click
    // that queues the identical job. The project id is captured before the awaits because the
    // selector stays live throughout.
    const projectId = state.project.id;
    const button = $("#music-submit");
    // The same re-entrancy refusal the Flux submit makes: in flight means shut, whatever
    // fired the event.
    if (button.disabled) return;
    const label = button.textContent;
    button.disabled = true;
    button.textContent = "Queuing…";
    try {
      if (plan.endpoint === "songplanner") {
        if (!window.confirm("Queue SongPlanner generation? It loads the 12B Gemma-3 planner plus the Music 3 stack and can use significant GPU time.")) return;
        await api.generateSongPlanner(projectId, plan.body);
        toast("SongPlanner job queued");
      } else {
        await api.generateMusic(projectId, plan.body);
        toast("Music 3 job queued");
      }
      if (state.project?.id === projectId) await loadProject(projectId);
    }
    catch (error) { toast(error.message, "error"); }
    finally { button.disabled = false; button.textContent = label; }
  });
  // Shut while its own request is in flight, like every other control that spends something.
  // This is the double-render defect itself: the form holds a fixed seed, so with no feedback a
  // second click queued the identical image — the live submit button was the invitation. The id
  // this render belongs to is captured before the await, because the project selector stays live
  // and a submission must land on the project the Director was looking at when they clicked.
  $("#flux-form").addEventListener("submit", async (event) => {
    event.preventDefault(); if (!requireProject()) return;
    const data = Object.fromEntries(new FormData(event.currentTarget));
    const [width, height] = data.aspect.split("x").map(Number);
    const projectId = state.project.id;
    const button = $("#flux-submit");
    // A browser refuses to submit a form whose submit button is disabled, but nothing obliges
    // every event path to be a browser's: the same refusal is made here, so a second submission
    // while the first is in flight is impossible rather than merely unlikely.
    if (button.disabled) return;
    const label = button.textContent;
    button.disabled = true;
    button.textContent = "Queuing…";
    try {
      await api.generateFlux(projectId, { name: data.name, kind: data.kind, prompt: data.prompt, width, height, steps: Number(data.steps), guidance: Number(data.guidance), seed: Number(data.seed) });
      toast("Flux image job queued");
      // The reload is what puts the new asset card on the grid as RENDERING and starts the
      // render poll that will land its image — the "something happened" the silence lacked.
      if (state.project?.id === projectId) await loadProject(projectId);
    }
    catch (error) { toast(error.message, "error"); }
    finally { button.disabled = false; button.textContent = label; }
  });
  $("#populate-timeline").addEventListener("click", async () => {
    if (!requireProject()) return;
    // The Director's own warning, word for word with the server's refusal: replaces every
    // shot, first run or deliberate redo. The server enforces the same acknowledgement.
    if (!window.confirm("Populate Timeline lays out the whole plan from the Song, Treatment and Assets. EVERY existing shot is replaced and unsaved timeline work is lost. Intended for a first run on an empty timeline, or for deliberately redoing the plan. Continue?")) return;
    const projectId = state.project.id;
    const button = $("#populate-timeline");
    button.disabled = true;
    try {
      const report = await api.populateTimeline(projectId, true);
      if (state.project?.id === projectId) {
        state.project = report.project;
        renderTimeline();
        renderJobs();
      }
      toast(`Timeline populated: ${report.created} shots laid out (${report.proposed} proposed by the model)`);
    } catch (error) { toast(error.message, "error"); }
    finally { button.disabled = false; }
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
  // Prefill writes the selected shot's context into the composer and sends nothing. The existing
  // text is kept and follows the context rather than being replaced: the context is the preamble
  // and the Director's own sentence is the request, so a Director who typed first must not lose it
  // to a click on a convenience.
  $(ASSISTANT_PREFILL_CONTROL).addEventListener("click", () => {
    const prefill = prefillControl(state.project, selectedShot());
    if (prefill.disabled) return;
    const field = $("#chat-form").elements.message;
    const typed = String(field.value || "").trim();
    field.value = typed ? `${prefill.text}${typed}` : prefill.text;
    field.focus?.();
  });
  // Two sends, one turn each, and neither is this form's submit: `Send to Director` is a
  // conversation and these write to shots, so pressing one must never be a way of doing the other.
  // The scope comes off the control's own decision -- see `runAssistantFill`.
  $(ASSISTANT_FILL_CONTROL).addEventListener("click", (event) => {
    const single = assistantControl(state.project, selectedShot());
    if (single.disabled) return;
    return runAssistantFill(event.currentTarget, single.shotIds);
  });
  $(ASSISTANT_FILL_ALL_CONTROL).addEventListener("click", (event) => {
    const bulk = assistantFillAllControl(state.project);
    if (bulk.disabled) return;
    return runAssistantFill(event.currentTarget, bulk.shotIds);
  });
  $("#send-treatment").addEventListener("click", () => document.querySelector('[data-panel="treatment"]').click());
  $("#expand-shot-prompts").addEventListener("click", expandShotPrompts);
  // Pass two, beside pass one and in that order on screen, because that is the order the two run
  // in: pass one lays the shots out so they flow together and writes each one's intent, pass two
  // turns each intent into H3's structured format one call at a time.
  $(EXPAND_ALL_PROMPTS_CONTROL).addEventListener("click", expandPlanPrompts);
  $("#add-shot").addEventListener("click", () => {
    if (!requireProject()) return;
    const shots = state.project.shots;
    const start = shots.length ? Math.max(...shots.map((shot) => shot.start + shot.duration)) : 0;
    // The placeholder comes from the one constant the readiness rule reads, because the server
    // blocks exactly this string: a second spelling here would create Shots the timeline draws as
    // prompted and the route then refuses.
    const shot = { id: `shot_${crypto.randomUUID().replaceAll("-", "").slice(0, 12)}`, start, duration: Math.min(5, Math.max(.5, projectDuration() - start)), prompt: PLACEHOLDER_PROMPT, mode: null, asset_ids: [], citations: [], singing: "unknown", seed: 0, status: "draft", prompt_id: "", approved_output: "", locked: false };
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
  $("#mute-song").addEventListener("click", () => {
    songLineMuted = !songLineMuted;
    $("#mute-song").classList.toggle("muted-line", songLineMuted);
    syncMonitor();
  });
  $("#mute-video").addEventListener("click", () => {
    videoLineMuted = !videoLineMuted;
    $("#mute-video").classList.toggle("muted-line", videoLineMuted);
    syncMonitor();
  });
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
    const replace = Boolean($("#replace-existing")?.checked);
    // Re-decided at the click from the same function that drew the button, so the count
    // the Director confirms is the count the request means. The server enforces the
    // confirmation too (confirm_gpu), so this dialog is the acknowledgement it names.
    const plan = generateAllPlan(state.project, readinessReport, replace);
    if (plan.disabled || !window.confirm(plan.confirm)) return;
    const projectId = state.project.id;
    const button = $("#queue-ready");
    button.disabled = true;
    try {
      const report = await api.generateBatch(projectId, {
        confirm_gpu: true,
        replace_existing: replace,
      });
      toast(batchReportToast(report), report.submitted.length ? "info" : "error");
      if (state.project?.id === projectId) await loadProject(projectId);
    } catch (error) { toast(error.message, "error"); }
    finally { renderJobs(); }
  });
  $("#queue-flagged").addEventListener("click", async () => {
    if (!requireProject()) return;
    const flagged = (state.project?.shots || []).filter((shot) => shot.flagged);
    if (!flagged.length) return;
    if (!window.confirm(`Resubmit ${flagged.length} flagged shot${flagged.length === 1 ? "" : "s"} as one batch? Each successful resubmission clears that shot's flag.`)) return;
    const projectId = state.project.id;
    try {
      const report = await api.generateBatch(projectId, { confirm_gpu: true, scope: "flagged" });
      toast(batchReportToast(report), report.submitted.length ? "info" : "error");
      if (state.project?.id === projectId) await loadProject(projectId);
    } catch (error) { toast(error.message, "error"); }
    finally { renderJobs(); }
  });
  // The checkbox changes what the button would submit, so the button's own label and
  // count redraw in the same gesture.
  $("#replace-existing").addEventListener("change", renderJobs);
  // The whole of the control. It sends the box's own value -- never a hardcoded one, which would
  // make the control decorative in one direction and unusable in the other -- and repaints from
  // the server's reply, so a refused change reverts the box instead of leaving it showing a
  // setting no render will honour. The same revert the document lock toggles do.
  //
  // Nothing about a render happens here. The setting reaches submissions through the server's
  // pre-submission hook, so no failure on this path can reach a render: the worst case is a
  // control that did not change.
  $(VRAM_EJECT_CONTROL).addEventListener("change", async (event) => {
    const control = event.currentTarget;
    const wanted = control.checked === true;
    control.disabled = true;
    try {
      state.vramEject = await api.setVramEject(wanted);
      toast(vramEjectToast(state.vramEject));
    } catch (error) {
      toast(error.message, "error");
    }
    renderVramEject();
  });
  window.addEventListener("resize", () => { if (state.audioBuffer) { renderSong(); renderTimeline(); } });
  // The same predicate the project switch asks: a tab closed on an unsaved lyric sheet loses it as
  // completely as a project switch does, and the browser's own dialog is the only warning left.
  window.addEventListener("beforeunload", (event) => { if (unsavedWorkPending(state)) event.preventDefault(); });
}

// The manual refresh, now one reconciliation call instead of a per-job GET fan-out -- forty open
// jobs used to be forty requests that each fetched ComfyUI's queue again, which is exactly the
// shape AD-1 forbids. The full project reload stays: unlike the 2 s poll this is a click, a
// decision to resynchronise everything, and it is the path that picks up server-side changes the
// poll's narrow patch deliberately leaves alone.
async function refreshJobs() {
  if (!state.project) return;
  const projectId = state.project.id;
  try {
    const report = await api.renderStatus(projectId);
    if (state.project?.id !== projectId) return;
    await loadProject(projectId);
    toast(
      report.comfy_online
        ? "Queue refreshed"
        : "Queue refreshed, but ComfyUI could not be reached; job states are as last known",
      report.comfy_online ? "info" : "error",
    );
  } catch (error) { toast(error.message, "error"); }
}

async function init() {
  bindEvents();
  await Promise.all([loadHealth(), loadVramEject(), api.workflows().catch(() => [])]);
  try { await loadProjects(); } catch (error) { toast(error.message, "error"); }
}

init();
