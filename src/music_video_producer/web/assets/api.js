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

// Pure decision: what a Song's context fields carry, from whatever the Director typed into them.
//
// Two arguments in a fixed order, and the *style* description lands on `caption` — the field both
// generation paths already use for the sonic and stylistic direction of a song. Crossing the two
// is the failure this function exists to make testable: a lyric sheet stored as the style summary
// would reach the Director as a description of how the song sounds, and the style description
// stored as lyrics would be read as the words being sung. Nothing about that is visible on screen,
// so it is executed as a test rather than grepped for.
//
// Edges only, exactly as `musicGenerationPlan` trims a supplied lyric sheet and as the server's
// `_song_context` does: interior blank lines, indentation and section tags are the structure of a
// sheet, and a whitespace-only field is an absent one rather than a stored blank line.
export function songContextFields(lyrics, style) {
  return { lyrics: String(lyrics ?? "").trim(), caption: String(style ?? "").trim() };
}

// True when this project's Song context can be edited at all. The controls read a Song that is
// not there as "" and would then PUT a blank context at a route that 404s, so the answer is the
// enabled state of the whole block rather than a check inside the save handler.
export function songContextEditable(project) {
  return Boolean(project?.song);
}

// The bounds `_song_context` enforces, mirrored from app.py's SONG_LYRICS_LIMIT and
// SONG_CAPTION_LIMIT and asserted equal to them by tests/test_frontend_contract.py.
//
// Held here rather than as a `maxlength` on the four textareas, which is what they carried before.
// `maxlength` truncates a paste at the client and says nothing: a Director pasting an oversized
// lyric sheet lost the tail silently and saved a sheet ending mid-line, while an API client sending
// the identical text got a 422 naming the length. One bound, enforced in one place — the route —
// and these numbers exist only to tell the Director where they stand against it before the click.
export const SONG_CONTEXT_LIMITS = { lyrics: 8_000, caption: 4_000 };

// Every bounded song-context control: the box, the element its count is written into, and which
// bound applies to it. One table because there are four boxes across two blocks, and a counter
// wired to the wrong bound would report a lyric sheet safe at 6000 characters or a style line
// oversized at 5000.
export const SONG_CONTEXT_COUNTS = [
  { field: "#import-lyrics", count: "#import-lyrics-count", limit: SONG_CONTEXT_LIMITS.lyrics },
  { field: "#import-style", count: "#import-style-count", limit: SONG_CONTEXT_LIMITS.caption },
  { field: "#song-lyrics", count: "#song-lyrics-count", limit: SONG_CONTEXT_LIMITS.lyrics },
  { field: "#song-style", count: "#song-style-count", limit: SONG_CONTEXT_LIMITS.caption },
];

// What one bounded box's counter says, and whether what is in it can be saved at all.
//
// Measured on the *trimmed* text because that is what the route measures — `_song_context` bounds
// after `.strip()` — so a sheet pasted with a trailing page of newlines is neither reported as
// oversized here nor refused there. The verdict is in the text rather than only in a colour: a
// count that merely turns red is not a message to a Director who is not looking at it.
export function songContextCount(value, limit) {
  const length = String(value ?? "").trim().length;
  const counted = `${length.toLocaleString("en-US")} / ${limit.toLocaleString("en-US")}`;
  return { length, limit, over: length > limit, label: length > limit ? `${counted} — too long to save` : counted };
}

// Every kind of unsaved work the two navigation guards must answer for, in one predicate.
//
// `state.dirty` covers what the project save writes. The Song context is saved by its own button
// through its own route, so it was invisible to both guards: an 8000-character lyric sheet typed
// and not saved was discarded without a question on a project switch and on a tab close, while
// three characters typed into a document textarea produced one.
//
// `songContextDirty` stays a separate flag rather than folding into `state.dirty`, because it
// answers a second question no other flag answers — whether an incidental `renderSong`, such as the
// audio element's `loadedmetadata`, may re-seed the editors from the stored Song. Folded in, it
// would also make `saveProject` clear it and re-seed a sheet mid-paste.
export function unsavedWorkPending(state) {
  return Boolean(state?.dirty || state?.songContextDirty);
}

// Why a discard question has anything to do with the Song workspace. Named separately from the
// project save because "unsaved changes" reads as the project, and a Director who has just pasted
// a lyric sheet into a different panel has no reason to connect the two.
export const UNSAVED_SONG_CONTEXT_CONSEQUENCE =
  "The lyric sheet and style description in the Song workspace are saved by their own button. " +
  "Anything typed into them and not saved is discarded.";

// The discard question, stated for what is actually unsaved.
export function unsavedWorkQuestion(question, state) {
  return state?.songContextDirty ? `${question}\n\n${UNSAVED_SONG_CONTEXT_CONSEQUENCE}` : question;
}

