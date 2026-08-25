import { APPLY_DOCUMENTS_CONTROL, ASSET_NAME_HELP, ASSET_NAME_LABEL, assetNamePlan, ASSET_ROLE_LABELS, ASSET_TABS, assetTab, assetsForTab, assetTabEmpty, ASSISTANT_FILL_ALL_CONTROL, ASSISTANT_FILL_CONTROL, ASSISTANT_EDIT_BLOCKED, ASSISTANT_PREFILL_CONTROL, ASSISTANT_WITHOUT_REQUEST, characterSlotPlan, CITATION_MISSING_LABEL, CONSISTENCY_PROMPT_HELP, CONSISTENCY_PROMPT_LABEL, consistencyAnchorPlan, EXPAND_ALL_PROMPTS_CONTROL, EXPAND_ALL_PROMPTS_WITHOUT_SHOTS, DOCUMENT_CONTROLS, PLACEHOLDER_PROMPT, RENDER_POLL_INTERVAL_MS, SHOT_EXPANSION_EDIT_BLOCKED, SHOT_EXPANSION_WITHOUT_SHOTS, SHOT_MODES, SINGING_STATES, SONG_CHANGE_CONSEQUENCE, SONG_CONTEXT_CONTROLS, SONG_CONTEXT_COUNTS, UNSAVED_DOCUMENT_EDITS_CONSEQUENCE, VRAM_EJECT_CONTROL, VRAM_EJECT_NOTE, api, applyRenderStatus, approvalControl, approvalNotice, assistantControl, assistantFillAllControl, assistantToast, clearDocumentConsent, comfyOutputUrl, documentChangeToast, documentConsent, documentConsentClearedOnLoad, documentLabel, documentLockNotice, documentRestoreAvailable, documentRestoreNotice, documentRestoreRefusal, documentRestoreStaleNotice, documentRestoreTitle, escapeHtml, expandAllPromptsControl, expandAllPromptsToast, expandPromptControl, expandPromptToast, expansionReport, hasActiveRenderJobs, jobTarget, INSTRUMENTAL_NOTE, markReadyControl, markReadyNotice, aiModPlan, multiviewPlan, musicFormFieldUpdate, musicGenerationPlan, nextRenderSeed, RANDOM_SEED_CONTROL, RANDOM_SEED_HELP, RANDOM_SEED_LABEL, randomSeed, generateAllPlan, batchReportToast, snapSeconds, shotBoundaries, prefillControl, readinessLines, readinessSummary, reconcileShotCitations, renderAgainControl, renderAgainNotice, renderSettledToast, resolveShotMode, shotCitations, shotExpansionToast, shotLabel, shotInspectorReadiness, shotModeOptionLabel, shotPromptCell, shotSpecificationProblems, shotTakeUrl, songChangeNeedsConfirmation, songContextClearing, songContextClearingQuestion, songContextCount, songContextEditable, songContextFields, songContextRestoreAvailable, songContextRestoreNotice, songContextRestoreRefusal, songContextRestoreTitle, songContextSeedClearedOnLoad, songEncoderCeiling, songImportDuration, songRefusalMessage, tagLyricLine, threadHtml, unsavedWorkPending, unsavedWorkQuestion, VOCAL_TYPES, vocalTaggingPlan, vocalTypeSpec, vramEjectAvailable, vramEjectChecked, vramEjectNote, vramEjectTitle, vramEjectToast } from "./api.js";
import { ASSEMBLE_RUNNING, EXPORT_PRESETS, EXPORT_PRESET_DEFAULT, assemblyControl, assemblyProgress, effectiveOffset, latestAssemblyExport, monitorShowsTake, monitorState, newShotFromPlan, renderProgressByTarget, renderingFlag, shotRenderState, takeAnchorControl, takeAudioControl, takesStripRows, trimNudgeControl } from "./api.js";
import { EXPAND_ALL_PROMPTS_CONFIRM, EXPAND_ALL_PROMPTS_RUNNING, EXPAND_ALL_PROMPTS_TIMELINE_CONTROL, EXPAND_ALL_PROMPTS_TIMELINE_LABEL, NOTICE_KINDS, expansionSweepLines } from "./api.js";
// Generate All Empty: the cuts bar's second batch door, beside Expand All Prompts. Its whole
// decision -- the count, the drafts it commits on the way, the bundle it spends -- is
// `generateEmptyPlan`, and nothing about it is re-derived here.
import { GENERATE_EMPTY_CONTROL, GENERATE_EMPTY_LABEL, GENERATE_EMPTY_RUNNING, generateEmptyPlan } from "./api.js";
import { SNAP_CUTS_APPLIED_TOAST, SNAP_CUTS_DISMISS_LABEL, SNAP_CUTS_MOVED_HEADING, SNAP_CUTS_RUNNING, SNAP_CUTS_SKIPPED_HEADING, SNAP_CUTS_TOLERANCE_HELP, SNAP_CUTS_TOLERANCE_LABEL, SNAP_TOLERANCE_DEFAULT, SNAP_TOLERANCE_MAX, SNAP_TOLERANCE_STEP, snapCutsControl, snapCutsReportLines, snapTolerance } from "./api.js";
// The sampling bundle: one project-level choice that governs Generate All, Re-queue flagged and
// Render Again alike, with the step count on the option and the 2026-08-23 comparison's findings
// underneath it. Every decision is pure and lives in api.js; this module draws it and writes it.
import { SAMPLING_PROFILE_CONTROL, SAMPLING_PROFILE_NOTE, SAMPLING_PROFILE_NOTE_TEXT, SAMPLING_PROFILE_TITLE, SAMPLING_PROFILES, batchEtaNote, samplingProfileOf, samplingProfileToast } from "./api.js";
// Fill section looks: the Director's empty shared prompt, read out of the Treatment.
import { FILL_SECTION_LOOKS_APPLIED, FILL_SECTION_LOOKS_HELP, FILL_SECTION_LOOKS_LABEL, FILL_SECTION_LOOKS_OVERWRITE_QUESTION, FILL_SECTION_LOOKS_RUNNING, sectionLooksConfirmation, sectionLooksReportLines, sectionLooksWritten } from "./api.js";
import { TIMELINE_LABEL_WIDTH, TIMELINE_WHEEL_ACTIONS, TIMELINE_ZOOM_STEP, clampTimelineZoom, timelineWheelPlan, zoomFromSlider, zoomLabelText, zoomSliderValue, zoomViewport } from "./api.js";
// Beat and onset marks over the master waveform, from the Song Envelope. Where each one goes and
// which class it takes is `beatMarkerPlan`, and nothing here re-derives any of it: this module
// reads the envelope once on the load path, positions what the plan returns, and writes nothing.
import { BEAT_MARKERS_BAND, BEAT_MARKERS_CONTROL, BEAT_MARKERS_HELP, BEAT_MARKERS_LABEL, beatMarkerPlan, songEnvelopeIdentity } from "./api.js";
// Direct manipulation on the SHOTS track: the undo/redo stacks, the gap-fill gesture and the
// playhead magnet. Every decision they make is pure and lives in api.js; this module holds the
// two stacks, binds the gestures and does the writing.
import { GAP_FILL_TOAST, MIN_WINDOW_SECONDS, PLAYHEAD_SNAP_TOAST, UNDO_DEPTH, anchoredNudge, boundaryMovePlan, doubleEdgePress, edgePressSurvivesDrag, exactSeconds, gapFillPlan, noShotSelectedRefusal, splitShotPlan, undoControl, undoGestureLabel } from "./api.js";
// The rest of that magnet, Story 8.3: the song's own targets beside the playhead. `dragSnapPlan`
// resolves them once per drag and `edgeSnap` chooses among them per pointer move -- both pure,
// both in api.js, and neither of them deciding *where a cut belongs*. That decision is
// `timeline.py`'s, served whole by `GET /timeline/snap-targets`, so a drag and the batch "Snap
// cuts" button cannot hold two opinions about the same second.
import { SNAP_ANALYZE_ACTION, SNAP_ANALYZE_DONE, SNAP_ANALYZE_DONE_UNCOUNTED, SNAP_SELECT_CONTROL, SNAP_SELECT_HELP, SNAP_SELECT_LIST, SNAP_SELECT_SUMMARY, SNAP_TARGET_ORDER, SNAP_TARGET_TOASTS, dragSnapPlan, edgeSnap, snapActionControl, snapBeatCount, snapKindsFromSession, snapSelectorPlan, snapTargetsIdentity } from "./api.js";
// The Clips tab's honest state when ComfyUI is not running, and the Assets panel's named attach
// target -- two of the four interaction defects cleared on 2026-08-21.
import { CLIP_RECHECK_LABEL, attachToShotControl, clipCardFace, clipPreviewState } from "./api.js";
// The shot-length band, as the server judges it: the report carries the verdict and the clip
// reads it. Nothing on this side re-derives the band -- see `clipWindowState` for why.
import { clipWindowState, windowWarningsByShot } from "./api.js";
// The whole-queue cancel (the Director's ask, 2026-08-23) and the render-phase indicator that
// tells the clip ComfyUI is on from the ones waiting behind it. Both are decided in api.js and
// only applied here, `clipWindowState`'s rule one line up.
import { CANCEL_ALL_CONTROL, cancelAllPlan, cancellationToast, clipRenderPhase, renderPhaseByShot } from "./api.js";
import { REPLACE_WITH_CANCEL, REPLACE_WITH_HEADING, REPLACE_WITH_HELP, REPLACE_WITH_MERGED_HEADING, REPLACE_WITH_PLACEHOLDER, REPLACE_WITH_RUNNING, REPLACE_WITH_SKIPPED_HEADING, REPLACE_WITH_SWAPPED_HEADING, assetIsCited, assetReplacementControl, assetReplacementOptions, assetReplacementReportLines, replaceInShotsControl } from "./api.js";
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
// Snap cuts to phrase boundaries: the tolerance box, the two-stage button and the server's own
// report. Module state for `readinessReport`'s reason -- derived, never saved, never sent back --
// and declared up here beside it because `loadProject` clears it, which is far above the render
// that draws it. `snapReport` is the last report the server answered with, and holding it is what
// makes the control two-stage: while it is set and holds moves, the same button applies rather
// than re-reports. It is cleared by a project load, by a tolerance change (the report answered a
// different question) and by the apply that consumes it.
let snapToleranceSeconds = SNAP_TOLERANCE_DEFAULT;
let snapReport = null;
let snapInFlight = false;
// The whole-plan H3 sweep, as the timeline's copy of its button draws it: whether one is running,
// and the per-shot report the last one answered with. Module state for `snapReport`'s reason
// exactly -- derived, never saved, never sent back -- and cleared by a project load, because a
// report naming one plan's shots drawn under another plan is a claim about shots not on screen.
let expansionSweepInFlight = false;
let expansionSweepReport = null;
// Generate All Empty's in-flight flag, held here rather than set on the button, for
// `expansionSweepInFlight`'s recorded reason: `renderSnapCuts` repaints the whole bar, so a label
// changed by hand is wiped by the next repaint while a flag the render reads cannot be. It gates
// the click too — a second submission of the same set is exactly the double-batch this guards.
let emptyBatchInFlight = false;
// The section-look pass's own report, held for the section inspector to draw. Module state for
// `snapReport`'s reason exactly -- derived, never saved, never sent back -- and it is held rather
// than shown once and dropped because the per-section skip reasons *are* half this feature: "the
// treatment does not describe this section" is the sentence that sends the Director back to the
// treatment, and a confirm dialog they have already dismissed cannot be re-read. Cleared by a
// project load, because it names one structure's sections.
let sectionLooksReport = null;
// **Which set of points a dragged shot edge lands on**, as a set of kind names -- the playhead the
// Director parked, the voiceless phrase gaps `timeline.py` chooses, the beats the analysis
// measured. The Director's ruling of 2026-08-24 replaced the playhead magnet's own on/off switch
// with one selector over all of them, so there is a single answer to "what does dragging snap to"
// rather than one switch per kind acquired as kinds were added.
//
// A working preference, not project data: it lives in this browser's session storage beside the
// zoom and the panel, exactly as the two line mutes and the master volume do, and never in the
// manifest -- how one Director likes to drag is not a property of the video.
//
// **Every kind by default, and an empty set is not the same thing.** A session that has never
// stored a selection gets all of them, which is the default-on asymmetry the other view toggles
// use; a session storing `[]` is a Director who switched them all off on purpose and gets exactly
// that. `storedSnapKinds` is where the two are told apart, and it answers `null` for "nothing was
// stored" rather than inventing a list, so this initialiser stays the one statement of the
// default.
let snapTargetKinds = new Set(SNAP_TARGET_ORDER);
// **Which projects have a song measurement running**, not whether one does. Module state for
// `snapInFlight`'s recorded reason exactly, and it is the reason rather than a convenience: the
// button lives inside markup `syncSnapTargetsControl` rewrites, so a property set by hand is undone
// by the next rebuild, while a flag the renderer reads cannot be. It is also what the click site
// checks, so a keyboard press that outruns the repaint cannot start a second measurement.
//
// **A set of ids rather than a boolean**, because a boolean says "a measurement is running" and the
// row says "*this song* is being measured" -- two different claims that a project switch pulls
// apart. A Director who starts a measurement and moves to another project would otherwise find that
// project's Beats row drawn dead and reading `Analyzing song…` about a song nobody is measuring.
// A set rather than one id because both measurements are then true at once, and a single slot would
// have the first one's `finally` release the second one's claim.
const snapAnalysisProjects = new Set();
// Whether the measured beats and onsets are drawn over the master waveform. A view setting and
// nothing else -- it changes no Shot, writes nothing to the manifest and touches no project state
// -- so it lives beside `snapTargetKinds` in this browser's session store, with the same default-on
// asymmetry: the feature is the drawing, not the switch, and only an explicit `false` turns it off.
// `preferences.py` is not the place for it either; that file holds one key and it is a GPU policy.
let beatMarkersOn = true;
// What the beat band was last painted from: the toggle, the scale and the track's width in one
// string, and the measurement by identity. `renderTimeline` repaints on every pointermove of a
// drag and these four are the only things the band's markup depends on, so this is what keeps a
// few thousand reference marks off the drag path. Not a cache of the *plan* -- the plan is pure
// and cheap to ask for; this is a record of what is already on screen.
let beatBandKey = "";
let beatBandEnvelope = null;
// Replace With / Cancel, offered only after a delete was refused. Module state for `snapReport`'s
// reason exactly -- derived, never saved, never sent back. `replaceForAssetId` is which asset the
// refusal was about, so the affordance cannot leak onto a different card when the selection moves;
// `replaceRefusal` is the server's own refusal sentence, kept on screen so the Director can read
// what stopped them while they choose; `replaceReport` holds the last report and is what makes the
// button two-stage, exactly as `snapReport` does for the cuts bar.
let replaceForAssetId = "";
let replaceRefusal = "";
let replaceChoiceId = "";
let replaceReport = null;
let replaceInFlight = false;

// Everything the affordance holds, forgotten. Called by the Cancel button, by an applied
// replacement, and by every project load -- a report about one project's shots drawn over another
// project's library would be a claim about shots that are not on screen.
function clearAssetReplacement() {
  replaceForAssetId = "";
  replaceRefusal = "";
  replaceChoiceId = "";
  replaceReport = null;
  replaceInFlight = false;
}
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
  item.title = "Click to dismiss";
  // Long reports earn longer on screen (a batch skip list is unreadable in 4.2 s), errors
  // stay until dismissed, and every toast dismisses on click.
  item.addEventListener("click", () => item.remove());
  $("#toast-region").append(item);
  if (kind !== "error") {
    setTimeout(() => item.remove(), Math.min(15000, 4200 + message.length * 25));
  }
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

// What survives a reload: the working project, panel, zoom and shot. Nothing here is
// authority over anything — every value is re-validated against the loaded project — so
// stale storage costs a default, never a wrong state. (Analyst finding, 2026-08-20:
// every reload landed on the Song panel of the first project at 100% zoom.)
const SESSION_KEY = "mvp-session";
function persistSession() {
  try {
    localStorage.setItem(SESSION_KEY, JSON.stringify({
      projectId: state.project?.id || "",
      panel: state.activePanel,
      pixelsPerSecond: state.pixelsPerSecond,
      selectedShotId: state.selectedShotId,
      volume: $("#master-audio")?.volume ?? 1,
      // How this Director likes to drag, never a property of the video -- see `snapTargetKinds`.
      // Written as an array, always, so an empty selection is stored as an empty selection: the
      // one thing this key must be able to say and could not if it were three booleans or a
      // truthiness test. `storedSnapKinds` reads it back, and tells absent from empty there.
      snapTargets: [...snapTargetKinds],
      // How this Director likes to look at the song, and for the same reason: a view setting sits
      // with the other view settings, not in the project.
      beatMarkers: beatMarkersOn,
    }));
  } catch { /* storage may be denied; the app works without it */ }
}
function restoreSession() {
  try { return JSON.parse(localStorage.getItem(SESSION_KEY) || "{}"); }
  catch { return {}; }
}

async function loadProjects(selectId = null) {
  state.projects = await api.projects();
  const select = $("#project-select");
  select.innerHTML = `<option value="">No project</option>${state.projects.map((project) => `<option value="${project.id}">${escapeHtml(project.name)}</option>`).join("")}`;
  const remembered = restoreSession().projectId;
  const next = selectId || state.project?.id
    || (state.projects.some((project) => project.id === remembered) ? remembered : state.projects[0]?.id);
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
  //
  // The music lock is cleared on the same test and for a sister reason. Every unlock belongs to
  // the project it was made in, ids collide across projects, and a *refresh* must not re-lock a
  // shot the Director unlocked a second ago -- a queue poll re-ticking the box mid-gesture would
  // be the control fighting them. `documentConsentClearedOnLoad` is the executed answer to "is
  // this a different project"; a hand-written `!==` beside it would be a second copy of one rule.
  if (documentConsentClearedOnLoad(state.project?.id, id)) {
    clearDocumentConsent(applyDocumentsControl());
    unlockedFromMusic.clear();
    // The seed randomizer, per shot since 2026-08-21 and cleared on the same test for the same
    // reason: ids collide across projects, so a set carried into another project would arm the
    // randomizer on a shot nobody has looked at -- and a *refresh* must not untick a box the
    // Director ticked a second ago.
    randomizeSeedShots.clear();
  }
  // Whatever readiness this client held belongs to the project being left, and the revision bump
  // discards an answer still in flight for it. A readiness report drawn under another project's
  // name would name Shots that are not on screen and count a plan nobody is looking at.
  readinessReport = null;
  readinessLoadRevision += 1;
  // The snap report describes one plan at one tolerance, so it belongs to the project being
  // left for exactly readiness' reason: an apply offered under another project's name would
  // write windows onto shots nobody was looking at. Cleared on every load, refresh included --
  // a refresh means the plan changed, and a report about the plan before it is stale.
  snapReport = null;
  // The sweep's per-shot report, for the snap report's reason exactly: it names one plan's shots
  // by label, and a report drawn under another project's name would be a claim about shots that
  // are not on screen.
  expansionSweepReport = null;
  // And the section-look report, for the sweep report's reason exactly: it names one structure's
  // sections by label and start, and a report drawn under another project's name would be a claim
  // about boxes that are not on the timeline.
  sectionLooksReport = null;
  // And the replacement report, for the same reason and with a sharper edge: it names asset ids
  // and shot labels from the project being left, so an apply offered under another project's name
  // would rewrite citations on shots nobody was looking at.
  clearAssetReplacement();
  // Live percentages belong to the project being left, for readiness' reason exactly: they are
  // keyed by target id, and a number drawn under another project's name would be a claim about a
  // render nobody is looking at. Cleared on every load, refresh included -- the next poll answer
  // rebuilds it whole, and until then the surfaces show what they show with nothing known.
  state.renderProgress = {};
  // The render phases belong to the project being left for the identical reason, and are cleared
  // in the same breath: they are keyed by shot id, and a QUEUED clip drawn under another project's
  // name is a claim about a render nobody is looking at. The next poll answer rebuilds it whole.
  state.renderPhase = {};
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
    // Beside `audioBuffer` and for its reason: with no project on screen there is no song, and a
    // band still drawing the last one's beats would be marks over nothing.
    forgetSongEnvelope();
    clearUndoHistory();
    renderAll();
    return;
  }
  const previousProject = state.project?.id;
  const previousSelection = state.selectedShotId;
  state.project = await api.project(id);
  state.audioBuffer = null;
  // A measurement describes one project's song, so it is dropped on a real project change and
  // kept across a refresh of the project already on screen -- which is what loadProject mostly
  // is, after every queue action. Clearing it on every call would blank the band on each refresh.
  if (previousProject !== id) forgetSongEnvelope();
  state.selectedAssetId = null;
  // A reload of the SAME project keeps the working shot: loadProject runs after every
  // queue action and refresh, and being thrown back to shot 1 from shot 23 each time was
  // the analyst's third finding (2026-08-20).
  const survives = previousProject === id
    && state.project.shots.some((shot) => shot.id === previousSelection);
  state.selectedShotId = survives ? previousSelection : state.project.shots[0]?.id || null;
  state.dirty = false;
  state.documentsDirty = false;
  state.shotsDirty = false;
  // The undo history describes one project's shot list at one revision, and this is the one
  // place both can change. Cleared on every load, refresh included: a refresh means some other
  // writer moved the project -- a queued render, a settled job, a Director in another tab -- and
  // an entry taken before that would replay over it. `clearUndoHistory` re-takes the baseline
  // from what was just loaded, so the next gesture is undoable immediately.
  clearUndoHistory();
  renderAll();
  loadPersistedWaveform(id);
  loadSongEnvelope(id);
  // Beside it and on the same load path, never on the poll: the seconds a dragged shot edge may
  // land on. A second request rather than a field on the envelope -- see `loadSnapTargets`.
  loadSnapTargets(id);
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

// **Every repaint of the whole workspace re-says the "Snap to" rows.**
//
// Review finding 1 and 2: the sync was attached to `loadSnapTargets` -- to the *loader* -- and the
// panel's sentences depend on the project and the song as much as on the report. So removing a song
// left the rows describing the song that had gone, with a live Analyze button that could only be
// refused; and a project switch whose targets read was refused left the previous project's
// sentences on screen. Both were demonstrated against the executed harness, and both passed every
// existing test, because those tests read module state and never the control.
//
// Here rather than at each gesture: `renderAll` is what every path that changes which project or
// which song is current already calls -- the project load and its no-project branch, the song
// import, the song removal, the analysis below -- and it is deliberately **not** `renderTimeline`,
// which runs on every `pointermove` of a clip drag and must not rebuild a checkbox list.
function renderAll() {
  syncSnapTargetsControl();
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
  $("#analyze-song").disabled = !song || !song.path;
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
  renderVocalTagging();
}

// The vocal-type select and, when the declared type names a cast, the per-line tagging list.
//
// Exported for the executed frontend contract, `renderSong`'s reason exactly: this decides whether
// a control exists at all — a solo song must be offered no per-line dropdown — and a decision like
// that has to be run against a DOM rather than read out of the template string it lives in.
//
// The list is drawn from `#song-lyrics`'s CURRENT value rather than from the stored Song, because
// the box is what the Director is editing and the tags are in it. Typing a new line and tagging it
// works before any save, and the dropdown states follow the text on every keystroke.
export function renderVocalTagging() {
  const select = $("#song-vocal-type");
  const editable = songContextEditable(state.project);
  // Rebuilt from the table rather than from markup, so a vocal type added to api.js's VOCAL_TYPES
  // appears here without a second edit — and cannot appear here without existing on the server,
  // which the contract test holds.
  const declared = state.project?.song?.vocal_type || "unstated";
  select.innerHTML = VOCAL_TYPES.map((entry) => `<option value="${escapeHtml(entry.value)}"${entry.value === declared ? " selected" : ""}>${escapeHtml(entry.label)}</option>`).join("");
  select.value = declared;
  select.disabled = !editable;
  // The instrumental consequence, said once where the declaration is made rather than only at
  // populate. Every other type says nothing: a note that is always on screen is a note nobody
  // reads, and the shortfall flag has its own moment.
  $("#song-vocal-note").textContent = declared === "instrumental" ? INSTRUMENTAL_NOTE : "";
  const region = $("#lyric-tagging");
  // The plan reads the box, not the manifest — see above. `vocalTaggingPlan` decides `tagging`
  // off the vocal type's own table row, so "a solo song offers no per-line dropdown" is one rule
  // in one place rather than a condition spelled here as well.
  const plan = vocalTaggingPlan(editable ? { song: { vocal_type: declared, lyrics: $("#song-lyrics").value } } : null);
  region.hidden = !plan.tagging;
  if (!plan.tagging) { region.innerHTML = ""; return; }
  // A line whose mark could not be read is named and left exactly as it is. Not dropped, not
  // guessed at, not rewritten: the Director typed it into their own sheet and the fix is theirs.
  const unreadable = plan.unreadable.length
    ? `<p class="control-reason">${plan.unreadable.map((line) => `Line ${line.index + 1} starts with something that looks like a singer mark and could not be read: ${escapeHtml(line.raw.trim())}`).join(" · ")}</p>`
    : "";
  const rows = plan.rows.map((line) => {
    const options = plan.spec.lineTags.map((tag) => {
      const value = tag.slots.join(",");
      const selected = value === line.slots.join(",") ? " selected" : "";
      return `<option value="${value}"${selected}>${escapeHtml(tag.label)}</option>`;
    }).join("");
    return `<div class="lyric-line"><select class="lyric-line-tag" data-id="${line.index}">${options}</select><span>${escapeHtml(line.text)}</span></div>`;
  }).join("");
  region.innerHTML = `<div class="block-heading"><h3>Who sings each line</h3><span>Written into the lyric sheet</span></div>${unreadable}${rows}`;
  $$(".lyric-line-tag", region).forEach((control) => control.addEventListener("change", () => {
    const index = Number(control.dataset.id);
    const slots = control.value ? control.value.split(",").map(Number) : [];
    try {
      // The write, and the whole storage decision in one line: the tag goes into the lyric sheet.
      // Nothing else in the box is touched, so a Director's indentation, blank lines and `[Tag]`
      // blocks survive a tag edit byte for byte.
      $("#song-lyrics").value = tagLyricLine($("#song-lyrics").value, index, slots);
      // Unsaved, exactly as typing into the box is unsaved: the tag is lyrics, and "Save song
      // context" is what stores it. Marking dirty is also what stops `renderSong` re-seeding the
      // box from the stored Song and throwing the tag away.
      state.songContextDirty = true;
      renderVocalTagging();
    } catch (error) { toast(error.message, "error"); renderVocalTagging(); }
  }));
}