// Which project loads may re-seed the Song context editors from the loaded project — which means
// discarding whatever is in them. True only when the project actually changes, for the same reason
// `documentConsentClearedOnLoad` exists: most of `loadProject`'s callers are refreshes of the
// project already on screen — the queue refresh, both generate paths, multiview, the queue-ready
// loop — and clearing the dirty flag there lets the very next render overwrite a sheet the Director
// is part-way through pasting, with nothing on screen to explain where it went. A real switch is
// guarded by the discard question, so re-seeding there is the Director's own answer.
export function songContextSeedClearedOnLoad(currentProjectId, nextProjectId) {
  return (currentProjectId || null) !== (nextProjectId || null);
}

// Which stored song-context fields a save would delete outright.
//
// The route assigns both fields from the body, so saving with an empty box deletes what is stored —
// and unlike the two creative documents, which Story 2.1 gave `treatment_previous` and
// `style_bible_previous`, a Song keeps no earlier copy. Nothing can bring a deleted lyric sheet
// back, and it is the largest hand-authored text this application accepts.
//
// Asked only for that unrecoverable case: text that exists being replaced with nothing. Editing a
// sheet down to *different* text is typing, and a question about every save would train the
// Director to click through the one question that protects real work.
export function songContextClearing(song, context) {
  const cleared = [];
  if (song?.lyrics?.trim() && !context?.lyrics?.trim()) cleared.push("lyric sheet");
  if (song?.caption?.trim() && !context?.caption?.trim()) cleared.push("style description");
  return cleared;
}

export const SONG_CONTEXT_CLEARING_CONSEQUENCE =
  "A song keeps no previous version of its context, so this cannot be restored the way a replaced " +
  "treatment or style bible can. Nothing else about the song changes: not the audio, its length or its provenance.";