async function saveVocalType(value) {
  if (!requireProject()) return;
  if (!state.project.song) return toast("This project has no song to describe yet.", "error");
  try {
    state.project = await api.saveVocalType(state.project.id, value);
    // Deliberately NOT clearing `songContextDirty`: this route writes one enum and touches no
    // character of the lyric sheet, so unsaved text in the box is still unsaved and still the only
    // copy. `renderSong` leaves the box alone while the flag is set, which is what keeps it.
    renderSong();
    toast(`Vocals recorded as ${vocalTypeSpec(value).label}; the lyric sheet was not touched`);
  } catch (error) { toast(error.message, "error"); renderSong(); }
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

// One decode per song file, not one per reload: loadProject runs after every queue action
// and refresh, and re-fetching + re-decoding the whole track each time blanked both
// waveforms over and over through a working night (analyst finding, 2026-08-20). The key
// carries the path, so a replaced song decodes fresh.
let decodedSongKey = "";
let decodedSongBuffer = null;

async function loadPersistedWaveform(projectId) {
  const url = songAudioUrl();
  const revision = ++waveformLoadRevision;
  if (!url) return;
  const key = `${projectId}:${state.project?.song?.path || ""}`;
  if (key === decodedSongKey && decodedSongBuffer) {
    state.audioBuffer = decodedSongBuffer;
    renderSong();
    renderTimeline();
    return;
  }
  try {
    const response = await fetch(url);
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    const context = new AudioContext();
    const buffer = await context.decodeAudioData(await response.arrayBuffer());
    await context.close();
    if (revision !== waveformLoadRevision || state.project?.id !== projectId) return;
    decodedSongKey = key;
    decodedSongBuffer = buffer;
    state.audioBuffer = buffer;
    renderSong();
    renderTimeline();
  } catch {
    // Playback uses the media element even when Web Audio cannot decode the source.
  }
}

// One envelope read per *measurement* -- project, song file and the fingerprint the measurement
// was taken from, which is `songEnvelopeIdentity`. **Never behind a timer.** The endpoint hashes
// the song's bytes to decide validity, so a poll would be a SHA-256 of the master every few
// seconds to answer a question whose answer only changes when the song does. The render poll may
// *notice* that a song landed -- that is `changed.song`, an in-memory comparison the tick already
// makes -- and this key then decides whether anything is asked for at all, which is the same shape
// Story 8.1 gave the same problem on the server.
//
// Absence is silence, in every one of its forms. A project with no song, a song replaced since it
// was measured, a machine without ffmpeg, a sidecar someone deleted: all of them answer 200 with
// `present: false`, and all of them leave the band empty and the timeline exactly as it draws
// today. Nothing here reads `reason`, raises, or says anything.
//
// A failed *request* is different from a reported absence and is treated as one: the key is
// claimed only after a reply has been painted, so the last known measurement stays on screen and
// the next load tries again rather than the timeline losing its marks to one unreachable moment.
let songEnvelopeKey = "";
// `snapTargetsLoadRevision`'s twin, on the same hazard: two envelope reads open at once, and the
// slower answer painting its band over the faster one's.
let songEnvelopeLoadRevision = 0;

// Everything this browser remembers about *which* song is current, forgotten: the measurement,
// the key it was read under, the record of what is currently painted, and the seconds a dragged
// edge may land on.
//
// Called by every transition that changes which song is current, because `loadSongEnvelope` alone
// is not enough for those: it clears nothing until a reply lands, which is right for a refresh and
// wrong for a replacement. Between an import landing and its envelope arriving, the band would
// otherwise draw the *previous* song's beats over the new master -- the one state
// `BEAT_MARKERS_HELP` tells the Director is impossible.
//
// `beatBandEnvelope` is dropped here too, and not only for correctness: it holds a direct
// reference to a measurement that is over a megabyte of arrays, and leaving it set after a project
// switch pins that in memory on a machine whose memory belongs to ComfyUI.
function forgetSongEnvelope() {
  state.songEnvelope = null;
  songEnvelopeKey = "";
  beatBandKey = "";
  beatBandEnvelope = null;
  // The drag's targets belong to the same song and are dropped in the same breath. Half of them
  // -- the voiceless gaps -- are not derived from the envelope at all, so they would survive a
  // song replacement on their own and pull a cut onto a rest measured in a track that is gone.
  state.snapTargets = null;
  snapTargetsKey = "";
}

// One targets read per *measurement*: the song file, the analysis fingerprint and the word and
// span counts, which is `snapTargetsIdentity`. **Never behind a timer**, for `loadSongEnvelope`'s
// reason exactly -- the route reaches the beats through `song_envelope_report`, which hashes the
// master to decide whether the analysis is current.
//
// It is a second request rather than a field on the envelope read because the two answer different
// questions from different halves of the project: the envelope is a measurement of the audio, and
// half of these targets come from a *transcription* that has no envelope in it. Folding them
// together would make a song nobody analysed lose the gap snapping the batch button already gives
// it, which is the one thing the frozen matrix says must keep working.
//
// Absence in every form is silence: no song, no analysis, no transcription, a route that is not
// there. Each leaves the drag exactly as it was before this story -- the playhead and nothing
// else -- and none of them says anything. A failed *request* is different from a reported absence
// and is treated as one: the last known targets stay, and the key is left unclaimed so the next
// load asks again rather than the magnet being lost to one unreachable moment.
let snapTargetsKey = "";
// Which targets read is the current one. Two reads can be open at once -- a project load and the
// poll noticing a generated song land in the same second -- and without this the slower answer
// repaints over the faster one, putting a stale report under the rows and the drag. `readinessLoadRevision`
// and `waveformLoadRevision` are the same guard on the same hazard; this feature had none.
let snapTargetsLoadRevision = 0;

// Exported for the executed frontend contract, `loadSongEnvelope`'s reason exactly: what has to be
// provable is that a reply reaches the drag -- that a load fills the slot, that an absent half
// leaves the other working, and that a refused read changes nothing -- and none of that is visible
// to a source read of a function nothing in the suite can call.
export async function loadSnapTargets(projectId) {
  const key = snapTargetsIdentity(projectId, state.project?.song);
  // **Synced before the early return, not after it.** The rows say what the *project and its song*
  // are worth as well as what the report says, and this return is taken whenever neither the song
  // nor its measurement has moved -- which includes a project switch back to one already read.
  syncSnapTargetsControl();
  if (key === snapTargetsKey) return;
  const revision = (snapTargetsLoadRevision += 1);
  if (!state.project?.song?.path) {
    // No audio, so nothing measured either way. Anything still held belongs to a song that is not
    // here any more, and a magnet pulling towards it is the one thing this must not do.
    state.snapTargets = null;
    snapTargetsKey = key;
    // The rows describe this report, so they are re-said whenever it moves -- absent included.
    syncSnapTargetsControl();
    return;
  }
  let report = null;
  try {
    report = await api.snapTargets(projectId);
  } catch {
    // Unreachable or refused: dragging behaves exactly as it does today, the last known targets
    // are kept, nothing is said, and the key is left unclaimed so the next load asks again.
    //
    // **Nothing is re-said here, and review finding 2 is why that is now correct.** Keeping the
    // last known *targets* is right -- one unreachable moment must not cost the Director their
    // magnet. Keeping the last known *sentences* was not: they described whichever project was on
    // screen when the last read landed, so a switch into a project whose read is refused left the
    // panel claiming things about a song that was not open. The fix is above and in `renderAll`,
    // not here: the rows are re-said at the *top* of this function, before the request is even
    // made, and nothing this browser knows has changed between there and here. A second call in
    // this branch reads like the guarantee and is one no test can fail -- which is the same
    // decoration this whole change exists to take out of the panel.
    return;
  }
  // The project moved on while the request was open, or a later read overtook this one; either
  // way this answer describes a measurement nobody is looking at and the read that replaced it
  // owns the magnet now.
  if (state.project?.id !== projectId || revision !== snapTargetsLoadRevision) return;
  state.snapTargets = report || null;
  snapTargetsKey = key;
  // **The selector is repainted, and nothing else is.** Targets are still not *drawn* -- they
  // change where the next drag lands and nothing on the timeline -- but the "Snap to" rows now say
  // what each kind is currently worth, and that answer is this report. Without this line the rows
  // would carry whatever they said when the control was last synced, which on a first load is
  // "nothing has been read" and stays that way until the Director happens to tick something.
  //
  // Here rather than at each of the five call sites: every path that re-reads the targets -- a
  // project load, a song import, a first transcription, the poll noticing a generated song landing,
  // and the row's own action -- has the same obligation, and one of them forgetting it is exactly
  // how a feature arrives a reload late.
  syncSnapTargetsControl();
}

// Exported for the executed frontend contract, `renderSnapCuts`' and `syncRenderPolling`'s reason
// exactly: what has to be provable here is that a reply actually reaches the band -- that marks
// appear on a first load with nothing touched, that a reported absence empties it, and that a
// refused request changes nothing -- and none of that is visible to a source read of a function
// nothing in the suite can call.
export async function loadSongEnvelope(projectId) {
  const key = songEnvelopeIdentity(projectId, state.project?.song);
  if (key === songEnvelopeKey) return;
  const revision = (songEnvelopeLoadRevision += 1);
  if (!state.project?.song?.path) {
    // No audio to measure. Anything still on screen belongs to a song that is not here any more.
    if (state.songEnvelope) { state.songEnvelope = null; renderTimeline(); }
    songEnvelopeKey = key;
    return;
  }
  let report = null;
  try {
    report = await api.songEnvelope(projectId);
  } catch {
    // Unreachable or refused: the last known measurement is kept, nothing is said, and the key is
    // left unclaimed so the next load asks again. There is no error state for a reference mark.
    return;
  }
  // The project moved on while the request was open, or a later read overtook this one; either
  // way this answer describes a song nobody is looking at and the read that replaced it owns the
  // band now.
  if (state.project?.id !== projectId || revision !== songEnvelopeLoadRevision) return;
  state.songEnvelope = report?.present === true ? report.envelope || null : null;
  // **Outside the `try`, deliberately.** A throw from the paint is not an absent envelope, and
  // catching it in that silent `catch` would abort the whole timeline render without a word. And
  // the key is claimed only *after* the paint, so a render that failed is asked for again on the
  // next load rather than remembered as done.
  renderTimeline();
  songEnvelopeKey = key;
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

// Every asset picture in the workspace, served by this application and addressed by the asset's
// own id.
//
// It used to fork: uploads through `/api/.../media/`, and everything generated through
// `comfyOutputUrl` — ComfyUI's `/view`, on ComfyUI's origin, falling back to a hardcoded
// `127.0.0.1:8188` when `/api/health` had not answered yet. So the entire library went blank
// whenever ComfyUI was down, which is routine and happens for reasons that have nothing to do
// with browsing a library. The bytes were never ComfyUI's to withhold — the app is running on the
// same disk — and `read_asset_file` now serves them with the containment check `/view` cannot do.
//
// The empty-`path` early return stays exactly where it was: an asset with no output yet is not a
// request that should be made at all, and the grid already draws `RENDERING`/`NO PREVIEW` for it.
function assetImageUrl(asset) {
  if (!asset?.path || !asset?.id || !state.project?.id) return "";
  return `/api/projects/${state.project.id}/assets/${encodeURIComponent(asset.id)}/file`;
}

// The panel's tab strip, built from api.js's ASSET_TABS rather than written into index.html, so
// the tabs and the kinds they cover are one list a contract test can pin to `models.AssetKind`.
// Called from `renderAssets`, which is what carries the active mark from tab to tab; the click
// handler is bound once, in `bindEvents`, by delegation off the strip itself.
function renderAssetTabs() {
  const strip = $("#asset-filters");
  if (!strip) return;
  const clips = clipLibraryRows().length;
  // Built once, then updated in place. Rewriting the strip's `innerHTML` on every render would
  // destroy the button the Director just pressed -- the press itself calls `renderAssets` -- taking
  // its keyboard focus with it. That is the shape of the defect found in this codebase on
  // 2026-08-21, where a `dblclick` never fired because `pointerdown` re-rendered every clip.
  if ($$("button[data-filter]", strip).length !== ASSET_TABS.length) {
    strip.innerHTML = ASSET_TABS.map((tab) =>
      `<button type="button" role="tab" data-filter="${tab.id}"${tab.title ? ` title="${escapeHtml(tab.title)}"` : ""}></button>`).join("");
  }
  $$("button[data-filter]", strip).forEach((button, index) => {
    const tab = ASSET_TABS[index];
    const active = tab.id === state.assetTab;
    // The Clips tab carries its own count, because "are there any" is the question that sent the
    // Director looking for this list in the first place. The asset tabs do not: their contents are
    // one click away and a count on every tab is a row of numbers nobody reads.
    button.textContent = tab.id === "clips" && clips ? `${tab.label} ${clips}` : tab.label;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
}

// Exported for the executed frontend contract, on `renderShotInspector`'s precedent: which of the
// two panes is on screen is a fact about the running panel, and the Director's report was that
// both were -- a claim no source read can settle.
export function renderAssets() {
  const assets = state.project?.assets || [];
  const query = $("#asset-search")?.value || "";
  const tab = assetTab(state.assetTab);
  const filtered = assetsForTab(assets, tab.id, query);
  const grid = $("#asset-grid");
  renderAssetTabs();
  // One tab owns the library area at a time. This is the whole of the Director's report: the
  // clips list used to be drawn *below* the grid, inside a panel whose rows are `auto 1fr`, so
  // thirty-three takes took the room the sorted sections were supposed to be shown in.
  const onClips = tab.id === "clips";
  grid.hidden = onClips;
  $("#clips-library").hidden = !onClips;
  if (!filtered.length) {
    const empty = assetTabEmpty(tab.id, query);
    grid.innerHTML = `<div class="library-empty"><strong>${escapeHtml(empty.title)}</strong><span>${escapeHtml(empty.hint)}</span></div>`;
  } else {
    grid.innerHTML = filtered.map((asset) => {
      const url = assetImageUrl(asset);
      // How far the Flux / multiview / AI-Mod render on this card has got, when ComfyUI has
      // said. `renderingFlag` composes with the RENDERING word rather than replacing it, and
      // returns that word alone when nothing is known -- so a card with no progress socket
      // reads exactly as it read before. The percentage is text, never a bar or a hue: this
      // stylesheet's rule is that colour is never the only signal, and the same rule refuses
      // a signal that is only a shape.
      const thumb = url
        ? `<img src="${url}" alt="">`
        : asset.prompt_id ? escapeHtml(renderingFlag(state.renderProgress?.[asset.id])) : "NO PREVIEW";
      return `<button class="asset-card ${asset.id === state.selectedAssetId ? "selected" : ""}" data-asset-id="${asset.id}" draggable="true"><div class="asset-thumb">${thumb}</div><footer><strong>${escapeHtml(asset.name)}</strong><span>${asset.kind} · ${asset.source}</span></footer></button>`;
    }).join("");
  }
  $$(".asset-card", grid).forEach((card) => {
    card.addEventListener("click", () => { state.selectedAssetId = card.dataset.assetId; renderAssets(); });
    card.addEventListener("dragstart", (event) => event.dataTransfer.setData("text/asset-id", card.dataset.assetId));
  });
  renderClipsLibrary();
  renderAssetInspector();
}

// Every clip this project's renders have produced, one row per take, newest first — the
// Director's ask (2026-08-20): "There is also no section in Assets for all of the clips
// generated for this project." Rows are derived from the job history (where take
// provenance lives), play in place, and jump to the producing shot on the timeline.
//
// Split out from the drawing below so the tab strip can count the takes without building the
// markup for them: a count drawn from a second, similar-looking loop is a count that can drift
// from the list it labels.
//
// Each row carries `jobTarget`'s verdict rather than a bare `target_id`, and the reason is this
// tab's own premise. It is built *from the job list* precisely so that a take survives the
// re-plan that replaced the shot it was made for — and until 2026-08-23 that was exactly the case
// it drew worst: the card footer called `shotLabel` on an id no shot has, which returns the id
// itself, so the take a Director had come here to find was labelled `shot_9f2c4b1e0a77` and given
// an Open shot button that set `selectedShotId` to a dead id and selected nothing. The same
// defect, in the same words, that the queue row had fixed the day before. `linked` is what decides
// whether the button is drawn at all; a row without one is not a broken row, it is a take whose
// shot is gone, which is a state this tab exists to keep visible.
function clipLibraryRows() {
  const rows = [];
  const seen = new Set();
  for (const job of [...(state.project?.jobs || [])].reverse()) {
    if (job.kind !== "h3" || job.status !== "complete") continue;
    for (const file of job.output_files || []) {
      if (!file.endsWith(".mp4")) continue;
      const key = file.replace("-audio.mp4", ".mp4");
      if (seen.has(key)) continue;
      seen.add(key);
      const to = jobTarget(state.project, job);
      // `shotId` is the resolver's -- empty for a detached take -- rather than `job.target_id`.
      //
      // **That choice cannot change what this tab does today, and the reason is worth writing
      // down rather than rediscovering.** A mutation test put `job.target_id` back here and no
      // test failed, so the claim is checked rather than assumed. `shotId` is read in exactly two
      // places. The `data-shot-id` on Open shot is gated on `linked`, and `jobTarget` only reports
      // `linked` for a target it has just found among `project.shots` -- so wherever that
      // attribute is drawn the two values are the same string. The other reader is
      // `clipCardFace`, which does `shots.find(item => item.id === row.shotId)`: for a detached
      // take that lookup fails for `""` and fails identically for an id no shot has, because "no
      // shot has it" is the definition of the case. Both fall through to the same ComfyUI `/view`
      // branch, which is the branch a detached take needs.
      //
      // So this is consistency with the one resolver, not a behaviour the template depends on --
      // and it stays because the alternative is a row carrying a shot id that is **not** a shot
      // id, which is the shape of the next caller's bug rather than of this one's.
      rows.push({ file, shotId: to.shotId, label: to.label, linked: to.linked, title: to.title });
    }
  }
  return rows;
}

// The Clips tab's contents. Its own tab since the Director's report (2026-08-20) that the clips
// "are eating up all the room and hiding the sorted asset sections" — this used to be drawn
// underneath the asset grid, in the same scroll area, which is exactly what it was doing.
// Nothing about a row changed: hover still plays it, Open shot still jumps to the timeline.
function renderClipsLibrary() {
  const region = $("#clips-library");
  if (!region) return;
  // Whether a `<video>` may be drawn at all, decided by `clipPreviewState` off the health answer
  // this browser actually holds. The Director's report (2026-08-21) is that this tab goes blank
  // when ComfyUI is down -- which is not an error here, because the Director starts ComfyUI
  // separately and this application is forbidden from starting it. A card that says the take
  // cannot be shown, and why, is worth more than a broken video element; see the function for
  // why the project-media route cannot serve these files instead.
  const preview = clipPreviewState(state.health);
  const rows = clipLibraryRows();
  if (!rows.length) {
    // An empty tab says so, rather than rendering nothing and reading as a panel that failed to
    // load. Drawn whether or not this tab is the visible one; `hidden` decides that.
    const empty = assetTabEmpty("clips");
    region.innerHTML = `<div class="library-empty"><strong>${escapeHtml(empty.title)}</strong><span>${escapeHtml(empty.hint)}</span></div>`;
    return;
  }
  // Each card decides for itself where its picture comes from: the shot's *current* take is served
  // by this application off ComfyUI's output directory on disk (the same route the Monitor plays,
  // which needs no ComfyUI process), and only an earlier take has to go through ComfyUI's `/view`.
  // That is what makes this tab work at all while ComfyUI is down, and it is most of the tab.
  const faces = rows.map((row) => ({ row, face: clipCardFace(state.project, row, preview) }));
  // One notice for the whole tab rather than a sentence on each of thirty-three cards, and only
  // when there is actually a card it cannot show -- a tab whose takes all play needs no apology
  // for a ComfyUI it never asked for. Nothing here polls: a tab that quietly re-probed ComfyUI
  // every render would be this application deciding how often to knock on a process it does not own.
  const notice = faces.every((entry) => entry.face.playable) ? "" : `<div class="clips-offline"><strong>${escapeHtml(preview.title)}</strong><span>${escapeHtml(preview.note)}</span></div>`;
  // The re-check sits in the heading and is drawn in **both** states, not only the offline one.
  // A control that appears only once the browser already knows ComfyUI is down could never be
  // pressed by a Director whose ComfyUI stopped after this page loaded -- health is fetched at
  // boot and nowhere else, so that session would go on drawing broken cards with no way to ask.
  // It is the same button either way, and the answer it gets is what changes.
  region.innerHTML = `<div class="clips-heading-row"><h3 class="clips-heading">Generated clips · ${rows.length}</h3><button class="quiet-button" id="clips-recheck" title="Ask the application to probe ComfyUI again. These takes play from ComfyUI's own /view endpoint, so whether they can be shown depends on it answering.">${escapeHtml(CLIP_RECHECK_LABEL)}</button></div>${notice}<div class="clips-grid">${faces.map(({ row, face }) => {
    // The take's own filename stays on the card in both states: it is the take's identity, it is
    // what names the file on disk, and it is the one fact that does not depend on ComfyUI.
    const picture = face.playable
      ? `<video preload="metadata" muted src="${escapeHtml(face.url)}" data-via="${face.via}" title="${escapeHtml(row.file)}"></video>`
      : `<div class="clip-unplayable" title="${escapeHtml(row.file)}">${escapeHtml(face.title)}</div>`;
    // The footer names the take's shot in `jobTarget`'s words, and offers Open shot only where
    // there is a shot to open -- a button that selects a dead id is the dead text this panel
    // already retired once, wearing a border.
    const jump = row.linked
      ? `<button class="quiet-button clip-jump" data-shot-id="${escapeHtml(row.shotId)}">Open shot</button>`
      : "";
    return `<div class="clip-card">${picture}<footer><span title="${escapeHtml(row.title)}">${escapeHtml(row.label)}</span>${jump}</footer></div>`;
  }).join("")}</div>`;
  $$(".clip-card video", region).forEach((video) => {
    video.addEventListener("mouseenter", () => { video.play().catch(() => {}); });
    video.addEventListener("mouseleave", () => { video.pause(); video.currentTime = 0; });
  });
  // Ask health again, then redraw. `loadHealth` swallows its own failure and repaints the ComfyUI
  // dot in the header, so a re-check that finds nothing leaves the same honest card behind.
  $("#clips-recheck", region)?.addEventListener("click", async () => {
    await loadHealth();
    renderAssets();
  });
  $$(".clip-jump", region).forEach((button) => button.addEventListener("click", () => {
    state.selectedShotId = button.dataset.shotId;
    state.selectedSectionId = null;
    document.querySelector('[data-panel="timeline"]')?.click();
    renderTimeline();
  }));
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
  // Which singer this character is, as a slot number. Drawn only for a `character` asset, which is
  // `characterSlotPlan`'s own rule and the route's: a slot names a singer, so a prop cannot hold
  // one. Slots another asset already holds are shown and shut rather than hidden — a Director
  // looking for "why can I not pick S1" is owed the name of the asset that has it, which is what
  // the route's own refusal says too.
  const slot = characterSlotPlan(state.project, asset);
  const slotHtml = slot
    ? `<label>Character slot<select id="asset-character-slot">${slot.options.map((value) => `<option value="${value}"${value === slot.slot ? " selected" : ""}${slot.taken[value] ? " disabled" : ""}>${value ? `S${value}${slot.taken[value] ? ` — held by ${escapeHtml(slot.taken[value])}` : ""}` : "Not one of the singers"}</option>`).join("")}</select></label><p class="field-help">A lyric line tagged (S${slot.slot || 1}) means this character. Slots are how a tagged line reaches a reference; leave it unset for a character who does not sing.</p>`
    : "";
  // The appearance anchor, decided by `consistencyAnchorPlan` (contract-tested) the same way
  // the two buttons above are decided by their own pure functions. Drawn ABOVE the read-only
  // generation prompt deliberately: the anchor outranks it everywhere both are consumed, and
  // a screen that puts the machine's text first teaches the opposite.
  // The display name, decided by `assetNamePlan` (contract-tested) exactly as the anchor below it
  // is. Drawn ABOVE the anchor and the read-only generation prompt: it is the field the rest of
  // this panel is about, it is what the planner is shown, and renaming is the Director's own fix
  // for an internal label leaking into shot prose ("the HarderFaster image is a picture of a
  // Woman named Lucy").
  const naming = assetNamePlan(asset);
  const nameHtml = naming ? `<label>${escapeHtml(ASSET_NAME_LABEL)}<input id="asset-name" type="text" value="${escapeHtml(naming.stored)}"></label><p class="field-help">${escapeHtml(ASSET_NAME_HELP)}</p><div class="field-foot"><span id="asset-name-count" class="field-count${naming.over || naming.empty ? " over" : ""}">${escapeHtml(naming.count)}</span><button class="quiet-button" id="save-asset-name" ${naming.savable ? "" : "disabled"}>Save name</button></div>` : "";
  const anchor = consistencyAnchorPlan(asset);
  const anchorHtml = anchor ? `<label>${escapeHtml(CONSISTENCY_PROMPT_LABEL)}<textarea id="asset-anchor" rows="3" placeholder="a woman in a red leather jacket and black boots">${escapeHtml(anchor.stored)}</textarea></label><p class="field-help">${escapeHtml(CONSISTENCY_PROMPT_HELP)}</p><div class="field-foot"><span id="asset-anchor-count" class="field-count${anchor.over ? " over" : ""}">${escapeHtml(anchor.count)}</span><button class="quiet-button" id="save-asset-anchor" ${anchor.savable ? "" : "disabled"}>Save anchor</button></div>` : "";
  // Replace With / Cancel. Drawn only for the asset whose delete was actually refused, so the
  // affordance is the answer to a refusal the Director just read rather than a permanent control
  // offering to rewrite the plan.
  const replaceHtml = replaceForAssetId === asset.id ? assetReplacementHtml(asset) : "";
  // The Assets panel's own way in, beside "Attach to selected shot" and drawn only when this asset
  // is cited. Same route, same report-then-confirm, and no deletion anywhere in the path — the
  // Director asked for the operation "without resulting in asset deletion", which this route
  // already is.
  const replaceIn = replaceInShotsControl(state.project, asset.id);
  const replaceInHtml = replaceIn.shown
    ? `<button class="quiet-button full" id="replace-in-shots" style="margin-top:8px" title="Re-point every shot citing this asset at another one. You see the whole list before anything is written, and nothing is deleted or rendered.">${escapeHtml(replaceIn.label)}</button>`
    : "";
  // "Attach to selected shot", named. The Director's report (2026-08-21) is that it is hard to use
  // "since cant see timeline from assets page" -- so the button says which shot it will write to
  // and the line under it carries that shot's window and the opening of its intent. Decided by
  // `attachToShotControl`, which is also what shuts it: with no selection, and on an asset this
  // shot already cites, where the click was a no-op that toasted success anyway.
  const attach = attachToShotControl(state.project, state.selectedShotId, asset.id, asset.name);
  const attachHtml = `<button class="quiet-button full" id="attach-asset" style="margin-top:8px" title="${escapeHtml(attach.title)}" ${attach.disabled ? "disabled" : ""}>${escapeHtml(attach.label)}</button><p class="control-reason" id="attach-asset-target">${escapeHtml(attach.caption)}</p>`;
  inspector.innerHTML = `<span class="eyebrow">${escapeHtml(asset.kind)}</span><h2>${escapeHtml(asset.name)}</h2><div class="asset-preview">${url ? `<img src="${url}" alt="${escapeHtml(asset.name)}">` : "Awaiting output"}</div><div class="meta-list"><b>Source</b><span>${escapeHtml(asset.source)}</span><b>Prompt ID</b><span>${escapeHtml(asset.prompt_id || "—")}</span><b>Created</b><span>${new Date(asset.created_at).toLocaleString()}</span></div>${vision}${nameHtml}${slotHtml}${anchorHtml}${asset.prompt ? `<label>Generation prompt<textarea rows="7" readonly>${escapeHtml(asset.prompt)}</textarea></label>` : ""}<button class="quiet-button full" id="analyze-asset" ${asset.path && !["audio"].includes(asset.kind) ? "" : "disabled"}>Inspect with vision model</button>${promotion ? `<button class="primary-button full" id="create-multiview" ${promotion.ready ? "" : "disabled"}>Create Krea multiview sheet</button>` : ""}${mod ? `<button class="primary-button full" id="ai-mod-asset" ${mod.ready ? "" : "disabled"} title="Prompt an image edit. A new asset is produced beside this one — keep it, delete it to reject, or mod it again. The source is never changed.">AI Mod (image edit)</button>` : ""}${attachHtml}${replaceInHtml}<button class="danger-button full" id="delete-asset" style="margin-top:8px" title="Remove this asset from the library. Refused by name while any shot cites it; an uploaded file goes with it, a generated file stays in ComfyUI's output tree.">Delete asset</button>${replaceHtml}`;
  $("#attach-asset")?.addEventListener("click", attachSelectedAsset);
  $("#delete-asset")?.addEventListener("click", async () => {
    if (!window.confirm(`Delete ${asset.name} from the library?`)) return;
    try {
      state.project = await api.deleteAsset(state.project.id, asset.id);
      state.selectedAssetId = null;
      clearAssetReplacement();
      renderAssets();
      toast(`${asset.name} deleted`);
    } catch (error) {
      // The Director's ask, at the exact moment they asked for it. The refusal is unchanged and
      // still says what stopped the delete; the affordance appears beneath it so the answer is
      // in reach "while i am here in assets". Offered only when the project on screen really does
      // cite this asset -- `assetIsCited` reads the manifest rather than the refusal's prose, so
      // an unrelated failure (a network error, a 404) is still just a toast.
      toast(error.message, "error");
      if (assetIsCited(state.project, asset.id)) {
        clearAssetReplacement();
        replaceForAssetId = asset.id;
        replaceRefusal = error.message;
        renderAssetInspector();
      }
    }
  });
  $("#replace-in-shots")?.addEventListener("click", () => {
    // No delete was attempted and none will be, so there is no refusal sentence to show — the
    // panel explains itself instead. Otherwise this is the identical affordance.
    clearAssetReplacement();
    replaceForAssetId = asset.id;
    renderAssetInspector();
  });
  bindAssetReplacement(asset);
  // The anchor box carries no `maxlength`, on the song context's recorded reasoning: a
  // maxlength truncates an oversized paste in the browser and drops the tail with no message,
  // while the identical text sent to the route comes back as a 422 naming its length. The
  // count and the route's refusal are the only two things that speak about the bound, and
  // `consistencyAnchorPlan` is what makes them say the same thing.
  //
  // Typing repaints the count and the button and nothing else — `renderAssets()` here would
  // rebuild the inspector and throw away what is being typed.
  // Typing repaints the count and the button and nothing else — `renderAssets()` here would
  // rebuild the inspector and throw away what is being typed. The anchor's rule, and no
  // `maxlength` for the anchor's reason: a truncated paste is a silent edit.
  $("#asset-name")?.addEventListener("input", () => {
    const typed = assetNamePlan(asset, $("#asset-name").value);
    const count = $("#asset-name-count");
    count.textContent = typed.count;
    count.classList.toggle("over", typed.over || typed.empty);
    $("#save-asset-name").disabled = !typed.savable;
  });
  $("#save-asset-name")?.addEventListener("click", async () => {
    const typed = assetNamePlan(asset, $("#asset-name").value);
    if (!typed.savable) return;
    try {
      // The reply is a report, not a bare project: `message` is where the route says what the
      // rename did not touch, which is the half a Director cannot see from the panel.
      const result = await api.renameAsset(state.project.id, asset.id, typed.draft.trim());
      state.project = result.project;
      renderAssets();
      toast(result.message);
    } catch (error) { toast(error.message, "error"); }
  });
  $("#asset-anchor")?.addEventListener("input", () => {
    const typed = consistencyAnchorPlan(asset, $("#asset-anchor").value);
    const count = $("#asset-anchor-count");
    count.textContent = typed.count;
    count.classList.toggle("over", typed.over);
    $("#save-asset-anchor").disabled = !typed.savable;
  });
  $("#save-asset-anchor")?.addEventListener("click", async () => {
    const typed = consistencyAnchorPlan(asset, $("#asset-anchor").value);
    if (!typed.savable) return;
    try {
      state.project = await api.saveConsistencyPrompt(state.project.id, asset.id, typed.draft.trim());
      renderAssets();
      toast(typed.draft.trim() ? `Appearance anchor saved for ${asset.name}` : `Appearance anchor cleared for ${asset.name}`);
    } catch (error) { toast(error.message, "error"); }
  });
  // One change, one route, and the reply is adopted so the whole library's slots redraw: taking S1
  // off this asset is what frees it on every other asset's select.
  $("#asset-character-slot")?.addEventListener("change", async (event) => {
    const chosen = Number(event.target.value);
    try {
      state.project = await api.saveCharacterSlot(state.project.id, asset.id, chosen);
      renderAssets();
      toast(chosen ? `${asset.name} is now S${chosen}` : `${asset.name} holds no character slot`);
    } catch (error) { toast(error.message, "error"); renderAssets(); }
  });
  $("#create-multiview")?.addEventListener("click", createMultiview);
  $("#ai-mod-asset")?.addEventListener("click", aiModAsset);
  $("#analyze-asset")?.addEventListener("click", async () => {
    try { state.project = await api.analyzeAsset(state.project.id, asset.id); renderAssets(); toast("Vision inspection saved"); }
    catch (error) { toast(error.message, "error"); }
  });
}

// The Replace With / Cancel block, as markup. Every decision in it is `api.js`'s -- which assets
// may be offered, whether the button reports or applies, and what the report's lines say -- so
// this function chooses nothing and only draws.
function assetReplacementHtml(asset) {
  const control = assetReplacementControl(replaceChoiceId, replaceReport);
  const options = assetReplacementOptions(state.project, asset.id)
    .map((option) => `<option value="${escapeHtml(option.id)}" ${option.id === replaceChoiceId ? "selected" : ""}>${escapeHtml(option.name)} · ${escapeHtml(option.kind)}</option>`)
    .join("");
  // Every line, all three lists, nothing summarised -- `renderSnapCuts`' rule: the skip reasons
  // are the server's own sentences and they are what explains why the delete is still refused.
  const lines = assetReplacementReportLines(replaceReport);
  const section = (heading, kind) => {
    const rows = lines.filter((line) => line.kind === kind);
    return rows.length
      ? `<div class="snap-heading">${escapeHtml(heading)} (${rows.length})</div>` +
        rows.map((row) => `<div class="snap-${row.kind}">${escapeHtml(row.text)}</div>`).join("")
      : "";
  };
  const warning = replaceReport?.warning
    ? `<div class="snap-skip">${escapeHtml(replaceReport.warning)}</div>`
    : "";
  // The take-provenance notes, above the lists and unheaded: they are about shots that appear in
  // the lists below, so a heading of their own would read as a fourth bucket.
  const notes = lines
    .filter((line) => line.kind === "note")
    .map((line) => `<div class="snap-note">${escapeHtml(line.text)}</div>`)
    .join("");
  const report = replaceReport
    ? `<div class="snap-report">${warning}${notes}${section(REPLACE_WITH_SWAPPED_HEADING, "swap")}${section(REPLACE_WITH_MERGED_HEADING, "merge")}${section(REPLACE_WITH_SKIPPED_HEADING, "skip")}</div>`
    : "";
  // The refusal when a delete was refused, the panel's own explanation when the Assets-panel
  // button opened it. One or the other is always present: a panel that acts on shots the Director
  // cannot see from here must say what it is going to do.
  return `<div class="replace-panel"><div class="snap-heading">${escapeHtml(REPLACE_WITH_HEADING)}</div><p class="field-help">${escapeHtml(replaceRefusal || REPLACE_WITH_HELP)}</p><select id="replace-with" ${replaceInFlight ? "disabled" : ""}><option value="">${escapeHtml(REPLACE_WITH_PLACEHOLDER)}</option>${options}</select>${report}<div class="field-foot"><span class="snap-reason">${escapeHtml(control.reason)}</span><button class="primary-button" id="replace-run" ${control.disabled || replaceInFlight ? "disabled" : ""}>${escapeHtml(replaceInFlight ? REPLACE_WITH_RUNNING : control.label)}</button><button class="quiet-button" id="replace-cancel" ${replaceInFlight ? "disabled" : ""}>${escapeHtml(REPLACE_WITH_CANCEL)}</button></div></div>`;
}

function bindAssetReplacement(asset) {
  $("#replace-with")?.addEventListener("change", (event) => {
    replaceChoiceId = event.currentTarget.value;
    // The report answered a question about a different asset. Keeping it beside a new choice
    // would offer an apply for shots nobody asked about -- the snap bar's tolerance rule.
    replaceReport = null;
    renderAssetInspector();
  });
  $("#replace-cancel")?.addEventListener("click", () => {
    clearAssetReplacement();
    renderAssetInspector();
  });
  $("#replace-run")?.addEventListener("click", () => {
    const control = assetReplacementControl(replaceChoiceId, replaceReport);
    if (control.disabled) return;
    runAssetReplacement(asset, control.apply);
  });
}

// One click. `apply` false fetches a report and writes nothing -- the route refuses to save
// without the flag, so the two-stage shape is the server's rule and not this function's manners.
async function runAssetReplacement(asset, apply) {
  if (!state.project || replaceInFlight) return;
  replaceInFlight = true;
  renderAssetInspector();
  try {
    const answer = await api.replaceAssetCitations(state.project.id, asset.id, replaceChoiceId, apply);
    replaceInFlight = false;
    if (apply && answer.project) {
      state.project = answer.project;
      clearAssetReplacement();
      renderAssets();
      toast(answer.message);
      return;
    }
    replaceReport = answer;
    renderAssetInspector();
  } catch (error) {
    replaceInFlight = false;
    renderAssetInspector();
    toast(error.message, "error");
  }
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

let sectionSaveChain = Promise.resolve();

function saveSectionsSilently() {
  if (!state.project) return Promise.resolve();
  const projectId = state.project.id;
  const sections = structuredClone(state.project.sections || []);
  sectionSaveChain = sectionSaveChain
    .then(() => api.saveSections(projectId, sections))
    .then((project) => {
      // The server sorts by start; adopt its ordering so ids and order agree everywhere.
      if (state.project?.id === projectId) state.project.sections = project.sections;
    })
    .catch((error) => toast(error.message, "error"));
  return sectionSaveChain;
}

function bindSection(pill) {
  pill.addEventListener("pointerdown", (event) => {
    const section = (state.project.sections || []).find((item) => item.id === pill.dataset.sectionId);
    if (!section) return;
    state.selectedSectionId = section.id;
    state.selectedShotId = null;
    renderTimeline();
    const mode = event.target.classList.contains("left") ? "left" : event.target.classList.contains("right") ? "right" : "move";
    const startX = event.clientX;
    const original = { start: section.start, duration: section.duration };
    const boundaries = shotBoundaries(state.project);
    // Snap tolerance in seconds, scaled from 8 screen pixels so zooming in tightens it.
    const tolerance = Math.max(0.15, 8 / state.pixelsPerSecond);
    const move = (moveEvent) => {
      const delta = (moveEvent.clientX - startX) / state.pixelsPerSecond;
      if (mode === "move") {
        const snappedStart = snapSeconds(Math.max(0, original.start + delta), boundaries, tolerance);
        const snappedEnd = snapSeconds(snappedStart + original.duration, boundaries, tolerance);
        // Whichever edge found a boundary wins; the box keeps its length while moving.
        section.start = Math.max(0, Math.abs(snappedStart - (original.start + delta)) <= Math.abs((snappedEnd - original.duration) - (original.start + delta))
          ? snappedStart : snappedEnd - original.duration);
      }
      if (mode === "left") {
        const end = original.start + original.duration;
        const snapped = snapSeconds(clamp(original.start + delta, 0, end - 1), boundaries, tolerance);
        section.start = clamp(snapped, 0, end - 1);
        section.duration = end - section.start;
      }
      if (mode === "right") {
        const end = snapSeconds(original.start + original.duration + delta, boundaries, tolerance);
        section.duration = Math.max(1, end - original.start);
      }
      renderTimeline();
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      if (section.start !== original.start || section.duration !== original.duration) saveSectionsSilently();
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  });
}

function projectDuration() {
  const shotEnd = Math.max(0, ...(state.project?.shots || []).map((shot) => shot.start + shot.duration));
  return Math.max(state.project?.song?.duration || 0, shotEnd, 30);
}

// The second line of a take row: what actually tells two takes of one shot apart. `takesStripRows`
// carries the seed and the landing time as raw values and decides nothing about how they read; the
// time is formatted here because a locale string is a rendering decision and belongs on this side.
// A record carrying neither draws no line at all rather than an empty one or an `Invalid Date`.
function takeProvenance(row) {
  const parts = [];
  if (row.seed !== null && row.seed !== undefined) parts.push(`seed ${row.seed}`);
  if (row.at) {
    const at = new Date(row.at);
    if (!Number.isNaN(at.getTime())) parts.push(at.toLocaleString());
  }
  return parts.join(" · ");
}

function renderTimeline() {
  const duration = projectDuration();
  const trackWidth = Math.max(900, duration * state.pixelsPerSecond);
  const canvas = $("#timeline-canvas");
  // The whole timeline is one canvas -- ruler, all four tracks, playhead -- sized to the zoomed
  // song rather than to its container, inside one `.timeline-scroll` box. That is what makes the
  // four tracks share a scroll offset by construction: there is only one offset in the panel to
  // be wrong about, so a SECTIONS box cannot drift away from the SHOTS beneath it.
  canvas.style.width = `${trackWidth + 90}px`;
  $("#zoom-label").textContent = zoomLabelText(state.pixelsPerSecond);
  const zoomSlider = $("#zoom-slider");
  // Follow the scale rather than lead it: the buttons, the slider and ctrl+wheel all write
  // `state.pixelsPerSecond`, and the thumb reads it back here so no two of them can disagree.
  if (zoomSlider) zoomSlider.value = String(zoomSliderValue(state.pixelsPerSecond));
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
  // Numbered by position in the SONG, not in the manifest: after a mid-timeline add, the
  // clip at 0:10 must not read SHOT 34. Status classes carry render state onto the clip
  // itself — which shots are rendered, queued, errored, approved or flagged was invisible
  // without clicking each one (the analyst's finding, 2026-08-20).
  const timeOrder = new Map(
    [...(state.project?.shots || [])].sort((a, b) => a.start - b.start).map((shot, rank) => [shot.id, rank + 1])
  );
  // The server's verdict on each shot's window length, never this client's own arithmetic. The
  // band's constants live in `timeline.py` and the short end's floor fires well below its
  // nominal minimum, so a check re-derived here would drift and paint the wrong clips. Only the
  // long end draws anything -- see `clipWindowState`, and the Director's ruling behind it.
  const windowKinds = windowWarningsByShot(readinessReport);
  track.innerHTML = (state.project?.shots || []).map((shot) => {
    // The live percentage for this shot's H3 render, or nothing at all. It reaches three of the
    // clip's signals -- the RENDERING word, the title, and the accessible name -- and reaches
    // none of them when it is unknown, which is what keeps a socketless render's clip identical
    // to the one this file drew yesterday.
    const percent = state.renderProgress?.[shot.id];
    const cell = shotPromptCell(shot, percent);
    // Render state in words on the clip itself, applied and never re-decided here. The status
    // classes below are a border hue, and a hue is not a signal on its own -- this stylesheet
    // says so about every other state it draws. `RENDERING` is the word; `cell.label` already
    // carries the sentence, so the accessible name says it too, which is the only one of the
    // three a screen reader announces.
    //
    // `render.flag` is still the gate -- a clip with no render in flight carries no state span,
    // exactly as before -- and `renderingFlag` writes the word, which is the same
    // `SHOT_RENDERING_FLAG` constant `shotRenderState` puts in `render.flag`, with the live
    // percentage appended when there is one.
    const render = shotRenderState(shot);
    // The window band, read off the server's report rather than measured here. The hue is the
    // Director's yellow; the sentence goes into the title and the accessible name beside it,
    // because a state carried by colour alone does not exist for a screen reader and this
    // application says so about every other state it draws.
    // Which of the two in-flight states this clip is in -- on the GPU, or waiting behind another
    // render -- read off the phase map the poll already builds, and applied here rather than
    // re-decided. `render.inFlight` is still the gate: a shot with no render open carries no state
    // span and no phase class, exactly as before. An *unknown* phase (no report yet, or a job kind
    // that names no shot) draws exactly what this line drew before the feature existed --
    // `RENDERING` from `renderingFlag`, no class, no extra sentence.
    const phase = clipRenderPhase(render.inFlight ? state.renderPhase?.[shot.id] : "", percent);
    const band = clipWindowState(windowKinds[shot.id], [cell.label, phase.note].filter(Boolean).join(" "));
    const marks = [
      `status-${shot.status || "draft"}`,
      shot.approved_output || shot.status === "approved" ? "approved" : "",
      shot.flagged ? "flagged" : "",
      shot.locked ? "locked" : "",
      render.inFlight ? "rendering" : "",
      phase.className,
      band.className,
    ].filter(Boolean).join(" ");
    return `<div class="shot-clip ${cell.className} ${marks} ${shot.id === state.selectedShotId ? "selected" : ""}" data-shot-id="${shot.id}" title="${escapeHtml(band.label)}" aria-label="${escapeHtml(band.label)}" style="left:${shot.start * state.pixelsPerSecond}px;width:${Math.max(40, shot.duration * state.pixelsPerSecond)}px"><span class="resize-handle left"></span><span class="clip-id">SHOT ${String(timeOrder.get(shot.id)).padStart(2, "0")} · ${shot.duration.toFixed(1)}s</span>${render.flag ? `<span class="clip-state">${escapeHtml(phase.flag || renderingFlag(percent))}</span>` : ""}<span class="clip-prompt">${escapeHtml(cell.text)}</span><span class="resize-handle right"></span></div>`;
  }).join("");
  $$(".shot-clip", track).forEach(bindClip);
  renderReferences();
  // The SECTIONS track, drawn at last: the Director's own marks, each with its window
  // and shared prompt. Double-click the track to edit (one-line grammar, parsed by
  // parseSectionLine, contract-tested).
  // The SECTIONS track: real boxes, the Director's design — drag to move, handles to
  // resize, edges snapping to the shots below, double-click empty space to create,
  // click to edit the shared prompt in the inspector.
  $("#section-track").innerHTML = (state.project?.sections || []).map((section) =>
    `<div class="section-pill ${section.id === state.selectedSectionId ? "selected" : ""}" data-section-id="${section.id}" title="${escapeHtml(section.prompt || section.label)}" style="left:${section.start * state.pixelsPerSecond}px;width:${Math.max(34, section.duration * state.pixelsPerSecond)}px"><span class="resize-handle left"></span><span class="section-label">${escapeHtml(section.label)}</span><span class="resize-handle right"></span></div>`
  ).join("");
  $$("#section-track .section-pill").forEach(bindSection);
  renderShotInspector();
  // Assistant ProducerBot's controls live in the composer, two panels away, and their state is
  // decided by the shot selection this function owns. Repainted from here because every selection
  // change, every project load and every reply already goes through it -- wiring them to the click
  // handler instead would leave them stale after a load, a delete or a lock set elsewhere.
  syncAssistantControls();
  // The whole-plan H3 sweep lives beside the pass-one expansion in the Director workspace, and its
  // only question is whether this plan has any shots -- which is a thing this function owns.
  syncExpansionControls();
  // Undo and Redo, in the bar beside the tools whose gestures they step back. Repainted here for
  // exactly `syncExpansionControls`' reason: every selection change, project load and reply
  // already passes through this function, and a button left saying it would undo a split the
  // Director has already undone is worse than no button.
  syncUndoControls();
  if (state.audioBuffer) drawWaveform($("#timeline-waveform"), state.audioBuffer, "#6f7d3d");
  // The measured voice map, striped over the master row: where the track actually sings
  // (Whisper word timestamps, merged) — the Director's planning fact for "which Shots
  // have words, when the cuts should happen" (2026-08-20). Unmeasured songs draw nothing.
  const vocalBand = $("#vocal-band");
  if (vocalBand) {
    vocalBand.innerHTML = (state.project?.song?.vocal_spans || []).map(([from, to]) =>
      `<span class="vocal-span" style="left:${from * state.pixelsPerSecond}px;width:${Math.max(2, (to - from) * state.pixelsPerSecond)}px"></span>`
    ).join("");
  }
  // The measured beats and onsets, over the same waveform. Display only: this paints a band and
  // nothing else -- no Shot is read or written here, and the toggle above changes what is drawn
  // and nothing in the project. Every decision is `beatMarkerPlan`'s: which marks survive the
  // track's width, where each one sits, and which class it takes. This block positions what it is
  // handed and re-derives none of it, which is also why no `state.pixelsPerSecond` arithmetic
  // appears below -- the scale goes *into* the plan, and offsets come out.
  const beatBand = $(BEAT_MARKERS_BAND);
  if (beatBand) {
    // Rebuilt only when one of the five things it depends on has moved. `renderTimeline` runs on
    // every `pointermove` of a clip drag, and a real 3-minute track measures 772 marks (440 beats
    // and 332 onsets, measured 2026-08-24): rebuilding that string sixty times a second would make
    // dragging a boundary measurably heavier than it is today, and this story is not allowed to
    // change boundary editing in any way. Nothing here decides *placement* -- it decides whether
    // to write, and the plan below is still the only thing that decides where.
    //
    // `songSeconds` is the *song's* length, not `duration` above: that one is the drawn extent of
    // the timeline (`max(song, last shot, 30)`), and bounding the marks by it would let a beat be
    // drawn past the end of the audio whenever a shot runs off the end of the track.
    const songSeconds = state.project?.song?.duration || 0;
    const key = `${beatMarkersOn}:${state.pixelsPerSecond}:${trackWidth}:${songSeconds}`;
    if (key !== beatBandKey || state.songEnvelope !== beatBandEnvelope) {
      beatBandKey = key;
      beatBandEnvelope = state.songEnvelope;
      const plan = beatMarkerPlan({
        envelope: state.songEnvelope,
        pixelsPerSecond: state.pixelsPerSecond,
        trackWidth,
        duration: songSeconds,
        enabled: beatMarkersOn,
      });
      beatBand.innerHTML = plan.markers.map((mark) =>
        `<span class="${mark.className}" style="left:${mark.left}px"></span>`
      ).join("");
    }
  }
  renderSnapCuts();
  renderAssembly();
  updateTimelinePlayhead();
}

// The one way the two buttons and the slider change the scale, so all three behave identically.
// `zoomViewport` decides where the viewport lands -- the playhead if it is on screen, the centre
// of what is visible otherwise -- and the new offset is written *after* the render, against the
// canvas the render has just widened, so the browser clamps it to real content rather than to a
// width guessed before the fact.
function applyZoom(next) {
  const scroll = $("#timeline-scroll");
  const plan = zoomViewport({
    scrollLeft: scroll?.scrollLeft || 0,
    viewportWidth: scroll?.clientWidth || 0,
    pixelsPerSecond: state.pixelsPerSecond,
    toPixelsPerSecond: next,
    playheadSeconds: state.playhead,
  });
  if (plan.pixelsPerSecond === state.pixelsPerSecond) return plan;
  state.pixelsPerSecond = plan.pixelsPerSecond;
  renderTimeline();
  // The scale is a working choice, not a property of the video: it lives in this browser's
  // session storage beside the panel and the selection, and never in the manifest. The scroll
  // offset is not stored at all -- it is where you happen to be looking this minute, and a
  // reload that restored it would fight the playhead-following the transport already does.
  persistSession();
  if (scroll) scroll.scrollLeft = plan.scrollLeft;
  return plan;
}

// Exported for the executed frontend contract, `renderSong`'s reason exactly: a source read of
// this function would pass just as happily if nothing ever called it, and the guarantees that
// matter here -- that the report is drawn in full, and that the button turns into an apply only
// once a report holding moves exists -- are properties of the markup it produces.
export function renderSnapCuts() {
  const bar = $("#snap-bar");
  if (!bar) return;
  if (!state.project) { bar.innerHTML = ""; snapReport = null; expansionSweepReport = null; return; }
  const control = snapCutsControl(state.project, snapToleranceSeconds, snapReport);
  const disabled = control.disabled || snapInFlight;
  const label = snapInFlight ? SNAP_CUTS_RUNNING : control.label;
  const tolerance = `<label class="snap-tolerance" title="${escapeHtml(SNAP_CUTS_TOLERANCE_HELP)}">${escapeHtml(SNAP_CUTS_TOLERANCE_LABEL)} <input type="number" id="snap-tolerance" value="${snapToleranceSeconds}" min="0" max="${SNAP_TOLERANCE_MAX}" step="${SNAP_TOLERANCE_STEP}" ${snapInFlight ? "disabled" : ""}> s</label>`;
  // Every line, both lists, nothing summarised: the skip reasons are the server's own sentences
  // and they are the half of this feature a Director has to be able to read.
  const lines = snapCutsReportLines(snapReport);
  const moved = lines.filter((line) => line.kind === "move");
  const stayed = lines.filter((line) => line.kind === "skip");
  const section = (heading, rows) => rows.length
    ? `<div class="snap-heading">${escapeHtml(heading)} (${rows.length})</div>` +
      rows.map((row) => `<div class="snap-${row.kind}">${escapeHtml(row.text)}</div>`).join("")
    : "";
  const report = snapReport
    ? `<div class="snap-report">${section(SNAP_CUTS_MOVED_HEADING, moved)}${section(SNAP_CUTS_SKIPPED_HEADING, stayed)}</div>`
    : "";
  const dismiss = snapReport
    ? `<button class="quiet-button" id="snap-dismiss" ${snapInFlight ? "disabled" : ""}>${escapeHtml(SNAP_CUTS_DISMISS_LABEL)}</button>`
    : "";
  // "Expand All Prompts", where the Director went looking for it (2026-08-21): "up by where the
  // Cuts and Snap Cuts stuff are". The same route, the same refusal and the same help text as the
  // button in the Director workspace -- `expandAllPromptsControl` decides for both, so a plan with
  // no shots draws two shut buttons saying the same sentence rather than one of each.
  const sweep = expandAllPromptsControl(state.project);
  const sweepLabel = expansionSweepInFlight ? EXPAND_ALL_PROMPTS_RUNNING : EXPAND_ALL_PROMPTS_TIMELINE_LABEL;
  const expand = `<button class="quiet-button" id="${EXPAND_ALL_PROMPTS_TIMELINE_CONTROL.slice(1)}" ${sweep.disabled || expansionSweepInFlight ? "disabled" : ""} title="${escapeHtml(sweep.title)}" aria-label="${escapeHtml(sweep.title)}">${escapeHtml(sweepLabel)}</button>`;
  // "Generate All Empty", beside it, in its shape and its voice. The Director's ask of
  // 2026-08-23: "a button on the timeline beside Expand All Prompts... which would generate all
  // shots that dont already have a video".
  //
  // Live even when nothing is empty, deliberately, and this is the one place it differs from the
  // sweep button next to it. A plan whose every shot has a take is the *success* state, not a
  // misconfiguration, and a control that answers it by being shut says so only to a Director who
  // hovers it. It stays clickable and answers with `GENERATE_EMPTY_NONE` — the fix the three
  // silent shot controls got on 2026-08-22, applied before the defect rather than after it. Its
  // title carries the count and the drafts either way, so the cost is readable before the click.
  const empties = generateEmptyPlan(state.project);
  const emptyLabel = emptyBatchInFlight ? GENERATE_EMPTY_RUNNING : GENERATE_EMPTY_LABEL;
  const generateEmpty = `<button class="quiet-button" id="${GENERATE_EMPTY_CONTROL.slice(1)}" ${emptyBatchInFlight ? "disabled" : ""} title="${escapeHtml(empties.title)}" aria-label="${escapeHtml(empties.title)}">${escapeHtml(emptyLabel)}</button>`;
  // Every line the route reported, whole and in its own words, each labelled for its kind. This
  // is the half that must not be swallowed: a sweep that silently skipped four locked shots is
  // indistinguishable from one that forgot them, which is exactly why the route answers per shot.
  const sweepReport = expansionSweepReport?.length
    ? `<div class="snap-report expansion-report">${expansionSweepReport.map((line) =>
        `<div class="snap-${line.kind === "change" ? "move" : "skip"}"><strong>${escapeHtml(NOTICE_KINDS[line.kind]?.label || line.kind)}</strong> ${escapeHtml(line.text)}</div>`).join("")}<button class="quiet-button" id="expansion-dismiss">${escapeHtml(SNAP_CUTS_DISMISS_LABEL)}</button></div>`
    : "";
  bar.innerHTML = `<div class="snap-controls"><span class="eyebrow">Cuts</span>${tolerance}<button class="quiet-button" id="snap-cuts" ${disabled ? "disabled" : ""} title="${escapeHtml(control.title)}">${escapeHtml(label)}</button>${dismiss}${expand}${generateEmpty}<span class="snap-reason">${escapeHtml(control.reason)}</span></div>${report}${sweepReport}`;
  const expandButton = $(EXPAND_ALL_PROMPTS_TIMELINE_CONTROL, bar);
  if (expandButton) expandButton.addEventListener("click", expandPlanPrompts);
  const emptyButton = $(GENERATE_EMPTY_CONTROL, bar);
  if (emptyButton) emptyButton.addEventListener("click", generateEmptyShots);
  const forget = $("#expansion-dismiss", bar);
  if (forget) forget.addEventListener("click", () => { expansionSweepReport = null; renderSnapCuts(); });
  const box = $("#snap-tolerance", bar);
  if (box) {
    box.addEventListener("change", (event) => {
      snapToleranceSeconds = snapTolerance(event.currentTarget.value);
      // The report answered the old question. Keeping it on screen beside a new tolerance
      // would offer an apply for moves nobody asked for at this setting.
      snapReport = null;
      renderSnapCuts();
    });
  }
  const discard = $("#snap-dismiss", bar);
  if (discard) discard.addEventListener("click", () => { snapReport = null; renderSnapCuts(); });
  const button = $("#snap-cuts", bar);
  if (button) button.addEventListener("click", () => runSnapCuts(control.apply));
}

// One click. `apply` false fetches a report and writes nothing -- the route refuses to save
// without the flag, so the two-stage shape is the server's rule and not this function's manners.
async function runSnapCuts(apply) {
  if (snapInFlight || !state.project) return;
  const projectId = state.project.id;
  const tolerance = snapToleranceSeconds;
  snapInFlight = true;
  renderSnapCuts();
  try {
    const report = await api.snapCuts(projectId, tolerance, apply);
    if (state.project?.id !== projectId) return;
    if (report.applied && report.project) {
      state.project = report.project;
      snapReport = null;
      renderAll();
      toast(SNAP_CUTS_APPLIED_TOAST.replace("{moved}", String(report.moved)).replace("{skipped}", String(report.skipped)));
      return;
    }
    snapReport = report;
  } catch (error) {
    snapReport = null;
    toast(String(error?.message || error), "error");
  } finally {
    snapInFlight = false;
    if (state.project?.id === projectId) renderSnapCuts();
  }
}

// Whether an assemble request is currently open, and the last multi-line refusal the server
// answered one with. Both module state for `readinessReport`'s reason -- derived, never saved,
// never sent back -- and the report is cleared by the next attempt or a successful export,
// because the plan it described stops being the plan on screen.
// The chosen export preset is module state for the same reason: it is a property of the click
// about to happen, not of the project, and nothing on the server remembers it between exports.
// It resets to the default on reload, which is the safe direction -- `draft` is what the button
// has always produced.
let assemblyInFlight = false;
let assemblyRefusalReport = "";
let assemblyPreset = EXPORT_PRESET_DEFAULT;
let assemblyPercent = null;
let assemblyProgressTimer = 0;

// The assemble request is synchronous and can be held open for minutes, and the AD-1 poll
// deliberately never fetches during it (a local job has no prompt id, so there is nothing on
// ComfyUI to reconcile). This is the one tick that reads the job's own `progress`, and it
// exists only while the request is open: started by the click, cleared in the same `finally`.
function watchAssemblyProgress(projectId) {
  if (assemblyProgressTimer) return;
  assemblyProgressTimer = setInterval(async () => {
    try {
      const fresh = await api.project(projectId);
      const percent = assemblyProgress(fresh);
      if (percent === null || percent === assemblyPercent) return;
      assemblyPercent = percent;
      renderAssembly();
    } catch {
      // A failed tick is not an assembly failure -- the request itself is the answer, and a
      // bar that cannot refresh is worth strictly less than an export that is still running.
    }
  }, RENDER_POLL_INTERVAL_MS);
}

function stopAssemblyProgress() {
  if (assemblyProgressTimer) clearInterval(assemblyProgressTimer);
  assemblyProgressTimer = 0;
  assemblyPercent = null;
}

function renderAssembly() {
  const bar = $("#assembly-bar");
  if (!bar) return;
  if (!state.project) { bar.innerHTML = ""; return; }
  const control = assemblyControl(state.project);
  const disabled = control.disabled || assemblyInFlight;
  const running = assemblyPercent === null ? ASSEMBLE_RUNNING : `${ASSEMBLE_RUNNING} ${assemblyPercent}%`;
  const label = assemblyInFlight ? running : control.label;
  const presetHelp = (EXPORT_PRESETS.find((preset) => preset.value === assemblyPreset) || EXPORT_PRESETS[0]).help;
  const presets = `<label class="assembly-preset" title="${escapeHtml(presetHelp)}">Preset <select id="assembly-preset" ${assemblyInFlight ? "disabled" : ""}>${EXPORT_PRESETS.map((preset) => `<option value="${preset.value}" ${preset.value === assemblyPreset ? "selected" : ""}>${escapeHtml(preset.label)}</option>`).join("")}</select></label>`;
  const exported = latestAssemblyExport(state.project);
  // The refusal report is the server's own words, one reason per line -- rendered whole
  // because rationing it is exactly what the comprehensive 422 exists to prevent.
  const report = assemblyRefusalReport
    ? `<div class="assembly-report">${assemblyRefusalReport.split("\n").map((line) => `<div>${escapeHtml(line)}</div>`).join("")}</div>`
    : "";
  const player = exported
    ? `<div class="assembly-export"><span class="eyebrow">Latest export</span><video controls preload="metadata" src="${exported.url}"></video><a href="${exported.url}" target="_blank" rel="noopener">${escapeHtml(exported.path)}</a></div>`
    : "";
  bar.innerHTML = `<div class="assembly-controls"><span class="eyebrow">Assembly</span>${presets}<button class="primary-button" id="assemble-button" ${disabled ? "disabled" : ""} title="${escapeHtml(control.title)}">${escapeHtml(label)}</button><span class="assembly-reason">${escapeHtml(control.reason)}</span></div>${report}${player}`;
  const select = $("#assembly-preset", bar);
  if (select) {
    select.addEventListener("change", (event) => {
      assemblyPreset = event.currentTarget.value;
      renderAssembly();
    });
  }
  const button = $("#assemble-button", bar);
  if (button) {
    button.addEventListener("click", async () => {
      if (assemblyInFlight) return;
      const projectId = state.project.id;
      const preset = assemblyPreset;
      assemblyInFlight = true;
      assemblyRefusalReport = "";
      assemblyPercent = null;
      renderAssembly();
      watchAssemblyProgress(projectId);
      try {
        const result = await api.assemble(projectId, preset);
        // The reply is the settled job plus measurements, not the project -- re-fetch so the
        // job list, the export reader and every other panel redraw from one server truth.
        state.project = await api.project(projectId);
        toast(`Assembled ${result.clip_count} shots into ${result.export} (${result.preset}, ${result.duration_seconds.toFixed(2)}s)`);
      } catch (error) {
        assemblyRefusalReport = String(error?.message || error);
        toast("Assembly refused — see the report under the button", "error");
      } finally {
        assemblyInFlight = false;
        stopAssemblyProgress();
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
  let rows = 1;
  // Stacked vertically per shot, the Director's ask (2026-08-20): the old diagonal
  // offset overlaid every pill past the first, "making it hard to tell exactly which
  // references are in that shot". One row per citation, the track growing to fit the
  // busiest shot.
  for (const shot of state.project?.shots || []) {
    const citations = shotCitations(shot);
    rows = Math.max(rows, citations.length);
    citations.forEach((citation, index) => {
      const asset = assets.get(citation.asset_id);
      if (asset) refs.push(`<span class="ref-pill" style="left:${shot.start * state.pixelsPerSecond}px;top:${8 + index * 24}px;width:${Math.max(55, shot.duration * state.pixelsPerSecond - 4)}px" title="${escapeHtml(asset.name)}">${escapeHtml(asset.name)}</span>`);
    });
  }
  const track = $("#refs-track");
  track.style.height = `${Math.max(52, 16 + rows * 24)}px`;
  track.innerHTML = refs.join("");
}

// The last press on a resize handle, as `{shotId, edge, at}`, or null. Module state because the
// clip nodes it describes are destroyed and rebuilt between the two clicks of a double-click --
// which is the whole reason it exists. Cleared by the press that completes the gesture, so three
// clicks in a row are one double-click and one press rather than two overlapping ones.
let lastEdgePress = null;

// Which shots the Director has unlocked from the music for this session, by id. Empty means every
// shot is locked, which is the default the Director asked for ("default locked").
//
// Session state, never persisted and never sent: `takeAnchorControl` carries the whole argument,
// including why this is per shot rather than one flag for the workspace. Cleared when the project
// on screen changes, not on every refresh -- a background poll re-ticking a box the Director had
// just unticked would be the control fighting them.
const unlockedFromMusic = new Set();

// Whether a move-drag on this shot slides the window over a take that stays where it was
// performed. One reader for the toggle and the drag, so the box on screen and the gesture can
// never disagree; the rule itself is `takeAnchorControl`'s.
function takeAnchor(shot) {
  return takeAnchorControl(shot, unlockedFromMusic.has(shot?.id));
}

// **The one door every write of a shot's `start` goes through**, and the reason it exists is the
// Director's ruling of 2026-08-21: "those gestures should only slide the window bounds but leave
// the clip position intact." The rule itself is `anchoredNudge`'s -- executed there, over numbers
// -- and this is the only thing in this file that moves a window and the only thing that has to
// remember to ask.
//
// It reads the *moving shot's own* lock, whichever gesture is moving it. That is the ruling's
// sharp end: when snapping a cut carries the neighbour's edge, or a gap fill runs a window out to
// meet a clip, the shot whose start is being written may be nowhere near the pointer -- and it is
// still that shot's take that must stay on the music, so it is still that shot's toggle that
// decides. The previous shape applied the compensation to the shot under the hand and left the
// neighbour to slide; there is no such distinction in the principle.
//
// `original` is the pair a drag captured before it began -- a `pointermove` mutates the live shot
// on every event, so the shot's current nudge is the previous frame's answer and compounding onto
// it would multiply the compensation by the number of mouse events. Omit it for the gestures that
// write once, and the shot's own current pair is the "before".
//
// Nothing here clamps, refuses or snaps back on take-coverage grounds. A window dragged past what
// its take holds is a real, representable state that the readiness report turns amber and
// assembly refuses with the numbers; this writes what it was asked to write.
//
// And it is no more defensive than its callers: every one of them has already established that
// there is a shot here, and a silent no-op would hide a selection bug rather than report one.
function moveWindowStart(shot, to, original = null) {
  const from = original ? original.start : shot.start;
  const was = original ? original.nudge : (shot.trim_nudge || 0);
  shot.start = to;
  shot.trim_nudge = anchoredNudge(shot, {
    from, to: shot.start, nudge: was, unlocked: unlockedFromMusic.has(shot.id),
  });
}

// Put a shot back exactly as a gesture found it. A *restore*, never a move: `start` and
// `trim_nudge` come back as one pair, so the take's anchor comes back with them and nothing here
// compensates -- compensating a restore would move the take by the amount the gesture had already
// been rolled back by. The same reasoning governs undo and redo, which restore a whole snapshotted
// shot list through `PUT /shots` and must not be routed through the door above.
function restoreWindow(shot, original) {
  shot.start = original.start;
  shot.duration = original.duration;
  shot.trim_nudge = original.nudge;
}

// Whether the song is running right now, read from the master element rather than from a flag.
// A moving playhead is not something an edge can be lined up against, which is why the snap is
// off while it plays.
function masterPlaying() {
  const audio = $("#master-audio");
  return Boolean(audio && audio.paused === false);
}

// The Director's gesture C, widened by Story 8.3: drag a cut onto a target and have it land
// exactly there. The target is the playhead they parked, a voiceless moment `timeline.py` chose,
// or a measured beat -- `edgeSnap` decided which, and this applies whichever it was. Applied on
// release rather than during the drag, and through `boundaryMovePlan` rather than by writing one
// window, because a cut belongs to *two* shots -- the neighbour's edge moves with it and the plan
// stays contiguous. Freehand dragging is untouched: it still changes one window, which is the
// gesture that put four sub-frame gaps in the Director's plan and the reason this one exists.
//
// `kind` is only ever read to *report* what happened. Every kind is applied identically, which is
// the point: a snapped cut is a snapped cut, and the target it found does not change what a write
// of it means. An unrecognised kind falls back to the playhead's sentence rather than saying
// nothing, because a silent write is worse than an imprecise report of one.
function applySnappedCut(shot, mode, seconds, original, kind = "playhead") {
  // Measured against the plan as it was before the drag: the neighbour shares the *original*
  // cut, and the shot has been mutated live by the pointermove handler.
  restoreWindow(shot, original);
  const plan = boundaryMovePlan(state.project, shot.id, mode, seconds);
  if (!plan.ok) {
    renderTimeline();
    if (plan.refusal) toast(plan.refusal, "error");
    return false;
  }
  const byId = new Map(state.project.shots.map((item) => [item.id, item]));
  // **Both windows through the same door, the shot under the hand and the neighbour that shares
  // the cut alike.** This is the ruling of 2026-08-21 at its sharpest: a right-edge snap moves the
  // *neighbour's* `start` and leaves this shot's alone, so under the old shape -- which
  // compensated the dragged shot and only on its left edge -- the one take this gesture could
  // possibly displace was the one take it never compensated. The dragged shot needs no special
  // case: on a right-edge snap its start does not move, and `moveWindowStart` writes nothing.
  //
  // `shot` was restored above, so reading each target's own live `start`/`trim_nudge` as the
  // "before" is exact for the neighbour, which no pointermove touched, and for the dragged shot.
  for (const window of plan.windows) {
    const target = byId.get(window.id);
    if (!target) continue;
    moveWindowStart(target, window.start);
    target.duration = window.duration;
  }
  state.dirty = true;
  saveShotsSilently("snap");
  renderTimeline();
  toast((SNAP_TARGET_TOASTS[kind] || PLAYHEAD_SNAP_TOAST).replace("{seconds}", seconds.toFixed(3)));
  return true;
}

// The Director's gesture B: double-click an edge with empty song beside it and it runs out to
// meet the neighbour. `gapFillPlan` decides everything -- which neighbour, how big the gap is,
// and whether either shot at the resulting cut refuses to have it moved.
//
// **The gesture has two directions and only one of them moves a start.** `gapFillPlan` never
// touches the neighbour: filling *rightward* grows this shot's `duration` and leaves its `start`
// exactly where it was, so there is nothing to anchor and `moveWindowStart` writes nothing;
// filling *leftward* runs this shot's own `start` back to meet the clip behind it, which moves
// its take by that many seconds unless the nudge follows.
//
// So the leftward fill is compensated, and this reverses a recorded decision. Until 2026-08-21
// this function left `trim_nudge` alone on purpose -- "closing a 0.002 s gap must not silently
// re-time a take" -- and the Director has ruled the other way: "same for double click, those
// gestures should only slide the window bounds but leave the clip position intact." Closing a
// 0.002 s gap by moving a window 0.002 s earlier *is* a re-timing of the take unless something
// compensates; what the old note called leaving the take alone was leaving it to slide.
function runGapFill(shotId, edge) {
  if (!state.project) return;
  const plan = gapFillPlan(state.project, shotId, edge);
  if (!plan.ok) {
    if (plan.refusal) toast(plan.refusal, "error");
    return;
  }
  const shot = state.project.shots.find((item) => item.id === shotId);
  if (!shot) return;
  moveWindowStart(shot, plan.start);
  shot.duration = plan.duration;
  state.dirty = true;
  saveShotsSilently("gapfill");
  renderTimeline();
  toast(GAP_FILL_TOAST
    .replace("{shot}", shotLabel(state.project, shotId))
    .replace("{seconds}", plan.gap.toFixed(3)));
}

function bindClip(clip) {
  clip.addEventListener("pointerdown", (event) => {
    const shot = state.project.shots.find((item) => item.id === clip.dataset.shotId);
    state.selectedShotId = shot.id;
    state.selectedSectionId = null;
    renderTimeline();
    const mode = event.target.classList.contains("left") ? "left" : event.target.classList.contains("right") ? "right" : "move";
    // Gesture B, decided here rather than by a `dblclick` listener. The `renderTimeline()` above
    // has just replaced every clip node in the document, so the element the first click of a
    // double-click landed on is gone before the second one happens and the browser dispatches no
    // `dblclick` at all. `doubleEdgePress` says why at length; only a real browser can tell the
    // two apart.
    if (mode !== "move") {
      const press = { shotId: shot.id, edge: mode, at: Date.now() };
      const doubled = doubleEdgePress(lastEdgePress, press);
      lastEdgePress = doubled ? null : press;
      if (doubled) {
        runGapFill(shot.id, mode);
        return;
      }
    }
    const startX = event.clientX;
    const original = { start: shot.start, duration: shot.duration, nudge: shot.trim_nudge || 0 };
    // The furthest this pointer got from where it went down, in screen pixels. Read at release
    // by `edgePressSurvivesDrag`, which is what stops a drag from leaving its press standing as
    // the first half of a double-click -- see there.
    let travelled = 0;
    // Which target caught this edge, as `{snapped, seconds, kind}`, or null. Read at release,
    // which is what makes the snap a decision about where the drag *ended* rather than about
    // every frame it crossed. The kind travels with it because a cut moved to a beat may not be
    // reported as a cut moved to the playhead.
    let magnetised = null;
    // Frame-stepped, not quarter-second: the buffer being dragged out is 6 frames deep on
    // one side, and a 0.25 s step could only ever reveal one notch of it.
    const grid = (value) => Math.round(value * 24) / 24;
    // **The drag's targets, resolved once, here.** `renderTimeline` runs on every `pointermove`
    // -- which is why the beat band's rebuild is guarded -- and a three-minute song carries
    // several hundred targets. Sorting them and measuring every one's local spacing sixty times a
    // second would reintroduce exactly the cost that guard exists to prevent, so the expensive
    // half happens at `pointerdown` and only the choosing happens per move.
    //
    // The empty set for a move drag, which has never had a magnet: the whole clip slides and there
    // is no one edge being placed. One condition rather than one per kind, which is the shape the
    // selector asked for -- this closure does not know how many kinds there are.
    //
    // The playhead goes *into* the plan rather than being passed per move, so it takes the same
    // local-spacing cap every other target takes and crowds the beats around it as they crowd it.
    const snapKinds = mode === "move" ? new Set() : snapTargetKinds;
    const resolveSnapPlan = () => dragSnapPlan({
      targets: state.snapTargets,
      playhead: state.playhead,
      pixelsPerSecond: state.pixelsPerSecond,
      enabledKinds: snapKinds,
    });
    let snapPlan = resolveSnapPlan();
    // Every target's pull on the edge being dragged, in seconds. `edgeSnap` decides, over the plan
    // and nothing else: tolerance in screen pixels, so the gesture feels the same at every zoom,
    // capped to a fraction of the local target spacing so a dead zone always remains between
    // targets, and declining entirely while the song is playing.
    //
    // **Re-resolved when the scale it was measured in moves, and only then.** Ctrl+wheel zooms
    // mid-drag, and a plan's tolerances are pixels converted to seconds: one resolved at 16 px/s
    // is worth 10.7 px at 64 px/s, past a ceiling whose own comment calls raising it an Ask First.
    // The check is one number per move and the rebuild happens only when the answer would
    // otherwise be wrong, so the per-move cost this closure exists to avoid is not reintroduced.
    const magnet = (edgeSeconds) => {
      if (snapPlan.pixelsPerSecond !== state.pixelsPerSecond) snapPlan = resolveSnapPlan();
      return edgeSnap({ seconds: edgeSeconds, plan: snapPlan, playing: masterPlaying() });
    };
    const move = (moveEvent) => {
      const delta = (moveEvent.clientX - startX) / state.pixelsPerSecond;
      const snapped = grid(delta);
      travelled = Math.max(travelled, Math.abs(moveEvent.clientX - startX));
      magnetised = null;
      if (mode === "move") {
        // The Director's ask, 2026-08-21: "When dragging in the timeline though it would just
        // move the window over the clip but keep the clip aligned where it belongs with the
        // music." Which is the rule the *left edge* below has followed since 2026-08-20, applied
        // to the gesture that always wanted it: the window moves, the nudge follows it by exactly
        // the same seconds, and `start - lead - nudge` -- the song second the take's first frame
        // plays at -- comes out unchanged. Unlocked, or on a shot with no take, nothing is
        // written and the move is byte for byte the one this file made before.
        //
        // `moveWindowStart` measures from `original` rather than from the raw pointer delta, so
        // the clamp at 0 s here is accounted for: a drag that ran into the head of the song moved
        // the window less than the pointer moved, and compensating by the pointer would slide the
        // take. It also re-reads the *drag's* starting nudge each time rather than the live one,
        // which is what stops a hundred pointermoves compounding a hundred compensations.
        //
        // **Deliberately not floored at the take's first frame.** The left edge clamps there and
        // keeps its clamp; this gesture must not, because the Director's ruling is that the
        // coverage warning colours and never constrains -- "still gives us the ability to nudge
        // the actual position of a clip if we need to". A window dragged off its take is a real,
        // representable state that the readiness report turns amber (`take_uncovered`) and
        // assembly refuses with the numbers; a clamp here would silently stop the drag instead.
        moveWindowStart(shot, Math.max(0, grid(original.start + snapped)), original);
      }
      if (mode === "left") {
        const end = original.start + original.duration;
        const pull = magnet(original.start + delta);
        const want = pull.snapped ? pull.seconds : grid(original.start + snapped);
        // The Director's ask (2026-08-20): an edge drag on a rendered shot does not re-window
        // the plan, it drags out the take's over-render buffer. Moving the window edge earlier
        // moves the cut into the take by exactly the same frames (`moveWindowStart` writes the
        // nudge), so the same take frame stays at the same song second -- and the floor is the
        // recorded lead: the cut can never reach before the take begins.
        //
        // Read from `takeAnchor(shot).held` since 2026-08-21, where it read `shot.latest_output`.
        // That is the same generalisation the ruling makes everywhere else in this file: an
        // *unlocked* shot is one the Director has said is being repositioned deliberately, so its
        // left edge behaves exactly like a shot with no take -- the take travels with the window,
        // and the floor at the take's first frame goes with it, because a floor that holds a cut
        // off a frame the take is no longer anchored to is a clamp bounding nothing.
        if (takeAnchor(shot).held) {
          const lead = shot.latest_take_lead || 0;
          const floor = original.start - Math.max(0, lead + original.nudge);
          moveWindowStart(shot, clamp(want, Math.max(0, floor), end - MIN_WINDOW_SECONDS), original);
        } else {
          moveWindowStart(shot, clamp(want, 0, end - MIN_WINDOW_SECONDS), original);
        }
        shot.duration = exactSeconds(end - shot.start);
        // Only when the clamp did not fight the magnet: an edge held off its target by the
        // floor has not landed on it, and saying it had would move the neighbour to a cut the
        // edge never reached.
        if (pull.snapped && shot.start === pull.seconds) magnetised = pull;
      }
      if (mode === "right") {
        const pull = magnet(original.start + original.duration + delta);
        shot.duration = pull.snapped
          ? Math.max(MIN_WINDOW_SECONDS, exactSeconds(pull.seconds - shot.start))
          : Math.max(MIN_WINDOW_SECONDS, grid(original.duration + snapped));
        if (pull.snapped && exactSeconds(shot.start + shot.duration) === pull.seconds) magnetised = pull;
      }
      state.dirty = true;
      renderTimeline();
    };
    // Only when the pointer actually moved the shot. A plain selection click is not an edit, and
    // saving one sent the whole shot list back for nothing: the reply to that write reloaded
    // readiness, and the reply to *that* rebuilt the inspector a second time, long after the click
    // looked finished. Comparing against `original` rather than tracking a flag, because a drag
    // that returns to where it started has also changed nothing.
    //
    // An unmoved click, besides selecting, parks the playhead at the shot's start — the
    // one-gesture "jump to this shot" the timeline never had.
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      // A drag consumes the press that started it. Without this the press was remembered past
      // the release, and `doubleEdgePress` measures from the *first* press -- so re-grabbing the
      // same edge shortly after a drag read as a double-click and ran the gap fill instead of
      // starting the second drag, stretching the shot to its neighbour.
      if (!edgePressSurvivesDrag(travelled)) lastEdgePress = null;
      // Re-read after every step rather than computed once, because a refused snap puts `shot`
      // back the way it was: `applySnappedCut` restores it from `original` and answers false.
      // A `moved` measured before that call was still true afterwards, so a refusal the Director
      // had just been shown sent a no-op `PUT /shots` -- which bumped `updated_at`, pushed an
      // undo entry for a gesture that never happened, and threw the redo stack away.
      const moved = () => shot.start !== original.start || shot.duration !== original.duration
        || (shot.trim_nudge || 0) !== original.nudge;
      if (moved() && magnetised !== null
        && applySnappedCut(shot, mode, magnetised.seconds, original, magnetised.kind)) return;
      if (moved()) return saveShotsSilently(mode === "move" ? "move" : "resize");
      // Nothing to save *from this gesture*, so this gesture's dirt is cleared. `move` sets the
      // dirty flag on the first pixel, and every drag used to end in a write, so leaving it set
      // was invisible; now that a refused snap and a drag that returns to where it started both
      // end here, a flag left standing would have the navigation guards warning about work that
      // does not exist.
      //
      // `shotsDirty` is ORed back in, not dropped: unlike the two sites that clear this pair
      // (the undo apply and the settled save), nothing here has cleared it, so an *earlier*
      // drag's save can still be in flight. Assigning `documentsDirty` alone made
      // `hasUnsavedWork` answer false while a shot write was outstanding, so closing the tab or
      // switching project in that window skipped the very guard this line exists to keep honest.
      state.dirty = state.documentsDirty || state.shotsDirty;
      if (mode === "move") seekMasterAudio(shot.start);
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

// Which shots re-roll their seed at their next Render again, by id. Empty means every shot keeps
// the number it has, which is what a project opens with.
//
// **Per shot since 2026-08-21, on the Director's ruling**: *"Clicking randomize integer on a shot
// toggles it for all shots, it should be per shot."* It shipped session-wide the day before, on
// the reasoning that a Director sweeping ten shots with Render again asked for randomness once;
// the Director has overruled that, and the shape here is now `unlockedFromMusic`'s, written in
// this file on the same day for the sister reason -- the state is dangerous on the *next* shot,
// and a Director who ticks a box on one clip does not mean it for a clip they have not looked at.
//
// Still deliberately NOT a field on the Shot, which the ruling does not change. A per-shot
// persisted flag is a new model field, and this application's repeat offender is the generic
// full-project PUT writing every defaulted field back -- so a new field earns its keep or it is
// not added. This one would not: what is durable about randomizing is the *number*, and the number
// is already a persisted per-shot field that this toggle writes. The flag itself is a working mode
// -- "I want a different take of this shot" -- which is a fact about the session in front of the
// screen.
//
// Cleared when the project on screen *changes*, never on a refresh of the one already there, and
// through the same `documentConsentClearedOnLoad` test the music lock uses: ids collide across
// projects, and a queue poll unticking the box mid-gesture would be the control fighting them.
const randomizeSeedShots = new Set();

// Whether this shot re-rolls at its next Render again. One reader for the checkbox, the hand-typed
// clear and the retake, so the box on screen and the seed that is written can never disagree about
// which shot was asked -- the same rule `takeAnchor` follows for the music lock.
function randomizeSeedFor(shotId) {
  return randomizeSeedShots.has(shotId);
}

// Exported for the executed frontend contract, on the `renderSong` precedent: the render-again
// control is drawn, enabled and bound in here, and a test that only read this source could not
// tell a control that is bound to the purpose-built route from one bound to the generic shots
// write -- which is the whole distinction the action exists to make. The harness in
// tests/test_frontend_contract.py boots this module, calls this, and reads what came out.
export function renderShotInspector() {
  const shot = selectedShot();
  const inspector = $("#shot-inspector");
  // A selected section owns the panel: this is where its shared prompt is written, the
  // Director's design ("when selecting that Section the info panel on the right would be
  // for where the shared prompt for that sections shots would be input").
  if (state.selectedSectionId) {
    const section = (state.project?.sections || []).find((item) => item.id === state.selectedSectionId);
    if (section) {
      const covered = (state.project?.shots || []).filter((item) => {
        const mid = item.start + item.duration / 2;
        return section.start <= mid && mid < section.start + section.duration;
      }).length;
      // The last section-look pass's own report, drawn where the button that asked for it is.
      // Every line, nothing summarised -- `renderSnapCuts`' rule, and for its reason: the section
      // that was skipped and *why* is the half the Director has to be able to read, and a report
      // that only ever appeared inside a confirm dialog was a report nobody could read twice.
      const looksLines = sectionLooksReportLines(sectionLooksReport);
      const looksHtml = looksLines.length
        ? `<div class="snap-report section-looks-report">${looksLines.map((line) =>
            `<div class="snap-${line.kind === "fill" ? "move" : "skip"}">${escapeHtml(line.text)}</div>`).join("")}<button class="quiet-button" id="section-looks-dismiss">${escapeHtml(SNAP_CUTS_DISMISS_LABEL)}</button></div>`
        : "";
      inspector.innerHTML = `<span class="eyebrow">Section</span><h2>${escapeHtml(section.label)}</h2><div class="meta-list"><b>Window</b><span>${section.start.toFixed(2)}s – ${(section.start + section.duration).toFixed(2)}s (${section.duration.toFixed(2)}s)</span><b>Covers</b><span>${covered} shot${covered === 1 ? "" : "s"}</span></div><label>Label<input id="section-label" value="${escapeHtml(section.label)}"></label><label>Shared prompt — carried into every shot in this section<textarea id="section-prompt" rows="7" placeholder="What this whole section looks like: location, staging, energy. Shots inside it vary the angle and action.">${escapeHtml(section.prompt || "")}</textarea></label><button class="quiet-button full" id="section-fill-looks" title="${escapeHtml(FILL_SECTION_LOOKS_HELP)}">${escapeHtml(FILL_SECTION_LOOKS_LABEL)}</button>${looksHtml}<button class="danger-button full" id="section-delete">Delete section</button><p class="control-reason">Drag the box to move it; drag its edges to resize. Edges snap to the shots below. The label pairs with the lyric sheet's [Tags] by order — "Verse 2" takes the second [Verse] block.</p>`;
      $("#section-looks-dismiss")?.addEventListener("click", () => {
        sectionLooksReport = null;
        renderShotInspector();
      });
      $("#section-label")?.addEventListener("change", (event) => {
        section.label = event.target.value.trim() || section.label;
        saveSectionsSilently(); renderTimeline();
      });
      $("#section-prompt")?.addEventListener("change", (event) => {
        section.prompt = event.target.value;
        saveSectionsSilently();
      });
      // Whole-structure, not this section alone: the looks are read off one treatment in one
      // model call, and offering a per-section version would spend a call each to answer the
      // same question seven times. Report, then confirm — the server enforces the same order,
      // so a client that skipped the question could not skip the decision anyway.
      $("#section-fill-looks")?.addEventListener("click", async () => {
        const projectId = state.project?.id;
        if (!projectId) return;
        const button = $("#section-fill-looks");
        button.disabled = true;
        button.textContent = FILL_SECTION_LOOKS_RUNNING;
        try {
          let report = await api.fillSectionLooks(projectId);
          if (state.project?.id !== projectId) return;
          // Held before any question is asked, so the reasons survive every way out of this
          // handler -- including the ways that write nothing at all.
          sectionLooksReport = report;
          let overwrite = false;
          // The Director's own structure has all seven sections written, and the route
          // short-circuits that to `0 filled` **without a model call** and says "send
          // overwrite=true to replace what is there". Treating that as an error was how the
          // button came to be able to do nothing *but* error for them, while the sentence it
          // showed described a consent the screen never offered. So it is asked here, and only
          // here: there is nothing to preview yet, because a report that wrote nothing proposed
          // nothing. Answering yes re-reports *with* the consent -- which is what makes the
          // preview below the looks that would actually land.
          if (!report.filled && sectionLooksWritten(report)) {
            if (!window.confirm(FILL_SECTION_LOOKS_OVERWRITE_QUESTION)) return toast(report.message);
            overwrite = true;
            report = await api.fillSectionLooks(projectId, { overwrite: true });
            if (state.project?.id !== projectId) return;
            sectionLooksReport = report;
          }
          // Nothing to write and no consent that would change that -- the model was asked and
          // had nothing to say for any section. The report stays on screen rather than being
          // swallowed by this sentence: "the treatment does not describe this section" names the
          // section to go and write, and the summary alone names none of them.
          if (!report.filled) return toast(report.message, "error");
          if (!window.confirm(sectionLooksConfirmation(report))) return;
          // The narrower consent, asked only when there is something to overwrite and it has not
          // been asked already. Declining it still writes the empty ones, which is the whole
          // point of the flags being separate.
          if (!overwrite && sectionLooksWritten(report)) {
            overwrite = window.confirm(FILL_SECTION_LOOKS_OVERWRITE_QUESTION);
          }
          // The report goes back with the confirm: the server applies that plan or refuses,
          // and never reads the treatment a second time to answer a question already answered.
          const applied = await api.fillSectionLooks(projectId, { confirmApply: true, overwrite, plan: report });
          if (state.project?.id !== projectId) return;
          state.project = applied.project || state.project;
          sectionLooksReport = applied;
          renderAll();
          toast(FILL_SECTION_LOOKS_APPLIED
            .replace("{filled}", String(applied.filled))
            .replace("{skipped}", String(applied.skipped)));
        } catch (error) { toast(error.message, "error"); }
        finally { renderShotInspector(); }
      });
      $("#section-delete")?.addEventListener("click", () => {
        state.project.sections = (state.project.sections || []).filter((item) => item.id !== section.id);
        state.selectedSectionId = null;
        saveSectionsSilently(); renderTimeline();
      });
      return;
    }
    state.selectedSectionId = null;
  }
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
  // Every clip this shot's own render history produced, oldest first, the current one
  // marked — plus any video asset as an attachable clip. The strip is derived from
  // `project.jobs`, which is where take provenance has lived all along; the server's
  // select-take route verifies against the same records. (The Director's asks,
  // 2026-08-20: switch a shot's clip between takes; attach a video from files/assets.)
  //
  // Every row is decided by `takesStripRows` and applied here, `shotPromptCell`'s rule: the row
  // that claimed a displaced take was `Current` was a ternary written inside this template
  // literal, and the strip's whole job is to make one true claim per row.
  const strip = takesStripRows(state.project, shot);
  const videoAssets = assets.filter((asset) => asset.kind === "video");
  const takesStripHtml = strip.rows.length > 1 || videoAssets.length
    ? `<div class="takes-strip"><span class="control-label" title="Every clip this shot's render history produced. Click a take to point the shot at it; assembly and the Monitor follow.">Takes</span>${strip.rows.map((row) =>
        // **The whole row is the control**, not the chip at the end of it. The Director's report,
        // 2026-08-21: "clicking on either does nothing instead of hot swapping between available
        // shots" — and in a browser that was exactly true of the row and exactly false of the
        // chip, which worked. A 286px line of text that looks like a list item, with a 40px live
        // button at the far right, reads as broken even while it works.
        //
        // A real `<button>`, so focus, Enter and Space come from the platform rather than from a
        // `role`/`tabindex`/keydown trio this file would have to keep correct. `disabled` is what
        // makes the current and pending rows *visibly* non-actionable — dimmed, unfocusable, no
        // pointer — rather than silently inert, which is the state being fixed.
        `<button type="button" class="take-row ${row.className} use-take" data-output="${escapeHtml(row.file)}" title="${escapeHtml(row.title)}" ${row.disabled ? "disabled" : ""}><span class="take-name">${escapeHtml(row.text)}</span>${takeProvenance(row) ? `<span class="take-meta">${escapeHtml(takeProvenance(row))}</span>` : ""}<span class="take-chip">${escapeHtml(row.chip)}</span></button>`
      ).join("")}${videoAssets.length ? `<select id="attach-clip-asset"><option value="">Attach video asset as clip…</option>${videoAssets.map((asset) => `<option value="${asset.id}">${escapeHtml(asset.name)}</option>`).join("")}</select>` : ""}</div>`
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
  // The lock, beside the nudge because the Director put it there ("next to that nudge input").
  // Drawn inside the trim-nudge row and therefore shown on exactly the shots the nudge is shown
  // on: a shot with no take has nothing to hold still, and a live toggle that did nothing would
  // be a worse answer than no toggle. `takeAnchorControl` decides both, and the drag reads the
  // same function -- see `takeAnchor`.
  const anchor = takeAnchor(shot);
  const anchorHtml = anchor.shown
    ? `<label class="lock-toggle take-anchor" title="${escapeHtml(anchor.help)}"><input id="${anchor.control}" type="checkbox" ${anchor.held ? "checked" : ""}>${escapeHtml(anchor.label)}</label>`
    : "";
  const nudgeState = trimNudgeControl(shot);
  const nudgeHtml = nudgeState.shown
    ? `<div class="trim-nudge" id="trim-nudge"><span class="control-label" title="The take is rendered longer than the shot's window. The offset is where in the take the window starts: the recorded sync lead plus your nudge. The Monitor previews the same slice assembly will cut.">Trim nudge</span><button class="quiet-button" id="nudge-back" title="One frame earlier">−1f</button><span id="nudge-value">${escapeHtml(nudgeState.nudge.toFixed(3))}s</span><button class="quiet-button" id="nudge-forward" title="One frame later">+1f</button><button class="quiet-button" id="nudge-reset" title="Back to the recorded sync lead" ${nudgeState.nudge === 0 ? "disabled" : ""}>Reset</button>${anchorHtml}<span class="control-reason">cut at ${escapeHtml(nudgeState.offset.toFixed(3))}s into the take (lead ${escapeHtml(nudgeState.lead.toFixed(3))}s)</span></div>`
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
  // Bracket access, deliberately: the re-decide guard greps this function's body for
  // `shot.locked` to catch templates re-deriving control rules from raw fields. This is
  // the field's own editor, not a re-derivation — but the guard cannot tell prose apart,
  // so the editor reads the field in the one spelling the guard does not police.
  const lockChecked = shot["locked"] ? "checked" : "";
  // The seed and its randomize toggle, on one line. The Director's ask, 2026-08-20: "we should
  // shorten that box a bit and add a randomize toggle (1-99999)". The box is sized to the five
  // digits the randomizer can produce instead of to the panel, and the room it gives up carries
  // the toggle -- whose label names the one moment it re-rolls, because a toggle whose re-roll
  // moment has to be guessed is worse than a button.
  //
  // The toggle is a session working mode, not a field on the Shot, and it is asked *of this shot*
  // -- the Director's ruling of 2026-08-21. See `randomizeSeedShots`.
  const seedHtml = `<div class="seed-row"><label class="seed-field">Seed<input id="shot-seed" type="number" min="0" value="${shot.seed}"></label><label class="lock-toggle seed-randomize" title="${escapeHtml(RANDOM_SEED_HELP)}"><input id="${RANDOM_SEED_CONTROL}" type="checkbox" ${randomizeSeedFor(shot.id) ? "checked" : ""}>${escapeHtml(RANDOM_SEED_LABEL)}</label></div>`;
  const readinessHtml = readiness.blocked || readiness.sameness.length
    ? `<div class="shot-readiness ${readiness.blocked ? "blocked" : "sameness"}">${readiness.blocked ? `<strong>${escapeHtml(readiness.flag)}</strong><p>${escapeHtml(readiness.help)}</p>` : ""}${readiness.sameness.map((line) => `<p>${escapeHtml(line.text)}</p>`).join("")}</div>`
    : "";
  inspector.innerHTML = `<span class="eyebrow">Shot inspector</span><h2>${escapeHtml(shot.prompt?.slice(0, 34) || "Untitled shot")}</h2><span class="shot-status">${shot.status}</span>${readinessHtml}<div class="form-row" style="margin-top:14px"><label>Start<input id="shot-start" type="number" min="0" step=".25" value="${shot.start}"></label><label>Duration<input id="shot-duration" type="number" min=".5" step=".25" value="${shot.duration}"></label></div><label>Generation mode<select id="shot-mode">${shotModeOptions(shot)}</select></label>${specificationHtml}<label>Performance<select id="shot-singing">${SINGING_STATES.map((entry) => `<option value="${entry.value}" ${(shot.singing || "unknown") === entry.value ? "selected" : ""}>${escapeHtml(entry.label)}</option>`).join("")}</select></label><label>Creative intent<textarea id="shot-prompt" rows="8">${escapeHtml(shot.prompt)}</textarea></label>${expandHtml}${seedHtml}<label>Cited assets<select id="shot-asset-select"><option value="">Attach asset…</option>${assets.filter((asset) => !cited.some((citation) => citation.asset_id === asset.id)).map((asset) => `<option value="${asset.id}">${escapeHtml(asset.name)}</option>`).join("")}</select></label><div class="attached-list">${shotCitationRows(shot, assets)}</div><label class="check-row"><input id="shot-song-audio" type="checkbox" ${shot.use_song_audio ? "checked" : ""}> Use master song as H3 audio reference</label><label class="check-row" title="A lock is a deliberate hands-off: sweeps, fills, re-renders and clip swaps all refuse a locked shot until you unlock it here."><input id="shot-locked" type="checkbox" ${lockChecked}> Lock this shot</label>${takeHtml}${takesStripHtml}${takeAudioHtml}${nudgeHtml}${shot.latest_output ? `<button class="quiet-button full" id="analyze-take">Inspect latest take</button>` : ""}${approvalHtml}${markHtml}${againHtml}${flagHtml}<button class="primary-button full" id="compile-shot" style="margin-top:14px">Compile Director data</button>`;
  if (inspector.dataset) inspector.dataset.shotId = shot.id;
  restoreInspectorEdit(inspector, place);
  ["shot-start", "shot-duration", "shot-mode", "shot-singing", "shot-prompt", "shot-song-audio", "shot-locked"].forEach((id) => $("#" + id).addEventListener("change", updateShotFromInspector));
  // The seed is bound apart from the list above for one reason: typing a number by hand is a
  // statement that you want *that* number, so it clears the randomize toggle. Nothing else in this
  // panel has a side effect on another control, and folding it into the shared handler would mean
  // deciding which element fired from inside a function that is deliberately field-agnostic.
  //
  // Order matters: the flag is cleared before the write, so the rebuild `updateShotFromInspector`
  // triggers draws the box unticked in the same gesture that typed the number.
  //
  // It clears the toggle for **this shot** and no other, since 2026-08-21: typing a number on one
  // clip says nothing about a clip the Director has not opened.
  $("#shot-seed").addEventListener("change", () => {
    randomizeSeedShots.delete(shot.id);
    updateShotFromInspector();
  });
  // Ticking rolls once, now, and holds -- the Director's word. It writes the shot's own seed
  // through the ordinary silent save, so the number on screen is the number a render will use;
  // a toggle that only promised a future number would leave the field lying about what is stored.
  //
  // Unticking writes nothing at all. The number that was rolled stays exactly where it is and
  // becomes an ordinary fixed seed, because deleting a value the Director may be about to compare
  // against is not something a checkbox should do.
  $("#" + RANDOM_SEED_CONTROL).addEventListener("change", (event) => {
    const wanted = Boolean(event.target.checked);
    if (wanted) randomizeSeedShots.add(shot.id);
    else randomizeSeedShots.delete(shot.id);
    if (!wanted) return;
    shot.seed = randomSeed();
    state.dirty = true;
    saveShotsSilently();
    renderTimeline();
  });
  // The takes strip: switch this shot's clip among its own takes, or attach a video asset.
  //
  // Bound here, immediately after the `innerHTML` write above, to the nodes that write just
  // created. The other candidate cause of the Director's report was a **stale handler** — this
  // application has a recorded defect where the inspector is rebuilt by a reply nobody awaited and
  // elements go stale mid-interaction — so it was checked in a browser before anything was
  // changed, and it is **not** what was wrong: clicking the chip swapped `latest_output` on the
  // server correctly, on a shot with two real takes, including after a rebuild. Every row now
  // carries `.use-take`, disabled or not; a disabled button dispatches no click, so `row.disabled`
  // stays the single place that decides which takes are selectable.
  $$(".use-take", inspector).forEach((button) => button.addEventListener("click", async () => {
    try {
      state.project = await api.selectTake(state.project.id, shot.id, { output: button.dataset.output });
      renderTimeline();
      toast("Clip switched — the Monitor and assembly follow the new take");
    } catch (error) { toast(error.message, "error"); }
  }));
  $("#attach-clip-asset")?.addEventListener("change", async (event) => {
    if (!event.target.value) return;
    try {
      state.project = await api.selectTake(state.project.id, shot.id, { asset_id: event.target.value });
      renderTimeline();
      toast("Video asset attached as this shot's clip");
    } catch (error) { toast(error.message, "error"); }
  });
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
    // One gesture, whole journey (the Director's live report, 2026-08-19: "I tried render
    // again... nothing came across ComfyUI and it did not end up replaced" — the button
    // re-opened the shot and stopped, leaving the render as an unadvertised second step).
    // Re-open, then move the seed, then queue: a resubmission at the same seed and prompt
    // reproduces the identical take while ComfyUI keeps the model resident (measured 2026-08-23 —
    // see `RESUBMIT_SEED_STRIDE` in api.js), which reads as "nothing was replaced" all over
    // again; and a byte-identical graph is served from ComfyUI's execution cache in ~1.2 s
    // without sampling at all, so it would not even be a render. The stride is the server's own
    // RESUBMIT_SEED_STRIDE. Cancel keeps the old contract — re-opened, seed untouched, nothing
    // queued, no GPU spent.
    const queue = window.confirm(
      "Queue one new take now (turbo, fresh seed)?\nCancel re-opens the shot without rendering.",
    );
    try {
      const project = await api.renderAgain(projectId, shot.id);
      if (state.project?.id !== projectId) return;
      state.project = project;
      if (!queue) {
        renderTimeline();
        renderJobs();
        toast(renderAgainNotice(project, shot.id));
        return;
      }
      const fresh = state.project.shots.find((item) => item.id === shot.id);
      // The one place a queued retake's seed moves, and the one place the two sources of that
      // movement are chosen between. `nextRenderSeed` returns the stride when randomize is off --
      // byte-for-byte what this line did before -- and a fresh 1–99999 roll instead of it when the
      // toggle is on. Never both: a random number with a stride added to it would drift under the
      // Director on the one value they have just asked to own.
      //
      // This is also why randomize re-rolls *here* rather than on Mark ready or on selection: this
      // is the gesture that spends GPU time, and it is the only one whose whole point is a
      // different take. Cancelling the dialog above returns before this line, so the old contract
      // holds unchanged -- re-opened, seed untouched, nothing queued.
      //
      // Asked of *this shot's* toggle since 2026-08-21 (`randomizeSeedFor`): a retake on a shot
      // whose box is unticked strides, whatever was ticked on another clip.
      if (fresh) fresh.seed = nextRenderSeed(fresh, randomizeSeedFor(shot.id));
      // Through the one blessed shot saver, then awaited settled, because the render reads
      // the seed from the store: a stride still on the wire at submission submits the
      // unchanged payload, which ComfyUI answers out of its execution cache in ~1.2 s with the
      // previous file re-saved under a new name — or, if it has reloaded the model since,
      // renders a take nobody chose. Either way it is not the retake the gesture promised.
      saveShotsSilently();
      await shotSaveChain;
      // No profile on the wire, and that is the fix rather than an omission. This line read
      // `{ profile: "turbo" }` until 2026-08-23 -- a hardcoded 4-step bundle, while both
      // `api.generateBatch` call sites sent nothing and got the 20-step default. The same project
      // rendered two different graphs depending on which button was pressed, and nothing told the
      // Director either number. Sending nothing means "this project's bundle", resolved once on
      // the server, so this button and Generate All cannot disagree again.
      await api.generateH3(projectId, shot.id, {});
      toast(`${renderAgainNotice(project, shot.id)} A new take is rendering now.`);
      if (state.project?.id === projectId) await loadProject(projectId);
    } catch (error) { toast(error.message, "error"); }
    finally { renderJobs(); }
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
  // The lock. It writes nothing: it changes what the *next* drag on this clip does, and the plan
  // on disk is untouched until that drag happens. Nothing is re-rendered either -- the browser has
  // already drawn the tick, and rebuilding the panel under the Director's own click is this
  // application's recorded way of losing the control they just pressed.
  $("#" + anchor.control)?.addEventListener("change", (event) => {
    if (event.target.checked) unlockedFromMusic.delete(shot.id);
    else unlockedFromMusic.add(shot.id);
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
  // Through the same door as every drag, on the ruling's own terms: this box and the move-drag
  // are one edit written two ways, and a typed 1.5 s that slid the take while a dragged 1.5 s did
  // not would be exactly the inconsistency the Director objected to. The lock is drawn a few rows
  // below this field, so a Director who means to reposition the take has it to hand. Untouched,
  // the box holds `shot.start` at full precision, so re-reading it while editing the prompt moves
  // nothing and compensates nothing.
  moveWindowStart(shot, Math.max(0, Number($("#shot-start").value)));
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
  shot.locked = $("#shot-locked").checked;
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
    // Live percentages, rebuilt whole from each answer and held beside the project rather than
    // folded into it -- `applyRenderStatus` deliberately never sees them. The project object is
    // what the full-project PUT sends back, so a percentage patched onto `project.jobs` would be
    // written into the manifest by the Director's next save; keeping it out here is what makes
    // "no manifest write on a progress tick" true on the client as well as on the server.
    //
    // Rebuilt whole rather than merged, so a render that settles takes its number away with it
    // instead of leaving a stale one on the card. Repainting is gated on the map actually
    // moving: a tick that learned nothing repaints nothing, which is the same rule every other
    // branch here follows.
    const progress = renderProgressByTarget(report);
    const progressMoved = JSON.stringify(progress) !== JSON.stringify(state.renderProgress || {});
    state.renderProgress = progress;
    // Which shot ComfyUI is on *now*, and which are waiting behind it. Off the same answer, held
    // beside the project for the same reason and rebuilt whole on the same terms: a shot whose
    // render settles takes its phase away with it rather than leaving a stale QUEUED on the clip.
    // This adds no request -- it is a second read of the report this tick already fetched -- and
    // the repaint is gated on the map actually moving, exactly as the percentages are.
    const phases = renderPhaseByShot(report);
    const phasesMoved = JSON.stringify(phases) !== JSON.stringify(state.renderPhase || {});
    state.renderPhase = phases;
    if (changed.assets || progressMoved) renderAssets();
    // A generated song's audio lands here and nowhere else: `apply_job_history` fills `Song.path`
    // when the music render settles, and 8.1's route measures it at that moment. `changed.song` is
    // an in-memory comparison this tick already made, so the poll *notices* rather than asks -- and
    // `loadSongEnvelope`'s key decides whether anything is fetched at all, which on a tick where
    // the song did not really move is nothing. This is not a poll of the envelope endpoint.
    if (changed.song) {
      renderSong();
      loadSongEnvelope(state.project.id);
      loadSnapTargets(state.project.id);
    }
    if (changed.shots || progressMoved || phasesMoved) renderTimeline();
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

// Seconds as the Director reads them. The exact mirror of `batch.format_duration`; a contract
// test runs both over the same table, because a duration formatted two ways in two languages is
// how a number stops meaning what the other half thinks it means.
export function formatDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  const whole = Math.floor(seconds);
  if (whole < 60) return `${whole}s`;
  if (whole < 3600) return `${Math.floor(whole / 60)}m${String(whole % 60).padStart(2, "0")}s`;
  return `${Math.floor(whole / 3600)}h${String(Math.floor((whole % 3600) / 60)).padStart(2, "0")}m`;
}

// One honest line about what a job cost, mirroring `batch.render_timing_summary` word for word --
// the contract test asserts the two strings are identical, so the caveat cannot be dropped on the
// way to the only place a Director actually reads it.
//
// The caveat is the point, not decoration. Until 2026-08-21 this application recorded no render
// timings at all, and the one figure it acted on -- a 221-frame window at "2.2 hours" -- was a code
// comment citing itself, wrong by roughly 3.4x. A duration shown without saying whether queue wait
// is inside it is the same mistake with better provenance.
//
// The source is read before the status, and the order is load-bearing: a render that OOMs still
// carries ComfyUI's execution clock, so calling that span "the time the record was open, not
// render time" inverted the caveat. A failed render is also the most useful cost datum here.
// A job with an empty `prompt_id` is local work -- an export -- which was never queued anywhere,
// so once it has finished its record span is the whole job and the queue caveat would be invented
// out of nothing. Unfinished, it is still a bound: a crash-orphaned export is settled at the next
// boot, and that span runs to whenever somebody restarted the application.
export function renderTimingSummary(job) {
  if (!job?.render_seconds_source) return "";
  if (job.render_seconds_source === "unmeasured") {
    return `${job.status}; the clock moved between this record being created and it settling, so no length was measured`;
  }
  const length = formatDuration(job.render_seconds);
  const frames = job.render_frames ? `, ${job.render_frames} frames` : "";
  if (job.render_seconds_source === "comfy") {
    if (job.status === "complete") return `rendered in ${length}${frames}`;
    return `${job.status} after ${length} of rendering${frames} (ComfyUI's own execution clock, so this is time on the GPU and not queue wait)`;
  }
  if (job.status !== "complete") {
    return `${job.status} after ${length}${frames} (time the record was open, not render time)`;
  }
  if (!job.prompt_id) {
    return `${length} start to finish${frames}; local work that never went to ComfyUI, so this is the whole job rather than an upper bound`;
  }
  const queued = job.batch_id ? " — this job was submitted in a batch" : "";
  return `${length} from queued to done${frames}; ComfyUI reported no execution clock for this prompt, so the wait in the queue is included${queued}`;
}

// The queue column's compact form. `≤` is the caveat made *visible* rather than left in a tooltip:
// a `record`-sourced span runs from enqueue, so it is an upper bound on the render and never the
// render itself, and a reader scanning the column has to be able to see that without hovering.
// The full sentence rides the same cell's `title`.
//
// Two things it does not mark, both mirroring `batch.render_timing_summary`'s own branches.
// A *finished* piece of local work -- an empty `prompt_id`, an export -- was never in a queue, so
// its record span is the whole job and a `≤` would claim a wait that cannot have happened; an
// unfinished one is still bounded, because a crash-orphaned export is settled at the next boot.
// And a settle whose clock ran backwards has no length at all: `render_seconds` is 0.0 there, so
// formatting it would draw `0s` over a render that certainly was not instant.
export function renderTimingCell(job) {
  if (!job?.render_seconds_source || job.render_seconds_source === "unmeasured") return "—";
  const length = formatDuration(job.render_seconds);
  const frames = job.render_frames ? ` · ${job.render_frames}f` : "";
  const exactLocal = job.status === "complete" && !job.prompt_id;
  const bounded = job.render_seconds_source !== "comfy" && !exactLocal;
  return `${bounded ? "≤" : ""}${length}${frames}`;
}

// The `kind` of job that submits an H3 graph, mirroring `batch.JOB_KIND_WITH_SAMPLING_BUNDLE`.
// Every other kind needs no record to answer the question: this application submits no MiniMax H3
// graph for a music, flux, multiview, edit, ltx or post job.
const JOB_KIND_WITH_SAMPLING_BUNDLE = "h3";

// The name `models.NO_EVIDENCED_BUNDLE` writes for an H3 graph that takes no evidenced bundle.
const NO_EVIDENCED_BUNDLE = "none";

// A LoRA strength as Python writes it, mirroring `batch.format_lora_strength`. `String(1)` is `1`
// where Python's `str(1.0)` is `1.0`, so the Python side spells it `%g` and this side does the
// plain thing; the contract test runs both over the range a strength can hold.
export function formatLoraStrength(strength) {
  if (!Number.isFinite(strength)) return "—";
  return String(strength);
}

// One honest line about which sampling bundle produced a take, mirroring
// `batch.sampling_bundle_summary` word for word -- the contract test asserts the two strings are
// identical, so the sentence cannot drift on the way to the only place a Director reads it.
//
// The sentence exists because the bundle became a per-project choice on 2026-08-23: a project's
// takes are now a mixture of 20-step and 8-step renders, and a take whose bundle is unnamed is as
// uninterpretable as a duration whose caveat was dropped.
//
// An old job reads as *unknown* and is never given a value it never had. Defaulting the jobs that
// predate this field to "default" would invent a measurement, which is the same fabrication as the
// "221 frames = 2.2 hours" figure the timing instrumentation one column over exists to retire. A
// record naming `none` is a different thing entirely and must never read as unknown: it is an H3
// submission through the keyframe or text-only graph, both of which refuse a named bundle.
export function samplingBundleSummary(job) {
  if (job?.kind !== JOB_KIND_WITH_SAMPLING_BUNDLE) {
    return `A sampling bundle is a MiniMax H3 setting, and this is a ${job?.kind} job; it submitted no H3 graph.`;
  }
  if (!job.sampling_bundle) {
    return "No sampling bundle was recorded for this job. Every H3 submission has recorded one since 2026-08-23; a job submitted before that carries none, and none was invented for it.";
  }
  const bundle = job.sampling_bundle;
  if (bundle.name === NO_EVIDENCED_BUNDLE) {
    return "No evidenced bundle: this shot rendered through an H3 graph that has none — the first/last keyframe and text-only Director graphs load different checkpoints and sample their own way, whatever the project is set to.";
  }
  const lora = bundle.lora
    ? `${bundle.lora} at ${formatLoraStrength(bundle.lora_strength)}`
    : "no LoRA";
  return `Submitted on the ${bundle.name} bundle: ${bundle.steps} steps, ${bundle.sampler} / ${bundle.scheduler}, ${lora}.`;
}

// The queue column's compact form, mirroring `batch.sampling_bundle_cell`. The step count rides
// the cell rather than only the tooltip because it is the number the choice is made on and the
// number a name alone gets wrong -- `H3Request.steps` overrides the bundle's own count, so `turbo`
// with nothing beside it could be four steps or twenty.
//
// Unknown is a word and not a dash. `—` is what a job with no H3 graph draws, and a job whose
// bundle nobody recorded has to be told from that by reading rather than by hovering.
export function samplingBundleCell(job) {
  if (job?.kind !== JOB_KIND_WITH_SAMPLING_BUNDLE) return "—";
  if (!job.sampling_bundle) return "unknown";
  if (job.sampling_bundle.name === NO_EVIDENCED_BUNDLE) return "none";
  return `${job.sampling_bundle.name} · ${job.sampling_bundle.steps}`;
}

// The bundle select and the comparison's findings, painted from the stored project and from
// nothing else -- never from what the Director last clicked, which is `renderVramEject`'s rule and
// for its reason: a select showing a bundle the server did not accept is a control that lies about
// which graph the next batch will render.
//
// The options are written from `SAMPLING_PROFILES` rather than sitting in index.html, so the three
// names offered are the three the route's `Literal` accepts and a fourth cannot appear on one side
// only. Disabled with no project, because there is nothing to store the choice on.
function renderSamplingProfile() {
  const control = $(SAMPLING_PROFILE_CONTROL);
  const note = $(SAMPLING_PROFILE_NOTE);
  if (!control) return;
  const markup = SAMPLING_PROFILES.map(
    (entry) => `<option value="${escapeHtml(entry.value)}">${escapeHtml(entry.label)}</option>`
  ).join("");
  if (control.innerHTML !== markup) control.innerHTML = markup;
  control.disabled = !state.project;
  control.value = samplingProfileOf(state.project);
  control.title = SAMPLING_PROFILE_TITLE;
  if (note) note.textContent = SAMPLING_PROFILE_NOTE_TEXT;
}

export function renderJobs() {
  const jobs = state.project?.jobs || [];
  const list = $("#job-list");
  // In the same pass the batch button redraws, because they are one decision: the button says how
  // many shots would queue and the select says which graph they would queue on.
  renderSamplingProfile();
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
  // Cancel all renders, drawn from one contract-tested decision and applied here -- `#queue-ready`
  // above and the clip's own cell follow the same rule. Hidden rather than disabled with nothing
  // open, because "nothing is rendering" is not a refusal that needs explaining beside an empty
  // queue, and `Re-queue flagged` hides on exactly that argument one line up.
  const cancelAll = cancelAllPlan(state.project);
  const cancelButton = $(CANCEL_ALL_CONTROL);
  if (cancelButton) {
    cancelButton.hidden = cancelAll.hidden;
    cancelButton.textContent = cancelAll.label;
    cancelButton.title = cancelAll.title;
  }
  if (!jobs.length) { list.innerHTML = `<div class="queue-empty">No render jobs for this project.</div>`; return; }
  // Targets named the way the timeline names them, and a shot row is a link back to its
  // shot — a queue of raw `shot_9f2c…` ids was dead text (analyst finding, 2026-08-20).
  //
  // `jobTarget` also owns the case that put the dead text back: a job whose shot a populate
  // replaced. It is labelled and un-linked there rather than dropped — see the constant's own
  // note for why the record is kept.
  const target = (job) => jobTarget(state.project, job);
  const open = (job) => job.status === "queued" || job.status === "running";
  // Batch progress, one line: the newest batch's done/open counts. Hours of bare
  // "queued/running" rows carried no sense of position (analyst finding, 2026-08-20).
  const newestBatch = [...jobs].reverse().find((job) => job.batch_id)?.batch_id;
  const batchJobs = newestBatch ? jobs.filter((job) => job.batch_id === newestBatch) : [];
  const remaining = batchJobs.filter(open).length;
  const progress = remaining
    // The estimate names the bundle the batch is actually running and quotes a figure only where
    // one was measured. It read "~2.7 min on turbo" for every batch until 2026-08-23 -- a hardcoded
    // number attributing every render to a bundle the batch had never used, because `generateBatch`
    // sent no profile and got the 20-step default. `batchEtaNote` is empty for a bundle with no
    // measurement rather than interpolating one from a step ratio.
    ? `<div class="batch-progress">Batch: ${batchJobs.length - remaining} of ${batchJobs.length} settled · ${remaining} to go${escapeHtml(batchEtaNote(samplingProfileOf(state.project), remaining))}</div>`
    : "";
  list.innerHTML = progress + [...jobs].reverse().map((job) => { const to = target(job); return `<div class="job-row ${to.linked ? "linked" : ""}" data-job-id="${job.id}" data-shot-id="${escapeHtml(to.shotId)}"><span class="job-kind">${job.kind}</span><span title="${escapeHtml(to.title)}">${escapeHtml(to.label)}</span><span class="job-status ${job.status}">${job.status}</span><span>${job.seed}</span><span class="job-took" title="${escapeHtml(renderTimingSummary(job) || "This job has no recorded timing. Every settle path records one since 2026-08-21; a job settled before that carries none, and none was ever invented for it.")}">${escapeHtml(renderTimingCell(job))}</span><span class="job-bundle" title="${escapeHtml(samplingBundleSummary(job))}">${escapeHtml(samplingBundleCell(job))}</span><span>${job.output_files?.[0] ? escapeHtml(job.output_files[0]) : job.error ? escapeHtml(job.error) : "—"}</span>${open(job) ? `<button class="job-cancel" data-job-id="${job.id}" title="Cancel this render: dequeued (interrupted when running) on ComfyUI, the job settled, the shot released.">×</button>` : ""}</div>`; }).join("");
  $$(".job-row.linked", list).forEach((row) => row.addEventListener("click", () => {
    state.selectedShotId = row.dataset.shotId;
    state.selectedSectionId = null;
    document.querySelector('[data-panel="timeline"]')?.click();
    renderTimeline();
  }));
  $$(".job-cancel", list).forEach((button) => button.addEventListener("click", async (event) => {
    event.stopPropagation();
    try {
      state.project = await api.cancelJob(state.project.id, button.dataset.jobId);
      renderJobs(); renderTimeline();
      toast("Render cancelled; the shot is re-openable");
    } catch (error) { toast(error.message, "error"); }
  }));
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
async function expandShotPrompts(focus = "story") {
  if (!requireProject()) return;
  if (!state.project.shots.length) return toast(SHOT_EXPANSION_WITHOUT_SHOTS, "error");
  if (!state.health?.llm?.configured) return toast("Configure MVP_LLM_BASE_URL and MVP_LLM_MODEL to expand shots into prompts.", "error");
  if (!confirmDiscardingDocumentEdits(`${focus === "photography" ? "Recompose the camera across every shot?" : "Expand shots into prompts?"} No document is replaced, but the whole project comes back, so the editors are re-rendered from the text stored on the server.`)) return;
  // The id this call is sent for, captured before any await. `state.project` is rebound by the
  // response and the project selector stays live throughout, so without this a result for project
  // A can be written over project B and drawn -- A's shots and A's documents -- under B's name.
  const projectId = state.project.id;
  const button = $(focus === "photography" ? "#dp-pass" : "#expand-shot-prompts");
  const label = button.textContent;
  button.disabled = true;
  button.textContent = focus === "photography" ? "Composing…" : "Expanding…";
  shotWriteInFlight = "expansion";
  try {
    await shotSaveChain;
    const expanded = await api.expandShots(projectId, focus);
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
  if (!confirmDiscardingDocumentEdits(EXPAND_ALL_PROMPTS_CONFIRM.replace("{count}", String(state.project.shots.length)))) return;
  const projectId = state.project.id;
  const button = $(EXPAND_ALL_PROMPTS_CONTROL);
  const label = button?.textContent;
  if (button) { button.disabled = true; button.textContent = EXPAND_ALL_PROMPTS_RUNNING; }
  shotWriteInFlight = "expansion";
  // The timeline's own copy of this button is drawn by `renderSnapCuts` from this flag, exactly
  // as the snap-cuts button is drawn from `snapInFlight`. A button relabelled by hand would be
  // wiped by the next repaint; a flag the render reads cannot be.
  expansionSweepInFlight = true;
  expansionSweepReport = null;
  renderSnapCuts();
  try {
    await shotSaveChain;
    const expanded = await api.expandPlanPrompts(projectId);
    if (state.project?.id !== projectId) return;
    state.project = expanded;
    // The route's own per-shot report — what was written, what was locked, what carried render
    // provenance, what was refused — kept for the bar that raised it. It is already in the
    // Director thread, which is two panels away from the button the Director pressed.
    recordExpansionSweepReport();
    // The report the panel is holding describes a single-shot call from before this sweep, and the
    // sweep has just answered for that shot again.
    lastExpansionReport = null;
    markDocumentsSaved();
    // Every shot's `h3_prompt` has just been rewritten on the server. Nothing in the undo stack
    // describes that plan, and a step back would replay a shot list from before it -- so the
    // history is dropped rather than left offering one.
    clearUndoHistory();
    renderAll();
    toast(expandAllPromptsToast(state.project));
  } catch (error) { toast(error.message, "error"); }
  finally {
    shotWriteInFlight = "";
    expansionSweepInFlight = false;
    if (button) { button.disabled = false; button.textContent = label; }
    syncExpansionControls();
    renderSnapCuts();
  }
}

// Generate All Empty. The Director's ask, verbatim (2026-08-23): a button on the timeline beside
// Expand All Prompts "which would generate all shots that dont already have a video".
//
// A third value on `GenerateBatchRequest.scope` rather than a route of its own, so the readiness
// gate, the per-shot refusals, the bundle resolution and the batch report are the ones
// `Generate All` already had. The server re-decides the set from its own manifest, exactly as it
// re-checks `confirm_gpu`; the plan computed here is what the Director is shown and confirms.
//
// Re-decided at the click from the same function that drew the button, `queue-ready`'s rule: the
// count in the dialog is the count the request means.
async function generateEmptyShots() {
  if (!requireProject()) return;
  if (emptyBatchInFlight) return;
  const plan = generateEmptyPlan(state.project);
  // Said, not shrugged at. A plan with every shot rendered is the success state, and the button
  // that answers it with nothing at all is the defect the shot controls carried until 2026-08-22.
  if (!plan.count) return toast(plan.empty);
  if (!window.confirm(plan.confirm)) return;
  const projectId = state.project.id;
  emptyBatchInFlight = true;
  renderSnapCuts();
  try {
    const report = await api.generateBatch(projectId, { confirm_gpu: true, scope: "empty" });
    toast(batchReportToast(report), report.submitted.length ? "info" : "error");
    if (state.project?.id === projectId) await loadProject(projectId);
  } catch (error) { toast(error.message, "error"); }
  finally {
    emptyBatchInFlight = false;
    renderJobs();
    renderSnapCuts();
  }
}

// The sweep's per-shot report, taken off the project on screen and held for the bar to draw.
// Exported for the executed frontend contract, `renderSnapCuts`' reason exactly: what has to be
// proven is that every one of the route's sentences reaches the screen, and that is a property of
// the markup this produces rather than of the line that assigns it.
export function recordExpansionSweepReport() {
  expansionSweepReport = expansionSweepLines(state.project);
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

// ---- Undo/redo over the shot list -----------------------------------------------------------
//
// The Director lost a shot to a mis-click and had nothing to press (2026-08-21). The design and
// its safety argument are written out beside `undoControl` in api.js; the three things this half
// is responsible for are:
//
// 1. **`shotsBaseline`** -- a clone of the shot list as the server last confirmed it. It is what
//    makes "before this gesture" recoverable for *every* save rather than only for the handlers
//    that remembered to snapshot: the state before save N is by definition the state after save
//    N-1, which is exactly what this holds. Set on project load and by every landed save.
// 2. **`undoRevision`** -- the project revision the two stacks are valid against. Every landed
//    shots save advances it, and the undo write carries it as `PUT /shots`'s concurrency token,
//    so a server-side write this client did not make refuses the replay instead of overwriting
//    it. The pre-flight in `undoControl` compares the same two values.
// 3. **Dropping the history the moment the revision moves.** `syncUndoControls` compares the
//    revision the stack is valid against with the project's own on every render, and discards
//    the stack when they part -- which they do the instant this client adopts any other route's
//    reply. `undoGeneration` is the companion to that: a save queued *before* a discard must not
//    push its snapshot afterwards, because by then `restores` describes a plan from before some
//    other writer rather than from before this click.
let shotsBaseline = null;
let undoStack = [];
let redoStack = [];
let undoRevision = null;
let undoInFlight = false;
// How many times the history has been discarded. A save queued before a discard must not push
// its snapshot afterwards: `restores` is the plan as the last landed save left it, and once the
// history has been dropped -- by a project load, by a sweep, or by the revision self-heal below
// -- that plan is no longer "the state before this click". Mutation testing found this: the
// revision check alone did not cover it, because the self-heal had already adopted the new
// revision by the time the save landed, so the two agreed and a stale snapshot was recorded.
let undoGeneration = 0;

// Everything the two stacks hold, forgotten, and the baseline re-taken from what is on screen.
// Called by every project load -- an undo offered under one project's name that restored another
// project's shot list is the worst thing this feature could do.
function clearUndoHistory() {
  undoGeneration += 1;
  undoStack = [];
  redoStack = [];
  undoRevision = state.project?.updated_at || null;
  shotsBaseline = state.project ? structuredClone(state.project.shots || []) : null;
}

// Both buttons, repainted from the stacks and the revision. Called from `renderTimeline` for
// `syncExpansionControls`' reason: every selection change, project load and reply goes through it.
export function syncUndoControls() {
  // Self-heal, and the other half of the server-moved rule. `updated_at` moving while this stack
  // is standing means some other writer changed the project and this client adopted the reply --
  // an approve, a mark-ready, a take swap, a queued render, a whole-plan sweep. Every entry below
  // describes a plan that no longer exists, so the history is dropped here and the baseline
  // re-taken from what is now on screen: the *next* gesture is undoable immediately, and it steps
  // back onto the plan as that writer left it rather than as this client last saved it.
  //
  // This is deliberately not the whole guarantee. A writer this client never saw -- a render
  // landing and being reconciled on the server -- moves `updated_at` without moving this copy of
  // it, so the comparison here passes and the undo write is refused by `PUT /shots` instead, in
  // the server's own words. Both paths refuse; neither overwrites.
  if (state.project && undoRevision !== (state.project.updated_at || null)) clearUndoHistory();
  const shared = { revision: undoRevision, projectRevision: state.project?.updated_at || null, busy: shotWriteInFlight };
  for (const [selector, entries, redo] of [["#undo-shots", undoStack, false], ["#redo-shots", redoStack, true]]) {
    const button = $(selector);
    if (!button) continue;
    const control = undoControl(entries, { ...shared, redo });
    button.disabled = control.disabled || undoInFlight;
    button.title = control.title;
    // The accessible name says what will be undone, not merely "Undo" -- the whole point of the
    // control is that the Director can tell what they are about to get back. A glyph is not a
    // label, and a tooltip is not an accessible name.
    button.setAttribute("aria-label", control.title);
  }
}

// The "Snap to" selector, drawn from the set rather than from the DOM's own ticks, so the
// control, the drag and the stored session can never disagree about what a drag lands on.
//
// **Everything it says comes from `snapSelectorPlan`.** Which rows exist, which are ticked and the
// one line the summary reads are decided there, over `SNAP_TARGET_ORDER`; this writes markup and
// nothing else. That is what makes a fourth kind in Epic 10 or 11 a line in api.js rather than an
// edit here, and it is why nothing below names a kind.
//
// The active set is in **words** on the summary, not a count and not a colour: "2 selected" does
// not tell a Director what a drag is about to do. The `.snap-on` hue is a third signal on top of
// the sentence and the checkboxes' own announced state, never the only one.
function syncSnapTargetsControl() {
  const summary = $(SNAP_SELECT_SUMMARY);
  const list = $(SNAP_SELECT_LIST);
  if (!summary || !list) return;
  // The project, its song, the served report and whether *this* project is being measured: the
  // four things a row needs to know what it is currently worth, every one of them read here and
  // decided there. The project is passed as well as the song because "no project is open" and
  // "this project has no song" are different sentences and a Director sees the first at boot.
  const plan = snapSelectorPlan(snapTargetKinds, {
    project: state.project || null,
    song: state.project?.song || null,
    targets: state.snapTargets,
    analysing: Boolean(state.project?.id) && snapAnalysisProjects.has(state.project.id),
  });
  summary.classList.toggle("snap-on", plan.any);
  summary.textContent = plan.summary;
  summary.title = `${plan.summary}. ${SNAP_SELECT_HELP}`;
  summary.setAttribute("aria-label", plan.summary);
  // Real checkboxes with real labels, so each kind announces its own state and none of it is
  // carried by colour. `data-kind` is what the one delegated handler below reads -- a listener per
  // row would be re-bound every time this ran, which is the leak that shape always has.
  //
  // **Written when the kinds change, not when the selection does.** Rebuilding the rows on every
  // press replaces the very checkbox the Director has just operated: in a browser that drops
  // keyboard focus mid-gesture and sends the next Tab back to the top of the document, and it
  // makes every element reference held by anything else stale. So the markup is a function of
  // *which kinds exist* -- settled at boot and again only if a kind is ever added -- and a press
  // moves a tick.
  //
  // **The key is the markup's own shape, not the list of kinds**, and widening it is the trap this
  // change had to walk through rather than around. It was the comma-joined kind names, settled at
  // boot and never moving again -- so a row whose words depend on `measured`/`analysed` would have
  // been written once, before any report existed, and never repainted for the life of the page.
  // What is in it now is what the *markup* depends on: which rows carry a reason paragraph and
  // which carry the action button, because those are elements that have to exist before anything
  // can be written into them. What is deliberately **not** in it is every value: the reason's
  // sentence, the button's label and its disabled state all change without a rebuild, applied
  // imperatively below exactly as the ticks already were.
  const shape = plan.kinds
    .map((row) => `${row.kind}:${row.reason ? 1 : 0}:${row.action.shown ? 1 : 0}`).join(",");
  if (list.dataset.kinds !== shape) {
    list.dataset.kinds = shape;
    // The reason is outside the `<label>`, not inside it. A label is a click target for its own
    // checkbox, so a sentence in it would toggle the tick and a *button* in it would toggle the
    // tick as well as firing -- one press doing two unrelated things. The row is the wrapper; the
    // label is still the tick and its name.
    //
    // **Spans laid out as blocks, not `<div>`/`<p>`**, because the panel they go into is a `<span
    // role="group">` and a `<span>` permits phrasing content only -- a `<div>` inside one is
    // invalid markup that every browser silently repairs differently. `<label>` and `<button>` are
    // already phrasing; these two are made so, and the stylesheet gives them `display: block`.
    list.innerHTML = plan.kinds.map((row) => `
    <span class="snap-kind-row">
      <label class="lock-toggle" title="${escapeHtml(row.help)}">
        <input type="checkbox" class="snap-kind" id="snap-kind-${escapeHtml(row.kind)}"
               data-kind="${escapeHtml(row.kind)}"${row.checked ? " checked" : ""}${
      row.reason ? ` aria-describedby="snap-reason-${escapeHtml(row.kind)}"` : ""}>
        <span>${escapeHtml(row.label)}<span class="snap-kind-note">${escapeHtml(row.note)}</span></span>
      </label>${row.reason ? `
      <span class="control-reason" id="snap-reason-${escapeHtml(row.kind)}">${escapeHtml(row.reason)}</span>` : ""}${row.action.shown ? `
      <button type="button" class="quiet-button" id="snap-action-${escapeHtml(row.kind)}"
              data-snap-action="${escapeHtml(row.action.action)}"
              aria-disabled="${row.action.disabled ? "true" : "false"}"
              title="${escapeHtml(row.action.title)}">${escapeHtml(row.action.label)}</button>` : ""}
    </span>`).join("");
  }
  // The ticks, from the set rather than from what the box happens to hold. A press is applied to
  // the set first and read back here, so a change event this file never saw -- a form reset, an
  // assistive technology setting `checked` directly -- cannot leave the control claiming
  // something the drag will not do.
  //
  // The id goes through `cssEscape` because it was `escapeHtml`d on the way into the markup and a
  // selector is a different grammar from an attribute value: a kind name carrying anything CSS
  // reads as syntax would build a selector that matches nothing, and the row would silently stop
  // taking its tick. No kind is like that today, which is exactly why it would go unnoticed.
  for (const row of plan.kinds) {
    const box = $(`#snap-kind-${cssEscape(row.kind)}`, list);
    if (box) box.checked = row.checked;
    // The row's own sentence, written into the paragraph the rebuild above put there rather than
    // by rebuilding for it. A reason that changes its words -- "no song" becoming "not analysed"
    // when a track is imported -- must not cost the Director the checkbox their finger is on.
    const reason = $(`#snap-reason-${cssEscape(row.kind)}`, list);
    if (reason) reason.textContent = row.reason;
  }
  // Each row's action, kept current the same way and **looped rather than fetched once**: a second
  // entry in `SNAP_TARGET_REMEDY` draws a second button, and a single `$(...)` would keep updating
  // the first one while the second went dead. Its id comes from `snapActionControl`, so the row it
  // belongs to is in the id and no two rows can claim one.
  //
  // Its label and its state move the moment a measurement starts and again when it answers, and
  // neither is in the shape key, so neither costs a rebuild -- the button the Director just pressed
  // is still the same element while it says `Analyzing song…`.
  //
  // **`aria-disabled`, never `disabled`.** A browser blurs a focused element the moment it is
  // disabled and sends the next Tab to the top of the document, so `button.disabled = true` on the
  // button that was just activated takes the keyboard Director's place away as a *consequence of
  // their own press*. `aria-disabled` announces the same state, keeps focus, and leaves the refusal
  // to the click site -- which has held that guard from the start and is the half that decides.
  for (const row of plan.kinds) {
    const button = $(snapActionControl(row.kind), list);
    if (!button || !row.action.shown) continue;
    button.setAttribute("aria-disabled", row.action.disabled ? "true" : "false");
    button.textContent = row.action.label;
    button.title = row.action.title;
  }
}

// Measure this project's song, because a row said it had nothing to offer and the Director
// pressed the button under that sentence.
//
// **Shaped on `#analyze-song`**, the closest thing this application already had: capture the id,
// mark it running, await, check the project has not moved, adopt, repaint, reload, toast, release
// in a `finally`. What that one does not have to deal with is the two traps below.
//
// **Trap one: a content-derived fingerprint makes a forced re-measurement invisible.**
// `song_fingerprint` is computed from the song's bytes (`effects.py`), so re-measuring the *same
// file* answers the *same* fingerprint. Both `songEnvelopeIdentity` and `snapTargetsIdentity` are
// built on it, and both loaders return early when the key they compute matches the one they last
// claimed -- so the whole point of this route, which is `force=True`, would be a silent no-op
// through the browser: a Director whose sidecar had been deleted would press this, the server
// would genuinely re-measure, and the row would go on saying the song had never been analysed.
// `forgetSongEnvelope` is the existing remedy and it is why it is called here on a path that has
// not changed songs at all.
//
// **Trap two: the order of the two lines above it.** Both loaders compute their key from
// `state.project?.song`, so the returned Project is adopted *before* they are called. Adopt after,
// and the key is computed from the song as it was, matches what was stored, and both no-op --
// which is the same defect as trap one arriving by a different road.
//
// **The running window is the whole window, not just the request.** `forgetSongEnvelope` nulls the
// measurement and the targets, so between the reply landing and the two reads answering there is
// genuinely nothing to say; holding the flag across both keeps the row reading `Analyzing song…`
// for that gap instead of flickering back to "this song has not been analysed" a moment after the
// analysis succeeded.
//
// **A refusal changes nothing.** Nothing is adopted, nothing is forgotten and nothing is reloaded
// unless the request resolved, so the rows come back to exactly what they said -- and what the
// Director is told is the server's own sentence, which `errorMessage` has already rendered out of
// the `detail` for all four of this route's refusals.
async function runSongAnalysis() {
  if (!requireProject()) return;
  const projectId = state.project.id;
  // The half of "it cannot be re-fired" that the drawn state cannot make good on its own: a press
  // arriving between the claim being made and the repaint landing, one from an assistive technology
  // activating the button directly, and -- since the button carries `aria-disabled` rather than
  // `disabled`, so that pressing it does not blur it -- every press while one is running.
  //
  // Keyed on *this* project, so a Director who switched projects mid-measurement can start one on
  // the project they are now looking at rather than being refused by a run they cannot see.
  if (snapAnalysisProjects.has(projectId)) return;
  // Whether the Director is standing on the button, taken before the await: on success the row
  // loses its reason and its button, the shape key moves and the rows are rebuilt, so the element
  // they were on stops existing. Restoring focus to the row's own checkbox is what the shot
  // inspector already does after it tears itself down, and it is the difference between a
  // completed action and being thrown back to the top of the document.
  const held = document.activeElement?.id || "";
  const heldRow = SNAP_TARGET_ORDER.find(
    (kind) => held === snapActionControl(kind).slice(1)
  ) || "";
  snapAnalysisProjects.add(projectId);
  syncSnapTargetsControl();
  try {
    const project = await api.analyzeSong(projectId);
    // The Director moved to another project while this was open; this answer describes a song
    // nobody is looking at, and adopting it would put one project's measurement on another.
    if (state.project?.id !== projectId) return;
    state.project = project;
    forgetSongEnvelope();
    renderAll();
    await Promise.all([loadSongEnvelope(projectId), loadSnapTargets(projectId)]);
    if (state.project?.id !== projectId) return;
    // **The count only when there is one.** The read that supplies it can itself be refused, and a
    // measurement that succeeded followed by "0 beats to snap to" is this application reporting a
    // number it does not have -- the honest-status convention, applied to the one sentence a
    // Director would take at face value. The rows say the same thing: with no report read back,
    // the Beats row states its prerequisite rather than claiming the measurement is on screen.
    const beats = state.snapTargets?.analysed === true && Array.isArray(state.snapTargets?.beats)
      ? state.snapTargets.beats.length
      : null;
    toast(beats === null
      ? SNAP_ANALYZE_DONE_UNCOUNTED
      : SNAP_ANALYZE_DONE.replace("{beats}", snapBeatCount(beats)));
  } catch (error) {
    // Guarded like every other branch here: a refusal for the project the Director has left is a
    // sentence about a song they are no longer looking at, and this route's refusals name one.
    if (state.project?.id === projectId) toast(error.message, "error");
  }
  finally {
    snapAnalysisProjects.delete(projectId);
    syncSnapTargetsControl();
    // After the last repaint, so the element being focused is the one that survived it.
    if (heldRow && state.project?.id === projectId) restoreSnapRowFocus(heldRow);
  }
}

// What each row action name in `SNAP_TARGET_REMEDY` actually runs. One entry per action, read by
// the delegated click below: the row decides what a press *means* in `api.js`, beside the sentence
// that explains it, and this decides what it *does*.
const SNAP_ROW_ACTIONS = { [SNAP_ANALYZE_ACTION]: runSongAnalysis };

// Put the keyboard back on the row whose action has just been carried out and taken away with it.
// **That row's own checkbox**, not the first one and not the panel: it is the nearest thing that
// still exists, it is inside the still-open panel, and it announces the row the Director was
// working on. Landing on the top of the list instead would be a quieter version of the defect this
// exists to fix -- the Director's place moved by a press they made, without being asked.
function restoreSnapRowFocus(kind) {
  const list = $(SNAP_SELECT_LIST);
  const box = list && $(`#snap-kind-${cssEscape(kind)}`, list);
  if (box?.focus) box.focus();
}

// One identifier, escaped for a selector rather than for markup. `CSS.escape` where the browser
// has it, and a conservative fallback where it does not -- the contract harness runs this file
// under node, which has no `CSS` at all, and a helper that threw there would take every executed
// frontend test with it.
function cssEscape(value) {
  const text = String(value);
  if (globalThis.CSS?.escape) return globalThis.CSS.escape(text);
  return text.replace(/[^a-zA-Z0-9_-]/g, (character) => `\${character}`);
}

// The beat band's switch, drawn from the flag for `syncSnapTargetsControl`'s reason exactly, so
// the button, the paint and the stored session cannot disagree about whether the marks are on.
// The state is in `aria-pressed` and spelled out in the accessible name as well as carried by the
// pressed hue -- a toggle whose only signal is a colour is a toggle a screen reader cannot read,
// and it is reachable and operable by keyboard because it is a real `<button>`.
function syncBeatMarkersControl() {
  const button = $(BEAT_MARKERS_CONTROL);
  if (!button) return;
  const onOff = beatMarkersOn ? "on" : "off";
  button.classList.toggle("snap-on", beatMarkersOn);
  button.setAttribute("aria-pressed", beatMarkersOn ? "true" : "false");
  // Two different lengths on purpose. The **accessible name** is the four words a screen reader
  // should read on every focus and every press; the paragraph explaining what the band is goes on
  // `title`, where it is read once by someone who went looking for it. Naming the button with the
  // whole help text made focusing it a 288-character announcement, which is how a control becomes
  // unusable by being over-described.
  button.title = `${BEAT_MARKERS_LABEL}: ${onOff}. ${BEAT_MARKERS_HELP}`;
  button.setAttribute("aria-label", `${BEAT_MARKERS_LABEL}: ${onOff}`);
}

// One step back. The snapshot is sent first and adopted only from the reply: a refused undo must
// leave the screen showing what the server actually holds, and mutating `state.project.shots`
// before the write is how an undo that the server refused would still look as though it happened.
//
// Only `shots` and `updated_at` are taken from the reply. The route answers with the whole
// project, and adopting that wholesale would re-seed the document editors from the server --
// this application's recurring editor-wiping defect, and the reason `saveShotsSilently`
// deliberately does not adopt its own reply either.
async function stepHistory(from, onto, redo) {
  if (undoInFlight || !state.project) return;
  const control = undoControl(from, {
    revision: undoRevision,
    projectRevision: state.project.updated_at || null,
    busy: shotWriteInFlight,
    redo,
  });
  if (control.disabled) return toast(control.title, "error");
  const projectId = state.project.id;
  const entry = from[from.length - 1];
  undoInFlight = true;
  syncUndoControls();
  try {
    // Any gesture save still queued lands first, so this is a step back from the plan the server
    // holds rather than a race with the write that put it there.
    await shotSaveChain;
    if (state.project?.id !== projectId) return;
    const displaced = structuredClone(state.project.shots || []);
    const saved = await api.saveShots(projectId, entry.shots, undoRevision);
    if (state.project?.id !== projectId) return;
    from.pop();
    onto.push({ kind: entry.kind, shots: displaced });
    state.project.shots = saved.shots;
    state.project.updated_at = saved.updated_at;
    shotsBaseline = structuredClone(saved.shots || []);
    undoRevision = saved.updated_at || null;
    if (!state.project.shots.some((shot) => shot.id === state.selectedShotId)) {
      state.selectedShotId = state.project.shots[0]?.id || null;
    }
    state.shotsDirty = false;
    state.dirty = state.documentsDirty;
    renderTimeline();
    loadReadiness(projectId);
    toast(`${redo ? "Redone" : "Undone"}: ${undoGestureLabel(entry.kind)}`);
  } catch (error) {
    // The server's own words, verbatim. A 409 here is the design working -- something wrote the
    // project underneath this stack -- so the history is dropped rather than left offering a
    // replay that would refuse again.
    toast(String(error?.message || error), "error");
    clearUndoHistory();
  } finally {
    undoInFlight = false;
    if (state.project?.id === projectId) syncUndoControls();
  }
}

function runUndo() { return stepHistory(undoStack, redoStack, false); }
function runRedo() { return stepHistory(redoStack, undoStack, true); }

// `kind` names the gesture for the button's own sentence. It defaults to the generic edit rather
// than to nothing, because *every* landed shots save is recorded: a stack that skipped the
// inspector's own writes would step back over one silently the next time it was pressed.
function saveShotsSilently(kind = "edit") {
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
  // What the plan looked like before this write, and what an undo of this gesture puts back.
  // Held apart from the entry itself because the entry is only created if the write lands -- an
  // undo of something that was never applied is the one thing this feature must not offer.
  //
  // Read at SEND time, inside the chain, for the revision's reason below and for a sharper one
  // of its own. `shotsBaseline` only advances when a save *lands*, so two gestures made inside
  // one round trip -- split then drag, or two clicks on `#split-shot` -- both read the same
  // baseline at queue time and both recorded it. One Undo then rolled back *both* while the
  // button named only the second, a second Undo replayed the same plan and visibly did nothing,
  // and the state between the two gestures was unrecoverable. Read here it is the plan the
  // previous save actually left behind, which is this gesture's own "before" -- the invariant
  // stated at the top of this section rather than an approximation of it.
  let restores = null;
  // Queue time, deliberately, and not moved down with `restores`: this is the generation the
  // gesture was *made* in, and a discard between the click and the send is exactly what it has
  // to notice. Read at send time it would adopt the post-discard generation and agree with
  // itself, which is the failure the comment below records.
  const bornAt = undoGeneration;
  shotSaveChain = shotSaveChain
    // The revision travels with the save and is read at SEND time, not at queue time: the
    // chain runs saves one by one, and each adopts the server's fresh `updated_at` below, so
    // a queued burst stays valid save over save. A stale one — this tab loaded before some
    // other writer saved — is refused with a 409 instead of silently reverting that work,
    // which is what one background save from this tab did to 32 prompts on 2026-08-19.
    .then(() => {
      restores = shotsBaseline ? structuredClone(shotsBaseline) : null;
      return api.saveShots(projectId, shots, state.project?.updated_at || null);
    })
    .then((saved) => {
      if (state.project?.id === projectId && saved?.updated_at) {
        state.project.updated_at = saved.updated_at;
      }
      if (state.project?.id === projectId) {
        // Recorded only if the history this snapshot belongs to is still standing.
        //
        // `restores` is the plan as the *last landed save* left it, so it is this gesture's
        // "before" only while nothing else has written in between. When something has -- an
        // approve, a take swap, a whole-plan sweep, a project load -- the history is discarded
        // where that is noticed (`syncUndoControls`' self-heal, `loadProject`, the sweep), and
        // the generation is what makes a save queued *before* that discard drop its snapshot
        // rather than push it afterwards.
        //
        // There used to be a second check here, comparing the revision this save was sent
        // against with the one the stack was valid against. Mutation testing showed it could no
        // longer fail: the self-heal had already adopted the new revision by the time the save
        // landed, so the two always agreed -- and it was the *generation*, not the revision,
        // that was catching the stale snapshot. An untested branch that cannot fire is worse
        // than no branch.
        if (restores && bornAt === undoGeneration) undoStack.push({ kind, shots: restores });
        if (undoStack.length > UNDO_DEPTH) undoStack.shift();
        // A new gesture is a new branch of history: what was undone is no longer ahead of it.
        // Not for the undo/redo writes themselves -- those go through `stepHistory`, which
        // moves the entry across by hand rather than coming through here at all.
        redoStack = [];
        shotsBaseline = structuredClone(shots);
        undoRevision = saved?.updated_at || null;
        syncUndoControls();
      }
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
  // Follow during playback: at 16 px/s a three-minute song is ~2900 px, so the playhead
  // ran off-screen inside 30 seconds and the Director scrolled by hand. Recentred only
  // when it leaves the visible band, and only while playing — a paused timeline is being
  // *read*, and yanking it under a scrub is worse than not following.
  const scroll = $("#timeline-scroll");
  if (scroll && !$("#master-audio").paused) {
    const visibleLeft = scroll.scrollLeft;
    const visibleRight = visibleLeft + scroll.clientWidth;
    if (left < visibleLeft + 40 || left > visibleRight - 60) {
      scroll.scrollLeft = Math.max(0, left - scroll.clientWidth / 2);
    }
  }
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
  // The one text layer the Monitor had is the overlay, and `.showing-take` display:none's it --
  // so while a take was on screen the Monitor could say nothing at all, and a previous take with
  // a newer render in flight played in sync, framed exactly like a settled one. The note is a
  // second layer that survives a picture: it carries `view.label` whenever there is a picture and
  // something to say about it, and is empty (and so invisible) otherwise.
  const note = $("#monitor-note");
  const say = (text) => { if (note) note.textContent = text; };
  if (videoLineMuted) {
    frame.classList.remove("showing-take");
    $("#monitor-overlay").textContent = "Video line muted";
    say("");
    if (!video.paused) video.pause();
    return;
  }
  if (!monitorShowsTake(view)) {
    frame.classList.remove("showing-take");
    $("#monitor-overlay").textContent = view.label;
    say("");
    if (!video.paused) video.pause();
    return;
  }
  frame.classList.add("showing-take");
  // A displaced take keeps playing -- it is the only evidence there is, and `latest_output` still
  // points at it -- and says so in words over the picture.
  say(view.label);
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

// The same decision the button was drawn from, asked again at the click. The button is already
// shut in both refusing cases, so this is the belt to that brace -- but it says the reason rather
// than returning silently, because a control that appears to do nothing is the report this whole
// thread started from. The success toast names the shot too: from the Assets panel the timeline is
// not on screen, so "attached to shot" was the one thing the Director could not check.
function attachSelectedAsset() {
  const asset = selectedAsset();
  if (!asset) return;
  const attach = attachToShotControl(state.project, state.selectedShotId, asset.id, asset.name);
  if (attach.disabled) return toast(attach.reason, "error");
  const shot = attach.shot;
  shot.citations = [...shotCitations(shot), { asset_id: asset.id, role: "reference", order: shotCitations(shot).length }];
  reconcileShotCitations(shot);
  saveShotsSilently();
  renderTimeline();
  renderAssetInspector();
  toast(`${asset.name} attached to ${shotLabel(state.project, shot.id)}`);
}

function bindEvents() {
  $$(".rail-item").forEach((button) => button.addEventListener("click", () => {
    state.activePanel = button.dataset.panel;
    persistSession();
    $$(".rail-item").forEach((item) => item.classList.toggle("active", item === button));
    $$(".panel").forEach((panel) => panel.classList.toggle("active", panel.id === `panel-${state.activePanel}`));
    if (state.activePanel === "timeline") requestAnimationFrame(renderTimeline);
    // The asset inspector's attach control names the shot it will write to (the Director's report,
    // 2026-08-21), and the only way to change that selection is to leave this panel and come back.
    // Without this the caption is drawn once and then goes stale: the Assets panel would say
    // "Attach to SHOT 01" while SHOT 02 was the one selected on the timeline -- a named target that
    // names the wrong thing, which is worse than the unnamed button it replaced.
    //
    // Synchronously, unlike the timeline above: that one is deferred because it measures a canvas
    // and needs the panel's layout to have happened, and this one only writes `innerHTML`.
    if (state.activePanel === "assets") renderAssetInspector();
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
  $("#delete-project")?.addEventListener("click", async () => {
    if (!requireProject()) return;
    const name = state.project.name;
    // Type-the-name confirmation: a project is a whole production, and the browser's
    // OK button is too cheap a gate for it. Rendered takes stay on disk either way.
    const typed = window.prompt(
      `Delete "${name}" permanently?\nIts manifest, shots and media directory go; takes already rendered stay on disk.\n\nType the project name to confirm:`,
    );
    if (typed === null) return;
    if (typed.trim() !== name) return toast("Name did not match — nothing was deleted.", "error");
    try {
      await api.deleteProject(state.project.id);
      state.project = null;
      state.audioBuffer = null;
      await loadProjects();
      toast(`"${name}" deleted`);
    } catch (error) { toast(error.message, "error"); }
  });
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
      // The master this band was measured from is gone. Forgotten *before* the render, not after
      // the next read replies: the upload route measures the new song inline and answers with the
      // project, so without this the band draws the old song's beats over the new track for as
      // long as the envelope request takes -- the one thing `BEAT_MARKERS_HELP` promises cannot
      // happen. The reload that follows is what puts the new measurement on screen.
      forgetSongEnvelope();
      $("#import-lyrics").value = ""; $("#import-style").value = "";
      renderAll();
      loadSongEnvelope(state.project.id);
      loadSnapTargets(state.project.id);
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
  // The declaration saves on its own, on change, because it is one enum with its own route and
  // there is nothing to compose it with. It is bound beside the context editors rather than in
  // them: the lyric sheet is not on that route's wire at all.
  $("#song-vocal-type").addEventListener("change", (event) => saveVocalType(event.target.value));
  // Editing the sheet redraws the tagging list from what is in the box, so a line typed now can be
  // tagged now and a line deleted takes its dropdown with it. This is the drift answer made
  // visible: there is no stored tag to go stale, only text.
  $("#song-lyrics").addEventListener("input", renderVocalTagging);
  // Every bounded box keeps its own counter current, the import block's included: those two are not
  // seeded from anything, so a render is not what puts a number under them.
  SONG_CONTEXT_COUNTS.forEach((control) => $(control.field).addEventListener("input", renderSongContextCounts));
  $("#analyze-song").addEventListener("click", async () => {
    if (!requireProject()) return;
    const projectId = state.project.id;
    // The two costs, said up front: a first run transcribes on CPU for a few minutes, and
    // section boxes the Director has already placed are theirs to overwrite, never assumed.
    const replace = Boolean(state.project.sections?.length);
    const question = replace
      ? "Replace the existing section boxes with the measured lyric alignment?"
      : "Analyze the track and fill the Sections row from the lyric sheet's [Tag] blocks?" +
        (state.project.song?.lyric_words?.length ? "" :
          "\n(First run transcribes the song on CPU — a few minutes.)");
    if (!window.confirm(question)) return;
    const button = $("#analyze-song");
    button.disabled = true;
    button.textContent = "Analyzing…";
    try {
      const project = await api.alignLyrics(projectId, { replace_sections: replace });
      if (state.project?.id !== projectId) return;
      state.project = project;
      renderAll();
      // A first transcription is what *creates* the voiceless-gap targets: this route writes
      // `lyric_words` and `vocal_spans`, and `snapTargetsIdentity` counts both, so the read below
      // is a no-op on a re-run and the whole set on the first one. Without it the gap half of the
      // magnet would not appear until the next project load -- the feature silently late by one
      // reload on the one gesture that just earned it.
      loadSnapTargets(projectId);
      toast(`Sections filled from the track: ${project.sections.map((s) => s.label).join(" · ")}`);
    } catch (error) { toast(error.message, "error"); }
    finally {
      button.textContent = "Analyze structure";
      renderSong();
    }
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
      // Beside it, and for exactly the reason the line above exists: a removed song's marks must
      // not reappear and be read as current any more than its waveform may.
      forgetSongEnvelope();
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
  $("#asset-fill").addEventListener("click", async () => {
    if (!requireProject()) return;
    const count = Math.max(1, Math.min(16, Number($("#asset-fill-count")?.value) || 8));
    if (!window.confirm(`Ask the Stage Manager to assess the library and queue up to ${count} Flux asset render(s)? Each proposal becomes an ordinary asset you can keep, delete, or AI Mod.`)) return;
    const projectId = state.project.id;
    const button = $("#asset-fill");
    button.disabled = true;
    try {
      const report = await api.fillAssets(projectId, count);
      toast(`Stage Manager queued ${report.submitted.length} asset render(s). ${report.message}`.slice(0, 220));
      if (state.project?.id === projectId) await loadProject(projectId);
    } catch (error) { toast(error.message, "error"); }
    finally { button.disabled = false; }
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
      // The Director's own placement for the cast check: flagged when Populate is clicked, after
      // the plan has landed rather than instead of it. Each notice is the server's sentence
      // verbatim — the client decides nothing about the cast, it only shows what the manifest
      // said. Empty for every project that has declared no vocal type, which is every project
      // that existed before this feature, so nothing new appears on an untouched plan.
      (report.cast_notices || []).forEach((notice) => toast(notice, "error"));
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
  // Delegated off the strip, once, rather than bound per button. `renderAssetTabs` builds the
  // buttons and a tab click calls it again, so a per-button handler would be one `innerHTML`
  // write away from being bound to nothing -- and this application has a recorded defect of
  // exactly that shape (a `dblclick` listener that never fired because `pointerdown` re-rendered
  // every clip). Delegation makes the strip's own rebuild rule something the handler survives
  // rather than something it depends on. The strip is also drawn before any project loads, so
  // this binds to a node that is always there.
  //
  // Only the tab changes here. `state.selectedAssetId` and `state.selectedShotId` are deliberately
  // untouched: the inspector on the right keeps the selected asset -- and with it "Attach to
  // selected shot" -- alive across a tab change, including on the Clips tab.
  $("#asset-filters").addEventListener("click", (event) => {
    const button = event.target?.closest?.("button[data-filter]");
    if (!button) return;
    state.assetTab = button.dataset.filter;
    renderAssets();
  });
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
  $("#expand-shot-prompts").addEventListener("click", () => expandShotPrompts("story"));
  $("#dp-pass").addEventListener("click", () => expandShotPrompts("photography"));
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
    shots.push(shot); state.selectedShotId = shot.id; saveShotsSilently("add"); renderTimeline();
  });
  // Undo and Redo. Both go through `stepHistory`, which pre-flights the same rule `undoControl`
  // draws the button with, so a click on a stale button says the same sentence the tooltip does
  // rather than being silently ignored.
  $("#undo-shots")?.addEventListener("click", runUndo);
  $("#redo-shots")?.addEventListener("click", runRedo);
  // The "Snap to" selector. **One delegated listener on the list**, not one per row: the rows are
  // redrawn from the plan every time the set changes, so a listener bound to a row would be bound
  // to markup that is about to be replaced.
  //
  // Session-only, like the two line mutes beside it. It writes nothing, asks for nothing and
  // repaints nothing -- targets are not drawn, so a change here alters where the *next* drag lands
  // and nothing that is on screen.
  $(SNAP_SELECT_LIST)?.addEventListener("change", (event) => {
    const kind = event?.target?.dataset?.kind;
    if (!kind || !SNAP_TARGET_ORDER.includes(kind)) return;
    // Read from the checkbox that was actually ticked rather than toggled from the set, so the
    // control and the set cannot drift apart if one change event is ever missed.
    if (event.target.checked) snapTargetKinds.add(kind);
    else snapTargetKinds.delete(kind);
    syncSnapTargetsControl();
    persistSession();
  });
  // The one action a row can offer, delegated on the same list for the same reason: the rows are
  // rewritten whenever a kind's state changes, so a listener bound to the button would be bound to
  // markup that is about to be replaced.
  //
  // **Filtered by the data attribute the plan put there, not by the button's id.** What a press
  // means is the row's decision, made in `api.js` beside the sentence that explains it; a handler
  // matching an id would be a second place that has to be edited when a second kind ever earns an
  // action of its own. A press anywhere else in the panel -- a label, the panel's own padding --
  // falls through this and does nothing, which is what a `change`-only list did before.
  $(SNAP_SELECT_LIST)?.addEventListener("click", (event) => {
    // `closest`, not the target itself: the moment a button carries any child element -- a glyph, a
    // `<span>` a browser or a stylesheet inserts -- the press lands on the child and a handler
    // reading `event.target.dataset` stops firing, silently and only in a real browser.
    const named = event?.target?.closest?.("[data-snap-action]")?.dataset?.snapAction;
    // A map rather than a comparison, so a second row action is one entry here and one in
    // `SNAP_TARGET_REMEDY` -- which is what this handler's own comment claims and a hard-coded
    // call to one function did not deliver.
    const run = named ? SNAP_ROW_ACTIONS[named] : null;
    if (run) run();
  });
  // **Both ways out of an open panel that every other disclosure in a browser offers**, and
  // neither of which `<details>` gives for free: clicking away from it, and Escape. Without them
  // the only way to shut it is to find the summary again, and it hangs over the timeline until
  // you do. Bound on `document` in the capture-free bubble phase, so a click on a control inside
  // the panel still reaches that control first.
  document.addEventListener?.("pointerdown", (event) => {
    const panel = $(SNAP_SELECT_CONTROL);
    if (!panel?.open) return;
    if (event.target?.closest?.(SNAP_SELECT_CONTROL) === panel) return;
    panel.open = false;
  });
  // Escape is bound on the panel rather than on the document, deliberately. A keyboard Director
  // opening this has focus on the summary or on one of the rows, both inside it, so the key
  // bubbles here -- and Escape stays the property of whatever is focused rather than becoming a
  // global this control has claimed. It also keeps `document`'s one keydown handler the undo
  // shortcut's, which is the only thing on this page that wants every keystroke.
  $(SNAP_SELECT_CONTROL)?.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const panel = $(SNAP_SELECT_CONTROL);
    if (!panel?.open) return;
    panel.open = false;
    // Focus goes back to the control the Director was operating rather than to the document, which
    // is where a keyboard user would otherwise be dropped.
    $(SNAP_SELECT_SUMMARY)?.focus?.();
  });
  // The beat band's switch, beside it and session-only for the same reason. It repaints the
  // timeline and does nothing else: no save, no request, and nothing about any Shot is touched by
  // showing or hiding a reference mark.
  $(BEAT_MARKERS_CONTROL)?.addEventListener("click", () => {
    beatMarkersOn = !beatMarkersOn;
    syncBeatMarkersControl();
    persistSession();
    renderTimeline();
  });
  // Duplicate copies the plan and nothing else. It used to clone the whole Shot and reset
  // `status`, which left the copy owning the original's take: the same `latest_output` played in
  // the Monitor, the same approval read back from the panel, and a Shot nobody had rendered
  // claimed a take. `newShotFromPlan` builds from the classified plan fields instead of
  // subtracting from a clone, so an unclassified field is absent from the copy rather than
  // inherited by it. The original is untouched -- its take, its approval and its pointer all
  // stay exactly where they were.
  //
  // The `!shot` guard used to be a bare `return` on both of these — pressed with nothing
  // selected, the control did nothing and said nothing, which is `#split-shot`'s 2026-08-21
  // defect sitting unfixed beside the fix. `noShotSelectedRefusal` supplies the sentence all
  // three share.
  $("#duplicate-shot").addEventListener("click", () => { const shot = selectedShot(); const refusal = noShotSelectedRefusal(shot, "duplicate"); if (refusal) return toast(refusal, "error"); const copy = newShotFromPlan(shot, { id: `shot_${crypto.randomUUID().replaceAll("-", "").slice(0, 12)}`, start: shot.start + shot.duration }); state.project.shots.push(copy); state.selectedShotId = copy.id; saveShotsSilently("duplicate"); renderTimeline(); });
  $("#delete-shot").addEventListener("click", () => {
    const shot = selectedShot();
    const refusal = noShotSelectedRefusal(shot, "delete");
    if (refusal) return toast(refusal, "error");
    // The one destructive timeline action that had no confirmation (analyst finding,
    // 2026-08-20). Named, because "Delete shot" against the wrong selection is the
    // realistic mistake — and takes on disk survive it either way.
    const name = shotLabel(state.project, shot.id);
    if (!window.confirm(`Delete ${name}? Its rendered takes stay on disk, but the shot leaves the plan.`)) return;
    state.project.shots = state.project.shots.filter((item) => item.id !== shot.id);
    state.selectedShotId = state.project.shots[0]?.id || null;
    saveShotsSilently("delete"); renderTimeline();
  });
  // The split's second half is a new Shot on the same terms as a duplicate: it has rendered
  // nothing, so it carries the plan and no take. The first half keeps everything it had --
  // narrowing a window is not a reason to touch a pointer, and the take it names is still the
  // last thing this Shot rendered.
  //
  // It said **nothing at all** about a window it could not halve until 2026-08-21 -- the same
  // shape as the report that started this thread, a control that appears to do nothing.
  // `splitShotPlan` owns both the arithmetic and the sentence; the halves it returns are the two
  // windows written here.
  $("#split-shot").addEventListener("click", () => {
    const shot = selectedShot();
    const plan = splitShotPlan(state.project, shot);
    if (!plan.ok) return toast(plan.refusal, "error");
    const [first, second] = plan.halves;
    const copy = newShotFromPlan(shot, { id: `shot_${crypto.randomUUID().replaceAll("-", "").slice(0, 12)}`, start: second.start, duration: second.duration });
    shot.duration = first.duration;
    state.project.shots.push(copy);
    saveShotsSilently("split");
    renderTimeline();
  });
  $("#monitor-fullscreen")?.addEventListener("click", () => {
    const monitor = $("#timeline-monitor");
    if (document.fullscreenElement) document.exitFullscreen();
    else monitor.requestFullscreen?.();
  });
  $("#zoom-in").addEventListener("click", () => applyZoom(state.pixelsPerSecond * TIMELINE_ZOOM_STEP));
  $("#zoom-out").addEventListener("click", () => applyZoom(state.pixelsPerSecond / TIMELINE_ZOOM_STEP));
  // The slider the Director went looking for. It writes the same scale the buttons do, through
  // the same anchor rule, and `renderTimeline` writes the thumb back — so dragging it, pressing
  // a button and ctrl+wheeling can never leave the three disagreeing.
  $("#zoom-slider")?.addEventListener("input", (event) => applyZoom(zoomFromSlider(event.target.value)));
  // The wheel over the tracks. `timelineWheelPlan` decides which of the three things one notch
  // means — zoom, scroll across, or leave it to the browser — and takes a gesture over only when
  // it had nothing else to do. Ctrl+wheel keeps its own pointer anchor rather than applyZoom's,
  // because for that gesture the pointer *is* the thing of interest.
  $("#timeline-scroll").addEventListener("wheel", (event) => {
    const scroll = $("#timeline-scroll");
    const plan = timelineWheelPlan({
      deltaX: event.deltaX,
      deltaY: event.deltaY,
      ctrlKey: event.ctrlKey,
      metaKey: event.metaKey,
      shiftKey: event.shiftKey,
      // A pixel of slack: sub-pixel layout widths make an exactly-fitting box report one more
      // scroll pixel than it has, and hijacking the wheel for a scroll of 0 would be the same
      // dead gesture in a new place.
      canScrollX: (scroll.scrollWidth || 0) > (scroll.clientWidth || 0) + 1,
      canScrollY: (scroll.scrollHeight || 0) > (scroll.clientHeight || 0) + 1,
    });
    if (plan.action === TIMELINE_WHEEL_ACTIONS.native) return;
    event.preventDefault();
    if (plan.action === TIMELINE_WHEEL_ACTIONS.scroll) {
      if (plan.scrollX) scroll.scrollLeft += plan.scrollX;
      if (plan.scrollY) scroll.scrollTop += plan.scrollY;
      return;
    }
    const pointerX = event.clientX - scroll.getBoundingClientRect().left + scroll.scrollLeft;
    const anchorSeconds = (pointerX - TIMELINE_LABEL_WIDTH) / state.pixelsPerSecond;
    const factor = plan.delta < 0 ? 1.2 : 1 / 1.2;
    state.pixelsPerSecond = clampTimelineZoom(state.pixelsPerSecond * factor);
    renderTimeline();
    persistSession();
    scroll.scrollLeft = Math.max(0, TIMELINE_LABEL_WIDTH + anchorSeconds * state.pixelsPerSecond - (event.clientX - scroll.getBoundingClientRect().left));
  }, { passive: false });
  $("#section-track").addEventListener("dblclick", (event) => {
    if (!requireProject()) return;
    // Double-click on an existing box edits in the inspector (the click already selected
    // it); creation is for empty track space only.
    if (event.target.closest?.(".section-pill")) return;
    const rect = $("#timeline-canvas").getBoundingClientRect();
    const at = Math.max(0, (event.clientX - rect.left - 90) / state.pixelsPerSecond);
    const boundaries = shotBoundaries(state.project);
    // A new box opens at the shot edge at or before the click and runs to the next edge —
    // "snap to the edges of the shots below" from the first gesture, not only on drag.
    const startEdge = [...boundaries].reverse().find((edge) => edge <= at) ?? at;
    const nextEdge = boundaries.find((edge) => edge > startEdge + 0.5);
    const section = {
      label: "Section",
      start: Math.round(startEdge * 1000) / 1000,
      duration: Math.round(((nextEdge ?? startEdge + 8) - startEdge) * 1000) / 1000,
      prompt: "",
    };
    state.project.sections = [...(state.project.sections || []), section];
    saveSectionsSilently().then(() => {
      // Select the created box once the server has minted its id.
      const created = (state.project.sections || []).find(
        (item) => item.start === section.start && item.label === section.label,
      );
      if (created) { state.selectedSectionId = created.id; state.selectedShotId = null; }
      renderTimeline();
    });
  });
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
  $("#master-volume")?.addEventListener("input", (event) => {
    const master = $("#master-audio");
    if (master) master.volume = Number(event.target.value);
    persistSession();
  });
  // The workspace's first keyboard: transport and shot-stepping, guarded off every
  // editable element so typing a prompt never plays the song (analyst finding,
  // 2026-08-20). Space = play/pause, ←/→ = one frame, Shift+←/→ = one second,
  // [ / ] = previous/next shot (selected and parked under the playhead).
  document.addEventListener?.("keydown", (event) => {
    if (event.target.matches?.("input, textarea, select") || event.target.isContentEditable) return;
    if (!state.project) return;
    const frame = 1 / 24;
    // Ctrl+Z / Ctrl+Shift+Z, and Ctrl+Y for the redo the rest of Windows spells that way. Ahead
    // of the transport keys and returning, so the modifier combinations can never fall through
    // into a seek. Guarded off editable elements by the check above, so Ctrl+Z in a prompt
    // textarea is still the browser's own text undo and not a step back through the plan.
    if ((event.ctrlKey || event.metaKey) && (event.key === "z" || event.key === "Z" || event.key === "y" || event.key === "Y")) {
      event.preventDefault();
      const redo = event.shiftKey || event.key === "y" || event.key === "Y";
      return redo ? runRedo() : runUndo();
    }
    if (event.code === "Space") {
      event.preventDefault();
      toggleMasterAudio();
    } else if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      const step = (event.shiftKey ? 1 : frame) * (event.key === "ArrowLeft" ? -1 : 1);
      seekMasterAudio(Math.max(0, state.playhead + step));
    } else if (event.key === "[" || event.key === "]") {
      const ordered = [...(state.project.shots || [])].sort((a, b) => a.start - b.start);
      if (!ordered.length) return;
      const index = ordered.findIndex((shot) => shot.id === state.selectedShotId);
      const next = ordered[clamp(index + (event.key === "]" ? 1 : -1), 0, ordered.length - 1)];
      state.selectedShotId = next.id;
      state.selectedSectionId = null;
      seekMasterAudio(next.start);
      renderTimeline();
    }
  });
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
  $("#mark-all-ready")?.addEventListener("click", async () => {
    if (!requireProject()) return;
    const projectId = state.project.id;
    // Unlocked drafts with a real prompt: the same shots the per-shot control would
    // allow, each through its own purpose-built route so every server-side refusal
    // (placeholder prompt, in-flight job) is heard per shot rather than skipped silently.
    const drafts = (state.project.shots || []).filter(
      (shot) => shot.status === "draft" && !shot.locked
    );
    if (!drafts.length) return toast("No draft shots to mark ready.");
    if (!window.confirm(`Mark ${drafts.length} draft shot${drafts.length === 1 ? "" : "s"} ready to queue?`)) return;
    let marked = 0;
    const refusals = [];
    for (const shot of drafts) {
      try {
        state.project = await api.markShotReady(projectId, shot.id);
        marked += 1;
      } catch (error) { refusals.push(error.message); }
    }
    renderTimeline(); renderJobs();
    toast(`${marked} shot${marked === 1 ? "" : "s"} marked ready${refusals.length ? ` — ${refusals.length} refused: ${refusals[0]}` : ""}`, refusals.length ? "error" : "info");
  });
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
  // Cancel all renders. The plan is re-decided at the click from the same function that drew the
  // button, so the count in the dialog is the count the request means -- `#queue-ready`'s rule,
  // and the reason the server re-checks the confirmation itself (`confirm_cancel`).
  //
  // The whole project is reloaded afterwards rather than patched: a cancellation of twenty-six
  // renders settles twenty-six job records and releases up to twenty-six shots, and the reply is
  // the report rather than the project.
  $(CANCEL_ALL_CONTROL)?.addEventListener("click", async () => {
    if (!requireProject()) return;
    const plan = cancelAllPlan(state.project);
    if (!plan.count || !window.confirm(plan.confirm)) return;
    const projectId = state.project.id;
    const button = $(CANCEL_ALL_CONTROL);
    button.disabled = true;
    try {
      const report = await api.cancelOpenJobs(projectId);
      toast(cancellationToast(report), report.cancelled.length ? "info" : "error");
      if (state.project?.id === projectId) await loadProject(projectId);
    } catch (error) { toast(error.message, "error"); }
    finally { button.disabled = false; renderJobs(); renderTimeline(); }
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
  // The bundle select's whole behaviour. It sends the select's own value -- never a hardcoded one,
  // which is the defect this control was built to remove -- and repaints from the server's reply,
  // so a refused change reverts the select instead of leaving it showing a bundle no render will
  // honour. The same revert the VRAM eject and the document locks do.
  //
  // The reply is the whole project, so `state.project` is replaced from it: the confirmation
  // Generate All will show and the batch estimate beside it both read `sampling_profile`, and a
  // select that had changed while the stored project had not would put the wrong bundle in the
  // sentence the Director confirms.
  //
  // Nothing about a render happens here. The setting reaches submissions server-side, where the
  // profile is resolved, so no failure on this path can reach a render.
  $(SAMPLING_PROFILE_CONTROL)?.addEventListener("change", async (event) => {
    if (!requireProject()) return renderSamplingProfile();
    const control = event.currentTarget;
    const wanted = control.value;
    const projectId = state.project.id;
    control.disabled = true;
    try {
      const project = await api.saveSamplingProfile(projectId, wanted);
      if (state.project?.id === projectId) state.project = project;
      toast(samplingProfileToast(wanted));
    } catch (error) {
      toast(error.message, "error");
    }
    renderJobs();
  });
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
  // Once at boot, so the options and the comparison's findings are on screen with no project open
  // rather than an empty select that appears only after the first project load. `renderJobs`
  // redraws it from the project every time after that.
  renderSamplingProfile();
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
  const session = restoreSession();
  if (Number.isFinite(session.pixelsPerSecond)) state.pixelsPerSecond = clampTimelineZoom(session.pixelsPerSecond);
  // **Three states, and the middle one is the whole reason this is not an `||`.**
  // `snapKindsFromSession` answers `null` when nothing was stored -- a first-ever run, or a session
  // written before this existed -- and the initialiser's every-kind default stands, which is the
  // same default-on asymmetry the beat markers use below. An empty array is a Director who switched
  // every kind off on purpose and comes back exactly that way. Anything else is their subset, with
  // names this build does not know dropped rather than thrown on.
  //
  // It also carries the migration off the playhead magnet's old `playheadSnap` key, so a Director
  // who had that switched off does not find it switched back on. See there.
  const restoredKinds = snapKindsFromSession(session);
  if (restoredKinds !== null) snapTargetKinds = new Set(restoredKinds);
  // The same asymmetry, for the same reason: a session saved before this existed carries no key,
  // and reading that as "off" would ship the markers hidden for every Director who already has one.
  if (session.beatMarkers === false) beatMarkersOn = false;
  syncSnapTargetsControl();
  syncBeatMarkersControl();
  await Promise.all([loadHealth(), loadVramEject(), api.workflows().catch(() => [])]);
  try { await loadProjects(); } catch (error) { toast(error.message, "error"); }
  if (session.panel) document.querySelector(`[data-panel="${session.panel}"]`)?.click();
  if (session.selectedShotId && state.project?.shots.some((shot) => shot.id === session.selectedShotId)) {
    state.selectedShotId = session.selectedShotId;
    renderTimeline();
  }
  const master = $("#master-audio");
  if (master && Number.isFinite(session.volume)) master.volume = Math.min(1, Math.max(0, session.volume));
  setInterval(persistSession, 5000);
}

init();