export function songContextClearingQuestion(cleared) {
  return `Save this? It deletes the stored ${cleared.join(" and ")} for this song.\n\n${SONG_CONTEXT_CLEARING_CONSEQUENCE}`;
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

// The chat composer's per-turn consent to replace the creative documents, sent as the route's
// `apply_documents`. One selector and one label because three copies — the markup's checkbox,
// the handler that reads it, and the sentence the server's notice quotes — are how a rename
// leaves the reply telling the Director to tick a control that no longer exists. The label is
// app.py's APPLY_DOCUMENTS_LABEL, asserted against the markup by a contract test.
//
// Unchecked by default, and nothing persists it: consent is for the turn being sent, never
// remembered across turns or projects.
export const APPLY_DOCUMENTS_CONTROL = "#apply-documents";
export const APPLY_DOCUMENTS_LABEL = "Apply document changes";

// Consent for exactly one turn, read off the composer control. Optional-chained and compared
// rather than returned raw: the send handler reads this *outside* the try/catch that reports
// failures, so a bare `.checked` on a missing control would throw past the handler and kill the
// send with no request, no toast and no error — the control silently deciding to send nothing at
// all. A control that is not there has given no consent, which is a decline.
export function documentConsent(control) {
  return control?.checked === true;
}

// Consent is per turn *and* per project: it is spent when a turn finishes, and cleared when a
// project loads. Without the first, one tick applies every later turn with no fresh consent —
// "on until the Director notices" rather than opt-in. Without the second, consent given in one
// project is inherited by the next one loaded, replacing a document in a project the Director
// was not even looking at when they ticked the box.
export function clearDocumentConsent(control) {
  if (control) control.checked = false;
}

// Which project loads clear the consent: the ones that actually change project. `loadProject` is
// the refresh path as well as the switch path -- the queue refresh, both generate paths, multiview
// and the queue-ready loop all reload the project already on screen -- and clearing there unticks a
// box the Director ticked seconds ago, in the project they are still looking at, with no visible
// cause. Leaving *and* arriving both count: switching to no project at all must not leave consent
// ticked for whichever project loads next.
export function documentConsentClearedOnLoad(currentProjectId, nextProjectId) {
  return (currentProjectId || null) !== (nextProjectId || null);
}

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
// The declined turn is its own sentence rather than the one above, because "no document
// changed" is true but useless here: nothing changed for a reason the Director controls, and
// the toast is the one place to say which box to tick. Mirrors app.py's
// DOCUMENT_NOT_REQUESTED_NOTICE, whose thread line names *which* documents were proposed;
// both sides say "opt-in per turn" and both name the control, asserted by a contract test.
export const DOCUMENT_NOT_APPLIED_TOAST =
  `No document was written: replacing a document is opt-in per turn. Tick "${APPLY_DOCUMENTS_LABEL}" ` +
  "and ask again to apply one; the reply says what this turn proposed, and that text is not kept.";

// True when the reply itself reports a proposal this turn declined. Keyed on a phrase from the
// server's own DOCUMENT_NOT_REQUESTED_NOTICE — the DOCUMENT_RESTORE_REFUSAL_MARKER shape, and a
// contract test asserts it is a real substring of it — because the server is deliberately
// *silent* in three cases the client cannot see from the project alone: nothing was proposed,
// the candidate was an echo, and the guard would have refused it anyway. Blaming the opt-in for
// any of those tells the Director to tick a box and retry a turn that will write nothing either
// way, and a locked document is a fourth: the lock notice is what the reply carries there, and
// ticking the box would not apply it.
export const DOCUMENT_NOT_REQUESTED_MARKER = "Proposed but not applied";

// The last thing the Director was actually told, or `null` when the project carries no reply.
// Shared by every toast that has to be decided by the reply rather than by a diff: the *last*
// assistant line decides, because an earlier turn's notice is still in the thread, and a system
// line is the restore audit trail rather than anything the Director said.
function assistantReply(project) {
  const messages = project?.messages;
  if (!Array.isArray(messages)) return null;
  const reply = messages.filter((message) => message?.role === "assistant").at(-1);
  return typeof reply?.content === "string" ? reply.content : null;
}

export function documentProposalDeclined(project) {
  return (assistantReply(project) ?? "").includes(DOCUMENT_NOT_REQUESTED_MARKER);
}

// The separator app.py puts between a reply's prose and the notices attached to it, and the one
// it joins the notices with. Both are app.py's NOTICE_SEPARATOR and NOTICE_JOIN, asserted
// identical by a contract test -- the splitter strips exactly this tail, so a drift here would
// leave every notice printed twice rather than silently mis-rendered.
export const NOTICE_SEPARATOR = "\n\n---\n";
export const NOTICE_JOIN = "\n\n";

// What each kind of notice is called on screen, and the class its left edge is coloured through.
//
// State is never conveyed by colour alone, so the label is the first signal and the edge is the
// second -- and both change together per kind, which is the whole point of the kind existing. One
// label for every notice meant a reply that had *successfully* replaced a document, or written
// prompts for four shots, announced that in amber under the word "Safety notice": the alarm
// fatigue that makes the real refusal beside it invisible.
//
// The keys are the server's own discriminator (`models.MessageNotice.kind`), asserted equal by a
// contract test. Each class is styled in styles.css and the mapping is executed there too, so a
// kind the client renders but never colours cannot pass.
export const NOTICE_KINDS = {
  // Something the guard would not do, or would not let the model do.
  refusal: { label: "Safety notice", className: "notice-refusal" },
  // Something that was done: a document replaced, prompts written, a version restored.
  change: { label: "Change applied", className: "notice-change" },
  // Neither -- a discrepancy worth a look, like prose claiming shots the reply did not carry.
  flag: { label: "Check this", className: "notice-flag" },
};

// An unknown or absent kind is presented as a refusal rather than as the quietest option. A
// refusal dressed as good news is the failure that costs something; a change dressed as a
// refusal is merely over-cautious, and it is what every notice looked like before kinds existed.
export const NOTICE_FALLBACK_KIND = "refusal";

export function noticeKind(kind) {
  return Object.prototype.hasOwnProperty.call(NOTICE_KINDS, kind) ? kind : NOTICE_FALLBACK_KIND;
}

// The disclosure over the model output a refusal is about. Collapsed by default because it is
// evidence, not reading -- and because it is exactly the degraded text the guard exists to keep
// out of sight and out of the next prompt.
export const NOTICE_RAW_LABEL = "Raw model output";

// Five characters, not four. This is a shared export used in attribute positions as well as text
// -- `value="${…}"`, `title="${…}"`, `data-*` -- and an unescaped `'` closes any single-quoted
// attribute and lets the rest of the value be read as markup. The apostrophe costs nothing in
// text position and is the difference between escaping and nearly escaping in attribute position.
const HTML_ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

export function escapeHtml(value = "") {
  return String(value).replace(/[&<>"']/g, (char) => HTML_ESCAPES[char]);
}

// One stored message split into the Director's prose and the notices the server attached to it.
//
// The notices are read from the message's own `notices` field, never recovered from the text. The
// message is *not* split on the `---` separator, and that is the whole reason the field exists:
// the Director's prose can legitimately contain that sequence, and so can the raw model output a
// rejection notice is about, so a search would split one notice into two blocks -- with the
// second being the model's own degraded text presented as a protective refusal.
//
// The joined tail is reconstructed from the notices and stripped from the end of `content`, which
// is exactly how the server built it. A message with no notices -- every message in every project
// saved before notices existed, and every ordinary reply -- is returned as prose and nothing else,
// so it renders exactly as it did before.
export function messageParts(message) {
  const content = typeof message?.content === "string" ? message.content : "";
  // Notice chrome belongs to an assistant reply and to nothing else. `app.assistant_reply` is the
  // one constructor that attaches notices, so a `user` or `system` message arriving with a
  // populated list is a hand-edited manifest or the Director's own words fed back -- and drawing
  // that as a protective refusal lets a bubble the Director typed be made to look like the guard
  // speaking, which is the exact confusion the block exists to remove.
  if (message?.role !== "assistant") return { prose: content, notices: [] };
  const carried = (Array.isArray(message?.notices) ? message.notices : []).map((notice) => ({
    kind: noticeKind(notice?.kind),
    text: typeof notice?.text === "string" ? notice.text : "",
    // Whitespace-only raw output is absent output. It is truthy, so it used to render a
    // disclosure that offered the evidence behind a refusal and then opened onto an empty box.
    raw: typeof notice?.raw === "string" && notice.raw.trim() !== "" ? notice.raw : "",
  }));
  if (!carried.length) return { prose: content, notices: [] };
  // The tail is reconstructed from *every* carried notice, including any whose text is empty,
  // because `NOTICE_JOIN.join(notice.text for notice in notices)` on the server joined every one
  // of them. Reconstructing it from the displayable subset instead made the tail stop matching
  // the moment one empty notice existed, and the fallback below then kept the whole joined string
  // as prose -- printing every remaining notice twice, once inside the prose and once as a block.
  const tail = NOTICE_SEPARATOR + carried.map((notice) => notice.text).join(NOTICE_JOIN);
  // Nothing is dropped when the tail does not match: a reply whose joined text the client cannot
  // account for keeps all of its content and still shows every notice as its own block. Printing
  // a notice twice is a visible defect; silently swallowing part of a refusal is not.
  const prose = content.endsWith(tail) ? content.slice(0, content.length - tail.length) : content;
  // An empty notice is not a block: there is no sentence to read in it. It still counted above,
  // which is the only thing it is good for.
  return { prose, notices: carried.filter((notice) => notice.text !== "") };
}

// The inner HTML of one message in the thread: the prose, then one block per notice, each
// labelled in words for its kind and each carrying its raw output behind a collapsed disclosure.
//
// Every part is escaped separately, after the split rather than before it -- the thread used to
// be one `innerHTML` map that escaped `content` whole, so notice markup built by splitting the
// escaped string would have had to re-parse its own escaping.
//
// `key` only has to be unique within one thread: it is what makes each block's label id unique,
// which is what `aria-labelledby` needs to name the right block.
export function messageBodyHtml(message, key = "") {
  const { prose, notices } = messageParts(message);
  const base = noticeIdBase(key === "" || key === null || key === undefined ? message?.id : key);
  return escapeHtml(prose) + notices.map((notice, index) => noticeHtml(notice, `${base}-${index}`)).join("");
}

function noticeIdBase(key) {
  const slug = String(key ?? "").replace(/[^A-Za-z0-9_-]/g, "-");
  return `notice-${slug || "0"}`;
}

// `role="note"` with the label as its accessible name, so the block is announced as an aside with
// a name rather than as one more run of text inside the Director's reply -- until this existed,
// the whole of "this is the guard speaking, not the Director" was a coloured edge and a line of
// small caps, neither of which a screen reader conveys at all.
function noticeHtml(notice, id) {
  const { label, className } = NOTICE_KINDS[notice.kind];
  const raw = notice.raw
    ? `<details class="notice-raw"><summary>${escapeHtml(NOTICE_RAW_LABEL)}</summary><pre>${escapeHtml(notice.raw)}</pre></details>`
    : "";
  return `<div class="message-notice ${escapeHtml(className)}" role="note" aria-labelledby="${escapeHtml(id)}">`
    + `<strong class="notice-label" id="${escapeHtml(id)}">${escapeHtml(label)}</strong>`
    + `<p>${escapeHtml(notice.text)}</p>${raw}</div>`;
}

// What the Director is told when a project has no reply yet. Here rather than in the template it
// is drawn into, because `threadHtml` below returns the *whole* body and there is exactly one
// thing the thread can be.
export const EMPTY_THREAD_TITLE = "Direct the video naturally";
export const EMPTY_THREAD_HINT =
  "Describe narrative, energy, references, camera language, what to avoid, and where the " +
  "performance should feel literal or abstract.";

// The entire inner HTML of the chat thread, built here so it can be executed.
//
// This is the string `renderTreatment` assigns to `thread.innerHTML` and nothing else: no markup
// is left in the DOM layer for the suite to be unable to reach. That split is the point. app.js
// is imported by no test and executed by none, so while the markup lived there, assigning it to
// `textContent` instead -- or escaping the body a second time -- left every assertion in the
// suite satisfied while every refusal rendered as literal `<div class="message-notice">` text and
// the block never appeared at all.
export function threadHtml(messages) {
  const list = Array.isArray(messages) ? messages : [];
  if (!list.length) {
    return `<div class="empty-thread"><strong>${escapeHtml(EMPTY_THREAD_TITLE)}</strong><p>${escapeHtml(EMPTY_THREAD_HINT)}</p></div>`;
  }
  return list
    .map((message, index) => {
      const key = message?.id ? message.id : `${index}`;
      return `<div class="message ${escapeHtml(message?.role ?? "")}">${messageBodyHtml(message, key)}</div>`;
    })
    .join("");
}

// `applied` is the consent that was actually sent, not a guess: with it off the server writes
// no document at all, so the reply cannot have replaced anything whatever the two projects
// differ by. It defaults to `true` because that is the safe default of the two — a caller that
// forgets it gets the diff-derived sentence, where defaulting to `false` would blame the flag
// for a lock or a rejection and send the Director to tick a box that was already ticked.
export function documentChangeToast(before, after, applied = true) {
  // A declined turn is decided by the reply, never by the diff. A restore or a hand edit
  // committed while the Director call was in flight moves the document without this reply
  // having touched it, and before/after cannot tell the two apart — so the diff would credit
  // the reply with a replacement that consent made impossible.
  if (!applied) {
    return documentProposalDeclined(after) ? DOCUMENT_NOT_APPLIED_TOAST : DOCUMENT_UNCHANGED_TOAST;
  }
  const changed = Object.keys(DOCUMENT_LABELS).filter(
    (document) => (before?.[document] ?? "") !== (after?.[document] ?? ""),
  );
  if (!changed.length) return DOCUMENT_UNCHANGED_TOAST;
  return DOCUMENT_CHANGE_TOAST.replace("{documents}", changed.map(documentLabel).join(" and "));
}

// The one claim both halves of the expansion control make about what pressing it costs. The
// button's `title` says it before the click and the toast says it after, and a Director deciding
// whether a "Director" button will spend GPU minutes must not read two different sentences about
// it. The markup cannot import this, so a contract test asserts the button carries this spelling.
export const SHOT_EXPANSION_NO_RENDER = "Nothing is rendered";

// Why an empty plan is refused before anything is sent, in the browser's voice. The rule is one
// rule: app.py's EXPANSION_WITHOUT_SHOTS refuses the same case with the same sentence, and a
// contract test asserts the two are identical -- two hand-written wordings for one refusal are how
// the pre-emptive toast starts describing a rule the server no longer has.
export const SHOT_EXPANSION_WITHOUT_SHOTS =
  "This project has no shots to expand. Expansion writes a prompt onto each existing shot " +
  "and never creates, retimes, or removes one, so add shots to the timeline first.";

// Silent shot saves are refused while an expansion is in flight, so the refusal has to be said out
// loud: the edit is not saved and the response re-renders the timeline over it, and a drag that
// vanishes with no explanation reads as the app losing work at random.
export const SHOT_EXPANSION_EDIT_BLOCKED =
  "Shots are being expanded into prompts, so this timeline edit was not saved -- a save queued now " +
  "would carry the prompts from before the expansion and revert it. Make the edit again once the " +
  "prompts land.";

// What one shot expansion did, taken from the reply rather than from a diff of the shots. The
// server knows which Shots it wrote and says so in EXPANSION_WRITTEN_NOTICE; a diff cannot tell a
// re-run that wrote the same text from a write that never happened, and would then announce "No
// shot prompt changed" directly under a reply saying prompts were written for two shots. The toast
// is the loudest thing on screen, so it says what the reply beside it says.
export const SHOT_EXPANSION_TOAST =
  `{count} shot prompt{plural} written by this expansion. ${SHOT_EXPANSION_NO_RENDER}, and every prompt is editable in the shot inspector.`;
export const SHOT_EXPANSION_UNCHANGED_TOAST =
  "No shot prompt changed; the reply says what the expansion returned and why none of it was applied.";

// Keyed on the server's own EXPANSION_WRITTEN_NOTICE -- a contract test asserts the count is read
// back out of a real formatted notice -- so the count in the toast is the count in the reply, by
// construction. The `shot(s):` tail is part of the match on purpose: the model's own prose sits
// above the notices in the same message, and a bare "Prompts written for" could appear in it.
export const SHOT_EXPANSION_WRITTEN_MARKER = "Prompts written for";
const SHOT_EXPANSION_WRITTEN_PATTERN = new RegExp(`${SHOT_EXPANSION_WRITTEN_MARKER} (\\d+) shot\\(s\\):`);

export function shotExpansionWritten(project) {
  const match = SHOT_EXPANSION_WRITTEN_PATTERN.exec(assistantReply(project) ?? "");
  return match ? Number(match[1]) : 0;
}

export function shotExpansionToast(project) {
  const written = shotExpansionWritten(project);
  if (!written) return SHOT_EXPANSION_UNCHANGED_TOAST;
  return SHOT_EXPANSION_TOAST.replace("{count}", written).replace("{plural}", written === 1 ? "" : "s");
}

// Why nothing was submitted, in the browser's voice. `batch.py`'s READINESS_REFUSAL is the server
// half and a contract test asserts the two templates are identical: the single-Shot route and the
// whole-batch check refuse for one reason, so a Director who hits it from either side must read
// one sentence. It is deliberately ASCII, for the reason recorded beside the server's copy.
//
// The remedy is its own constant because the refusal is not the only place it is said: the blocked
// clip's tooltip states the same fix before the click that the refusal states after it, and a
// Director who reads two different instructions for one problem tries both.
export const READINESS_REMEDY =
  "Write a prompt in the shot inspector, or run the Director's shot expansion";
export const READINESS_REFUSAL =
  "Not submitted: no prompt on {shots}. An empty prompt spends a full GPU pass and returns " +
  `noise, so nothing was sent to ComfyUI. ${READINESS_REMEDY}, then submit again.`;

// The plan-level sentence, mirroring batch.py's PLAN_WITHOUT_SHOTS: nothing is wrong with any
// Shot, there is no Shot. A contract test asserts the two are identical.
export const PLAN_WITHOUT_SHOTS =
  "This project has no shots, so there is nothing to submit. Add shots to the timeline first.";

// How many Shots one refusal names before it counts the rest, mirroring batch.py's
// REFUSAL_NAME_LIMIT. A batch over twenty blocked Shots would otherwise render as an unreadable
// wall of names in a toast -- and it must render as the *same* wall the server would have sent.
export const REFUSAL_NAME_LIMIT = 5;

// `names` are display names, not ids: `labels` off the report at the batch check, raw ids only
// when a caller has nothing better. The server's `readiness_refusal` takes the same, for the same
// reason -- one sentence, and the caller decides what a Shot is called.
//
// An empty list is a designed-for input rather than a caller error: the empty-plan note carries no
// ids, so every extractor returns `[]` for it, and "no prompt on ." is not a thing to tell anyone.
export function readinessRefusal(names) {
  const list = names || [];
  if (!list.length) return PLAN_WITHOUT_SHOTS;
  const remaining = list.length - REFUSAL_NAME_LIMIT;
  const listed = list.slice(0, REFUSAL_NAME_LIMIT).join(", ");
  return READINESS_REFUSAL.replace("{shots}", remaining > 0 ? `${listed} and ${remaining} more` : listed);
}

// Every Shot id a readiness report blocks, flattened out of its notes. A note carries the Shots it
// is about, and the empty-plan note deliberately carries none -- there is no Shot to name -- so an
// empty plan yields an empty list here rather than a placeholder id nothing can be matched against.
export function blockedShotIds(report) {
  return (report?.blocking || []).flatMap((note) => note?.shot_ids || []);
}

// Every Shot id a readiness report blocks, under the names the Director sees. Positionally
// aligned with the ids inside each note, and carried by the report rather than derived here: the
// server names a Shot `SHOT 01 (shot_id)` by its position in the *manifest* while the notes
// themselves are in song order, so a browser that recomputed the numbering would disagree with the
// server for any plan whose manifest order is not its time order.
export function blockedShotLabels(report) {
  return (report?.blocking || []).flatMap((note) => noteLabels(note));
}

// One note's display names, falling back to the raw ids for a note that carries none.
function noteLabels(note) {
  const ids = note?.shot_ids || [];
  const labels = note?.labels || [];
  return ids.map((id, index) => labels[index] || id);
}

// The prompt with case and whitespace differences removed, mirroring batch.py's `_collapsed`
// (`" ".join(prompt.lower().split())`). Every emptiness decision below compares collapsed text, so
// a placeholder that picked up stray spacing on the way through a duplicate is still a placeholder.
function collapsePrompt(prompt) {
  return String(prompt ?? "").toLowerCase().trim().replace(/\s+/g, " ");
}

// The prompt `app.js` writes onto every Shot it creates, and that duplicating a Shot copies.
// Exported so the creation site and the emptiness rule cannot spell it differently -- and mirroring
// batch.py's PLACEHOLDER_PROMPT, because the server refuses it.
export const PLACEHOLDER_PROMPT = "New shot";

// The two reasons a prompt cannot be submitted, in the server's own words: batch.py's
// SHOT_WITHOUT_PROMPT and SHOT_WITH_PLACEHOLDER_PROMPT, asserted identical by a contract test. Kept
// as constants rather than reworded here, so the sentence the clip shows before the click is the
// sentence the report carries after it.
export const SHOT_WITHOUT_PROMPT =
  "This shot has no prompt. Submitting it would spend a full GPU pass and return noise.";
export const SHOT_WITH_PLACEHOLDER_PROMPT =
  `This shot still carries the "${PLACEHOLDER_PROMPT}" placeholder every new shot is created ` +
  "with, which is not a prompt anyone wrote. Submitting it would spend a full GPU pass on it.";

// The client half of `batch.prompt_rejection`: why this Shot cannot be submitted, or "" when it
// can. Mirrored rather than fetched because the timeline redraws on every drag, resize and
// keystroke and a round trip per redraw would be a request storm -- the server remains the gate,
// and this decides only what is drawn.
//
// The placeholder is treated exactly as blank, and this is the case that matters most: `""` takes
// a deliberate deletion, while "New shot" arrives by default on every Shot the Director adds. A
// client that let it through would draw a plan of placeholder clips as fully prompted and then be
// refused by the route, which is worse than either half alone. Compared after collapse so a copy
// with stray spacing or different case is caught, while a real prompt that merely *begins* with
// those words is not.
export function promptRejection(shot) {
  const collapsed = collapsePrompt(shot?.prompt);
  if (!collapsed) return SHOT_WITHOUT_PROMPT;
  if (collapsed === collapsePrompt(PLACEHOLDER_PROMPT)) return SHOT_WITH_PLACEHOLDER_PROMPT;
  return "";
}

export function promptIsMissing(shot) {
  return Boolean(promptRejection(shot));
}

// What an unprompted clip says instead of a prompt. The timeline used to fall back to "Untitled
// shot", which is indistinguishable from a real prompt reading "Untitled shot", so emptiness was
// invisible until a submission failed. Text rather than a colour, because state is never conveyed
// by colour alone; the dashed clip border in styles.css is the second, redundant signal.
export const SHOT_WITHOUT_PROMPT_FLAG = "NO PROMPT";
// Its own flag, because the two states are different things to be in and the fix differs: nobody
// has written this Shot yet, versus its text was cleared. The server splits its reasons for the
// same reason; a placeholder reported as "NO PROMPT" sends the Director looking for a prompt they
// can see is there.
export const SHOT_WITH_PLACEHOLDER_FLAG = "PLACEHOLDER";

// What a blocked clip tells the Director when they reach it, before anything is submitted. The
// flag alone says *that* something is wrong; this says what -- in the server's own sentence -- and
// how to fix it, in the words the refusal uses afterwards. It is the clip's accessible name as
// well as its tooltip: until it existed, the whole of a blocked clip's state was a word and a
// dashed border, neither of which a screen reader announces as a state.
export function shotPromptHelp(shot) {
  const rejection = promptRejection(shot);
  return rejection ? `${rejection} ${READINESS_REMEDY}.` : "";
}

// Everything the timeline draws for one clip's prompt cell, decided here rather than in the
// template. The template used to hold the ternaries, and swapping their arms -- stamping NO PROMPT
// on every *written* clip and rendering the unprompted one empty -- kept every substring the suite
// asserted, so the one signal that costs a wasted GPU pass could be rendered exactly backwards
// with the tests green. Executed by tests/test_frontend_contract.py for every state.
//
// `label` is the clip's title and accessible name: the help for a blocked shot, and the full
// prompt otherwise, since the cell itself is clamped to two lines.
export function shotPromptCell(shot) {
  const prompt = String(shot?.prompt ?? "");
  const rejection = promptRejection(shot);
  if (!rejection) return { blocked: false, text: prompt, className: "", label: prompt };
  return {
    blocked: true,
    text: rejection === SHOT_WITH_PLACEHOLDER_PROMPT ? SHOT_WITH_PLACEHOLDER_FLAG : SHOT_WITHOUT_PROMPT_FLAG,
    className: "no-prompt",
    label: shotPromptHelp(shot),
  };
}

// Whether a batch may be submitted, given the server's report and the ids actually being queued.
//
// The filter is the whole decision, and it is the one that inverts silently: negating it to
// "blocked *outside* this batch" refuses the button over a blank draft elsewhere in the plan --
// which is every plan, most of the time -- while letting the batch that really does contain a
// blocked Shot through, producing exactly the half-submitted batch the check exists to prevent.
// Both directions are executed as tests rather than grepped for.
// The refusal names the blocked Shots by the report's own labels, never by id: `SHOT 03 (shot_id)`
// is what the server would have said and what the timeline draws, and an id alone names something
// that appears nowhere on screen.
export function batchReadinessBlock(report, queuedIds = []) {
  const queued = new Set(queuedIds || []);
  const blocked = [];
  const labels = [];
  for (const note of report?.blocking || []) {
    const names = noteLabels(note);
    (note?.shot_ids || []).forEach((id, index) => {
      if (!queued.has(id)) return;
      blocked.push(id);
      labels.push(names[index]);
    });
  }
  return { refused: blocked.length > 0, blocked, labels, message: blocked.length ? readinessRefusal(labels) : "" };
}

// Why the batch button is off when it is off. Nothing to queue is one reason and a batch the route
// will certainly refuse is another, and they need different sentences: "mark a shot ready" is
// useless advice for a batch whose Shots are all ready and one of which has no prompt.
export const QUEUE_WITHOUT_READY_SHOTS = "Mark a shot ready to queue H3";

export function queueButtonState(report, readyShots = []) {
  const shots = (readyShots || []).filter(Boolean);
  if (!shots.length) return { disabled: true, blocked: [], title: QUEUE_WITHOUT_READY_SHOTS };
  const block = batchReadinessBlock(report, shots.map((shot) => shot?.id));
  if (block.refused) return { disabled: true, blocked: block.blocked, title: block.message };
  return {
    disabled: false,
    blocked: [],
    title: `Queue ${shots.length} reviewed H3 shot${shots.length === 1 ? "" : "s"}`,
  };
}

// Which half of the report a note came from, said in words. State is never carried by colour
// alone, and these two lines are otherwise distinguished only by the colour of a list marker --
// so the kind is part of the sentence.
export const READINESS_BLOCKING_LABEL = "Blocked";
export const READINESS_SAMENESS_LABEL = "Near-duplicate";

// Every note in a readiness report, rendered as a line, in the server's own words and under the
// server's own names for the Shots.
//
// The warnings half is the reason this exists. Nothing in the browser read `report.warnings` at
// all: the batch check reads only the blocking ids and the compile toast prints the timeline's
// frame warnings, so the near-duplicate pairs the server computes reached no surface a Director
// could act on -- and FR-26 says they may "differentiate or accept them deliberately", which is
// not a choice anyone can make about a pair they cannot see.
//
// The reason is passed through rather than reworded: it is the one sentence the server wrote for
// this exact case, and a second wording here is how the browser starts describing a rule the
// server no longer has. A note that names no Shot -- the empty plan -- names none here either.
export function readinessLines(report) {
  const render = (kind, label) => (note) => {
    const shotIds = note?.shot_ids || [];
    const names = noteLabels(note);
    const reason = note?.reason || "";
    return { kind, shotIds, shots: names, reason, text: names.length ? `${label} - ${names.join(" and ")}: ${reason}` : `${label} - ${reason}` };
  };
  return [
    ...(report?.blocking || []).map(render("blocking", READINESS_BLOCKING_LABEL)),
    ...(report?.warnings || []).map(render("warning", READINESS_SAMENESS_LABEL)),
  ];
}

// The standing one-line state of the plan, above the button that acts on it. Before this the
// button was enabled purely from the ready-status count, so a batch the server would certainly
// refuse looked fully submittable until it was clicked and refused.
export const READINESS_NOT_CHECKED = "Readiness has not been checked for this project yet.";
// "We did not look" is not "there is nothing to find". The submission route asks for a
// blocking-only report, so an empty `warnings` list means one of two completely different things
// and the report says which -- reporting the wrong one would tell the Director their plan has no
// duplicates on the strength of a pass that never ran.
export const SAMENESS_NOT_CHECKED = "near-duplicate prompts were not checked";

export function readinessSummary(report) {
  if (!report) return READINESS_NOT_CHECKED;
  const total = Number(report.shot_count) || 0;
  const ready = Number(report.ready_count) || 0;
  const blocked = blockedShotIds(report).length;
  const pairs = (report.warnings || []).length;
  const omitted = Number(report.warnings_omitted) || 0;
  const parts = [`${ready} of ${total} ${total === 1 ? "shot has" : "shots have"} a prompt`];
  if (blocked) parts.push(`${blocked} cannot be submitted`);
  if (report.warnings_computed === false) parts.push(SAMENESS_NOT_CHECKED);
  // The overflow is counted rather than dropped: a plan with more pairs than the report lists
  // must not look like a plan with exactly as many as it lists.
  else if (pairs) parts.push(`${pairs} near-duplicate pair${pairs === 1 ? "" : "s"}${omitted ? ` (${omitted} more not listed)` : ""}`);
  return `${parts.join("; ")}.`;
}

// What the shot inspector says about the Shot in front of the Director. The refusal sends them
// here -- "Write a prompt in the shot inspector" -- and until now the panel said nothing at all
// about the Shot being blocked, so the instruction led to a screen that looked ordinary.
//
// The block is decided from the prompt on screen rather than from the report, because the report
// is fetched per project load and the textarea is edited between fetches: a Shot the Director has
// just written a prompt for must stop reading as blocked immediately. Sameness cannot be decided
// locally -- it is a comparison across the whole plan -- so those lines come from the report.
export function shotInspectorReadiness(report, shot) {
  const cell = shotPromptCell(shot);
  const sameness = shot?.id
    ? readinessLines(report).filter((line) => line.kind === "warning" && line.shotIds.includes(shot.id))
    : [];
  // Whether sameness was looked for at all is a plan-level fact, said once in the readiness
  // summary rather than repeated on every Shot: the report this client fetches always carries the
  // pairwise pass, and a per-Shot caveat about a pass that did run is noise beside a prompt.
  return { blocked: cell.blocked, flag: cell.blocked ? cell.text : "", help: shotPromptHelp(shot), sameness };
}

// What a batch that failed partway has to say beyond the failure itself. The Shots already
// accepted are burning GPU minutes right now, and a bare refusal reads as "nothing happened" --
// so the Director edits and resubmits a plan half of which is already in flight.
export const BATCH_QUEUE_PROGRESS =
  "{queued} of {total} shots had already been queued when this failed; what was accepted is " +
  "already rendering, and the rest was not sent.";
export const BATCH_QUEUE_NO_PROGRESS = "Nothing was queued, so no GPU time was spent.";

export function batchQueueProgress(queued, total) {
  const done = Number(queued) || 0;
  if (!done) return BATCH_QUEUE_NO_PROGRESS;
  return BATCH_QUEUE_PROGRESS.replace("{queued}", `${done}`).replace("{total}", `${Number(total) || 0}`);
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
  // Its own route, and it carries only the two context fields: the audio, the duration and the
  // provenance are not editable text, so nothing that could overwrite them is on the wire.
  saveSongContext: (id, context) => request(`/api/projects/${id}/song/context`, { method: "PUT", headers: jsonHeaders, body: JSON.stringify(context) }),
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
  // A GET, and nothing is cached from it: readiness is derived from the prompts on every call, so
  // the only stale answer possible is one this client held on to.
  readiness: (id) => request(`/api/projects/${id}/readiness`),
  generateH3: (projectId, shotId, body = {}) => request(`/api/projects/${projectId}/shots/${shotId}/generate/h3`, { method: "POST", headers: jsonHeaders, body: JSON.stringify(body) }),
  directorChat: (id, body) => request(`/api/projects/${id}/director/chat`, { method: "POST", headers: jsonHeaders, body: JSON.stringify(body) }),
  // Its own route, and it carries no body: expansion is not a chat turn. The whole input the
  // model sees is derived on the server from the project itself, so there is nothing here for a
  // message to travel in — and nothing that could queue a render.
  expandShots: (id) => request(`/api/projects/${id}/director/expand`, { method: "POST" }),
  job: (projectId, jobId) => request(`/api/projects/${projectId}/jobs/${jobId}`),
  workflows: () => request("/api/workflows"),
};
