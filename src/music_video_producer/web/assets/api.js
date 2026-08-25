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
      body: { title: data.title, idea: data.caption, duration: Number(data.duration), duration_headroom: plannedHeadroom(data), seed: Number(data.seed) },
    };
  }
  if (data.preset === "songplanner-known") {
    return {
      endpoint: "songplanner",
      body: { title: data.title, idea: data.caption, lyrics: (data.lyrics || "").trim(), duration: Number(data.duration), duration_headroom: plannedHeadroom(data), seed: Number(data.seed) },
    };
  }
  return {
    endpoint: "music",
    body: { title: data.title, caption: data.caption, lyrics: data.lyrics, duration: Number(data.duration), seed: Number(data.seed) },
  };
}

// The multiplier a SongPlanner request carries. Sent on every SongPlanner submission rather than
// omitted, so the number on screen is the number the route uses — an omitted field silently took
// the route's own default, which is how a duration above 240 s started taking a 422 from a form
// whose `max` still said 300. Direct Music 3 never gets one: `MusicRequest` has no planner, so no
// lyrics that can overrun and no such field to send.
//
// A missing or cleared value falls back to the same default the route has, which is the only
// answer that does not invent a number: the form's own box is `required`, so this is reachable
// only from a caller that never had a box.
function plannedHeadroom(data) {
  const raw = data.duration_headroom;
  if (raw === "" || raw === null || raw === undefined) return SONGPLANNER_HEADROOM.default;
  return Number(raw);
}

// Pure preset → lyrics/headroom/duration/seed field state for the Song workspace form, kept
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
    // The headroom belongs to the planner path alone. Direct Music 3 has no `M3SongPlanner`
    // between the Director and the encoder, so its `max_duration` *is* the requested duration and
    // there is nothing to multiply. Its bounds are `null` rather than Music 3's own numbers: a
    // control that does not exist must not be handed a plausible-looking range.
    headroomVisible: songplanner,
    headroomMin: songplanner ? SONGPLANNER_HEADROOM.min : null,
    headroomMax: songplanner ? SONGPLANNER_HEADROOM.max : null,
    headroomDefault: songplanner ? SONGPLANNER_HEADROOM.default : null,
    durationMin: songplanner ? 30 : 4,
    durationMax: songplanner ? 300 : 360,
    seedMin: 0,
    seedMax: songplanner ? "4294967295" : "18446744073709551615",
  };
}

// `SongPlannerRequest.duration_headroom`'s floor, ceiling and default, carried once here because
// JavaScript cannot import a Pydantic model — tests/test_frontend_contract.py reads `ge`, `le` and
// `default` off the model and asserts each equals its entry, which is what makes these one number
// rather than two, exactly as it already does for the duration and seed bounds.
//
// The floor is 1.0 and stays reachable: it hands the encoder exactly the song's length, which is
// the pre-headroom payload byte for byte and one of the two candidate answers to a question the
// evidence has not settled. Nothing here may round it away.
const SONGPLANNER_HEADROOM = { min: 1, max: 12, default: 1.5 };

// `MiniMaxMusic3TextEncode.max_duration`'s own schema ceiling, equal to
// `workflows.MUSIC3_MAX_DURATION_SECONDS` and asserted so by the same test. It bounds the
// *product* of the duration and the headroom, not either field, which is the whole reason the
// form shows the product instead of pretending one input describes the limit.
export const MUSIC3_MAX_DURATION_SECONDS = 360;

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
  const numeric = {
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
  };
  // Only for the presets that have the field. Leaving it out of `numeric` for direct Music 3 is
  // what stops the DOM applier from writing bounds onto a control that route knows nothing about,
  // and it also leaves the Director's chosen multiplier untouched by a trip through Balanced.
  if (fields.headroomVisible) {
    numeric.duration_headroom = {
      min: fields.headroomMin,
      max: fields.headroomMax,
      // An empty box seeds the default instead of staying cleared, which is the one place this
      // field parts company with `duration`. A cleared duration is a Director mid-edit and theirs
      // to leave blank; an absent multiplier is not a request at all, and the entire point of the
      // control is that whatever multiplier is in force is legible on screen rather than applied
      // by a default nobody saw. Anything actually typed is clamped to this field's own bounds
      // and to nothing else — see `songEncoderCeiling` for why the duration is not one of them.
      value: emptyValue(current.duration_headroom)
        ? fields.headroomDefault
        : clampToBounds(current.duration_headroom, fields.headroomMin, fields.headroomMax),
    };
  }
  return {
    lyricsVisible: fields.lyricsVisible,
    lyricsRequired: fields.lyricsRequired,
    headroomVisible: fields.headroomVisible,
    numeric,
  };
}

function emptyValue(raw) {
  return raw === "" || raw === null || raw === undefined;
}

// A number the way a Director reads one: at most two decimals, and no trailing zeros on a value
// that did not need them. 240 x 1.5 is 360, not 360.00, and 360 / 300 is 1.2, not 1.2000000001.
function readable(value) {
  return String(Number(value.toFixed(2)));
}

// Pure decision: what the encoder's latent ceiling works out to for a duration and a headroom, and
// whether that product is submittable at all.
//
// This is the form's answer to the one question the two fields raise together: 300 s at the
// default 1.5 is 450 s, which the route refuses, so the form must not sit there promising it. Four
// answers were available and this is the one taken, with the three rejected ones and why:
//
//   * Bound the duration against the current headroom, or the headroom against the current
//     duration. Both were rejected for the same reason: whichever field is made to follow becomes
//     a trap. Raise the headroom and the duration's `max` slides under the number already in the
//     box, so either the Director's typed duration is silently rewritten — the one thing this
//     feature exists to prevent, since a quietly shortened ceiling is exactly the truncation it
//     guards — or the box holds a value its own `max` now forbids. Neither field is subordinate to
//     the other; they are two independent inputs whose *product* is what the schema bounds.
//   * Leave both free and let the 422 explain. Honest, and worse: the Director spends a submit,
//     a replacement confirmation and a GPU-cost confirmation to learn something arithmetic the
//     browser already had every number for.
//
// So the product is shown, continuously, beside the two fields that make it, and a product outside
// the schema is refused locally in the same words the readout is already showing. Nothing is
// clamped, both fields keep their own model bounds whatever the other holds, and the sentence
// names both alternatives — a lower headroom or a shorter song — so the Director chooses which of
// their two numbers gives way rather than having the form choose for them.
//
// `refusal` is the readout's own text rather than a second sentence: the line on screen and the
// toast that blocks the submit say exactly the same thing, so there is no wording to drift.
export function songEncoderCeiling(duration, headroom) {
  const target = Number(duration);
  const multiplier = Number(headroom);
  // A half-filled pair states nothing. The browser's own `required`/`min` validation and the
  // route's 422 both report an empty or non-numeric box; inventing a product from one would put a
  // number on screen that no field holds.
  if (emptyValue(duration) || emptyValue(headroom) || !Number.isFinite(target) || !Number.isFinite(multiplier)) {
    return { ceiling: null, exceeds: false, text: CEILING_UNSET, refusal: null };
  }
  const ceiling = target * multiplier;
  const shown = `Song written to ${readable(target)} s · encoder ceiling ${readable(ceiling)} s`;
  if (ceiling <= MUSIC3_MAX_DURATION_SECONDS) {
    return { ceiling, exceeds: false, text: `${shown} of ${readable(MUSIC3_MAX_DURATION_SECONDS)} s allowed.`, refusal: null };
  }
  // Both ways out, computed rather than described, and both rounded *down* so a suggestion the
  // Director types back in cannot land a hair over the ceiling it was offered to clear.
  const headroomFits = Math.floor((MUSIC3_MAX_DURATION_SECONDS / target) * 100) / 100;
  const durationFits = Math.floor((MUSIC3_MAX_DURATION_SECONDS / multiplier) * 100) / 100;
  const ways = headroomFits >= SONGPLANNER_HEADROOM.min
    ? `Lower the headroom to ${readable(headroomFits)}, or the duration to ${readable(durationFits)} s.`
    : `Lower the duration to ${readable(durationFits)} s.`;
  const text = `${shown} — over the encoder's ${readable(MUSIC3_MAX_DURATION_SECONDS)} s maximum. ${ways}`;
  return { ceiling, exceeds: true, text, refusal: text };
}

// What the readout says when the two boxes do not yet make a product. Not a blank: the line has to
// hold its place in the form, or the layout jumps as a Director types.
export const CEILING_UNSET = "Encoder ceiling — fill in both the duration and the headroom.";

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
// The route assigns both fields from the body, so saving with an empty box deletes what is stored.
// It is now recoverable — each field keeps the one version a save displaced, exactly as the
// treatment and the style bible do — but recovery is one step deep and the next save spends it, so
// clearing 8000 characters of pasted lyrics is still the save worth stopping on.
//
// Asked only for that case: text that exists being replaced with nothing. Editing a sheet down to
// *different* text is typing, and a question about every save would train the Director to click
// through the one question that protects real work.
export function songContextClearing(song, context) {
  const cleared = [];
  if (song?.lyrics?.trim() && !context?.lyrics?.trim()) cleared.push("lyric sheet");
  if (song?.caption?.trim() && !context?.caption?.trim()) cleared.push("style description");
  return cleared;
}

// What is true after the click, stated exactly. It used to say a song "keeps no previous version
// of its context", which was true when it was written and is a lie now — and a consequence that
// overstates the damage is as corrosive as one that understates it: a Director who believes an
// emptied field is gone forever will not look for the Restore button that would bring it back.
export const SONG_CONTEXT_CLEARING_CONSEQUENCE =
  "The version being replaced is kept, and Restore beside the box swaps it back — but only the one " +
  "most recent version, so the next save spends it. Nothing else about the song changes: not the audio, its length or its provenance.";

export function songContextClearingQuestion(cleared) {
  return `Save this? It deletes the stored ${cleared.join(" and ")} for this song.\n\n${SONG_CONTEXT_CLEARING_CONSEQUENCE}`;
}

// Every per-field song-context control, in one table: the restore button's element id, the field
// on the stored Song its enabled state reads, and the path segment the restore route accepts.
// Same shape and same argument as DOCUMENT_CONTROLS — a field's selector, slot and route segment
// spelled out again at the render, bind and call sites is how a rename half-lands and leaves a
// button wired to the other field's slot. `field` is the route's own path segment, so a rename
// here 404s rather than restoring the wrong half of the context.
export const SONG_CONTEXT_CONTROLS = {
  lyrics: { field: "lyrics", box: "#song-lyrics", restore: "#restore-song-lyrics", previousField: "lyrics_previous", label: "Lyric sheet" },
  caption: { field: "caption", box: "#song-style", restore: "#restore-song-style", previousField: "caption_previous", label: "Style description" },
};

// One lookup, throwing rather than returning undefined — the DOCUMENT_CONTROLS argument exactly:
// a field the server has no slot for must fail loudly here instead of rendering "undefined" into a
// toast or binding a control to nothing.
export function songContextControls(field) {
  const control = SONG_CONTEXT_CONTROLS[field];
  if (!control) throw new Error(`Unknown song context field: ${JSON.stringify(field)}`);
  return control;
}

// Pure: is anything actually recoverable for this field? An always-enabled button offers a restore
// the server refuses with 409, and the client then misreads its own bad offer as stale state.
//
// The code is named here for the reader's sake and nothing branches on it: `request` throws an
// Error carrying the server's sentence and no status at all, so every recovery path below is
// decided by a substring of that sentence. A contract test executes both refusals through the real
// routes to keep this comment honest.
//
// The test is `null`/`undefined`, NOT emptiness, and that is the one place this deliberately does
// not copy `documentRestoreAvailable`. `Song.lyrics_previous` is `str | None`: `null` means no save
// has ever displaced anything, and `""` means a save displaced a blank. A Director who pasted a
// sheet over an empty field has a real previous version — the blank — and wanting it back is an
// ordinary undo. Treating `""` as "nothing kept" would disable the button on exactly that case.
export function songContextRestoreAvailable(song, field) {
  const previous = song?.[songContextControls(field).previousField];
  return typeof previous === "string";
}

// What the restore button says it will do, in the two states it has.
export function songContextRestoreTitle(field, available) {
  const label = songContextControls(field).label;
  return available
    ? `Swap the ${label.toLowerCase()} back to the version kept before the last save that changed it; the text on screen becomes the kept version`
    : `No previous version of the ${label.toLowerCase()} is kept yet; one is kept when a save replaces it`;
}

// The one wording for a song-context restore, mirroring app.py's SONG_CONTEXT_RESTORE_NOTICE so
// the toast the Director reads is the sentence the server would state for the same act.
export const SONG_CONTEXT_RESTORE_NOTICE =
  "{field} was restored to the version kept before the last save that changed it. The text " +
  "that was replaced is now the kept version, so restoring again swaps back. Nothing else " +
  "about the song changed: not the audio, its length or its provenance.";

export function songContextRestoreNotice(field) {
  return SONG_CONTEXT_RESTORE_NOTICE.replace("{field}", songContextControls(field).label);
}

// -------------------------------------------------------------------------------------------
// Who sings the song, and which of them sings each line.
//
// The Director's ask (2026-08-21): "We should be able to select if the song is Instrumental,
// Female sung, Male sung, Duet, 3+, Choir. If selecting Duet or more then each line of the Lyric
// sheet would need to be tagged with Char1/Char2/Char3+/Both/All."
//
// **The per-line dropdown edits the lyric sheet.** There is no parallel structure holding a tag
// per line, because such a structure is wrong the instant a line is inserted or deleted and wrong
// silently. The mark is written into the line as `(S1)` — H3's own speaker notation, which
// `h3_prompt._SPEAKER` already parses — so the tag and the words it belongs to are the same
// characters and cannot drift apart. Saving is the existing "Save song context" button; nothing
// new goes over the wire for a tag.
//
// Every table and every parse below mirrors the server's (`models.VOCAL_TYPE_SPECS`,
// `timeline.lyric_line_tags`, `timeline.tag_lyric_line`) and `tests/test_frontend_contract.py`
// holds the two sides equal against shared fixtures — a client that read `(s1)` where the server
// did not would show the Director a dropdown state their sheet does not carry.
// -------------------------------------------------------------------------------------------

// `models.VOCAL_TYPE_SPECS`, mirrored. `slots` is what the type's marks can name and therefore
// how many characters need slotting; `lineTags` is the per-line dropdown, and an EMPTY array
// means no dropdown is drawn at all rather than an empty select.
//
// Four of the seven are deliberately untagged, and the reasons are the server's: `unstated` has
// declared no cast to choose from, `instrumental` has no sung line, a solo song's every line is
// sung by the one voice the song-level choice already names, and a choir is a mass voice rather
// than a cast. Only a duet and a 3+ ensemble name singers a line can be attributed to.
export const VOCAL_TYPES = [
  { value: "unstated", label: "Not stated", slots: [], lineTags: [] },
  { value: "instrumental", label: "Instrumental", slots: [], lineTags: [] },
  { value: "female", label: "Female sung", slots: [], lineTags: [] },
  { value: "male", label: "Male sung", slots: [], lineTags: [] },
  {
    value: "duet",
    label: "Duet",
    slots: [1, 2],
    lineTags: [
      { slots: [], label: "Untagged" },
      { slots: [1], label: "Char 1" },
      { slots: [2], label: "Char 2" },
      { slots: [1, 2], label: "Both" },
    ],
  },
  {
    value: "ensemble",
    label: "3+ voices",
    slots: [1, 2, 3],
    lineTags: [
      { slots: [], label: "Untagged" },
      { slots: [1], label: "Char 1" },
      { slots: [2], label: "Char 2" },
      { slots: [3], label: "Char 3" },
      { slots: [1, 2, 3], label: "All" },
    ],
  },
  { value: "choir", label: "Choir", slots: [], lineTags: [] },
];

// `models.INSTRUMENTAL_NOTE`, mirrored so the Song workspace says it at the moment the
// declaration is made and populate says the identical sentence at the moment it matters. A
// contract test holds the two equal — a consequence worded two ways is a consequence the Director
// cannot trust, which is the argument SONG_CHANGE_CONSEQUENCE already records.
export const INSTRUMENTAL_NOTE =
  "This song is declared instrumental, so no shot's words come from a singer and no character " +
  "slot is needed. The treatment carries the whole story — environments and instruments have to " +
  "do the work a performance would. Shots are untouched: whether a window is sung is still " +
  "decided by what Whisper measured on the track, never by this declaration.";

// One lookup, falling back to `unstated` rather than throwing — `songContextControls` throws
// because a bad field id would bind a control to the wrong server slot, while a vocal type this
// client does not know is a manifest from a newer build, and drawing it as undeclared is the
// honest reading of "this client cannot offer that cast".
export function vocalTypeSpec(value) {
  return VOCAL_TYPES.find((entry) => entry.value === value) || VOCAL_TYPES[0];
}

// The per-line dropdown for one vocal type. `[]` means draw nothing.
export function lineTagOptions(value) {
  return vocalTypeSpec(value).lineTags;
}

// `models.speaker_notation`, mirrored: `(S1)`, `(S1, S2)`, and "" for no singers named.
export function speakerNotation(slots = []) {
  return slots.length ? `(${slots.map((slot) => `S${slot}`).join(", ")})` : "";
}

// `models.CHARACTER_SLOT_LIMIT`, derived from the table above exactly as the server derives it
// from its own, so a fourth voice added to `ensemble` widens both bounds in one edit.
export const CHARACTER_SLOT_LIMIT = VOCAL_TYPES.reduce(
  (limit, entry) => Math.max(limit, ...entry.slots, 0), 0);

// `timeline._LINE_SPEAKER` and `timeline._LINE_SPEAKER_SUSPECT`, mirrored. The suspect pattern is
// deliberately narrow — `(`, an `S`, a digit — so an ordinary lyric line opening `(she said)` is
// never dragged into it.
const LINE_SPEAKER = /^([ \t]*)\([ \t]*(S\d+(?:[ \t]*,[ \t]*S\d+)*)[ \t]*\)[ \t]*/i;
const LINE_SPEAKER_SUSPECT = /^[ \t]*\([ \t]*S[ \t]*\d/i;
// Lines AND the separators between them, so a rejoin is byte-exact and a sheet pasted from
// Windows is not silently normalised to `\n` by a tag edit. `timeline._LINE_SPLIT`.
const LINE_SPLIT = /(\r\n|\r|\n)/;
// `timeline._SHEET_TAG` applied to one line: `[Verse]`, `[Chorus 2]`.
const SHEET_TAG_LINE = /^[ \t]*\[[^\]\r\n]+\][ \t]*$/;

// One line's mark as slot numbers, or `null` when the head of the line was meant to be a mark and
// could not be read. `[]` and `null` are different answers: `[]` is "carries no mark", `null` is
// "carries something unreadable", and only the second is worth telling the Director about.
function parseLineSlots(line) {
  const match = LINE_SPEAKER.exec(line);
  if (!match) return { slots: LINE_SPEAKER_SUSPECT.test(line) ? null : [], rest: line };
  const numbers = (match[2].match(/\d+/g) || []).map(Number);
  // The same two re-validations the server makes: a slot past the bound names a character no
  // dropdown offers and no asset may hold, and a repeat names one singer twice.
  if (numbers.some((number) => number < 1 || number > CHARACTER_SLOT_LIMIT)) return { slots: null, rest: line };
  if (new Set(numbers).size !== numbers.length) return { slots: null, rest: line };
  return { slots: numbers, rest: line.slice(match[0].length) };
}

// Every line of the sheet, in order, with what its mark says. `timeline.lyric_line_tags`.
//
// Blanks and `[Tag]` headers come back too, with `taggable: false`, because `index` is an index
// into the sheet's own text and the writer uses it to touch exactly one line — a list numbering
// only the sung lines would hand the writer a number meaning a different line.
export function lyricLineTags(lyrics) {
  const parts = String(lyrics ?? "").split(LINE_SPLIT);
  const lines = [];
  for (let index = 0; index * 2 < parts.length; index += 1) {
    const raw = parts[index * 2];
    const parsed = parseLineSlots(raw);
    lines.push({
      index,
      raw,
      text: parsed.rest,
      slots: parsed.slots || [],
      taggable: Boolean(raw.trim()) && !SHEET_TAG_LINE.test(raw),
      unreadable: parsed.slots === null,
    });
  }
  return lines;
}

// Set or clear one line's mark, and change NOTHING else in the sheet. `timeline.tag_lyric_line`.
//
// Every other line comes back byte for byte, separators and indentation included: only
// `parts[2 * index]` is ever replaced. Nothing here re-wraps, re-indents, collapses blank lines or
// touches a `[Tag]` block — the sheet is the Director's own text and a tag edit is not a licence
// to reformat it.
//
// Throws rather than guessing, on the server's own three refusals, and the sheet is untouched by
// a throw: an out-of-range line, a line that is not sung, and a line whose existing mark is
// unreadable. Overwriting that last one would silently repair a typo the Director cannot then see
// they made.
export function tagLyricLine(lyrics, index, slots = []) {
  const parts = String(lyrics ?? "").split(LINE_SPLIT);
  const position = 2 * index;
  if (!(index >= 0) || position >= parts.length) throw new Error(`The lyric sheet has no line ${index}.`);
  const line = parts[position];
  const parsed = parseLineSlots(line);
  if (parsed.slots === null) {
    throw new Error(
      `Line ${index + 1} starts with something that looks like a singer mark but could not be read: ` +
      `"${line.trim()}". Fix the line in the lyric sheet and the dropdown will follow it.`);
  }
  if (!line.trim() || SHEET_TAG_LINE.test(line)) {
    throw new Error(`Line ${index + 1} is not a sung line, so it carries no singer.`);
  }
  // Round-trip closure, the server's own check: whatever this writes, `lyricLineTags` must read
  // back — otherwise a caller could store a mark this module's own reader then reports as
  // unreadable, and the Director would be told their sheet is broken by an edit they never made.
  if (slots.some((slot) => slot < 1 || slot > CHARACTER_SLOT_LIMIT)) {
    throw new Error(`A singer slot is a number from 1 to ${CHARACTER_SLOT_LIMIT}, and ${JSON.stringify(slots)} is not.`);
  }
  if (new Set(slots).size !== slots.length) {
    throw new Error(`${JSON.stringify(slots)} names one singer twice, which is not a sung line.`);
  }
  const marked = LINE_SPEAKER.exec(line);
  const indent = marked ? marked[1] : (/^[ \t]*/.exec(line) || [""])[0];
  const body = parsed.rest.replace(/^[ \t]+/, "");
  const notation = speakerNotation(slots);
  parts[position] = notation ? `${indent}${notation} ${body}` : `${indent}${body}`;
  return parts.join("");
}

// Everything the Song workspace needs to draw the vocal-type control and, when the declared type
// asks for one, the per-line tagging list beneath it.
//
// `tagging` is what decides whether the list exists at all, and it is the vocal type's own table
// row: a solo song offers no per-line dropdown, so there is nothing to draw and no row to read.
// `unreadable` is surfaced separately from the rows because it is the one thing the Director has
// to fix in the sheet by hand — a count of zero draws no warning at all.
export function vocalTaggingPlan(project) {
  const song = project?.song || null;
  const spec = vocalTypeSpec(song?.vocal_type);
  if (!song) return { editable: false, value: "unstated", spec, tagging: false, rows: [], unreadable: [] };
  const tagging = spec.lineTags.length > 0;
  const lines = tagging ? lyricLineTags(song.lyrics || "") : [];
  return {
    editable: true,
    value: spec.value,
    spec,
    tagging,
    rows: lines.filter((line) => line.taggable && !line.unreadable),
    unreadable: lines.filter((line) => line.unreadable),
  };
}

// Which slot each character asset holds, and which slots the declared type still needs.
// `models.character_slot_assets` and `models.vocal_cast_problems`, mirrored for the Assets tab so
// the Director can see the shortfall before Populate tells them about it.
export function characterSlotPlan(project, asset) {
  if (!asset || asset.kind !== "character") return null;
  const held = new Map();
  for (const item of project?.assets || []) {
    if (item.kind === "character" && item.character_slot && !held.has(item.character_slot)) held.set(item.character_slot, item);
  }
  const spec = vocalTypeSpec(project?.song?.vocal_type);
  return {
    slot: asset.character_slot || 0,
    // Zero is always offered — it is how a character says "not one of the singers" — and then
    // every slot the declared type can name. A type that names none offers the full bound, so a
    // Director can slot the cast before declaring it and in either order.
    options: [0, ...(spec.slots.length ? spec.slots : Array.from({ length: CHARACTER_SLOT_LIMIT }, (_, index) => index + 1))],
    // Named so the select can shut the slots another asset already holds rather than offering a
    // choice the route refuses by name.
    taken: Object.fromEntries([...held].filter(([, item]) => item.id !== asset.id).map(([slot, item]) => [slot, item.name])),
  };
}

// True when a rejection is the song-context restore route refusing because no version was kept.
// The buttons are disabled when the loaded project has no slot, so a refusal means this client is
// looking at stale state — the same recovery shape as DOCUMENT_RESTORE_REFUSAL_MARKER, and keyed
// on a phrase that appears in the server's song refusal and in no other refusal it sends.
export const SONG_CONTEXT_RESTORE_REFUSAL_MARKER = "was kept for this song";

export function songContextRestoreRefusal(message) {
  return typeof message === "string" && message.includes(SONG_CONTEXT_RESTORE_REFUSAL_MARKER);
}

// True when a rejection is the Song gate refusing an unacknowledged change, which the
// client can recover from by refreshing and asking again -- as opposed to any other
// error, where a refresh would tell the Director nothing new. Keyed on the server's
// own instruction sentence so the two cannot drift apart silently.
export const SONG_REFUSAL_MARKER = "confirm_song_replacement=true";

export function songRefusalMessage(message) {
  return typeof message === "string" && message.includes(SONG_REFUSAL_MARKER);
}

// True when a rejection is the one `align-lyrics` refusal that **saved something first**.
//
// `align_song_lyrics` writes `lyric_words` and `vocal_spans` onto the Song and only then refuses,
// when no `[Tag]` block could be timed against what it heard: the transcription is on disk and the
// reply is a 422. Every other refusal that route sends -- no song, no `[Tag]` blocks in the sheet,
// section boxes already placed, the transcription itself failing -- wrote nothing, so this is the
// one that a client must take up rather than only report.
//
// **Why the client has to care.** `snapTargetsIdentity` counts `lyric_words` and `vocal_spans`,
// so a transcription left unadopted leaves the key unmoved and the phrase-gap targets that
// transcription just created unreachable until a page reload -- the "silently late by one reload"
// the success path's own comment says it exists to close.
//
// Keyed on a phrase from `app.ALIGN_LYRICS_NOTHING_PLACED` that appears in no other sentence that
// route sends, exactly as the two markers above are, and a contract test holds the two together.
export const ALIGN_LYRICS_KEPT_MARKER = "The transcribed words were kept";

export function alignLyricsKeptTranscription(message) {
  return typeof message === "string" && message.includes(ALIGN_LYRICS_KEPT_MARKER);
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

//: The kinds a *blocking* note carries. `batch.NOTE_KIND_PROMPT` and `NOTE_KIND_STALE_MAP`, pinned
//: by a contract test. Read for two things and nothing else: which heading the readiness list gives
//: the line, and which noun the batch confirmation uses for a shot that will be skipped. Neither
//: re-derives anything -- the server decides both blocks, and a stale map in particular cannot be
//: decided here at all, since it is a comparison against the reference map the *submission* would
//: build from the shot's citations.
export const NOTE_KIND_PROMPT = "prompt";
export const NOTE_KIND_STALE_MAP = "stale_map";

//: What the batch confirmation calls each block, in the few words a parenthesis holds. The server's
//: whole sentence for each is in the readiness list; this is the heads-up on the button.
export const BATCH_SKIP_NOUNS = {
  [NOTE_KIND_PROMPT]: "no prompt",
  [NOTE_KIND_STALE_MAP]: "stale reference map",
};
//: For a kind this client does not know -- an older client against a newer server. "Blocked" is the
//: one thing that is certainly true of any note in `blocking`, and it is better than naming the
//: wrong reason or silently dropping the count.
export const BATCH_SKIP_NOUN_UNKNOWN = "blocked";

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

// ------------------------------------------------------------------------------------------
// Making one Shot from another: Duplicate, and the second half of a Split.
//
// Both handlers `structuredClone`d the source Shot and reset `status` alone, so the new Shot
// arrived owning the original's take -- it played that take in the Monitor, offered it in the
// takes strip, and read as approved wherever the original did. Nothing had rendered it. The
// copy also read as `shot_render_provenance` on the server, so the automated writers refused
// to touch it, citing a render nobody ran.
//
// So a new Shot is *built from the plan*, never subtracted from a clone: a field nobody
// classified is then absent from the copy rather than silently inherited by it. The
// classification itself is `models.SHOT_PLAN_CONTENT_FIELDS` and its two companions -- one
// place, partitioned against `Shot.model_fields` and pinned to this list by a contract test, so
// a field added to the model and classified by nobody fails the suite.
// ------------------------------------------------------------------------------------------

// Mirrors `models.SHOT_PLAN_CONTENT_FIELDS`. Every other Shot field is take provenance (a
// render, a file, an approval, a review, a slice of one take) or the Director's hands-off on
// one Shot -- see the model for why each lands where it does.
export const SHOT_PLAN_CONTENT_FIELDS = [
  "start",
  "duration",
  "prompt",
  "h3_prompt",
  // `Shot.h3_prompt_map`: the reference map that expansion was written against. It travels with
  // `h3_prompt` because a copy carrying the prompt without the map would claim an expansion
  // nothing can check for staleness -- see the model's own note on the field.
  "h3_prompt_map",
  "mode",
  "asset_ids",
  "citations",
  "reference_labels",
  "singing",
  "use_song_audio",
  "seed",
];

// What a Shot nobody has rendered reads as. `Shot.status`'s own default, said out loud because
// the copy carries it explicitly: the inspector draws the chip from this field, and an absent
// one would render the word "undefined" over a perfectly ordinary draft.
export const NEW_SHOT_STATUS = "draft";

export function newShotFromPlan(shot, overrides = {}) {
  const copy = { status: NEW_SHOT_STATUS };
  for (const field of SHOT_PLAN_CONTENT_FIELDS) {
    if (shot?.[field] !== undefined) copy[field] = structuredClone(shot[field]);
  }
  return { ...copy, ...overrides };
}

// ------------------------------------------------------------------------------------------
// A render is in flight for this shot, said in words on every surface that shows its take.
//
// The inspector was the only place that said so: the status chip and the disabled Approve
// button's APPROVE_IN_FLIGHT. The Monitor played the previous take framed exactly like a
// settled one, the takes strip labelled that take `Current`, and the clip carried the state
// as a border hue alone. Two of those are affirmative wrong claims and the third is state by
// appearance, which this stylesheet's own rule forbids.
//
// The fix is to tell the truth about the state, never to change it: `latest_output` still
// points at the previous take, it still plays, it is still what the finishing stages consume.
// Nothing here clears a pointer -- a re-render that silently moved which take a downstream
// stage read is a recorded provenance incident, and blanking the pointer would also take away
// the very take the Director needs to judge whether the re-render was worth it.
// ------------------------------------------------------------------------------------------

// The word the clip carries. Upper case and terse, like NO PROMPT and PLACEHOLDER, because it
// sits on the same small surface and is read at a glance rather than in a sentence.
export const SHOT_RENDERING_FLAG = "RENDERING";
// The same fact about a shot with nothing to displace: a first render, so no take is being
// replaced and the displacement sentence would name a harm that is not happening.
export const RENDER_IN_FLIGHT_NO_TAKE = "A render for this shot has not finished.";

export function shotRenderInFlight(shot) {
  return RENDER_IN_FLIGHT_SHOT_STATUSES.includes(String(shot?.status || ""));
}

// ------------------------------------------------------------------------------------------
// How far that render has got -- the Director's ask (2026-08-20): "I am not so concerned with
// time to generate as long as we can get better information to the app so i can know about what
// % done a generation is whether displayed on the asset card or timeline Shot box."
//
// The number arrives on the render-status poll (`report.progress`), sourced from ComfyUI's own
// WebSocket and held only in the server's memory -- see `comfy.ProgressTracker` for why it is
// never persisted. These three functions are the whole of the client's decision about it, kept
// pure and executed by the contract tests, because the one thing that must not happen here is a
// *fabricated* percentage: a made-up number on a stuck render is worse than none, and this
// application refuses invented values on principle.
//
// **Unknown is not zero.** No row for a job means nobody has said anything -- the socket never
// connected, the prompt is still waiting its turn, the build speaks a dialect the server does
// not read -- and every surface then shows exactly what it showed before this feature existed.
// A row saying `0` is the different, real statement that the render started and no step is done.
// ------------------------------------------------------------------------------------------

// `null` for unknown; a clamped whole-number `"42%"` otherwise. Everything that is not a finite
// number is unknown, so a `null`, an absent key, or a string that arrived where a number belongs
// all degrade to showing nothing rather than to `NaN%`.
export function renderProgressLabel(percent) {
  const number = Number(percent);
  if (percent === null || percent === undefined || percent === "" || !Number.isFinite(number)) {
    return "";
  }
  return `${Math.max(0, Math.min(100, Math.round(number)))}%`;
}

// The word the surfaces carry, composed with the percentage rather than replaced by it. RENDERING
// is the signal that a render is in flight and it survives with or without a number beside it --
// which is exactly what "degrade to what is shown today" means when the socket is down.
export function renderingFlag(percent) {
  const label = renderProgressLabel(percent);
  return label ? `${SHOT_RENDERING_FLAG} ${label}` : SHOT_RENDERING_FLAG;
}

// The same fact as a sentence, for the accessible name -- the only one of a clip's signals a
// screen reader announces. Empty when unknown, so the label is byte-identical to today's.
export function renderProgressNote(percent) {
  const label = renderProgressLabel(percent);
  return label ? `${label} of this render is done.` : "";
}

// The poll report -> `{ targetId: percent }`, which is what the asset grid and the timeline draw
// from. Pure over the report alone: it joins `report.progress` (keyed by job id) to `report.jobs`
// (which carry `target_id`), so nothing depends on the order the caller patches things in.
//
// Attribution is per job and therefore per prompt -- a batch of H3 renders is the normal case,
// and two shots rendering at once must never read each other's number. Where two open jobs somehow
// name one target, the higher percentage wins: both describe work on the same box, and the lower
// one is the older, more finished-ago claim.
export function renderProgressByTarget(report) {
  const jobs = new Map((report?.jobs || []).map((job) => [job.id, job]));
  const byTarget = {};
  for (const entry of report?.progress || []) {
    const job = jobs.get(entry?.job_id);
    if (!job?.target_id) continue;
    const percent = Number(entry?.percent);
    if (!Number.isFinite(percent)) continue;
    const clamped = Math.max(0, Math.min(100, Math.round(percent)));
    const held = byTarget[job.target_id];
    if (held === undefined || clamped > held) byTarget[job.target_id] = clamped;
  }
  return byTarget;
}

// One shot's render state as the surfaces draw it: whether a render is in flight, the word for
// it, and the sentence that says what that means for what is on screen. `displaced` is the case
// the wrong claims lived in -- there is a take, and it is not this shot's answer any more.
export function shotRenderState(shot) {
  if (!shotRenderInFlight(shot)) {
    return { inFlight: false, displaced: false, flag: "", note: "" };
  }
  const displaced = Boolean(shot?.latest_output);
  return {
    inFlight: true,
    displaced,
    flag: SHOT_RENDERING_FLAG,
    note: displaced ? TAKE_DISPLACED_BY_RENDER : RENDER_IN_FLIGHT_NO_TAKE,
  };
}

// ------------------------------------------------------------------------------------------
// Which shot is rendering *now* -- the Director's ask (2026-08-23): "Would be nice to have a more
// active indicator marking the currently generating clip on the timeline though."
//
// The gap is real and narrow. `shotRenderInFlight` reads `Shot.status`, and **nothing in this
// application ever writes `running` onto a Shot**: `generate_h3` sets `queued`, and the reconciler
// writes `running` onto the *job* rather than onto its target. So a batch of twenty-six draws
// twenty-six identical `RENDERING` clips, one of which is on the GPU and twenty-five of which are
// waiting -- and the queue panel knows the difference (it renders `job.status`) while the timeline,
// which is what the Director actually watches, does not.
//
// **No new polling, and no new request.** The phase is read off the render-status report
// `pollRenderStatus` already fetches every two seconds while, and only while, a render is open.
// This is `renderProgressByTarget` one field over -- the same report, the same job-to-target join,
// the same `{ targetId: … }` map held beside the project rather than patched into it, for that
// function's recorded reason: a value folded into `project.jobs` is written into the manifest by
// the Director's next save.
//
// **Cheap by construction.** One pass over the report's jobs per tick builds the map; the timeline
// then does one keyed lookup per clip, which is what it already does for the percentage and for
// `windowWarningsByShot`. Nothing here walks every clip, and nothing runs per frame.
// ------------------------------------------------------------------------------------------

//: The two phases a clip can wear, as the job records spell them -- adopted verbatim rather than
//: renamed, so the timeline and the queue panel's status column cannot come to disagree about
//: which shot is on the GPU.
export const RENDER_PHASE_RUNNING = "running";
export const RENDER_PHASE_QUEUED = "queued";

//: The report -> `{ shotId: phase }`. Pure over the report alone, and only ever the two phases
//: above: a settled job is not a phase, and a job whose kind names no shot has no clip to mark.
//:
//: `running` wins wherever two open jobs name one shot. Both describe work on the same box, and
//: the one ComfyUI has actually started is the one the Director is looking for -- the same
//: "stronger claim wins" rule `renderProgressByTarget` applies to two percentages.
export function renderPhaseByShot(report) {
  const found = {};
  for (const job of report?.jobs || []) {
    if (!JOB_KINDS_TARGETING_A_SHOT.includes(job?.kind) || !job?.target_id) continue;
    if (job.status === RENDER_PHASE_RUNNING) found[job.target_id] = RENDER_PHASE_RUNNING;
    else if (job.status === RENDER_PHASE_QUEUED && !found[job.target_id]) {
      found[job.target_id] = RENDER_PHASE_QUEUED;
    }
  }
  return found;
}

//: The class the clip carries for the shot ComfyUI is rendering right now, and the one for a shot
//: waiting its turn behind it. Two classes rather than one plus a modifier, because the class names
//: the state -- `window-long` and `take-uncovered` share an amber and are still two classes for
//: exactly that reason.
export const CLIP_RENDERING_NOW_CLASS = "rendering-now";
export const CLIP_RENDERING_QUEUED_CLASS = "rendering-queued";

//: The word a waiting clip carries in place of `RENDERING`. Upper case and terse like
//: `SHOT_RENDERING_FLAG`, `NO PROMPT` and `PLACEHOLDER`, because it sits on the same small surface.
//: It is a *different word* and not a dimmer copy of the same one: colour is never the only signal
//: this stylesheet draws a state with, and "queued behind another render" and "on the GPU" are the
//: two states this whole feature exists to tell apart.
export const CLIP_RENDERING_QUEUED_FLAG = "QUEUED";

//: And the sentences, for the accessible name -- the only one of a clip's signals a screen reader
//: announces. Short, because the clip's title is not the queue panel; the panel carries the job row
//: with its own status, seed and timing.
export const CLIP_RENDERING_NOW_NOTE = "ComfyUI is rendering this shot now.";
export const CLIP_RENDERING_QUEUED_NOTE =
  "This shot is waiting its turn in ComfyUI's queue; nothing is being rendered for it yet.";

//: What the clip draws for its render phase: `clipWindowState`'s shape, and its rule that a state
//: with a class always comes with a sentence. `percent` is this shot's live percentage or nothing,
//: and it rides the running phase only -- a queued shot has no progress to report and a number
//: beside `QUEUED` would be one invented for it.
//:
//: **An unknown phase draws exactly what this file drew before the feature existed.** A report that
//: has not been fetched, a job whose kind names no shot, a build whose queue answer the server
//: could not read: all of them return an empty flag, and the timeline falls back to `RENDERING`
//: with no new class -- which is the same degrade-to-today rule `renderProgressLabel` follows for
//: an unknown percentage, and what makes a shot that is neither running nor queued draw nothing.
export function clipRenderPhase(phase, percent) {
  if (phase === RENDER_PHASE_RUNNING) {
    return {
      className: CLIP_RENDERING_NOW_CLASS,
      flag: renderingFlag(percent),
      note: CLIP_RENDERING_NOW_NOTE,
    };
  }
  if (phase === RENDER_PHASE_QUEUED) {
    return {
      className: CLIP_RENDERING_QUEUED_CLASS,
      flag: CLIP_RENDERING_QUEUED_FLAG,
      note: CLIP_RENDERING_QUEUED_NOTE,
    };
  }
  return { className: "", flag: "", note: "" };
}

// Everything the timeline draws for one clip's prompt cell, decided here rather than in the
// template. The template used to hold the ternaries, and swapping their arms -- stamping NO PROMPT
// on every *written* clip and rendering the unprompted one empty -- kept every substring the suite
// asserted, so the one signal that costs a wasted GPU pass could be rendered exactly backwards
// with the tests green. Executed by tests/test_frontend_contract.py for every state.
//
// `label` is the clip's title and accessible name: the help for a blocked shot, and the full
// prompt otherwise, since the cell itself is clamped to two lines -- with this shot's render
// state appended when a render is in flight. The accessible name is the only one of the clip's
// signals a screen reader announces, so a state carried by the border hue and the RENDERING word
// alone would not exist at all for a Director reading it that way. A settled shot's label is
// unchanged, byte for byte: `shotRenderState` returns an empty note and the join drops it.
// `percent` is this shot's live render percentage or nothing at all. It appends one more sentence
// to the accessible name and changes not a byte otherwise -- an unknown percentage, which is what
// every caller passes when the progress socket is down, leaves the label exactly as it was.
export function shotPromptCell(shot, percent) {
  const state = shotRenderState(shot);
  const progress = state.inFlight ? renderProgressNote(percent) : "";
  const withState = (label) => [label, state.note, progress].filter(Boolean).join(" ");
  const prompt = String(shot?.prompt ?? "");
  const rejection = promptRejection(shot);
  if (!rejection) return { blocked: false, text: prompt, className: "", label: withState(prompt) };
  return {
    blocked: true,
    text: rejection === SHOT_WITH_PLACEHOLDER_PROMPT ? SHOT_WITH_PLACEHOLDER_FLAG : SHOT_WITHOUT_PROMPT_FLAG,
    className: "no-prompt",
    label: withState(shotPromptHelp(shot)),
  };
}

// `batchReadinessBlock` stood here until 2026-08-21 and is deliberately gone rather than fixed.
//
// It decided whether a batch could be submitted, given the server's report and the ids being
// queued. **Nothing called it.** Client-side batch refusal was removed when FR-4 made the batch
// skip a blocked shot by name and submit the rest -- which is what the comment below has said ever
// since -- and the function outlived its only caller, exported and reachable from the executed
// contract alone. Its `message` was built by `readinessRefusal`, which words every block as "no
// prompt on X"; `readiness_report` has since gained other blocking kinds, so that sentence was
// wrong as well as unreachable. The Director's ruling: *"Ok lets not leave the wrong sentence
// laying around."* Fixing the sentence would have left dead code behind a correct-looking message,
// which is worse than either problem alone.
//
// Nothing is lost by the deletion: `blockedShotIds` and `blockedShotLabels` still read the same
// report and are what `generateAllPlan` uses to warn before the click.

// Why the batch button is off when it is off. Nothing to queue is the only remaining
// reason: a blocked shot no longer disables the whole batch, because the server-side
// batch (FR-4) skips it by name and submits the rest — the pre-click warning survives as
// a warning in the title and the confirm, never as a refusal the route would not make.
export const QUEUE_WITHOUT_READY_SHOTS = "Mark a shot ready to queue H3";
export const QUEUE_REPLACE_WITHOUT_TARGETS =
  "Mark a shot ready — or render something for Replace existing to re-render";

// -- The sampling bundle: one choice, and both render paths obey it ---------------------------
//
// The Director's ruling of 2026-08-23, on the 8-step-vs-20-step comparison: turbo is "almost
// sweaty" but "both still look good so **up to user**, and perhaps the video style would benefit
// from it in some cases". Neither bundle is correct, so neither may be a silent default -- which
// is exactly what both of them were. `api.generateBatch` sent no profile and got 20 steps;
// "Render Again" hardcoded `turbo` and got 4. The same project rendered two different graphs
// depending on which button was pressed, and nothing on screen named either number.
//
// **The choice is stored on the project, not on the machine.** That is the opposite of the VRAM
// eject beside it, and deliberately: the eject is a property of the card, so a shared project
// carrying one would change how someone else's renders behave. A bundle is a property of the
// *look* -- the Director's own word was "style" -- so two videos on one workstation must be able
// to disagree, and the choice must travel with the project directory to another machine. It is
// written by `PUT /projects/{id}/sampling-profile` and by nothing else; the generic full-project
// save re-adopts the stored value rather than trusting a body.
//
// Nothing here submits anything. The select writes the setting; the setting reaches submissions
// server-side, where the profile is resolved -- so no failure on this path can reach a render,
// and the worst case is a control that did not change.
export const SAMPLING_PROFILE_CONTROL = "#sampling-profile";
export const SAMPLING_PROFILE_NOTE = "#sampling-profile-note";
export const SAMPLING_PROFILE_LABEL = "Bundle";

//: The three evidenced bundles, in the order they are offered, each carrying the number that
//: makes the choice mean something. The step count is in the option's own text rather than in a
//: tooltip: "8 steps" is the information the ruling turns on, and a Director choosing a look must
//: not have to hover to find out what it costs.
//:
//: `minutes` is a *measured* per-shot figure or `null`, never an estimate. Both numbers come from
//: the one clean back-to-back pair in the 2026-08-23 comparison -- the 158-frame b-roll, 479.1 s
//: against 236.6 s of ComfyUI execution -- so they are the same shot at the same length on the
//: same card, which is the only comparison either number is good for. The 4-step `turbo` bundle
//: was not in that comparison at all, so it carries `null` and this workspace quotes no figure
//: for it rather than interpolating one from a step ratio.
//:
//: The values are the server's `SamplingProfile`, and `tests/test_frontend_contract.py` asserts
//: this list and that `Literal` hold the same three names, so a select cannot offer a bundle the
//: route would 422.
export const SAMPLING_PROFILES = [
  { value: "default", label: "Default — 20 steps", steps: 20, minutes: 8 },
  { value: "turbo-references2v", label: "Turbo References2V — 8 steps", steps: 8, minutes: 4 },
  { value: "turbo", label: "Turbo LTX LoRA — 4 steps", steps: 4, minutes: null },
];

//: What the comparison found, in the Director's own terms, under the control that acts on it.
//: Four sentences and no essay -- the full evidence is the report, and this is what someone
//: choosing needs to know without opening it. The last clause is the one that stops a wrong
//: belief rather than reporting a result: nothing here is evidence about hands.
export const SAMPLING_PROFILE_NOTE_TEXT =
  "Measured 2026-08-23, 8 steps against 20, three shots at one seed: lip-sync is "
  + "indistinguishable (envelope correlation within 0.005, lag within 0.1 ms) and turbo is "
  + "sharper rather than softer (high-frequency energy +52% to +152%), at the cost of waxier "
  + "skin. The saving is ~2.0×, and it comes from the step count, not from per-step speed. "
  + "No hands were in frame in any of the six renders, so hands are untested.";

//: The hover text: the scope, which is a real limit and not a caveat. A bundle names a
//: *reference*-graph configuration; the first/last keyframe and text-only graphs load different
//: checkpoints and have no evidenced bundle, so they go on rendering at 20 steps whatever is
//: chosen here. Said out loud because the alternative -- a Director picking turbo and quietly
//: getting 20 steps on some shots -- is the silent disagreement this control exists to end.
export const SAMPLING_PROFILE_TITLE =
  "Which evidenced H3 bundle this project renders on. It governs Generate All, Re-queue flagged "
  + "and Render Again alike — one setting, every path. Reference shots only: first/last keyframe "
  + "and text-only shots have no evidenced bundle and always render at 20 steps. Existing takes "
  + "are not touched; a bundle change re-rolls the next take rather than re-rendering this one.";

//: The stored choice, re-validated on every read. A manifest carrying a name this build does not
//: know draws as `default` rather than as a blank select or an invented option — the same rule
//: `default_setting_asset` applies server-side, and for the same reason: stale storage may cost a
//: default, never a wrong state on screen.
export function samplingProfileOf(project) {
  const stored = project?.sampling_profile;
  return SAMPLING_PROFILES.some((entry) => entry.value === stored) ? stored : "default";
}

//: One bundle's row, or the default's — never `undefined`, so every caller can read `.label`.
export function samplingProfileSpec(name) {
  return SAMPLING_PROFILES.find((entry) => entry.value === name) || SAMPLING_PROFILES[0];
}

//: What just changed, said in renders rather than in a select. Names the step count again,
//: because the toast is the confirmation that the click did what the label promised.
export function samplingProfileToast(name) {
  const spec = samplingProfileSpec(name);
  return `Reference shots will render on ${spec.label.replace(" — ", ", ")}. `
    + "Generate All, Re-queue flagged and Render Again all use it.";
}

//: The batch-progress estimate, and the reason it is a function rather than a constant. The queue
//: line read "~2.7 min on turbo" for every batch — a hardcoded figure attributing every render to
//: a bundle the batch had *never* used, since `generateBatch` sent no profile and got 20 steps.
//: It was not merely stale; it named the wrong graph. Now it names the bundle actually chosen and
//: quotes a figure only where one was measured.
export function batchEtaNote(profile, remaining) {
  const spec = samplingProfileSpec(profile);
  if (!spec.minutes || !remaining) return "";
  return ` (~${Math.round(remaining * spec.minutes)} min on ${spec.label.split(" — ")[0]})`;
}

// Generate All's whole decision (spec-generate-all): the count the confirmation names,
// the settled shots Replace Existing would re-open (approved and locked excluded — the
// server names those in its report), and the readiness advisory as a heads-up rather
// than a gate. Executed by the contract tests for every state.
export function generateAllPlan(project, report = null, replaceExisting = false) {
  const shots = (project?.shots || []).filter(Boolean);
  const ready = shots.filter((shot) => shot.status === "ready");
  const replace = replaceExisting
    ? shots.filter(
        (shot) =>
          ["complete", "error"].includes(shot.status)
          && !shot.locked
          && !shot.approved_output,
      )
    : [];
  const targets = [...ready, ...replace];
  if (!targets.length) {
    return {
      disabled: true, count: 0, blocked: [],
      title: replaceExisting ? QUEUE_REPLACE_WITHOUT_TARGETS : QUEUE_WITHOUT_READY_SHOTS,
      confirm: "",
    };
  }
  // Why each blocked shot will be skipped, from the note that blocked it. This said "(no prompt)"
  // outright while an empty prompt was the only block; a stale reference map is a second one
  // (2026-08-21), and it is on shots that very much do have a prompt -- so the reason is read off
  // the report rather than assumed. The nouns are deduplicated and joined, because a batch can
  // carry both and "3 will be skipped (no prompt)" over two stale shots is a sentence that sends a
  // Director to the wrong box.
  const blockedKinds = new Map();
  for (const note of report?.blocking || []) {
    for (const shotId of note?.shot_ids || []) blockedKinds.set(shotId, note?.kind);
  }
  const blocked = targets.filter((shot) => blockedKinds.has(shot.id)).map((shot) => shot.id);
  const noun = (n) => `${n} H3 shot${n === 1 ? "" : "s"}`;
  const reasons = [...new Set(blocked.map((id) => BATCH_SKIP_NOUNS[blockedKinds.get(id)]).filter(Boolean))];
  const skipNote = blocked.length
    ? ` — ${blocked.length} will be skipped (${reasons.join(", ") || BATCH_SKIP_NOUN_UNKNOWN})`
    : "";
  // The bundle the batch will actually render on, named in the confirmation — because this is the
  // last moment before hours of GPU are spent, and until 2026-08-23 nothing anywhere told the
  // Director whether they were about to spend 20 steps a shot or 4. The default's own cost
  // sentence is kept word for word, so a Director who has touched nothing reads exactly what they
  // read before; a chosen bundle adds its own measured figure beside it and never replaces it
  // with an interpolation.
  return {
    disabled: false,
    count: targets.length,
    blocked,
    // The button's own title stays about *what would queue* and says nothing about the bundle:
    // the select sits beside it carrying the bundle's name and step count, and a tooltip
    // repeating it would be a second copy to keep true. The confirmation is where the bundle
    // belongs, because that is the click that spends the GPU.
    title: `Generate ${noun(targets.length)}${skipNote}`,
    confirm:
      `Queue ${noun(targets.length)} as one batch?${skipNote ? `${skipNote}.` : ""}`
      + `${bundleSpend(project)} `
      + "One confirmation covers the batch.",
  };
}

//: What this batch will spend, in the sentence every batch confirmation ends with. One copy,
//: because two batch buttons now show it and a bundle named correctly in one dialog and stale in
//: the other is exactly the drift `batchEtaNote` was built to end. The default's own cost sentence
//: is kept word for word, so a Director who has touched nothing reads exactly what they read
//: before; a chosen bundle adds its own measured figure beside it and never replaces it with an
//: interpolation.
export function bundleSpend(project) {
  const bundle = samplingProfileSpec(samplingProfileOf(project));
  const costNote = bundle.value === "default"
    ? " A reference shot measured 288-438 s on the default profile."
    : bundle.minutes
      ? ` Measured ~${bundle.minutes} min a shot, about 2.0× faster than default (2026-08-23).`
      : " Per-shot cost on this bundle is not measured.";
  return ` Bundle: ${bundle.label}.${costNote}`;
}

//: "Generate All Empty" -- the Director's ask of 2026-08-23, beside Expand All Prompts on the cuts
//: bar: "which would generate all shots that dont already have a video". The gesture it replaces
//: is `Mark all drafts ready` then `Generate All`, two clicks in a panel the plan is not in.
export const GENERATE_EMPTY_CONTROL = "#timeline-generate-empty";
export const GENERATE_EMPTY_LABEL = "Generate All Empty";
export const GENERATE_EMPTY_RUNNING = "Queueing…";
//: Said out loud rather than left to a dead button. Three shot controls were silently inert until
//: 2026-08-22 and this is the same failure waiting to happen: a plan whose shots all have takes is
//: the *success* state, and a button that answers it with nothing reads as broken.
export const GENERATE_EMPTY_NONE =
  "Every shot already has a video. Nothing to generate — a locked or approved shot without one is "
  + "left alone too, and the queue panel's Generate All still takes the ready set.";

//: The empty scope, decided client-side in the same terms the server decides it (`batch_targets`,
//: scope `empty`): no take, not already rendering, and neither locked nor approved -- the two
//: protections the server *names* in its report and this count therefore leaves out, exactly as
//: `generateAllPlan`'s Replace Existing set does.
//:
//: `latest_output` and nothing else is the test for "has a video", matching `shot_has_take`. The
//: Director's own live plan is the case that makes it matter: thirty shots all reading `draft`,
//: three of them already holding a finished take. A status test would re-render those three.
export function emptyScopeShots(project) {
  return (project?.shots || []).filter(Boolean).filter(
    (shot) =>
      !shot.latest_output
      && !["queued", "running"].includes(shot.status)
      && !shot.locked
      && !shot.approved_output
      && shot.status !== "approved",
  );
}

//: Generate All Empty's whole decision: the count it will submit, how many of those are drafts it
//: will commit on the way, and the bundle it will spend. Executed by the contract tests for every
//: state, never re-derived in the template or in the click handler.
export function generateEmptyPlan(project) {
  const targets = emptyScopeShots(project);
  const drafts = targets.filter((shot) => shot.status === "draft");
  if (!targets.length) {
    return { count: 0, drafts: 0, title: GENERATE_EMPTY_NONE, confirm: "", empty: GENERATE_EMPTY_NONE };
  }
  const noun = (n) => `${n} shot${n === 1 ? "" : "s"}`;
  // The arming is named before the click, not discovered after it. Committing a draft is a
  // decision the Director otherwise makes per shot (or in one pass with `Mark all drafts ready`),
  // so a batch that does it on their behalf has to say so in the sentence they confirm.
  const draftNote = drafts.length
    ? ` ${noun(drafts.length)} ${drafts.length === 1 ? "is" : "are"} still a draft and will be committed to the render queue first.`
    : "";
  return {
    count: targets.length,
    drafts: drafts.length,
    title: `Generate ${noun(targets.length)} with no video yet${drafts.length ? `, committing ${drafts.length} draft${drafts.length === 1 ? "" : "s"} first` : ""}`,
    confirm:
      `Queue ${noun(targets.length)} — every shot with no video yet — as one batch?`
      + `${draftNote}${bundleSpend(project)} `
      + "One confirmation covers the batch.",
    empty: "",
  };
}

// The batch report, as one toast the Director actually reads: what queued, what was
// skipped, each skip in the server's own sentence.
export function batchReportToast(report) {
  const queued = report?.submitted?.length || 0;
  const skipped = report?.skipped || [];
  let message = queued
    ? `${queued} shot${queued === 1 ? "" : "s"} queued as one batch`
    : "Nothing queued";
  if (skipped.length) {
    const reasons = skipped.map((entry) => `${entry.label}: ${entry.reason}`).join(" · ");
    message += ` — ${skipped.length} skipped. ${reasons}`;
  }
  return message;
}

// ------------------------------------------------------------------------------------------
// Cancel every open render (the Director's report, 2026-08-23). The per-job `×` works, and
// twenty-six of them is a chore rather than a control -- so a batch they changed their mind about
// was cleared from ComfyUI's own UI instead, which is the path that left twenty-six takeless shots
// reading `error`.
//
// The scope is every open render in this project rather than one batch, and the whole argument is
// in `app.CANCEL_ALL_NONE_OPEN`'s note. The one line of it that lives here: this counts exactly
// what `hasActiveRenderJobs` counts, so the poll standing down is the observable proof the button
// did what it said.
// ------------------------------------------------------------------------------------------

export const CANCEL_ALL_CONTROL = "#queue-cancel-all";
export const CANCEL_ALL_LABEL = "Cancel all renders";
//: The button's hover text. It names the scope out loud, because the panel it sits in also draws a
//: per-batch progress line, and a Director reading "Batch: 4 of 30 settled" beside a button that
//: turned out to stop a lone Render again as well would rightly call that a surprise.
export const CANCEL_ALL_HELP =
  "Stop every queued and running render for this project on ComfyUI, in one act — not just the "
  + "newest batch. Each is dequeued (interrupted when running), its job record settled, and its "
  + "shot released. It asks first and names the count.";

//: The whole decision the button is drawn from: how many renders are open, and the sentence the
//: Director confirms. Pure and contract-tested, never re-derived in the template or at the click,
//: for `generateAllPlan`'s reason -- the count shown and the count acted on must be one number.
//:
//: `hidden` rather than `disabled` for an idle project: a disabled control carries a reason worth
//: reading, and "nothing is rendering" is not a refusal a Director needs explained beside an empty
//: queue. `Re-queue flagged` hides on the same argument, one button over.
export function cancelAllPlan(project) {
  const open = (project?.jobs || []).filter(
    (job) => job?.prompt_id && !TERMINAL_JOB_STATUSES.includes(job.status),
  );
  if (!open.length) {
    return { count: 0, hidden: true, label: CANCEL_ALL_LABEL, title: CANCEL_ALL_HELP, confirm: "" };
  }
  const noun = `${open.length} open render${open.length === 1 ? "" : "s"}`;
  return {
    count: open.length,
    hidden: false,
    // The count rides the label as well as the dialog, so the size of the gesture is legible
    // before the click rather than only inside the confirmation -- `Re-queue flagged (3)`'s form.
    label: `${CANCEL_ALL_LABEL} (${open.length})`,
    title: CANCEL_ALL_HELP,
    confirm:
      `Cancel ${noun} for this project? This is every queued and running render, not just the `
      + "newest batch. It cannot be undone: a render stopped part-way produces nothing, and the "
      + "GPU time already spent on it is gone. Shots left with no take go back to ready.",
  };
}

//: What the cancellation did, in `batchReportToast`'s shape and its voice. A job that settled
//: between the click and the loop reaching it lands in `skipped` with the server's own sentence,
//: and is reported rather than folded into the total -- a Director who stopped nineteen of
//: twenty-six renders has to be able to see that seven are still running.
export function cancellationToast(report) {
  const stopped = report?.cancelled?.length || 0;
  const skipped = report?.skipped || [];
  let message = stopped
    ? `${stopped} render${stopped === 1 ? "" : "s"} cancelled`
    : "Nothing cancelled";
  if (skipped.length) {
    const reasons = skipped.map((entry) => `${entry.label}: ${entry.reason}`).join(" · ");
    message += ` — ${skipped.length} left alone. ${reasons}`;
  }
  return message;
}

// Which half of the report a note came from, said in words. State is never carried by colour
// alone, and these two lines are otherwise distinguished only by the colour of a list marker --
// so the kind is part of the sentence.
export const READINESS_BLOCKING_LABEL = "Blocked";
export const READINESS_SAMENESS_LABEL = "Near-duplicate";
//: The second blocking kind, `batch.NOTE_KIND_STALE_MAP`, pinned by a contract test. It reads under
//: its own name rather than the generic "Blocked" for the same reason the two window states do: the
//: two blocks send a Director to two different places -- one to write a prompt, one to clear the
//: expanded prompt box -- and a list that called both "Blocked" would make them look like one
//: problem with one fix. The server's own sentence still carries the whole remedy.
export const READINESS_STALE_MAP_LABEL = "Stale reference map";
//: The two window states, named apart because they *are* apart: one is handled and one is not.
//: Neither is a block, and neither may be drawn as one -- both of the server's sentences end by
//: saying so.
export const READINESS_WINDOW_LONG_LABEL = "Long window";
export const READINESS_WINDOW_SHORT_LABEL = "Short window";
//: The third, and it is about the take rather than about the band: this window has been moved or
//: stretched off the picture its own take holds. Named for what a Director sees rather than for
//: the arithmetic -- the clip's bounds have gone past what the clip covers.
export const READINESS_TAKE_UNCOVERED_LABEL = "Past the take";
//: The fourth, and the only one in this list that is not about a window: the shot has been given
//: two sources of location, its own cited setting and a section look that describes a different
//: one. Named for the state rather than for the rule -- "Two locations" is the Director's own
//: reading of it, and a heading naming the word-overlap test would describe the arithmetic instead
//: of the problem. Amber, never a block; the server's sentence carries the two names and the
//: evidence.
export const READINESS_SETTING_CONFLICT_LABEL = "Two locations";
//: And the heading for a kind this client has no name for -- see `NOTE_KIND_WINDOW_UNKNOWN`. It
//: says only that the server sent a note, which is the whole of what is known. Deliberately not
//: "Unknown warning" or anything else that editorialises: the sentence that follows is the
//: server's and it explains itself, and a heading guessing at severity would be the same
//: invention, one notch quieter, as the "Short window" this replaced.
export const READINESS_UNNAMED_NOTE_LABEL = "Note";

// ------------------------------------------------------------------------------------------
// The shot-length band, as the *server* judges it. `batch.NOTE_KIND_WINDOW_SHORT` and
// `NOTE_KIND_WINDOW_LONG`, pinned by a contract test.
//
// **Nothing here re-derives the band.** The constants live in `timeline.py` and the short end
// has deliberately subtle arithmetic -- the render floors at H3's minimum frame count and
// centres the window inside it, so the floor fires well below the nominal 4 s. A client-side
// band check would drift from the server's and paint a clip yellow the server considers fine,
// or leave one plain that it does not. The report carries the verdict; this reads it.
//
// The two states are asymmetric on purpose, and the asymmetry is the Director's own ruling:
//
// * `window_long` is the yellow. "when dragging a clip past that it should turn yellow but we
//   arent dead yet" -- a warning on the clip, never a refusal anywhere. The shot stays
//   editable, submittable and armable.
// * `window_short` is **not a problem at all** any more. The render floors and centres, so the
//   exposed cut is exactly the window and a micro-cut is legitimate. It is reported in the
//   readiness list, where the server's sentence explains what it costs, and it deliberately
//   puts no state on the clip: a colour there would say "wrong" about something that is fine.
// ------------------------------------------------------------------------------------------

export const NOTE_KIND_WINDOW_SHORT = "window_short";
export const NOTE_KIND_WINDOW_LONG = "window_long";
//: `batch.NOTE_KIND_TAKE_UNCOVERED`, and the Director's second yellow (2026-08-21): "if the
//: bounds of the shots window are dragged beyond where that clip covers then the shot would turn
//: yellow to warn that the bounds was gone past." Read exactly as the band's two are -- the
//: server decides it, from the window it recorded at submission, and nothing here re-derives it.
//: A client-side coverage check would need the take's own window, and the *live* window is not
//: it: a shot that has been edited since its render is precisely the case this reports on.
export const NOTE_KIND_TAKE_UNCOVERED = "take_uncovered";
//: `batch.NOTE_KIND_SETTING_CONFLICT`, pinned by a contract test. The Director's report of
//: 2026-08-23: five shots on the live plan cite a bed while their section's look prompt reads
//: "Vast empty warehouse floor", so the render is handed one place and told the location is
//: another. The server decides it -- structurally, from word overlap between the section look and
//: this project's own setting-asset names, because readiness may not make a model call -- and
//: nothing here re-derives it. A client-side version would need the asset library and the sections
//: together, and would be a second opinion about a rule that already has one.
export const NOTE_KIND_SETTING_CONFLICT = "setting_conflict";
//: **Not a kind the server sends.** It is what this client calls a kind it does not recognise, so
//: that the fallback has a name of its own instead of borrowing a real one.
//:
//: Until 2026-08-23 the fallback returned `NOTE_KIND_WINDOW_SHORT`, and that is a lie rather than
//: a gap: a note the server added last week would have reached the Director's readiness list under
//: the heading **"Short window"**, over a sentence about something else entirely, in the muted
//: colour that says "this one is fine" — the exact failure the comment on `readinessLines` warns
//: about two screens down, committed by the branch that comment points at. Every kind the server
//: has gets a branch; this is what the ones it has not got yet get, and a wrong label is worse
//: than an unknown one.
//:
//: A note that lands here still keeps everything that carries meaning: the server's own sentence,
//: the Shot names, and a place in the list. What it loses is a name this client cannot honestly
//: supply. The remedy is to add a branch, and the note is visible enough to make somebody do it.
export const NOTE_KIND_WINDOW_UNKNOWN = "window_unknown";

//: Which of the kinds this client knows a note is, or `NOTE_KIND_WINDOW_UNKNOWN`. Written as one
//: explicit branch per known kind rather than as a table lookup so that a kind can never be
//: recognised by inheriting `Object.prototype`.
function windowNoteKind(note) {
  if (note?.kind === NOTE_KIND_WINDOW_SHORT) return NOTE_KIND_WINDOW_SHORT;
  if (note?.kind === NOTE_KIND_WINDOW_LONG) return NOTE_KIND_WINDOW_LONG;
  if (note?.kind === NOTE_KIND_TAKE_UNCOVERED) return NOTE_KIND_TAKE_UNCOVERED;
  if (note?.kind === NOTE_KIND_SETTING_CONFLICT) return NOTE_KIND_SETTING_CONFLICT;
  return NOTE_KIND_WINDOW_UNKNOWN;
}

//: Which of two window states a clip wears when the report has both to say about it. A shot can
//: carry two notes now -- a long window over a take it has outgrown is both -- and a clip has one
//: border and one accessible name. Higher wins.
//:
//: Uncovered outranks the band, and the ranking is the Director's own reading of the two. The
//: band is a standing property of the plan: a 20 s shot has been 20 s since it was written, and
//: the sentence about it will be just as true tomorrow. Uncovered is a thing that has just
//: *happened* to this take under a gesture the Director made a second ago, and it is the one they
//: can undo. The unranked list order would have made the answer depend on which check the server
//: ran first, which is not a decision anyone made.
//:
//: The setting conflict ranks **below every window state** and draws nothing on the clip. It is
//: ranked rather than omitted because an unranked kind would make the answer order-dependent
//: again -- `undefined > 1` and `1 > undefined` are both false, so a shot that is in two places
//: *and* has outgrown its take would keep whichever note the server happened to list first, and a
//: real amber border would be lost to a note that paints none. Below, and not merely last, because
//: a clip's one border is about the clip: the band and the coverage are properties of the window
//: the Director is dragging, and where the shot is set is a property of its references, fixed in
//: the inspector. The readiness list prints its whole sentence either way.
//:
//: An unrecognised kind ranks below every known one, and is ranked rather than omitted for the
//: same reason the setting conflict is: an absent rank makes both comparisons false and hands the
//: answer to whichever note the server happened to list first. Lowest, because the one thing that
//: *is* known about it is that this client cannot say what it means — so it must never displace a
//: border that a kind with a branch earned.
const WINDOW_KIND_RANK = {
  [NOTE_KIND_WINDOW_UNKNOWN]: -2,
  [NOTE_KIND_SETTING_CONFLICT]: -1,
  [NOTE_KIND_WINDOW_SHORT]: 0,
  [NOTE_KIND_WINDOW_LONG]: 1,
  [NOTE_KIND_TAKE_UNCOVERED]: 2,
};

// Which shots the report says have something wrong with their window, and what. `{ shotId: kind }`,
// empty for a report that has not been fetched or that found nothing. Every note is still printed
// in full by `readinessLines`; this is only what the *clip* can wear.
export function windowWarningsByShot(report) {
  const found = {};
  for (const note of report?.window_warnings || []) {
    const kind = windowNoteKind(note);
    for (const shotId of note?.shot_ids || []) {
      const standing = found[shotId];
      if (standing === undefined || WINDOW_KIND_RANK[kind] > WINDOW_KIND_RANK[standing]) {
        found[shotId] = kind;
      }
    }
  }
  return found;
}

//: The class a clip carries for its window state, and the sentence appended to its accessible
//: name. Only two kinds draw anything: the long band and a take the window has left. The short
//: end draws neither class nor words, because it is not a problem; the setting conflict draws
//: none because it is not about the window; and an unrecognised kind draws none because this
//: client cannot say what it would be claiming. State is never carried by colour alone here
//: either, so a class always comes with a sentence.
export const CLIP_WINDOW_LONG_CLASS = "window-long";
export const CLIP_WINDOW_LONG_NOTE =
  "Longer than the range H3 is trained for. It still submits and renders; expect motion and " +
  "lipsync to drift late in the take.";
//: The same amber, a different class, because the class names the state and this is not the band.
export const CLIP_TAKE_UNCOVERED_CLASS = "take-uncovered";
//: Short, because it is a clip's title and its accessible name -- the readiness list carries the
//: server's whole sentence with the numbers in it. It names the fix in the Director's own pair,
//: "readjust or regenerate", and it does not say the gesture was stopped, because it was not.
export const CLIP_TAKE_UNCOVERED_NOTE =
  "The window has been moved or stretched past the picture this take holds. Nothing is stopped: " +
  "re-cut it back over the take, or render the shot again for the window it has now.";

//: One entry per state that draws anything. A table rather than a chain of `if`s so that adding a
//: state cannot silently keep a class while losing its sentence -- the two live in one place and
//: are read together.
const CLIP_WINDOW_STATES = {
  [NOTE_KIND_WINDOW_LONG]: { className: CLIP_WINDOW_LONG_CLASS, note: CLIP_WINDOW_LONG_NOTE },
  [NOTE_KIND_TAKE_UNCOVERED]: {
    className: CLIP_TAKE_UNCOVERED_CLASS, note: CLIP_TAKE_UNCOVERED_NOTE,
  },
};

// `label` is the clip's whole accessible name: `shotPromptCell`'s label with this state's
// sentence folded in, or that label untouched when there is no state. Returned from here rather
// than joined in the template, on `shotPromptCell`'s own argument -- the timeline's markup is a
// thin applier of decisions made in this file, and a ternary in the template is a second place
// the two signals could come apart. A clip with nothing to say is byte for byte what it was.
export function clipWindowState(kind, label = "") {
  const drawn = CLIP_WINDOW_STATES[kind];
  if (!drawn) return { className: "", note: "", label };
  return {
    className: drawn.className,
    note: drawn.note,
    label: label ? `${label} — ${drawn.note}` : drawn.note,
  };
}

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
//: The readiness list's own class and heading for each window kind, keyed by the server's kind so
//: the two can never be paired up wrongly. The classes are the stylesheet's, and only the ones
//: that colour have a rule -- `window-short` deliberately has none.
//:
//: `window-note` is the unrecognised kind's row, and it is amber rather than muted. Muted is a
//: claim -- it is what `window-short` wears to say "this is a cost, not a problem" -- and this
//: client has no grounds to make that claim about a note it cannot name. Amber says only what the
//: server already said by putting it in this list: something here wants looking at.
const WINDOW_LINE_KINDS = {
  [NOTE_KIND_WINDOW_LONG]: "window-long",
  [NOTE_KIND_WINDOW_SHORT]: "window-short",
  [NOTE_KIND_TAKE_UNCOVERED]: "take-uncovered",
  [NOTE_KIND_SETTING_CONFLICT]: "setting-conflict",
  [NOTE_KIND_WINDOW_UNKNOWN]: "window-note",
};
const WINDOW_LINE_LABELS = {
  [NOTE_KIND_WINDOW_LONG]: READINESS_WINDOW_LONG_LABEL,
  [NOTE_KIND_WINDOW_SHORT]: READINESS_WINDOW_SHORT_LABEL,
  [NOTE_KIND_TAKE_UNCOVERED]: READINESS_TAKE_UNCOVERED_LABEL,
  [NOTE_KIND_SETTING_CONFLICT]: READINESS_SETTING_CONFLICT_LABEL,
  [NOTE_KIND_WINDOW_UNKNOWN]: READINESS_UNNAMED_NOTE_LABEL,
};

//: The heading each blocking kind reads under. Only the kinds that have a name of their own appear;
//: anything else -- an older server, a kind added and not yet mirrored -- falls back to
//: `READINESS_BLOCKING_LABEL`, so a note can never lose its sentence by gaining a kind. Every line
//: keeps the `blocking` list-marker class: these are all refusals, and the heading is what tells
//: them apart, not the colour.
const BLOCKING_LINE_LABELS = {
  [NOTE_KIND_STALE_MAP]: READINESS_STALE_MAP_LABEL,
};

export function readinessLines(report) {
  const render = (kind, label) => (note) => {
    const shotIds = note?.shot_ids || [];
    const names = noteLabels(note);
    const reason = note?.reason || "";
    return { kind, shotIds, shots: names, reason, text: names.length ? `${label} - ${names.join(" and ")}: ${reason}` : `${label} - ${reason}` };
  };
  return [
    ...(report?.blocking || []).map((note) =>
      render("blocking", BLOCKING_LINE_LABELS[note?.kind] || READINESS_BLOCKING_LABEL)(note)),
    ...(report?.warnings || []).map(render("warning", READINESS_SAMENESS_LABEL)),
    // The third list, drawn as its own kind and never folded into the second. `warnings` means
    // exactly one thing to every reader it already has -- `READINESS_SAMENESS_LABEL` and
    // `readinessSummary`'s "N near-duplicate pairs" -- so a window note posted into it would
    // reach the Director under a name that is not what it says, counted as a pair it is not.
    //
    // Each kind in that list under its own name and its own list-marker class, decided from the
    // note's `kind` in one place: a line that read "Long window" over a coverage sentence, or
    // "Short window" over a two-locations one, would be describing a rule the server does not
    // have. Every kind the server sends gets a branch, and since 2026-08-23 a kind it does not
    // send yet gets `NOTE_KIND_WINDOW_UNKNOWN` -- which prints the server's sentence under a
    // heading that claims nothing, rather than under the "Short window" the fallback used to
    // borrow. The mislabel this paragraph warns about was, until then, what the fallback did.
    ...(report?.window_warnings || []).map((note) => {
      const kind = windowNoteKind(note);
      return render(WINDOW_LINE_KINDS[kind], WINDOW_LINE_LABELS[kind])(note);
    }),
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

// The Shot statuses the render-again control is drawn for at all: app.py's RENDER_AGAIN_STATUSES,
// asserted identical by a contract test. A control offered for a status the route does not
// re-open is a button whose only possible outcome is a refusal.
export const RENDER_AGAIN_STATUSES = ["complete", "error", "approved"];

export const RENDER_AGAIN_LABEL = "Render again";
export const RENDER_AGAIN_HELP =
  "Re-open this shot and queue one new take (turbo, fresh seed). The previous take stays until " +
  "the new one lands; cancelling the dialog re-opens without rendering.";
// app.py's RESUBMIT_SEED_STRIDE, asserted identical by a contract test: a re-render at the same
// seed and prompt reproduces the identical take *while ComfyUI keeps the model resident*, which
// reads as "nothing was replaced".
//
// The condition is measured rather than assumed (2026-08-23, §8.8 follow-up of the H3 attention
// experiment): four repeats of one byte-identical payload at one seed produced two pairs, each
// pair bit-identical across all 141 frames, and the two pairs differ from each other only because
// ComfyUI evicted and reloaded the model between them, unprompted. So the sampler is
// deterministic and the take is not guaranteed to be: without the stride a Director gets the same
// take, *or* a different one at a moment nobody chose, and neither reads as a decision.
//
// And the stride carries a second load the seed alone would not. ComfyUI serves an unchanged
// graph from its **execution cache**: a byte-identical resubmission came back in 1.157 s with the
// sampler reported cached — no sampling, no power draw, the previous file re-saved under a new
// name. Moving the seed is what makes the submission a render at all.
export const RESUBMIT_SEED_STRIDE = 101;
// The two refusals the browser can see coming, in the server's words. Drawn as a disabled control
// carrying the reason rather than as no control at all: "why can I not render this again" is a
// question the panel should answer where it is asked, and a control that silently vanishes for a
// locked or approved shot answers it nowhere.
export const RENDER_AGAIN_LOCKED =
  "This shot is locked. A lock is a deliberate hands-off on this shot, and re-opening it for " +
  "another render is exactly the kind of change it refuses. Unlock the shot first.";
export const RENDER_AGAIN_APPROVED =
  "This shot carries an approved take. An approval is an editorial decision about one specific " +
  "take, so rendering over it would leave that decision describing a take that no longer exists. " +
  "Clear the approval first if the decision has changed.";

// Everything the inspector draws for the render-again control, decided here rather than in the
// template -- the same reason `shotPromptCell` exists. The states this has to tell apart are
// "not applicable", "applicable but refused, here is why" and "go ahead", and a template holding
// those ternaries can have its arms swapped while every string the suite greps for survives.
// Executed by tests/test_frontend_contract.py for every status and every refusal.
//
// The prompt is checked here, from the Shot on screen, and that check is the whole design note of
// this feature: passing the readiness gate once is not a permanent property of a Shot. The
// textarea writes `shot.prompt` and re-renders the inspector on every change, so a Shot whose
// prompt is deleted after it rendered stops offering this control immediately -- before the click,
// as well as after it, where the route refuses it again from scratch.
//
// `disabled` is never inferred from `shown` and never the other way round: an approved Shot is
// shown *and* disabled, which is the case that carries the reason worth reading.
export function renderAgainControl(shot) {
  const status = String(shot?.status ?? "");
  if (!RENDER_AGAIN_STATUSES.includes(status)) {
    return { shown: false, disabled: true, label: RENDER_AGAIN_LABEL, title: "", reason: "" };
  }
  const refuse = (reason) => ({ shown: true, disabled: true, label: RENDER_AGAIN_LABEL, title: reason, reason });
  if (shot?.locked) return refuse(RENDER_AGAIN_LOCKED);
  if (shot?.approved_output || status === "approved") return refuse(RENDER_AGAIN_APPROVED);
  const rejection = promptRejection(shot);
  if (rejection) return refuse(`${rejection} ${READINESS_REMEDY}.`);
  return { shown: true, disabled: false, label: RENDER_AGAIN_LABEL, title: RENDER_AGAIN_HELP, reason: "" };
}

// What re-opening did to the take that was already there, mirroring app.py's
// RENDER_AGAIN_PREVIOUS_TAKE so the sentence the Director reads is the one the server implements.
// A contract test asserts the two are identical.
//
// Said on every success rather than buried in the docs, because the belief this exists to prevent
// -- that the application is keeping the takes -- is exactly the belief a silent "re-opened" toast
// would leave in place.
export const RENDER_AGAIN_PREVIOUS_TAKE =
  "{shot} is open for another render. The take already there is not deleted: ComfyUI numbers " +
  "its output files, so the next render writes a new numbered file beside the old one rather " +
  "than over it, and the job that produced the old take goes on naming it in the render queue. " +
  "What moves is this shot's single latest-take pointer, once the new take lands. This " +
  "application does not track takes, so the older file is on disk and not in a take list.";

// The Shot named as the timeline names it: `SHOT 03 (shot_id)`, matching batch.py's `shot_label`.
// Both halves, for that function's reason -- the number is what is drawn on the clip and the id is
// what is unambiguous -- and numbered by manifest position, which is what the timeline draws.
export function shotLabel(project, shotId) {
  const shots = project?.shots || [];
  const index = shots.findIndex((item) => item?.id === shotId);
  return index < 0 ? String(shotId ?? "") : `SHOT ${String(index + 1).padStart(2, "0")} (${shotId})`;
}

// -- A job whose shot is no longer on the plan -------------------------------------------------
//
// A populate, a lay-out confirm and the generic shots `PUT` all replace `project.shots` wholesale,
// and the first two **mint new ids for every window**. Nothing prunes jobs — deliberately, and
// `_adopt_job_measurements` says so: "no route in this application removes one". So after a
// populate the queue holds records pointing at shot ids that do not exist.
//
// **Retained rather than dropped, and that is the decision.** The takes those jobs produced are
// still on disk, and the record is the only thing linking a file to the render that made it — its
// seed, its frame count, its measured time, the batch it belonged to. `clipLibraryRows` builds the
// Clips tab *from the job list*, so pruning would delete the provenance of exactly the takes a
// Director goes looking for after a re-plan. Dropping is cheap to do and impossible to undo.
//
// What is **not** acceptable is the state before this: the row fell back to `shotLabel`'s bare id
// — the dead text a 2026-08-20 finding removed from this very panel — and stayed marked `linked`,
// so clicking it set `selectedShotId` to an id no shot has and quietly selected nothing.
export const JOB_TARGET_DETACHED = "shot no longer on the plan";
export const JOB_TARGET_DETACHED_TITLE =
  "The shot this render was made for is not on the plan any more — a populate or a lay-out " +
  "replaces every window and mints new shot ids. The record is kept on purpose: the take it " +
  "produced is still on disk, and this row is the only thing that says which render made that " +
  "file, with what seed and how long it took. There is no shot to open.";

//: The job kinds whose `target_id` is a **Shot** id, and therefore the kinds this function has an
//: opinion about. `models.RenderJob.kind` also holds `music` (the song), `flux` and `multiview`
//: (an asset), `edit` and `post` — none of which name a shot, so none of them can be detached from
//: one.
//:
//: `ltx` is here since 2026-08-23. `app.generate_ltx_enhance` writes `target_id=shot.id` exactly
//: as `generate_h3` does, and `app.py:1568` reads it back as a shot id when it looks for an
//: in-flight enhancement — so an LTX row was drawing the raw `shot_9f2c…` the 2026-08-20 finding
//: removed from this panel, and drawing it as unlinked dead text even while its shot was right
//: there on the timeline. One kind was special-cased where two were meant.
export const JOB_KINDS_TARGETING_A_SHOT = ["h3", "ltx"];

//: How the queue panel names one job's target and whether the row is a way back to it. Pure, so
//: the "is this shot still here" question is answered once rather than three times inside a
//: template literal.
export function jobTarget(project, job) {
  const targetId = job?.target_id || "";
  if (!JOB_KINDS_TARGETING_A_SHOT.includes(job?.kind) || !targetId) {
    return { label: targetId || "—", shotId: "", linked: false, title: "" };
  }
  if (!(project?.shots || []).some((shot) => shot?.id === targetId)) {
    return {
      label: `${targetId} — ${JOB_TARGET_DETACHED}`,
      shotId: "",
      linked: false,
      title: JOB_TARGET_DETACHED_TITLE,
    };
  }
  return { label: shotLabel(project, targetId), shotId: targetId, linked: true, title: "" };
}

export function renderAgainNotice(project, shotId) {
  return RENDER_AGAIN_PREVIOUS_TAKE.replace("{shot}", shotLabel(project, shotId));
}

// -- The seed's randomize toggle (the Director's ask, 2026-08-20) ------------------------------
//
// "we should shorten that box a bit and add a randomize toggle (1-99999) which would RNG a number
// and hold it unless regenerate gets hit later with randomize still checked."
//
// The bounds are the Director's own numbers, inclusive at both ends. `0` is deliberately outside
// them even though the seed field accepts it: 0 is the value populate and the Flux form leave
// behind for "nobody chose", and a randomizer that can return it would make "random" and "unset"
// indistinguishable on the one field where they have to be told apart.
export const RANDOM_SEED_MIN = 1;
export const RANDOM_SEED_MAX = 99999;

//: The toggle's own control id and the two sentences it is drawn with. The label names the moment
//: it re-rolls, in the inspector's own word for that button ("Render again"), because a toggle
//: whose re-roll moment has to be guessed is worse than a button: the Director cannot tell a seed
//: that held from one that moved without reading the number back afterwards.
export const RANDOM_SEED_CONTROL = "shot-seed-randomize";
export const RANDOM_SEED_LABEL = "Randomize on Render again";
export const RANDOM_SEED_HELP =
  "Ticking this rolls a seed in 1–99999 now and holds it. It re-rolls at one moment only: when " +
  "Render again queues a take. It does not re-roll on Mark ready, on selecting another shot, or " +
  "on a redraw. Typing a seed by hand clears this toggle — a number you typed is a number you " +
  "chose. It is set per shot and for this session only: ticking it here arms this shot and no " +
  "other, it is not saved into the project, and it is off again after a reload. Generate All has " +
  `its own +${RESUBMIT_SEED_STRIDE} step on the server and does not read this box.`;

// One roll, inside the Director's bounds. `random` is injected so the contract tests can drive the
// edges rather than sample and hope; every caller in the application uses the default.
export function randomSeed(random = Math.random) {
  const span = RANDOM_SEED_MAX - RANDOM_SEED_MIN + 1;
  const roll = Math.floor(Number(random()) * span);
  return RANDOM_SEED_MIN + Math.min(Math.max(roll, 0), span - 1);
}

// What the seed becomes when a re-render is actually queued -- the single place the two sources of
// seed movement are chosen between, so they cannot both fire.
//
// Without randomize this is the server's own RESUBMIT_SEED_STRIDE, unchanged: a resubmission at the
// same seed and prompt reproduces the identical take while ComfyUI keeps the model resident, which
// reads as "nothing was replaced", and that stride is what the inspector's Render again has always
// applied. See RESUBMIT_SEED_STRIDE for what "while resident" was measured to mean, and for the
// second thing the stride buys -- an unchanged graph is served from ComfyUI's execution cache
// without sampling at all.
//
// With randomize it is a fresh roll *instead of* the stride, never as well as it. Adding a stride
// to a random number would be a second, invisible source of drift on a value the Director has just
// asked to own; and rolling on top of the stride would make the stride's guarantee unreachable.
//
// A roll that lands on the number already stored is nudged one step on. One take in 99999 is rare
// enough to be reported as a bug rather than as luck, and the whole point of the gesture is a
// *different* take -- the same reason the stride exists.
export function nextRenderSeed(shot, randomize, random = Math.random) {
  const current = Math.max(0, Number(shot?.seed) || 0);
  if (!randomize) return current + RESUBMIT_SEED_STRIDE;
  const rolled = randomSeed(random);
  if (rolled !== current) return rolled;
  return rolled === RANDOM_SEED_MAX ? RANDOM_SEED_MIN : rolled + 1;
}

// The Shot statuses the commit control is drawn for at all: app.py's MARK_READY_STATUSES, asserted
// identical by a contract test. The exact complement of RENDER_AGAIN_STATUSES and the in-flight
// pair -- the two actions divide the status vocabulary between them, one for each side of a Shot's
// first render -- so a Shot always shows exactly one of the two controls and never neither.
export const MARK_READY_STATUSES = ["draft", "ready"];

export const MARK_READY_LABEL = "Mark ready to queue";
export const MARK_DRAFT_LABEL = "Back to draft";
// Both help strings lead with what the action does *not* do, because that is the belief worth
// managing on either side. "Ready" sits immediately before the expensive step, so a Director who
// reads it as "render this" would avoid the one control that starts the primary journey; and a
// Director unsure whether un-committing loses their work will leave a shot armed rather than risk
// it.
export const MARK_READY_HELP =
  "Commit this shot to the render queue. Nothing is rendered by this and no GPU time is spent " +
  "until the queue is submitted.";
export const MARK_DRAFT_HELP =
  "Take this shot back out of the render queue. Nothing else about the shot changes and nothing " +
  "is deleted.";
// The refusals the browser can see coming, in the server's words. Drawn as a disabled control
// carrying the reason rather than as no control at all, for the render-again control's reason:
// "why can I not queue this" is a question the panel should answer where it is asked.
export const MARK_READY_LOCKED =
  "This shot is locked. A lock is a deliberate hands-off on this shot, and committing it to the " +
  "render queue is exactly the kind of change it refuses. Unlock the shot first.";
export const MARK_READY_APPROVED =
  "This shot carries an approved take, which is an editorial decision about one specific take " +
  "rather than a shot waiting to be rendered. Clear the approval first if the decision has changed.";

// Everything the inspector draws for the commit control, decided here rather than in the template
// -- `renderAgainControl`'s reason exactly. The states to tell apart are "not applicable", "shown
// but refused, here is why" and "go ahead", in either direction, and a template holding those
// ternaries can have its arms swapped while every string the suite greps for survives.
//
// `action` is carried out rather than re-derived at the click site: the label and the route have to
// agree, and a handler that recomputed the direction from `shot.status` would be a second copy of
// the decision that could disagree with the button the Director actually pressed.
//
// The prompt is checked here, from the Shot on screen, and only in the arming direction. Arming
// because passing the gate is never a permanent property of a Shot -- the textarea writes
// `shot.prompt` and re-renders the inspector on every change, so a Shot whose prompt is deleted
// stops offering the commit immediately. Only arming because `draft` is the un-armed state, and a
// control that refused to un-commit an unprompted shot would trap it armed.
//
// `disabled` is never inferred from `shown` and never the other way round: a locked shot is shown
// *and* disabled, which is the case that carries the reason worth reading.
export function markReadyControl(shot) {
  const status = String(shot?.status ?? "");
  if (!MARK_READY_STATUSES.includes(status)) {
    return { shown: false, disabled: true, action: "", label: MARK_READY_LABEL, title: "", reason: "" };
  }
  const action = status === "ready" ? "draft" : "ready";
  const label = action === "draft" ? MARK_DRAFT_LABEL : MARK_READY_LABEL;
  const refuse = (reason) => ({ shown: true, disabled: true, action, label, title: reason, reason });
  if (shot?.locked) return refuse(MARK_READY_LOCKED);
  if (shot?.approved_output) return refuse(MARK_READY_APPROVED);
  if (action === "ready") {
    const rejection = promptRejection(shot);
    if (rejection) return refuse(`${rejection} ${READINESS_REMEDY}.`);
  }
  return {
    shown: true, disabled: false, action, label,
    title: action === "draft" ? MARK_DRAFT_HELP : MARK_READY_HELP, reason: "",
  };
}

export const APPROVE_LABEL = "Approve take";
export const UNAPPROVE_LABEL = "Un-approve take";
// Both help strings lead with what the click does *not* do, on the mark-ready controls'
// argument: approval is the one editorial decision in this panel, and a Director deciding
// whether to press it must know it renders nothing and that it is reversible -- FR-21's own two
// promises.
export const APPROVE_HELP =
  "Approve this shot's latest take. Nothing is rendered by this and nothing is deleted; while " +
  "the approval stands the shot cannot be re-rendered, and un-approving reverses it.";
export const UNAPPROVE_HELP =
  "Clear this shot's approval. Nothing is deleted: the take stays this shot's latest output, " +
  "and the shot becomes re-renderable again.";
// The refusal the browser can see coming, in the server's words -- app.py's
// APPROVE_IN_FLIGHT_REFUSAL with the label the panel already shows standing in for the name.
// Drawn as a disabled control carrying the reason rather than as no control at all, for the
// render-again control's reason: the take on screen is real and "why can I not approve it" is a
// question the panel should answer where it is asked.
//
// Its first sentence is the whole fact the Monitor, the takes strip and the clip also have to
// state, so it is a constant of its own and this refusal is built from it: four surfaces
// describing one state in four wordings is how a Director ends up believing they are four
// states. Spelled out here rather than templated, so the concatenation below stays legible as
// the server's sentence.
export const TAKE_DISPLACED_BY_RENDER =
  "A render for this shot has not finished, so the take on screen is about to be displaced.";
export const APPROVE_IN_FLIGHT =
  `${TAKE_DISPLACED_BY_RENDER} ` +
  "Approving it now would leave the decision attached to whichever file lands next. Wait for " +
  "it, or refresh the render queue if it has already finished and this project has not been " +
  "told yet.";

// Everything the inspector draws for the approve/un-approve pair, decided here rather than in
// the template -- `renderAgainControl`'s reason exactly. The states to tell apart are "nothing
// to decide about", "approve", "un-approve" and "shown but refused, here is why", and a template
// holding those ternaries can have its arms swapped while every string the suite greps for
// survives. Executed by tests/test_frontend_contract.py for every state.
//
// The approved arm is decided from the Shot's two approval fields -- either signal, matching the
// server's `shot_is_approved` -- and comes first, ahead of the take check, because un-approve is
// the one way back and must be offered even on a hand-edited Shot whose `latest_output` is gone.
//
// `action` is carried out rather than re-derived at the click site, for `markReadyControl`'s
// reason: a handler that recomputed the direction from the Shot would be a second copy of the
// decision that could disagree with the button the Director actually pressed.
//
// `disabled` is never inferred from `shown` and never the other way round: a shot with a take
// and a render in flight is shown *and* disabled, which is the case carrying the reason worth
// reading.
export function approvalControl(shot) {
  const status = String(shot?.status ?? "");
  const approved = Boolean(shot?.approved_output) || status === "approved";
  if (approved) {
    return { shown: true, disabled: false, action: "unapprove", label: UNAPPROVE_LABEL, title: UNAPPROVE_HELP, reason: "" };
  }
  // No take, no decision to make: an approval is about a specific piece of media, and a control
  // drawn here could only refuse. The absent state is decided from the Shot's own field, never
  // from what the template happened to draw.
  if (!shot?.latest_output) {
    return { shown: false, disabled: true, action: "", label: APPROVE_LABEL, title: "", reason: "" };
  }
  // The same predicate the Monitor, the takes strip and the clip decide from: one reading of
  // "a render is in flight", so a surface cannot be honest while another is not.
  if (shotRenderInFlight(shot)) {
    return { shown: true, disabled: true, action: "approve", label: APPROVE_LABEL, title: APPROVE_IN_FLIGHT, reason: APPROVE_IN_FLIGHT };
  }
  return { shown: true, disabled: false, action: "approve", label: APPROVE_LABEL, title: APPROVE_HELP, reason: "" };
}

// The shot-mode taxonomy, mirroring `models.SHOT_MODE_SPECS` field for field. A contract test
// executes both and asserts the two tables are identical, because two hand-written copies of what a
// mode requires is how the inspector starts drawing a shot as complete that the route then refuses.
//
// Order matters: it is the order the mode select offers, so it is data here rather than markup.
//
// `workflow` is the graph a renderable mode renders through, under the name the Director knows it
// by, and `""` for a mode with no adapter. The mode select prints it, so which MiniMax workflow a
// mode employs is readable before the click; the mirror test holds it to the server's table, which
// is the one place the mode→workflow mapping is decided.
export const SHOT_MODES = [
  { value: "text_to_video", label: "Text to video", roles: [], song_audio: false, adapter: "h3-director", workflow: "the MiniMax H3 Director graph" },
  // The two keyframe modes share one adapter over `MiniMaxH3ImageToVideo`, whose live schema
  // declares both frames optional and offers no reference-audio input at all — `song_audio:
  // false` is the node's fact, and the workflow strings say "no song lip-sync" so the Director
  // reads it before the click rather than after the render.
  { value: "image_to_video", label: "Image to video", roles: [{ role: "first", minimum: 1, maximum: 1 }], song_audio: false, adapter: "h3-keyframe", workflow: "MiniMax H3 I2V-FLframe (first frame only, no song lip-sync)" },
  { value: "first_last", label: "First / last frame", roles: [{ role: "first", minimum: 1, maximum: 1 }, { role: "last", minimum: 1, maximum: 1 }], song_audio: false, adapter: "h3-keyframe", workflow: "MiniMax H3 I2V-FLframe (first and last frames, no song lip-sync)" },
  { value: "first_middle_last", label: "First / middle / last", roles: [{ role: "first", minimum: 1, maximum: 1 }, { role: "middle", minimum: 1, maximum: 1 }, { role: "last", minimum: 1, maximum: 1 }], song_audio: false, adapter: "", workflow: "" },
  // `first` and `last` ride the references mode too, per MiniMax's guide §2.2.2: the picture
  // travels as an ordinary reference slot on the one graph that takes the windowed master song,
  // and the structured prompt is what makes it the shot's first or last frame — which is how a
  // pinned keyframe and song lip-sync combine at all. The workflow string says so, because the
  // mode select is where a Director would otherwise learn from the audio-less keyframe modes
  // that a singing shot cannot pin its opening frame.
  { value: "references", label: "References to video", roles: [{ role: "reference", minimum: 0, maximum: 15 }, { role: "first", minimum: 0, maximum: 1 }, { role: "last", minimum: 0, maximum: 1 }], song_audio: true, adapter: "h3-reference", workflow: "MiniMax H3 References-to-Video (with the sampling profiles; a first or last keyframe may ride as a reference picture, with song lip-sync)" },
  { value: "extend", label: "Extend an existing video", roles: [{ role: "source_video", minimum: 1, maximum: 1 }], song_audio: false, adapter: "", workflow: "" },
];

// How one option in the mode select reads: the mode's own label, then either the workflow it
// really renders through or the honest admission that nothing renders it yet. A pure function
// rather than a ternary inside the template string, so the sentence a Director spends GPU minutes
// on the strength of is executed by a test instead of read — and so "planned" wording can never be
// drawn on a mode that has an adapter, whichever way the template is later rearranged.
export function shotModeOptionLabel(entry) {
  if (!entry) return "";
  if (!entry.adapter) return `${entry.label} — planned, not yet renderable`;
  return `${entry.label} — renders through ${entry.workflow}`;
}

// How each role reads in a sentence, mirroring `models.ASSET_ROLE_LABELS`. The insertion order is
// the order a role select offers, so it is one list rather than a table plus a separate ordering.
export const ASSET_ROLE_LABELS = {
  reference: "reference",
  first: "first frame",
  middle: "middle frame",
  last: "last frame",
  source_video: "source video",
};

// Whether the performer is singing, mirroring `models.SingingState`. `unknown` is first and is what
// an unset shot reads as -- it is deliberately *not* "not singing", because the enhancer moves lip
// position and a wrong value in either direction costs the Director something real. Nothing here
// infers it from the mode, the assets or anything else.
export const SINGING_STATES = [
  { value: "unknown", label: "Not decided" },
  { value: "singing", label: "Performer is singing" },
  { value: "not_singing", label: "Performer is not singing" },
];

// The three `mode` strings saved before the field meant anything, mirroring
// `models.LEGACY_SHOT_MODES`. A project loaded from an older manifest arrives with the field
// already resolved to `null` by the server, so this exists for the one case the server cannot
// reach: a shot this client is still holding in memory from before a reload.
export const LEGACY_SHOT_MODES = ["text", "image", "reference"];

export function shotModeSpec(mode) {
  return SHOT_MODES.find((entry) => entry.value === mode) || null;
}

// This shot's citations, whatever shape it arrived in. Mirrors `Shot._reconcile_citations`' read
// side: `citations` is the truth, and a shot carrying only the flat `asset_ids` is one saved before
// roles existed, whose assets were all references.
export function shotCitations(shot) {
  if (shot?.citations?.length) {
    return shot.citations.map((citation, index) => ({
      asset_id: citation.asset_id,
      role: citation.role || "reference",
      order: Number.isFinite(citation.order) ? citation.order : index,
    }));
  }
  return (shot?.asset_ids || []).map((asset_id, index) => ({ asset_id, role: "reference", order: index }));
}

// The citations in one role, ordered. A *stable* sort, so citations sharing an order -- the default,
// and what every migrated shot has -- keep their list position and FR-19's numbering survives.
export function citationsInRole(shot, role) {
  return shotCitations(shot)
    .map((citation, index) => ({ citation, index }))
    .sort((left, right) => left.citation.order - right.citation.order || left.index - right.index)
    .filter((entry) => entry.citation.role === role)
    .map((entry) => entry.citation);
}

// What this shot renders as: its declaration, or the mode it already behaves as. Mirrors
// `models.resolve_shot_mode`, including the fallback, which is the branch the server used to make
// inline -- assets or the master song mean the reference graph, nothing means the text-only one.
export function resolveShotMode(shot) {
  const declared = shot?.mode;
  if (declared && !LEGACY_SHOT_MODES.includes(declared)) return declared;
  return shotCitations(shot).length || shot?.use_song_audio ? "references" : "text_to_video";
}

// Everything this shot is missing or carrying wrongly for its mode, in the server's own sentences.
// Mirrors `models.mode_specification_problems`; a contract test runs both over the same shots and
// asserts the two lists match, so the inspector cannot call a shot complete that the route refuses.
export function shotSpecificationProblems(shot) {
  const spec = shotModeSpec(resolveShotMode(shot));
  if (!spec) return [];
  const counted = {};
  for (const citation of shotCitations(shot)) counted[citation.role] = (counted[citation.role] || 0) + 1;
  const problems = [];
  for (const requirement of spec.roles) {
    const held = counted[requirement.role] || 0;
    const label = ASSET_ROLE_LABELS[requirement.role];
    if (held < requirement.minimum) {
      problems.push(`${spec.label} needs ${requirement.minimum} ${label}, and this shot cites ${held}.`);
    } else if (held > requirement.maximum) {
      problems.push(`${spec.label} takes at most ${requirement.maximum} ${label}, and this shot cites ${held}.`);
    }
  }
  const declared = spec.roles.map((requirement) => requirement.role);
  for (const role of Object.keys(ASSET_ROLE_LABELS)) {
    if (declared.includes(role) || !counted[role]) continue;
    problems.push(`${spec.label} has no ${ASSET_ROLE_LABELS[role]} role, and this shot cites ${counted[role]}.`);
  }
  if (shot?.use_song_audio && !spec.song_audio) {
    problems.push(`${spec.label} has no slot for the master song, so the audio reference this shot asks for would not be sent.`);
  }
  return problems;
}

// The cited assets this project's library no longer holds. Mirrors `models.dangling_citations`.
// Reported and never quietly skipped: the inspector used to render nothing at all for a citation
// whose asset was gone, so a shot could look like it had lost an attachment it was still sending.
export function danglingCitations(project, shot) {
  const library = new Set((project?.assets || []).map((asset) => asset.id));
  return shotCitations(shot).filter((citation) => !library.has(citation.asset_id)).map((citation) => citation.asset_id);
}

export const CITATION_MISSING_LABEL = "Missing from the library";

// The write side of the same reconciliation, applied in place so this client's copy of a shot never
// disagrees with what the server will store. The shots write does not adopt its own reply -- it
// deliberately re-renders from local state -- so a client that only wrote `citations` would go on
// drawing a stale `asset_ids` until the next full project load.
export function reconcileShotCitations(shot) {
  shot.citations = shotCitations(shot);
  shot.asset_ids = citationsInRole(shot, "reference").map((citation) => citation.asset_id);
  return shot;
}

// ---------------------------------------------------------------------------------------------
// Assistant ProducerBot
// ---------------------------------------------------------------------------------------------

// Whether an automated writer may change this shot at all, mirroring app.py's `shot_write_refusal`
// and the `shot_render_provenance` it calls. A contract test runs both sides over the same shots.
//
// Mirrored rather than fetched, for `promptRejection`'s reason: this decides what is *drawn* --
// which shots the bulk fill offers to send, and whether the single-shot control is enabled -- and
// the server remains the gate. Both refusals are pre-empted rather than discovered by a click,
// because "why will this not fill in my shot" is a question the composer should answer where it
// is asked.
export function shotWriteRefusal(shot) {
  if (!shot) return null;
  if (shot.locked) return "locked";
  if (shot.prompt_id || shot.latest_output || shot.approved_output || (shot.status && shot.status !== "draft")) return "rendered";
  return null;
}

// Seconds the way a Director reads them, and never NaN. A shot arriving without a window is a
// client bug, not something to print `NaN s` about in text the Director is invited to send.
function seconds(value) {
  return readable(Number.isFinite(Number(value)) ? Number(value) : 0);
}

// The composer text the prefill control writes: the selected shot's context, as a sentence a
// Director could have typed, ending part-way through their own request so they finish it.
//
// **Convenience, not a channel.** This is the constraint the planning document fixed early and it
// decides the whole shape of this function: the structured shot context the model actually works
// from is built server-side by `timeline.assistant_input` and sent in the request, so nothing here
// has to be machine-readable and nothing downstream parses it. If it did double as a channel, the
// Director editing their own composer text would be silently editing the model's input.
//
// It ends on "Make it" because that is where the Director's sentence begins -- their own described
// interaction is "make that shot a B-roll of a grey wolf walking through a forest" -- and a prefill
// that ends on a full stop invites deleting it rather than continuing it.
export function shotPrefillText(project, shot) {
  if (!shot) return "";
  const spec = shotModeSpec(resolveShotMode(shot));
  const names = new Map((project?.assets || []).map((asset) => [asset.id, asset.name]));
  const cited = shotCitations(shot).map((citation) => `${names.get(citation.asset_id) || citation.asset_id} as its ${ASSET_ROLE_LABELS[citation.role] || citation.role}`);
  const written = promptRejection(shot) ? "" : String(shot.prompt || "").trim();
  return [
    `${shotLabel(project, shot.id)} runs from ${seconds(shot.start)}s to ${seconds(Number(shot.start) + Number(shot.duration))}s, ${seconds(shot.duration)}s long.`,
    `Today it is a ${spec ? spec.label.toLowerCase() : resolveShotMode(shot)} shot${cited.length ? `, citing ${cited.join(" and ")}` : " and cites no assets"}.`,
    written ? `Its prompt reads: ${written}` : "It has no prompt yet.",
    "Make it ",
  ].join(" ");
}

// Why a control is off when it is off. Drawn as a disabled control carrying the reason rather than
// as no control at all -- `renderAgainControl`'s argument -- except for the "no shot selected"
// case, which the frozen matrix says must be "absent or shut, not a silent no-op": shut, with the
// reason, is the version that answers the question instead of leaving the Director hunting.
export const ASSISTANT_WITHOUT_SHOT = "Select a shot on the timeline first.";
export const ASSISTANT_LOCKED =
  "This shot is locked. A lock is a deliberate hands-off, and filling it in is exactly the kind of change it refuses. Unlock the shot first.";
export const ASSISTANT_RENDERED =
  "A render or a take already depends on this shot being what it is, so filling it in would leave the take describing something that never produced it. Edit it yourself in the shot inspector.";
export const ASSISTANT_WITHOUT_WRITABLE_SHOTS =
  "Every shot in this plan is locked or has already been rendered, so there is nothing to fill in.";
// The composer is empty. Refused in the browser rather than sent, because `AssistantRequest.message`
// is `min_length=1` and an empty send would come back as a 422 about a field the Director cannot
// see -- and because an assistant handed no request has nothing to fill the shot in *from*.
export const ASSISTANT_WITHOUT_REQUEST =
  "Type what you want the shot to be first, or press Prefill from shot and finish the sentence.";
// The assistant's half of SHOT_EXPANSION_EDIT_BLOCKED: the same protection, and a different
// sentence because the two writes revert different work. An expansion loses prompts; a fill loses
// modes and citations too, which is worse to have silently reverted and harder to notice.
export const ASSISTANT_EDIT_BLOCKED =
  "Assistant ProducerBot is filling shots in, so this timeline edit was not saved -- a save queued " +
  "now would carry the shots from before the fill and revert it. Make the edit again once the " +
  "fill lands.";

// The claim every assistant control makes about what pressing it costs, in the one spelling the
// expansion control already uses -- a Director deciding whether an assistant button will spend GPU
// minutes must not read two different sentences about it.
export const ASSISTANT_PREFILL_CONTROL = "#prefill-shot";
export const ASSISTANT_FILL_CONTROL = "#assistant-fill";
export const ASSISTANT_FILL_ALL_CONTROL = "#assistant-fill-all";
export const ASSISTANT_PREFILL_LABEL = "Prefill from shot";
export const ASSISTANT_PREFILL_HELP =
  "Fill the composer with the selected shot's context, as text you could have typed yourself. Nothing is sent and nothing changes.";
export const ASSISTANT_FILL_LABEL = "Fill selected shot";
export const ASSISTANT_FILL_HELP =
  `Ask Assistant ProducerBot to fill in the selected shot: its mode, its prompt and the assets it cites. ${SHOT_EXPANSION_NO_RENDER} and no shot window is changed.`;
export const ASSISTANT_FILL_ALL_LABEL = "Fill every open shot";
export const ASSISTANT_FILL_ALL_HELP =
  `Ask Assistant ProducerBot to fill in every shot that is not locked and has not been rendered, judged one at a time. ${SHOT_EXPANSION_NO_RENDER} and no shot window is changed.`;

// Everything the composer draws for one assistant control, decided here rather than in the
// template -- `renderAgainControl`'s reason exactly. `shotIds` is carried out rather than
// re-derived at the click site, so the request is sent for the shots the enabled state was decided
// from and a control that is off cannot be made to send anything.
export function prefillControl(project, shot) {
  if (!shot) return { disabled: true, title: ASSISTANT_WITHOUT_SHOT, text: "" };
  return { disabled: false, title: ASSISTANT_PREFILL_HELP, text: shotPrefillText(project, shot) };
}

export function assistantControl(project, shot) {
  if (!shot) return { disabled: true, title: ASSISTANT_WITHOUT_SHOT, shotIds: [] };
  const refusal = shotWriteRefusal(shot);
  if (refusal) return { disabled: true, title: refusal === "locked" ? ASSISTANT_LOCKED : ASSISTANT_RENDERED, shotIds: [] };
  return { disabled: false, title: ASSISTANT_FILL_HELP, shotIds: [shot.id] };
}

// The bulk fill's own control. It sends the shots the server would accept and no others, so a plan
// of thirty shots with four locked ones sends twenty-six -- rather than sending thirty and reading
// four refusals back, which is the same outcome after a model call instead of before one.
export function assistantFillAllControl(project) {
  const shotIds = (project?.shots || []).filter((shot) => !shotWriteRefusal(shot)).map((shot) => shot.id);
  if (!shotIds.length) return { disabled: true, title: ASSISTANT_WITHOUT_WRITABLE_SHOTS, shotIds: [] };
  return { disabled: false, title: ASSISTANT_FILL_ALL_HELP, shotIds };
}

// What one assistant turn did, taken from the reply rather than from a diff of the shots --
// `shotExpansionToast`'s argument, and it is stronger here: a re-fill that lands the same mode and
// the same citations is indistinguishable from a turn where every call was refused, and the toast
// is the loudest thing on screen.
//
// Keyed on the server's own ASSISTANT_APPLIED_NOTICE, with the `shot(s):` tail part of the match on
// purpose: the model's own prose sits above the notices in the same message and could otherwise
// supply the marker itself. A contract test reads the count back out of a real formatted notice.
export const ASSISTANT_APPLIED_MARKER = "Assistant ProducerBot filled in";
const ASSISTANT_APPLIED_PATTERN = new RegExp(`${ASSISTANT_APPLIED_MARKER} (\\d+) shot\\(s\\):`);
export const ASSISTANT_TOAST =
  `Assistant ProducerBot filled in {count} shot{plural}. ${SHOT_EXPANSION_NO_RENDER}; open a shot to generate an image for it.`;
export const ASSISTANT_UNCHANGED_TOAST =
  "No shot was changed. The reply beside the composer says, per shot, what the assistant returned and why none of it was applied.";

export function assistantFilled(project) {
  const match = ASSISTANT_APPLIED_PATTERN.exec(assistantReply(project) ?? "");
  return match ? Number(match[1]) : 0;
}

export function assistantToast(project) {
  const filled = assistantFilled(project);
  if (!filled) return ASSISTANT_UNCHANGED_TOAST;
  return ASSISTANT_TOAST.replace("{count}", filled).replace("{plural}", filled === 1 ? "" : "s");
}

// ---------------------------------------------------------------------------------------------
// The H3 expansion specialist, pass two
// ---------------------------------------------------------------------------------------------

// The Director's own words for what this control is: "an 'Expand Prompt' button in the text section
// of the shots for if they want to edit a shots individual prompt". It sits under the creative
// intent it expands, because that is the thing it reads and the thing it must be seen not to
// overwrite.
export const EXPAND_PROMPT_CONTROL = "#expand-prompt";
export const EXPAND_PROMPT_LABEL = "Expand prompt";
// A different label when there is already an expansion, decided here rather than by the template.
// "Expand prompt" over a shot that already has one reads as an offer to add a second, and the
// matrix's re-expansion row says plainly that it replaces: the label is where that is said before
// the click.
export const EXPAND_PROMPT_AGAIN_LABEL = "Expand prompt again";
export const EXPAND_PROMPT_HELP =
  "Turn this shot's creative intent into MiniMax H3's structured prompt format, in one call to the " +
  `expansion specialist. The creative intent is not overwritten. ${SHOT_EXPANSION_NO_RENDER} and no shot window is changed.`;
export const EXPAND_PROMPT_AGAIN_HELP =
  "Write this shot's H3 prompt again from its creative intent, replacing the one it has. The " +
  `creative intent is not overwritten, so this is repeatable. ${SHOT_EXPANSION_NO_RENDER} and no shot window is changed.`;
// Why the control is off, in the browser's voice. Mirrors app.py's EXPAND_PROMPT_LOCKED,
// EXPAND_PROMPT_RENDERED and EXPAND_PROMPT_WITHOUT_INTENT in *substance* rather than word for word,
// on `assistantControl`'s precedent: those are `{shot}`-templated sentences written for a refusal
// the Director has already clicked into, and these are hover text on a control that names its own
// shot by sitting inside its panel.
export const EXPAND_PROMPT_LOCKED =
  "This shot is locked. A lock is a deliberate hands-off, and rewriting its prompt is exactly the kind of change it refuses. Unlock the shot first.";
export const EXPAND_PROMPT_RENDERED =
  "This shot has already rendered, so its prompt is the record of what produced a take rather than an intention. Use Render again if you want a different take.";
export const EXPAND_PROMPT_WITHOUT_INTENT =
  "Write the creative intent above first. This pass turns an intent into the H3 format; it does not invent one, which is what the Director's shot expansion is for.";

// The one decision behind the per-shot control: may this shot be expanded, and with which words.
// A pure function rather than an expression inside the inspector's template, on `markReadyControl`'s
// precedent, so every state can be executed by a test instead of read.
//
// The refusal order is the server's, and it is not cosmetic: `shot_write_refusal` before the prompt
// gate, so a locked shot with no intent hears that it is locked rather than being sent to write an
// intent that would then be refused anyway. Phase one pinned that order with its own test; this is
// the same rule, one screen earlier.
export function expandPromptControl(shot) {
  if (!shot) return { shown: false, disabled: true, label: EXPAND_PROMPT_LABEL, title: "", reason: "" };
  const expanded = Boolean(String(shot.h3_prompt || "").trim());
  const label = expanded ? EXPAND_PROMPT_AGAIN_LABEL : EXPAND_PROMPT_LABEL;
  // The prose exemption, mirroring app.py's expansion_write_refusal: a song-audio
  // reference shot's expansion is deterministic prose derived from the intent, so
  // re-deriving it after a render loses nothing — the prompt each take rendered from is
  // on its job and in the take's own metadata. Without this, a fully-rendered plan could
  // never carry an intent edit into its prompt again (the Director's live break,
  // 2026-08-20).
  let refusal = shotWriteRefusal(shot);
  if (refusal === "rendered" && shot.use_song_audio && resolveShotMode(shot) === "references") {
    refusal = null;
  }
  if (refusal) {
    const reason = refusal === "locked" ? EXPAND_PROMPT_LOCKED : EXPAND_PROMPT_RENDERED;
    return { shown: true, disabled: true, label, title: reason, reason };
  }
  if (promptIsMissing(shot)) {
    return { shown: true, disabled: true, label, title: EXPAND_PROMPT_WITHOUT_INTENT, reason: EXPAND_PROMPT_WITHOUT_INTENT };
  }
  return { shown: true, disabled: false, label, title: expanded ? EXPAND_PROMPT_AGAIN_HELP : EXPAND_PROMPT_HELP, reason: "" };
}

// The plan-wide sweep's control. Off when there is nothing to sweep, and never off for a plan whose
// shots are merely locked or unprompted: unlike `assistantFillAllControl`, this route sends no
// selection at all, so filtering here would be this client deciding what the report says about
// shots it never mentioned.
export const EXPAND_ALL_PROMPTS_CONTROL = "#expand-h3-prompts";
export const EXPAND_ALL_PROMPTS_LABEL = "Expand into H3 prompts";
export const EXPAND_ALL_PROMPTS_HELP =
  "Run the H3 expansion specialist once for every shot in the plan, one call per shot, each judged " +
  `on its own. Creative intents are not overwritten and a malformed answer is reported rather than saved. ${SHOT_EXPANSION_NO_RENDER}.`;
// The whole-plan refusal, mirroring app.py's EXPAND_PROMPTS_WITHOUT_SHOTS. A contract test asserts
// the two are identical: two hand-written wordings for one rule is how the pre-emptive toast starts
// describing a rule the server no longer has.
export const EXPAND_ALL_PROMPTS_WITHOUT_SHOTS =
  "This project has no shots to expand into H3 prompts. Expansion writes onto shots that already " +
  "exist and never creates one, so add shots to the timeline first.";

export function expandAllPromptsControl(project) {
  const shots = project?.shots || [];
  if (!shots.length) return { disabled: true, title: EXPAND_ALL_PROMPTS_WITHOUT_SHOTS };
  return { disabled: false, title: EXPAND_ALL_PROMPTS_HELP };
}

// What one sweep did, read out of the reply rather than diffed off the shots -- `shotExpansionToast`'s
// argument, and stronger here because `h3_prompt` is not drawn on the timeline at all, so a diff
// would have nothing on screen to be checked against.
export const EXPAND_ALL_PROMPTS_MARKER = "H3 prompts written for";
const EXPAND_ALL_PROMPTS_PATTERN = new RegExp(`${EXPAND_ALL_PROMPTS_MARKER} (\\d+) shot\\(s\\):`);
export const EXPAND_ALL_PROMPTS_TOAST =
  `{count} H3 prompt{plural} written by this sweep. ${SHOT_EXPANSION_NO_RENDER}, and the reply says per shot what happened to the rest.`;
export const EXPAND_ALL_PROMPTS_UNCHANGED_TOAST =
  "No H3 prompt was written; the reply says, per shot, what the specialist returned and why none of it was saved.";

export function expandAllPromptsWritten(project) {
  const match = EXPAND_ALL_PROMPTS_PATTERN.exec(assistantReply(project) ?? "");
  return match ? Number(match[1]) : 0;
}

export function expandAllPromptsToast(project) {
  const written = expandAllPromptsWritten(project);
  if (!written) return EXPAND_ALL_PROMPTS_UNCHANGED_TOAST;
  return EXPAND_ALL_PROMPTS_TOAST.replace("{count}", written).replace("{plural}", written === 1 ? "" : "s");
}

//: The control the Director asked for "up by where the Cuts and Snap Cuts stuff are" (2026-08-21).
//: A second affordance for `expand_plan_prompts`, not a second feature: it shares the route, the
//: label, the help and the refusal with `EXPAND_ALL_PROMPTS_CONTROL` in the Director workspace.
//: Its own id because two elements may not carry one id, and a control the timeline draws must be
//: addressable from the timeline.
export const EXPAND_ALL_PROMPTS_TIMELINE_CONTROL = "#timeline-expand-prompts";
export const EXPAND_ALL_PROMPTS_TIMELINE_LABEL = "Expand All Prompts";
export const EXPAND_ALL_PROMPTS_RUNNING = "Expanding…";
//: What the click is about to cost, in the shape every expensive bulk action in this interface
//: states it: what it does, to how many shots, and what it does not touch.
export const EXPAND_ALL_PROMPTS_CONFIRM =
  "Expand all {count} shot(s) into H3 prompts? That is one model call per shot and takes a " +
  "while. No creative intent is overwritten, but the whole project comes back, so the editors " +
  "are re-rendered from the text stored on the server.";

// The sweep's own per-shot report, for the bar that raised it. The route answers with the whole
// project and attaches one notice per group -- what was written, what was locked, what carried
// render provenance, what was refused -- and those notices are the half a Director has to be able
// to read. They live in the Director thread two panels away, which is nowhere near the button
// this control puts on the timeline, so they are drawn beside it as well.
//
// Read from the reply's own `notices` field through `messageParts`, never recovered from the text
// -- the same rule the thread follows, and for the same reason: the model's own output can
// legitimately contain the separator.
export function expansionSweepLines(project) {
  const messages = project?.messages;
  if (!Array.isArray(messages)) return [];
  const reply = messages.filter((message) => message?.role === "assistant").at(-1);
  if (!reply) return [];
  return messageParts(reply).notices.map((notice) => ({ kind: notice.kind, text: notice.text }));
}

// What one per-shot expansion did, taken from the route's own `applied` flag rather than inferred.
// The malformed case is deliberately not summarised into "something went wrong": the checker's
// sentences are the actionable half, and they are shown in the panel by `expansionReportHtml` --
// this is only the line that says which of the two happened.
export const EXPAND_PROMPT_APPLIED_TOAST =
  `H3 prompt written for {shot}. Its creative intent is unchanged. ${SHOT_EXPANSION_NO_RENDER}.`;
export const EXPAND_PROMPT_MALFORMED_TOAST =
  "{shot} was NOT changed: the answer is not a well-formed H3 prompt. What came back, and what is wrong with it, is below the creative intent.";

export function expandPromptToast(result, label) {
  const template = result?.applied ? EXPAND_PROMPT_APPLIED_TOAST : EXPAND_PROMPT_MALFORMED_TOAST;
  return template.replace("{shot}", label);
}

// The malformed answer, rendered where the Director can act on it. A toast carrying five checker
// sentences and a thousand characters of returned prompt is a toast nobody reads, and the report is
// the whole reason a refused answer is returned at all rather than dropped.
//
// Keyed to the shot it is about, so a report cannot outlive its selection: switching shots and
// finding the previous shot's failure under this one's intent would be a straightforwardly false
// claim about the panel it is drawn in.
export const EXPANSION_REPORT_TITLE = "The last answer was not saved";

export function expansionReport(report, shot) {
  if (!report || !shot || report.shotId !== shot.id) return { shown: false, title: "", problems: [], prompt: "" };
  return {
    shown: true,
    title: EXPANSION_REPORT_TITLE,
    problems: report.problems || [],
    prompt: report.prompt || "",
  };
}

// What each direction did, mirroring app.py's MARK_READY_NOTICE and MARK_DRAFT_NOTICE so the
// sentence the Director reads is the one the server implements. A contract test asserts they are
// identical.
//
// Said on every success rather than left to the status chip, because the belief this prevents --
// that committing a shot started a render -- is exactly the belief a silent state change leaves in
// place, and the next thing the Director does depends on which of the two they think happened.
export const MARK_READY_NOTICE =
  "{shot} is committed to the render queue. Nothing has been rendered and no GPU time has been " +
  "spent by this: the queue submits it when you choose to.";
export const MARK_DRAFT_NOTICE =
  "{shot} is back to draft, so the render queue will not submit it. Nothing else about the shot " +
  "changed and nothing was deleted.";

export function markReadyNotice(project, shotId, action) {
  const template = action === "draft" ? MARK_DRAFT_NOTICE : MARK_READY_NOTICE;
  return template.replace("{shot}", shotLabel(project, shotId));
}

// What each approval direction did, mirroring app.py's APPROVE_NOTICE and UNAPPROVE_NOTICE so
// the sentence the Director reads is the one the server implements. A contract test asserts they
// are identical.
//
// Said on every success rather than left to the status chip, because each direction carries a
// consequence the chip does not show: approving takes re-rendering away, and un-approving has to
// say nothing was deleted or a Director unsure of the cost will leave a wrong approval standing.
export const APPROVE_NOTICE =
  "{shot}'s latest take is approved. The approval names that exact file, so the shot cannot " +
  "be re-rendered or re-queued while it stands. Un-approve it if the decision changes.";
export const UNAPPROVE_NOTICE =
  "{shot}'s approval is cleared and the shot is back to complete, so it can be re-opened and " +
  "rendered again. Nothing was deleted: the take is still this shot's latest output.";

export function approvalNotice(project, shotId, action) {
  const template = action === "unapprove" ? UNAPPROVE_NOTICE : APPROVE_NOTICE;
  return template.replace("{shot}", shotLabel(project, shotId));
}

// Where one Shot's latest take streams from: ids only, per the route's own contract -- the
// server resolves the file from its manifest, so no path travels in this URL. The take pointer
// rides along as a cache key: the URL would otherwise be identical before and after a re-render,
// and the browser would keep playing the take the approval no longer describes.
export function shotTakeUrl(projectId, shotId, latestOutput) {
  return `/api/projects/${projectId}/shots/${shotId}/take?v=${encodeURIComponent(latestOutput || "")}`;
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

// The VRAM eject control. It describes *this machine* — one card shared by LM Studio and
// ComfyUI — not the video, which is why it lives in the topbar's system state beside ComfyUI's
// own status rather than in any workspace, and why nothing about it is ever written into a
// project manifest. A shared project carrying "do not eject" would silently change how someone
// else's renders behave.
//
// Deliberately *not* the `apply_documents` pattern. That control is consent for one turn and is
// cleared after every send and every project load, because remembering it would apply consent
// the Director never gave again. This is the opposite case: it is a standing property of the
// machine, so it is remembered — on the server, in `machine-preferences.json` — and a project
// load refreshes it rather than clearing it.
export const VRAM_EJECT_CONTROL = "#vram-eject";
export const VRAM_EJECT_NOTE = "#vram-eject-note";
export const VRAM_EJECT_LABEL = "Eject LLM";

// What the last attempt did, one sentence per `vram.EjectStatus`. `{before}` and `{after}` are
// filled from the host's own residency listing and from nothing else.
//
// **There is no VRAM figure here, and adding one would be a defect.** Measured on 2026-08-18, the
// free-VRAM reading fell 31.6 → 16.0 GB across one eject of a 4.71 GB model, because ComfyUI
// released its own cache at the same moment: the number is confounded and attributes ComfyUI's
// behaviour to us. Which models were resident and whether they are gone is directly observed, and
// is the honest version of the same reassurance.
export const VRAM_EJECT_LAST = {
  "disabled": "Last render: no eject was attempted.",
  "not-configured": "Last render: no language-model host is configured.",
  "director-busy": "Last render: skipped, a Director call was in flight.",
  "host-unreachable": "Last render: the language-model host did not answer.",
  "nothing-loaded": "Last render: no model was resident.",
  "released": "Last render: released {before}.",
  "still-resident": "Last render: {after} did not go. The render was submitted anyway.",
  "mechanism-absent": "Last render: no `lms` CLI to eject with. The render was submitted anyway.",
  "mechanism-failed": "Last render: the eject failed. The render was submitted anyway.",
  "timed-out": "Last render: the eject timed out. The render was submitted anyway.",
  "unreadable": "Last render: the release could not be confirmed. The render was submitted anyway.",
};

export const VRAM_EJECT_UNKNOWN = "Eject state unknown — the application has not answered yet.";
export const VRAM_EJECT_IDLE_ON = "On. Nothing has been submitted yet.";
export const VRAM_EJECT_IDLE_OFF = "Off. No eject before a render.";

// Why the setting is what it is, and what changing it here will and will not survive.
export const VRAM_EJECT_SOURCES = {
  environment: "MVP_LLM_EJECT_BEFORE_RENDER set this when the application started. Changing it here applies from the next submission, but the environment decides again at the next start.",
  director: "You chose this. It is remembered on this machine and is never stored in a project.",
  default: "The built-in default. Changing it here is remembered on this machine and is never stored in a project.",
};

// Whether the server has actually said anything about the eject. Distinguished from "off" on
// purpose: a control drawn unticked because a GET failed would be a machine-wide setting reported
// as off while every render still ejects, which is the exact lie this feature exists to remove.
export function vramEjectAvailable(status) {
  return typeof status?.enabled === "boolean";
}

// Ticked only when the server says the eject is on. Never a hardcoded default and never the last
// thing the Director clicked: the server owns this value, including when the environment pinned it
// to something other than the default.
export function vramEjectChecked(status) {
  return status?.enabled === true;
}

export function vramEjectNote(status) {
  if (!vramEjectAvailable(status)) return VRAM_EJECT_UNKNOWN;
  const last = status.last;
  if (!last) return status.enabled ? VRAM_EJECT_IDLE_ON : VRAM_EJECT_IDLE_OFF;
  const names = (values) => (Array.isArray(values) && values.length ? values.join(", ") : "the model");
  const sentence = VRAM_EJECT_LAST[last.status];
  // An unrecognised status is named rather than dressed up as one of the known ones. A new
  // `EjectStatus` the client has not learned about must not be reported as a success.
  if (!sentence) return `Last render: the eject ended as "${last.status}". The render was submitted anyway.`;
  return sentence
    .replace("{before}", names(last.resident_before))
    .replace("{after}", names(last.resident_after));
}

// The hover text: where the setting came from, plus the host's own words about the last attempt.
// The detail is quoted, never summarised into a number.
export function vramEjectTitle(status) {
  if (!vramEjectAvailable(status)) return VRAM_EJECT_UNKNOWN;
  const source = VRAM_EJECT_SOURCES[status.source] || "";
  const detail = status.last?.detail || "";
  return [source, detail].filter(Boolean).join("\n\n");
}

// What just changed, said in terms of renders rather than of a checkbox.
export function vramEjectToast(status) {
  return vramEjectChecked(status)
    ? "The language model will be released before each render."
    : "The language model will be left loaded. Renders share the card with it.";
}

// The default prompt a character is promoted with. Word for word what it has always been,
// less the two characters of "four-panel ": a probe asked the QuadView LoRA for four views
// and it returned six, so that phrase was a claim about the output rather than a request
// for it. Everything the sentence *asks* for is untouched — the same identity clause, the
// same four named views, the same lighting and proportion constraint — because sheets an
// existing Director already has must not come back as a different person.
export const MULTIVIEW_CHARACTER_PROMPT = `Preserve the exact identity, facial features, body type and wardrobe of this character. Convert the character into a clean character sheet showing a face close-up, front full body, side full body and back full body view. Consistent neutral lighting and proportions across every view.`;

// Its own template, not the character one with the nouns swapped. An object has no face,
// no wardrobe and no front/back asymmetry to hold, and the things that do have to hold —
// silhouette, materials, livery, the scale of one part against another — have no wording in
// the character sentence at all. The views are the ones the ship probe asked for and got.
export const MULTIVIEW_OBJECT_PROMPT = `Preserve the exact design, silhouette, proportions, materials, colour and surface markings of this object. Convert the object into a clean reference sheet showing a front view, a three-quarter view, a side view and a rear view of the whole object. Consistent neutral lighting, consistent scale and identical detailing across every view, against a plain neutral background.`;

// Which Asset kinds can be promoted, and what each is promoted with. `app.py`'s
// MULTIVIEW_SUBJECTS is the route half and holds the same kinds; a contract test runs both
// and fails if they drift, because the two failure modes — a button whose route refuses it,
// and a promotable kind with no button — are both invisible from either side alone.
//
// Neither template names a number of panels or views, and nothing that reads them counts:
// the sheet is handed to Krea whole and comes back whole.
export const MULTIVIEW_SUBJECTS = {
  character: MULTIVIEW_CHARACTER_PROMPT,
  prop: MULTIVIEW_OBJECT_PROMPT,
  setting: MULTIVIEW_OBJECT_PROMPT,
};

// The one decision behind the promote control: may this asset be promoted, is it ready to
// be, and with what. Returns null for a kind the feature does not cover, so the inspector
// omits the button entirely rather than offering one the route answers with a 422.
//
// A pure function rather than an expression inside the inspector's template string, so the
// rule can be executed by a test for every kind an Asset can carry instead of read.
export function multiviewPlan(asset) {
  const prompt = asset && MULTIVIEW_SUBJECTS[asset.kind];
  if (!prompt) return null;
  // `path` is empty until the source render lands. The button is shown but shut, which says
  // "this can be promoted, once it exists" — omitting it would say the wrong thing.
  return { prompt, ready: Boolean(asset.path) };
}

// -- The Assets panel's subtabs (the Director's ask, 2026-08-20) -------------------------------
//
// "in Assets the generated clips are eating up all the room and hiding the sorted asset sections
// and should be their own subtab along with CHaracters/Settings/Props/Style."
//
// The panel's filter strip becomes the panel's tab strip: one tab owns the library area at a time,
// so the clips can no longer push the sorted sections off the bottom of it.
//
// `kinds` is the set of `models.AssetKind` values a tab shows. `null` means every kind — the All
// tab, kept because it is the strip's existing behaviour and the only view that sorts a whole
// library by nothing. `models.AssetKind` has SEVEN members and the Director named four, so the
// three unnamed ones (`image`, `audio`, `video`) get the Media tab rather than being dropped:
// `image` is what every upload defaults to, and an asset that appears under no tab is one a
// Director can neither cite, replace nor delete from this screen. A contract test asserts the
// union of the kinds below is exactly `models.AssetKind`, so a kind added later cannot go
// invisible by omission.
//
// `clips` carries no kinds at all and is not an asset view: it is the take library, drawn from job
// history, and it is on this strip because that is where the Director asked for it.
export const ASSET_TABS = [
  { id: "all", label: "All", kinds: null },
  { id: "character", label: "Characters", kinds: ["character"], noun: "characters", singular: "Character" },
  { id: "setting", label: "Settings", kinds: ["setting"], noun: "settings", singular: "Setting" },
  { id: "prop", label: "Props", kinds: ["prop"], noun: "props", singular: "Prop" },
  { id: "style", label: "Style", kinds: ["style"], noun: "style references", singular: "Style reference" },
  {
    id: "media",
    label: "Media",
    kinds: ["image", "audio", "video"],
    title:
      "Uploaded and modified images, audio and video — every asset kind without a tab of its " +
      "own. An upload with no type chosen lands here.",
  },
  {
    id: "clips",
    label: "Clips",
    kinds: [],
    title: "Every completed H3 take this project has rendered. Not assets — takes.",
  },
];

//: The tab a Director lands on, and the fallback for a stored value no tab answers to.
export const ASSET_TAB_DEFAULT = "all";

export function assetTab(tabId) {
  return ASSET_TABS.find((tab) => tab.id === tabId)
    || ASSET_TABS.find((tab) => tab.id === ASSET_TAB_DEFAULT);
}

// Which assets this tab shows, after the search box. One function so the grid and the tab's own
// empty message cannot disagree about whether there is anything to draw.
export function assetsForTab(assets, tabId, query = "") {
  const tab = assetTab(tabId);
  const wanted = String(query || "").trim().toLowerCase();
  return (assets || []).filter((asset) => {
    if (tab.kinds !== null && !tab.kinds.includes(asset?.kind)) return false;
    return !wanted || String(asset?.name || "").toLowerCase().includes(wanted);
  });
}

// What an empty tab says. Every tab says something specific, because "No matching assets" under a
// tab a Director deliberately opened reads as a broken panel rather than as an honest count — and
// the Clips tab, which is not an asset view at all, would be describing the wrong thing entirely.
export function assetTabEmpty(tabId, query = "") {
  const tab = assetTab(tabId);
  const wanted = String(query || "").trim();
  if (wanted && tab.id !== "clips") {
    return {
      title: `No ${tab.noun || "assets"} match “${wanted}”`,
      hint: "Clear the search box to see everything on this tab.",
    };
  }
  if (tab.id === "clips") {
    return {
      title: "No clips yet",
      hint:
        "Every completed H3 take in this project appears here, newest first. Render a shot and "
        + "its take lands on this tab.",
    };
  }
  if (tab.id === "all") {
    return {
      title: "No assets yet",
      hint: "Generate a character or setting with Flux, or upload existing media.",
    };
  }
  if (tab.id === "media") {
    return {
      title: "No media yet",
      hint: "Uploaded images, audio and video land here — as does anything uploaded without a type.",
    };
  }
  return {
    title: `No ${tab.noun} yet`,
    hint: `Generate one with Flux, or upload existing media and set its type to ${tab.singular}.`,
  };
}

// -- The Clips tab when ComfyUI is not running ------------------------------------------------
//
// The Director's report (2026-08-21): the clips library "goes blank" whenever ComfyUI is down.
// Each card points a `<video>` straight at ComfyUI's own `/view`, so every one of them 404s --
// thirty-three broken video elements on a tab that exists to show thirty-three takes.
//
// **ComfyUI being down is an ordinary state here, not an error.** The Director starts it
// separately and this application is forbidden from starting it. So the honest thing is a card
// that says the take cannot be shown and why, and this decides that once for the whole tab.
//
// **Which takes this application can serve itself, and which it cannot.** Investigated before
// this was written, because "fail honestly" is worth less than "do not fail":
//
// * `GET /api/projects/{id}/media/{path}` cannot reach a take. An H3 take's `output_files` entry
//   is a path inside **ComfyUI's own output tree** (`music-video-producer/{project}/shots/…`),
//   and that route serves `store.media_dir(project_id)` and 404s anything outside it.
// * `GET /api/projects/{id}/shots/{shot}/take` **can**, and does — it is what the Monitor plays.
//   It resolves the shot's own `latest_output` under `settings.comfy_root / "output"` on disk and
//   streams it with Range support. No ComfyUI process is involved at any point.
//
// The second route takes ids and deliberately **no path** ("there is no path-injection surface to
// defend"), so it can serve exactly one take per shot: the current one. Every earlier take in the
// library is addressable only by its path, and the only thing in this system that serves a path is
// ComfyUI's `/view`.
//
// So a card decides for itself (`clipCardFace`), and this decides the tab-level question the cards
// that cannot be served ask: is ComfyUI answering, and what is said when it is not.
//
// Three answers, not two, because "we have not asked yet" and "we asked and it said no" are
// different facts and only one of them is about ComfyUI:
//
// * `online`   — health says ComfyUI answered. Play the take from `/view`, exactly as before.
// * `offline`  — health says it did not answer. Name that, and name the address that was tried.
// * `unknown`  — this browser has no health answer at all (the first paint, or the probe failed).
//                Saying "ComfyUI is offline" there would be inventing a fact about someone
//                else's process.
export const CLIP_OFFLINE_TITLE = "Earlier take — ComfyUI offline";
export const CLIP_UNKNOWN_TITLE = "Earlier take — ComfyUI not checked yet";
export const CLIP_OFFLINE_NOTE =
  "The take each shot currently points at is served by this application from ComfyUI's output " +
  "directory on disk, so those still play. An earlier take is addressable only by its path, and " +
  "the only thing that serves a path is ComfyUI's own /view at {url} — which is not answering. " +
  "The list itself is read from this project's job history and is complete either way.";
export const CLIP_UNKNOWN_NOTE =
  "The take each shot currently points at is served by this application from ComfyUI's output " +
  "directory on disk, so those still play. An earlier take needs ComfyUI's own /view, and this " +
  "browser has no answer from ComfyUI yet. Re-check to ask. The list itself is read from this " +
  "project's job history and is complete either way.";
export const CLIP_RECHECK_LABEL = "Re-check ComfyUI";

//: The address the cards are pointed at when health has not said which one it is.
export const COMFY_DEFAULT_URL = "http://127.0.0.1:8188";

// Whether the Clips tab may draw video elements at all, and what it says when it may not.
//
// `health` is `state.health` -- the whole answer, so the three cases above stay distinguishable;
// a caller passing only a boolean cannot tell "false" from "never asked".
export function clipPreviewState(health) {
  const url = health?.comfy?.url || COMFY_DEFAULT_URL;
  if (health?.comfy?.online === true) return { playable: true, status: "online", url, title: "", note: "" };
  if (health?.comfy?.online === false) {
    return {
      playable: false,
      status: "offline",
      url,
      title: CLIP_OFFLINE_TITLE,
      note: CLIP_OFFLINE_NOTE.replace("{url}", url),
    };
  }
  return {
    playable: false,
    status: "unknown",
    url,
    title: CLIP_UNKNOWN_TITLE,
    note: CLIP_UNKNOWN_NOTE,
  };
}

// One card's own answer: where its picture comes from, or why there is not one.
//
// `row` is a `clipLibraryRows` entry -- `{file, shotId}` -- and `preview` is the tab-level state
// above. Three outcomes, in the order a card prefers them:
//
// * `via: "app"`   — this file is the shot's current take, so the application serves it from disk
//                    and ComfyUI's state is irrelevant. This is the case that makes the tab work
//                    at all with ComfyUI down, and it is most of the tab: a shot's newest take is
//                    the one it points at.
// * `via: "comfy"` — an earlier take, and ComfyUI is answering, so `/view` serves it by path.
// * `via: ""`      — an earlier take with no ComfyUI. The card says which, and says nothing it
//                    cannot support.
export function clipCardFace(project, row, preview) {
  const shot = (project?.shots || []).find((item) => item?.id === row?.shotId) || null;
  if (shot && row?.file && shot.latest_output === row.file) {
    return {
      playable: true,
      via: "app",
      url: shotTakeUrl(project.id, shot.id, shot.latest_output),
      title: "",
    };
  }
  if (preview?.playable) {
    return { playable: true, via: "comfy", url: comfyOutputUrl(preview.url, row.file), title: "" };
  }
  return { playable: false, via: "", url: "", title: preview?.title || CLIP_UNKNOWN_TITLE };
}

// Section boxes snap to the edges of the shots below them (the Director's design:
// a section spans whole shots, so its boundaries ARE shot boundaries when any are near).
// Pure so the contract tests can execute the rule: the nearest boundary within
// `tolerance` seconds wins; nothing near leaves the value free.
export function snapSeconds(value, boundaries, tolerance = 0.4) {
  let best = null;
  for (const boundary of boundaries || []) {
    const distance = Math.abs(boundary - value);
    if (distance <= tolerance && (best === null || distance < Math.abs(best - value))) {
      best = boundary;
    }
  }
  return best === null ? value : best;
}

// Every edge a section box may snap to: the song's ends and every shot boundary.
export function shotBoundaries(project) {
  const edges = new Set([0]);
  if (project?.song?.duration) edges.add(Math.round(project.song.duration * 1000) / 1000);
  for (const shot of project?.shots || []) {
    edges.add(Math.round(shot.start * 1000) / 1000);
    edges.add(Math.round((shot.start + shot.duration) * 1000) / 1000);
  }
  return [...edges].sort((a, b) => a - b);
}

// ------------------------------------------------------------------------------------------
// Fill section looks. The Director's report (2026-08-20): a Section clicked in the timeline
// had an empty shared prompt, "nothing transferred the appropriate section descriptions into
// the actual sections once they were populated on the timeline". The route reads them out of
// the Treatment and the Style Bible; these are the words the button and its confirm use.
// ------------------------------------------------------------------------------------------

export const FILL_SECTION_LOOKS_LABEL = "Fill looks from Treatment";
export const FILL_SECTION_LOOKS_RUNNING = "Reading the treatment…";
export const FILL_SECTION_LOOKS_HELP =
  "Read every section's shared look out of the Treatment and the Style Bible. Shows what " +
  "it would write before writing it; a look you wrote yourself is never replaced without " +
  "you saying so.";
export const FILL_SECTION_LOOKS_OVERWRITE_QUESTION =
  "Also replace the section looks you wrote yourself?";
export const FILL_SECTION_LOOKS_APPLIED = "{filled} section look(s) written, {skipped} left alone.";

// The report as one confirm question, in the server's own sentences. Every section is a line
// and no line is summarised away -- `snapCutsReportLines`' rule, and for its reason: the
// section that was skipped, and why, is exactly the one the Director needs to see, because
// "the treatment does not describe this section" is a sentence that sends them back to the
// treatment rather than leaving a box mysteriously blank.
export function sectionLooksReportLines(report) {
  return (report?.sections || []).map((row) => {
    const at = `${Number(row.start).toFixed(1)}s ${row.label}`;
    const proposal = String(row.prompt || "");
    const previous = String(row.previous || "");
    // What a look would replace, said beside what it would write, wherever both exist. A look
    // the Director wrote themselves is the one thing this pass can destroy, and the overwrite
    // consent is only a real question while the words it takes away are on screen next to the
    // words it puts there -- "6 filled, 1 left alone" is not a sentence anybody can agree to.
    if (row.filled) {
      return { kind: "fill", text: previous
        ? `${at}: ${proposal} — replaces what you wrote: “${previous}”`
        : `${at}: ${proposal}` };
    }
    // A skipped row carries the look it *would* have written whenever the only thing stopping it
    // is a consent: the route puts it on the row so the report can show what saying yes buys.
    // And a section that was skipped because it is already written says what is already there,
    // because "already has a look you wrote" names no words at all.
    let text = `${at}: skipped — ${row.reason}`;
    if (proposal) text += ` — it would write: “${proposal}”`;
    if (previous) text += ` — yours now: “${previous}”`;
    return { kind: "skip", text };
  });
}

export function sectionLooksConfirmation(report) {
  if (!report) return "";
  const lines = sectionLooksReportLines(report).map((line) => line.text);
  const head = report.message ? [report.message, ""] : [];
  return [...head, ...lines, "", "Write these looks?"].join("\n");
}

// Whether this report holds a look the Director wrote themselves that the pass would replace --
// the one question `FILL_SECTION_LOOKS_OVERWRITE_QUESTION` asks.
//
// Derived rather than read off `filled`, and that is the whole point. A structure where *every*
// section already carries a look short-circuits server-side to `0 filled` **without a model
// call**, which is exactly the state in which the consent is worth asking for: a client that
// treated "nothing was filled" as an error could never ask it, and the sentence the route
// answers with -- "send overwrite=true to replace what is there" -- would describe something the
// screen could not do.
export function sectionLooksWritten(report) {
  return (report?.sections || []).some((row) => row.previous && !row.filled);
}

// AI Mod (the Director's stage-3 ask): whether this asset can take a prompted image edit.
// Any image-kinded asset qualifies — a character, a setting, a prop, a style frame, an
// already-edited child (edits chain) — and only audio/video media cannot. Same shown/ready
// split as multiviewPlan and for its reason: an asset still rendering can be modded, once
// it exists.
export function aiModPlan(asset) {
  if (!asset || ["audio", "video"].includes(asset.kind)) return null;
  return { ready: Boolean(asset.path) };
}

// ------------------------------------------------------------------------------------------
// The appearance anchor (`Asset.consistency_prompt`). One user-owned phrase per asset that
// wins over the generation prompt and the vision summary everywhere a description of that
// asset is consumed -- the reference map's tag lines, the H3 specialist's per-reference block,
// the assistant's library. Written only by `PUT .../consistency-prompt`; nothing infers one.
// ------------------------------------------------------------------------------------------

// `app.CONSISTENCY_PROMPT_LIMIT`. A contract test holds the two together, for the reason the
// song-context bound already learned the hard way: a client that shortens a paste silently and
// a route that refuses the same text with a 422 are two different rules wearing one number.
export const CONSISTENCY_PROMPT_LIMIT = 400;

export const CONSISTENCY_PROMPT_LABEL = "Appearance anchor";

// Said on screen because the field is unlike every other box in this inspector: it is the one
// text the Director writes that OUTRANKS what the machine produced, and it reaches prompts the
// Director never opens. A box whose only explanation is its name gets filled in with a second
// generation prompt.
export const CONSISTENCY_PROMPT_HELP =
  "Your words for what this looks like — carried into every prompt that cites it, and it wins " +
  "over the generation prompt and the vision summary. Short is better: \"a woman in a red " +
  "leather jacket and black boots\". Leave it empty and nothing is added anywhere.";

// The one decision behind the anchor editor: may this asset carry one, what does it hold, and
// can what is in the box be saved.
//
// Offered for every kind except `audio`, and that exclusion is the whole rule rather than a
// list of blessed kinds: an anchor says what something LOOKS like, a sound has no appearance,
// and every other kind -- character, setting, prop, style, image, video -- reaches a prompt as
// a picture or a clip whose tag line the anchor rides. A list of allowed kinds would silently
// omit whichever kind is added next; the exclusion fails the safe way.
//
// `draft` is what is in the box right now, and defaults to the stored value so a plan built for
// a freshly selected asset reports the truth. `changed` is what makes the save button mean
// something -- saving an unchanged anchor is a write that spends a manifest save to do nothing.
// Compared on the trimmed values, because the route trims before it stores: whitespace typed
// after the last word is not an edit.
export function consistencyAnchorPlan(asset, draft) {
  if (!asset || asset.kind === "audio") return null;
  const stored = String(asset.consistency_prompt ?? "");
  const text = String(draft ?? stored);
  const length = text.trim().length;
  const over = length > CONSISTENCY_PROMPT_LIMIT;
  const changed = text.trim() !== stored.trim();
  const counted = `${length.toLocaleString("en-US")} / ${CONSISTENCY_PROMPT_LIMIT.toLocaleString("en-US")}`;
  return {
    stored,
    draft: text,
    length,
    over,
    changed,
    // Savable only when it is both a real change and inside the bound, so the button cannot
    // send a request the route is certain to refuse.
    savable: changed && !over,
    count: over ? `${counted} — too long to save` : counted,
  };
}

// `app.ASSET_NAME_LIMIT`. Held to the route's number by a contract test, on the anchor's own
// argument: a client that shortens silently and a route that 422s the same text are two rules
// wearing one number.
export const ASSET_NAME_LIMIT = 80;

export const ASSET_NAME_LABEL = "Name";

// Said on screen because a rename is the one asset edit with consequences the Director cannot
// see in the panel: what it cannot break, and what it does not touch. The route's own message
// says the same thing after the fact; this says it before.
export const ASSET_NAME_HELP =
  "What this asset is called everywhere — the library, the reference map a render is " +
  "conditioned on, and the roster the planner is shown. Shots keep their references when you " +
  "rename, but prompts already written keep the old name until they are re-expanded.";

// The one decision behind the name editor: what is stored, what is in the box, and whether it can
// be saved.
//
// Offered for every asset, unlike the anchor — a sound has no appearance but it does have a name.
// `draft` defaults to the stored value so a plan built for a freshly selected asset reports the
// truth, and both the change test and the bound are measured on the TRIMMED text, because the
// route trims before it stores and before it measures.
//
// Empty is unsavable rather than a clear, which is the route's rule: an anchor's empty box means
// "no anchor", and a name has no such meaning.
export function assetNamePlan(asset, draft) {
  if (!asset) return null;
  const stored = String(asset.name ?? "");
  const text = String(draft ?? stored);
  const trimmed = text.trim();
  const over = trimmed.length > ASSET_NAME_LIMIT;
  const empty = trimmed.length === 0;
  const changed = trimmed !== stored.trim();
  const counted = `${trimmed.length.toLocaleString("en-US")} / ${ASSET_NAME_LIMIT.toLocaleString("en-US")}`;
  return {
    stored,
    draft: text,
    length: trimmed.length,
    over,
    empty,
    changed,
    // Savable only when it is a real change, non-empty and inside the bound, so the button
    // cannot send a request the route is certain to refuse.
    savable: changed && !over && !empty,
    count: over ? `${counted} — too long to save` : empty ? `${counted} — a name cannot be empty` : counted,
  };
}

// -------------------------------------------------------------------------------------------
// Replace With / Cancel: the way through the delete refusal. The Director's own ask
// (2026-08-20) -- "a nice Replace With/Cancel option set would be nice so then i could select
// another image while i am here in assets and auto replace the one i am trying to remove across
// the affected shots". Every decision below is pure and executed under node; app.js owns the
// markup and the two clicks, and the *rules* are the server's.
// -------------------------------------------------------------------------------------------

export const REPLACE_WITH_HEADING = "Replace with";
export const REPLACE_WITH_PLACEHOLDER = "Choose an asset…";
export const REPLACE_WITH_LABEL = "Report the replacement";
export const REPLACE_WITH_RUNNING = "Working…";
export const REPLACE_WITH_CANCEL = "Cancel";
export const REPLACE_WITH_UNCHOSEN = "Pick the asset that takes over.";
// The panel's own explanation, shown when it was opened from the Assets panel rather than from a
// refused delete -- there is no refusal sentence to read in that case, and a panel that explained
// nothing would be a control offering to rewrite the plan for reasons of its own.
export const REPLACE_WITH_HELP =
  "Every shot citing this asset is re-pointed at the one you pick, keeping each citation's role " +
  "and position. Nothing is deleted and nothing is rendered — you see the whole list before " +
  "anything is written.";
export const REPLACE_WITH_NOTHING_TO_DO =
  "Nothing can be rewritten — every shot citing this asset is listed below with its reason.";
export const REPLACE_WITH_SWAPPED_HEADING = "Would be replaced";
export const REPLACE_WITH_MERGED_HEADING = "Already cite the replacement";
export const REPLACE_WITH_SKIPPED_HEADING = "Would be left alone";

// The library as this menu may offer it: every asset except the one being removed.
//
// The exclusion is not cosmetic. The route refuses an asset replacing itself by name, so an
// option for it is a control whose only outcome is a 422 -- and the count it would otherwise
// report ("30 shots changed") is exactly the false reassurance the refusal exists to prevent.
export function assetReplacementOptions(project, assetId) {
  return (project?.assets || []).filter((asset) => asset && asset.id !== assetId);
}

// Whether any shot cites this asset, which is what the delete refusal is about. Read from the
// project the browser already holds rather than parsed out of the refusal sentence: the sentence
// is prose meant for a person and matching on it would make the affordance appear or vanish with
// a wording change.
export function assetIsCited(project, assetId) {
  return citingShotCount(project, assetId) > 0;
}

export function citingShotCount(project, assetId) {
  return (project?.shots || []).filter((shot) =>
    (shot?.citations || []).some((citation) => citation?.asset_id === assetId)
  ).length;
}

// The Assets panel's own way in, beside "Attach to selected shot". The Director's ask
// (2026-08-20): "since we are already building the structure, when in the Assets page with an
// asset selected - since we already know if the asset is used in any shots we could offer a
// 'Replace in shots with:' button down by the 'Attach to selected shot' ... which would offer the
// same replacement function but without resulting in asset deletion."
//
// Drawn only when the asset is actually cited, because the browser already knows, and a button
// that could only ever answer "no shot cites this" is a control whose only outcome is the route's
// 422. The count is in the label for the reason the "Attach to selected shot" button next to it
// has a usability problem the Director reported: from the Assets panel the timeline is not
// visible, so a control that acts on shots has to say how many and — through the report — which.
export function replaceInShotsControl(project, assetId) {
  const count = citingShotCount(project, assetId);
  return {
    shown: count > 0,
    count,
    label: `Replace in ${count} shot(s) with…`,
  };
}

// -- "Attach to selected shot", made legible -----------------------------------------------
//
// The Director's report (2026-08-21), verbatim: *"Attach to selected shot (hard to use since cant
// see timeline from assets page)"*. From the Assets panel neither the timeline nor the selection
// is on screen, so the button acts on something invisible -- and the failure mode is silent, since
// attaching to the wrong shot looks exactly like attaching to the right one.
//
// The fix is the one the neighbouring control already made: `replaceInShotsControl` puts the
// *count* in its own label and lists the shots in its report, and that was called out as the right
// pattern. This puts the *identity* in the label and the window and the intent in a caption beside
// it, so the target is readable before the press rather than discoverable after it.
//
// Three states, and only one of them is a live button:
//
// * no shot selected — shut, and the caption says why and what to do. A disabled control naming
//   its reason beats an enabled one that fails.
// * already cited    — shut. The click was a no-op that toasted "attached" all the same, which is
//   the "control that appears to do nothing" shape this whole thread started from.
// * attachable       — live, named, with the window and the opening of the intent beside it.
export const ATTACH_LABEL_UNSELECTED = "Attach to selected shot";
export const ATTACH_NO_SHOT_REASON =
  "No shot is selected. Pick a clip on the Timeline first — this attaches the asset to that " +
  "shot's cited references, and nothing on this screen shows which shot that is.";
export const ATTACH_ALREADY_CITED_REASON =
  "{shot} already cites {asset}. Attaching it again would write the same list back and change " +
  "nothing; remove the citation in the shot inspector if it should not be there.";
export const ATTACH_HELP =
  "Cite {asset} as a reference on {shot} ({window}). The shot's other citations are kept.";

//: How much of a shot's creative intent the caption carries. Enough to recognise the shot,
//: short enough that the button does not become a paragraph.
export const ATTACH_INTENT_CHARS = 48;

//: A shot's window as the caption says it: `12.00–17.00 s`. The en dash matches the section
//: inspector's own window line, which is the other place this application prints one.
export function shotWindowLabel(shot) {
  const start = Number(shot?.start) || 0;
  const duration = Number(shot?.duration) || 0;
  return `${start.toFixed(2)}–${(start + duration).toFixed(2)} s`;
}

// The button and the line under it, decided once. `shotId` is `state.selectedShotId`, which is
// what the Assets panel has and the timeline owns.
export function attachToShotControl(project, shotId, assetId, assetName = "this asset") {
  const shot = (project?.shots || []).find((item) => item?.id === shotId) || null;
  if (!shot) {
    return {
      disabled: true,
      label: ATTACH_LABEL_UNSELECTED,
      shot: null,
      caption: ATTACH_NO_SHOT_REASON,
      title: ATTACH_NO_SHOT_REASON,
      reason: ATTACH_NO_SHOT_REASON,
    };
  }
  const name = shotLabel(project, shot.id);
  const window = shotWindowLabel(shot);
  const intent = String(shot.prompt || "").trim();
  const shown = intent.length > ATTACH_INTENT_CHARS
    ? `${intent.slice(0, ATTACH_INTENT_CHARS).trimEnd()}…`
    : intent;
  // The number is what the timeline paints on the clip, so the label is the thing a Director can
  // match against the timeline they cannot currently see. The id disambiguates it in the caption.
  const label = `Attach to ${name.split(" (")[0]}`;
  const cited = (shot.citations || []).some((citation) => citation?.asset_id === assetId);
  const caption = `${name} · ${window}${shown ? ` · ${shown}` : " · no creative intent written yet"}`;
  if (cited) {
    const reason = ATTACH_ALREADY_CITED_REASON
      .replace("{shot}", name)
      .replace("{asset}", assetName);
    return { disabled: true, label, shot, caption, title: reason, reason };
  }
  return {
    disabled: false,
    label,
    shot,
    caption,
    title: ATTACH_HELP
      .replace("{asset}", assetName)
      .replace("{shot}", name)
      .replace("{window}", window),
    reason: "",
  };
}

// The one decision behind the button: is it runnable, is this the report stage or the apply
// stage, what does it say. `snapCutsControl`'s shape, and for its reason -- the two-stage
// confirm has to be unskippable in the interface as well as on the wire, and the *same* button
// becoming the apply is what makes a Director read the report before confirming.
export function assetReplacementControl(replacementId, report) {
  if (!replacementId) {
    return { disabled: true, apply: false, label: REPLACE_WITH_LABEL, reason: REPLACE_WITH_UNCHOSEN };
  }
  if (report) {
    const writes = (report.swapped || 0) + (report.merged || 0);
    if (!writes) {
      return {
        disabled: true, apply: false, label: REPLACE_WITH_LABEL,
        reason: REPLACE_WITH_NOTHING_TO_DO,
      };
    }
    return {
      disabled: false,
      apply: true,
      label: `Replace in ${writes} shot(s)`,
      reason: report.message || "",
    };
  }
  return { disabled: false, apply: false, label: REPLACE_WITH_LABEL, reason: "" };
}

// The report as lines to draw: every swap, every "already have", every skip **with the server's
// own sentence**. `snapCutsReportLines`' rule verbatim -- nothing summarised and nothing
// rationed, because a skipped shot whose reason was dropped is exactly the one that explains why
// the delete is still refused.
export function assetReplacementReportLines(report) {
  if (!report) return [];
  const lines = [];
  const roles = (row) => (row.roles || []).join(", ") || "reference";
  // The take-provenance sentences, first and in the server's own words. They are notes and not
  // refusals -- those shots ARE being changed, on the Director's ruling that a citation swap does
  // not touch a take -- so they are drawn above the lists rather than among the skips, where they
  // would read as shots nothing happened to.
  for (const note of report.notes || []) lines.push({ kind: "note", text: note });
  // The per-shot half of the same fact. The grouped note carries the count and the consequence;
  // this marks the individual row, so a Director scanning the list can see which of fourteen
  // shots is the one with a take behind it.
  const take = (row) => (row.provenance ? ` · has a take rendered against ${report.replaced}` : "");
  for (const row of report.swaps || []) {
    const carried = row.carried_label ? ` · label "${row.carried_label}" carried` : "";
    lines.push({ kind: "swap", text: `${row.label}: ${roles(row)}${carried}${take(row)}` });
  }
  for (const row of report.merges || []) {
    const carried = row.carried_label ? ` · label "${row.carried_label}" carried` : "";
    lines.push({
      kind: "merge",
      text: `${row.label}: already cites ${report.replacement}, so the ${roles(row)} ` +
        `citation of ${report.replaced} is removed${carried}${take(row)}`,
    });
  }
  for (const row of report.skips || []) lines.push({ kind: "skip", text: row.reason });
  return lines;
}

// -------------------------------------------------------------------------------------------
// Render polling -- the client half of AD-1's transport decision. Every decision here is pure
// and executed under node by the contract tests; app.js only owns the timer and the repaints.
// -------------------------------------------------------------------------------------------

// The statuses a job never leaves, mirroring `batch.TERMINAL_JOB_STATUSES`; a contract test holds
// the two sets together, because a client that polls for a status the server calls settled polls
// forever, and one that calls settled what the server still reconciles stops watching a live job.
export const TERMINAL_JOB_STATUSES = ["complete", "error", "cancelled"];

// AD-1's interval: 2 s while a batch is active, zero requests while none is. Shot renders run
// 288-438 s, so this is two orders of magnitude finer than the event rate it watches.
export const RENDER_POLL_INTERVAL_MS = 2000;

// Whether this project has renders whose answer still lives on ComfyUI -- the whole polling
// contract, mirroring `batch.reconcilable_jobs`. A job with no prompt id has nothing to look up,
// so counting it would poll forever for an answer no tick can deliver.
export function hasActiveRenderJobs(project) {
  return (project?.jobs || []).some(
    (job) => job.prompt_id && !TERMINAL_JOB_STATUSES.includes(job.status),
  );
}

// ------------------------------------------------------------------------------------------
// Assembly (FR-22). One decision function for the bar's button, one reader for the newest
// export. An assembly job is the local kind: `post` with an empty prompt_id -- AD-9's own
// marker, and the same emptiness `hasActiveRenderJobs` keys on to keep local work out of the
// ComfyUI poll. Only the *cheap* readiness facts are decided here (shots, song, approvals,
// open renders); the plan-shaped refusals -- gaps, overlaps, stale windows -- are the
// server's comprehensive 422 report, rendered verbatim, never re-derived in a second
// implementation that could disagree with the one that matters.
// ------------------------------------------------------------------------------------------

export const ASSEMBLE_LABEL = "Assemble video";
export const ASSEMBLE_HELP =
  "Trim every approved take to its shot's window, join them in shot order and lay the master " +
  "song under the whole video. Runs locally in seconds; no render is queued and no take is " +
  "modified. The export lands under this project's media.";
export const ASSEMBLE_NO_SHOTS = "No shots to assemble yet.";
export const ASSEMBLE_NO_SONG = "Assembly needs a master song to synchronize to.";
export const ASSEMBLE_UNAPPROVED = "{count} of {total} shots still need an approved take.";
export const ASSEMBLE_RENDERS_OPEN =
  "Renders are still in flight; assemble when the queue settles.";
export const ASSEMBLE_RUNNING = "Assembling…";

// The two export presets, mirroring `assembly.EXPORT_PRESETS`. `draft` is first and is the
// default because it *is* what this application has always exported: choosing nothing keeps
// producing the file the button always produced. The server holds the same two names in a
// Literal, so an unknown one is a 422 before ffmpeg exists; a contract test holds the pair
// together, because a select offering a preset the route refuses is a dead control.
export const EXPORT_PRESETS = [
  {
    value: "draft",
    label: "Draft",
    help:
      "Fast review build — the settings this application has always exported with " +
      "(x264 veryfast, CRF 18). Your song's own levels and sample rate, untouched.",
  },
  {
    value: "master",
    label: "Master",
    help:
      "Delivery build — slower x264 at a lower CRF, faststart, and a 48 kHz audio " +
      "conform. Your song's own levels are preserved. Slower to encode.",
  },
];
export const EXPORT_PRESET_DEFAULT = "draft";

// How far the running export has got, or null when nothing local is running. Read from the
// job records for `latestAssemblyExport`'s reason -- the export is the job -- and keyed on
// the same `post` + empty-prompt_id marker AD-9 uses everywhere else. `hasActiveRenderJobs`
// deliberately ignores exactly these jobs (nothing on ComfyUI to reconcile), so this is the
// only reader of local progress there is.
export function assemblyProgress(project) {
  const jobs = project?.jobs || [];
  for (let index = jobs.length - 1; index >= 0; index -= 1) {
    const job = jobs[index];
    if (job.kind === "post" && !job.prompt_id && !TERMINAL_JOB_STATUSES.includes(job.status)) {
      return Math.max(0, Math.min(100, Number(job.progress) || 0));
    }
  }
  return null;
}

export function assemblyControl(project) {
  const shots = project?.shots || [];
  const refuse = (reason) => ({ disabled: true, label: ASSEMBLE_LABEL, title: reason, reason });
  if (!shots.length) return refuse(ASSEMBLE_NO_SHOTS);
  if (!project?.song?.path) return refuse(ASSEMBLE_NO_SONG);
  const unapproved = shots.filter((shot) => !shot.approved_output).length;
  if (unapproved) {
    return refuse(
      ASSEMBLE_UNAPPROVED
        .replace("{count}", String(unapproved))
        .replace("{total}", String(shots.length)),
    );
  }
  if (hasActiveRenderJobs(project)) return refuse(ASSEMBLE_RENDERS_OPEN);
  return { disabled: false, label: ASSEMBLE_LABEL, title: ASSEMBLE_HELP, reason: "" };
}

// ------------------------------------------------------------------------------------------
// Snap cuts to phrase boundaries. The Director's ruling on the roadmap's "vocal transition
// points between shots" item: a cut placed where nobody is singing has no mouth to mismatch
// across it. Two shots share a cut, so moving one is a single move that changes both windows.
//
// Everything decided here is *cheap and local*: whether the button can run at all (a song, a
// measurement, two shots, a non-zero tolerance) and how a returned report reads. The plan
// itself -- which cuts move, which are refused and in whose words -- is the server's, rendered
// verbatim, for `assemblyControl`'s reason: a second implementation of a refusal is a refusal
// that can disagree with the one that matters.
//
// Report first, apply on confirm. The button is a two-stage control: it fetches a report, the
// report is drawn in full, and only the second click sends `confirm_apply`. That shape is
// `populate`'s and `spec-arm-a-plan`'s -- "22 cuts moved, 3 skipped" is the moment a Director
// notices that three is wrong, and a report rationed into a confirm dialog cannot show it.
// ------------------------------------------------------------------------------------------

// `timeline.SNAP_TOLERANCE_DEFAULT` and `SNAP_TOLERANCE_MAX`. A contract test holds each
// against the Python constant, because a box offering a tolerance the request schema refuses
// is a control whose only outcome is a 422.
export const SNAP_TOLERANCE_DEFAULT = 0.75;
export const SNAP_TOLERANCE_MAX = 3;
export const SNAP_TOLERANCE_STEP = 0.05;

export const SNAP_CUTS_LABEL = "Snap cuts";
export const SNAP_CUTS_APPLY_LABEL = "Apply {moved} move(s)";
export const SNAP_CUTS_DISMISS_LABEL = "Discard report";
export const SNAP_CUTS_RUNNING = "Measuring…";
export const SNAP_CUTS_HELP =
  "Move each cut to the nearest moment the track is not singing, within the tolerance. " +
  "Reports what would move first and writes nothing until you confirm. Locked, approved and " +
  "rendering shots are never moved. Nothing is rendered and no take is touched.";
export const SNAP_CUTS_APPLY_HELP =
  "Write the windows above. Only the shot start and duration change; every other field on " +
  "every shot is left alone, and nothing is rendered.";
export const SNAP_CUTS_TOLERANCE_LABEL = "Snap ±";
export const SNAP_CUTS_TOLERANCE_HELP =
  "How far one cut may travel, in seconds. 0 switches snapping off entirely.";

// The cheap refusals, each a fact the browser already holds. They mirror the server's own
// sentences in intent rather than in bytes: these decide whether the button is *drawn shut*,
// and the server's are what a Director reads if one is sent anyway.
export const SNAP_CUTS_NO_SONG = "Snapping cuts needs a master song to measure against.";
export const SNAP_CUTS_UNMEASURED =
  "This track has not been heard yet, so where the singing is is unknown. Run Analyze " +
  "structure on the Song first.";
export const SNAP_CUTS_WITHOUT_CUTS =
  "A cut is the boundary two shots share, and this plan has fewer than two shots.";
export const SNAP_CUTS_TOLERANCE_OFF =
  "Tolerance is 0, so snapping is off. Raise it to let a cut move.";

export const SNAP_CUTS_SUMMARY = "{moved} cut(s) would move, {skipped} would stay.";
export const SNAP_CUTS_APPLIED_TOAST = "{moved} cut(s) moved, {skipped} stayed.";
export const SNAP_CUTS_NOTHING_TO_MOVE =
  "No cut can move within this tolerance. Nothing was written.";
export const SNAP_CUTS_MOVED_HEADING = "Would move";
export const SNAP_CUTS_SKIPPED_HEADING = "Would stay";

// The tolerance the box should hold, as a number the request body can carry. `clampToBounds`
// keeps a cleared box cleared for the form's sake; this one always answers a number, because
// the request needs one -- an empty box means the default rather than an empty key.
export function snapTolerance(raw) {
  if (raw === "" || raw === null || raw === undefined) return SNAP_TOLERANCE_DEFAULT;
  const value = Number(raw);
  if (!Number.isFinite(value)) return SNAP_TOLERANCE_DEFAULT;
  return Math.min(Math.max(value, 0), SNAP_TOLERANCE_MAX);
}

// Whether the button can run, and what it says. `report` is the last report this project
// answered with, or null; a report holding moves turns the same button into the apply half,
// which is what makes the confirm step unskippable in the interface as well as on the wire.
export function snapCutsControl(project, tolerance, report = null) {
  const shots = project?.shots || [];
  const refuse = (reason) => ({
    disabled: true, apply: false, label: SNAP_CUTS_LABEL, title: reason, reason,
  });
  if (!project?.song?.path) return refuse(SNAP_CUTS_NO_SONG);
  // Either measurement counts, in `timeline.vocal_gaps`' own order: the words are what cut
  // placement reads and the merged spans are its fallback, so a button drawn shut on the
  // spans alone would refuse a song the server can plan against.
  if (!(project?.song?.lyric_words || []).length
    && !(project?.song?.vocal_spans || []).length) return refuse(SNAP_CUTS_UNMEASURED);
  if (shots.length < 2) return refuse(SNAP_CUTS_WITHOUT_CUTS);
  if (!(snapTolerance(tolerance) > 0)) return refuse(SNAP_CUTS_TOLERANCE_OFF);
  if (report && report.moves?.length) {
    return {
      disabled: false,
      apply: true,
      label: SNAP_CUTS_APPLY_LABEL.replace("{moved}", String(report.moves.length)),
      title: SNAP_CUTS_APPLY_HELP,
      reason: SNAP_CUTS_SUMMARY
        .replace("{moved}", String(report.moves.length))
        .replace("{skipped}", String(report.skips?.length || 0)),
    };
  }
  return {
    disabled: false,
    apply: false,
    label: SNAP_CUTS_LABEL,
    title: SNAP_CUTS_HELP,
    reason: report ? SNAP_CUTS_NOTHING_TO_MOVE : "",
  };
}

// The report as lines to draw: every move, then every skip **with the server's own sentence**.
// Both lists in full and neither summarised -- a skipped cut whose reason was rationed away is
// exactly the one a Director needed to see.
export function snapCutsReportLines(report) {
  if (!report) return [];
  const seconds = (value) => `${Number(value).toFixed(3)}s`;
  const signed = (value) => `${Number(value) >= 0 ? "+" : ""}${Number(value).toFixed(3)}s`;
  const lines = [];
  for (const move of report.moves || []) {
    // How long the gap is, appended to the line that already exists. The Director's reason
    // (2026-08-20): "a 1 second gap may just be an extended shot where a 4 second gap would be
    // great for a b-roll or non singing character shot" -- the length is what separates the
    // two, and it is unreadable from the boundary alone. Drawn only when the server sent a
    // number, so a report from before the field existed loses the clause rather than printing
    // `NaNs`. Nothing here suggests a shot type; the number is the whole addition.
    const gap = Number(move.gap);
    const found = Number.isFinite(gap) && gap > 0 ? ` in a ${seconds(gap)} gap` : "";
    // An overlapping seam is a transition (R-3), and the two seconds figures above are its
    // *centre* -- an instant at which neither clip has an edge, so a Director looking for it
    // on the timeline would find nothing there unless the line says what it is. The clause
    // also says the length twice over: it is what went in and what comes out, because the
    // snap moves both edges together and never resizes a blend somebody authored. Drawn only
    // when the server sent a positive number, so a hard cut reads exactly as it always has
    // and a report from before the field existed loses the clause rather than printing `NaN`.
    const overlap = Number(move.overlap);
    const blend = Number.isFinite(overlap) && overlap > 0
      ? ` · centre of a ${seconds(overlap)} overlap, moved whole`
      : "";
    lines.push({
      kind: "move",
      text: `${move.before} → ${move.after}: ${seconds(move.boundary)} → ` +
        `${seconds(move.proposed)} (${signed(move.shift)})${found}${blend}`,
    });
  }
  for (const skip of report.skips || []) {
    lines.push({ kind: "skip", text: skip.reason });
  }
  return lines;
}

// ------------------------------------------------------------------------------------------
// Direct manipulation on the SHOTS track: undo/redo, the gap-fill gesture, and snapping an
// edge to the playhead. The Director's asks, 2026-08-21:
//
//   "I accidentally hit split on a clip, but there is no Undo button"
//   "when i shorten one clip and leave a gap ... double-click on a shot edge next to that gap"
//   "Snap to timeline play marker ... so i can line up to beats in the music"
//
// Every decision here is pure and executed by the contract tests. Nothing in this section
// writes: the two gestures below answer with the windows a caller should save, and the caller
// sends them through the same `PUT /shots` every other timeline edit goes through, so every
// server-side gate on that route applies to them unchanged.
//
// **Contiguity is the invariant.** The plan tiles the song, so a shot's edge is its
// neighbour's edge. Both gestures are written in terms of the *boundary* rather than of one
// window, and `contiguityProblems` is what the tests hold them to.
// ------------------------------------------------------------------------------------------

//: `assembly.ASSEMBLY_FPS`, `BOUNDARY_TOLERANCE_SECONDS` and `COVERAGE_TOLERANCE_SECONDS`,
//: each pinned to the Python constant by a contract test. Read rather than hardcoded per the
//: same rule the snap tolerances follow: these are the numbers that decide whether a plan
//: still assembles, and a second, drifting copy would let the timeline call a plan contiguous
//: that the assembler then refuses.
export const ASSEMBLY_FPS = 24;
export const BOUNDARY_TOLERANCE_SECONDS = 1 / (2 * ASSEMBLY_FPS);
export const COVERAGE_TOLERANCE_SECONDS = 1 / ASSEMBLY_FPS;

// Seconds are stored to the microsecond. Not a grid: the drag handlers quantise to frames
// because a drag is an approximation of an intent, and these gestures are the opposite --
// closing a 0.002 s gap and landing on the playhead are both requests for an *exact* number,
// and quantising either would re-open the very gap the gesture was pressed to close.
export function exactSeconds(value) {
  return Math.round(Number(value) * 1e6) / 1e6;
}

// Below this, two numbers are the same instant and there is no gap to close. A microsecond:
// a millionth of a frame, which is float noise and nothing else. Deliberately *not*
// `BOUNDARY_TOLERANCE_SECONDS` -- the Director's live plan carries 0.002 s, 0.004 s, 0.014 s
// and 0.015 s gaps, every one of them well inside assembly's tolerance and every one of them
// a gap they asked to be able to close.
export const GAP_EPSILON_SECONDS = 1e-6;

// Every neighbouring pair in the plan, in the order the song plays them, with the signed
// seconds between them: positive is a gap, negative is an overlap. The song's head and tail
// are pairs too, so a plan that starts late or ends early is not invisible here.
export function planSeams(shots, songDuration = null) {
  const ordered = [...(shots || [])].filter(Boolean).sort((a, b) => a.start - b.start);
  const seams = [];
  if (!ordered.length) return seams;
  seams.push({ kind: "head", before: null, after: ordered[0].id, seconds: exactSeconds(ordered[0].start) });
  for (let index = 0; index < ordered.length - 1; index += 1) {
    const shot = ordered[index];
    const next = ordered[index + 1];
    seams.push({
      kind: "seam",
      before: shot.id,
      after: next.id,
      seconds: exactSeconds(next.start - (shot.start + shot.duration)),
    });
  }
  const last = ordered[ordered.length - 1];
  if (Number.isFinite(songDuration) && songDuration > 0) {
    seams.push({
      kind: "tail",
      before: last.id,
      after: null,
      seconds: exactSeconds(songDuration - (last.start + last.duration)),
    });
  }
  return seams;
}

// The plan's contiguity, judged by assembly's own numbers: a seam beyond
// `BOUNDARY_TOLERANCE_SECONDS`, or a head/tail beyond `COVERAGE_TOLERANCE_SECONDS`, is
// something the assembler would report. An empty list means the plan tiles the song.
//
// This is an *assertion helper*, not a gate: nothing in the interface refuses an edit because
// of it. A Director mid-edit is allowed to have a plan that does not yet assemble.
export function contiguityProblems(shots, songDuration = null) {
  const problems = [];
  for (const seam of planSeams(shots, songDuration)) {
    const tolerance = seam.kind === "seam" ? BOUNDARY_TOLERANCE_SECONDS : COVERAGE_TOLERANCE_SECONDS;
    if (Math.abs(seam.seconds) <= tolerance) continue;
    problems.push({ ...seam, problem: seam.seconds > 0 ? "gap" : "overlap" });
  }
  return problems;
}

// ---- who may move a cut -------------------------------------------------------------------
//
// `timeline.cut_move_refusal`'s three sentences and its order, verbatim, held byte for byte
// against the Python constants by a contract test. They are reused rather than reworded for
// the reason this codebase reuses every refusal: a second wording of one rule is a second
// thing to keep true, and the Director reads whichever one happens to fire.
export const CUT_LOCKED_REFUSAL =
  "{shot} is locked. A lock is a deliberate hands-off on this shot, and moving the cut at " +
  "its edge changes its window, which is exactly the kind of change it refuses. Unlock the " +
  "shot first.";
export const CUT_APPROVED_REFUSAL =
  "{shot} carries an approved take, and an approval records the window it was approved in. " +
  "Moving this cut would change that window and assembly would then refuse the shot as " +
  "stale. Un-approve the take first if the cut should move.";
export const CUT_IN_FLIGHT_REFUSAL =
  "A render for {shot} has not finished, and it was submitted for the window this cut " +
  "would change. Wait for it, or refresh the render queue if it has already finished and " +
  "this project has not been told yet.";

// Why this shot's cut may not move, or "" when it may. The client half of
// `timeline.cut_move_refusal`, on the evidence a browser holds: `status` is the in-flight
// signal here, exactly as `shotRenderInFlight` reads it everywhere else in this file.
export function cutMoveRefusal(project, shot) {
  if (!shot) return "";
  const named = (wording) => wording.replace("{shot}", shotLabel(project, shot.id));
  if (shot.locked) return named(CUT_LOCKED_REFUSAL);
  if (shot.approved_output || shot.status === "approved") return named(CUT_APPROVED_REFUSAL);
  if (shotRenderInFlight(shot)) return named(CUT_IN_FLIGHT_REFUSAL);
  return "";
}

// ---- B. double-click an edge beside a gap ---------------------------------------------------

// ---- the shortest window a gesture may leave behind -----------------------------------------
//
//: The floor every window-editing gesture in the workspace already enforced with a bare `.5`:
//: the left-edge drag clamps at `end - .5`, the right-edge drag at `Math.max(.5, …)`, and the
//: shot inspector's duration box carries `min=".5"`. Named here so the split's refusal can quote
//: the same number the drags obey, rather than a second copy of it that could drift.
//:
//: It is a *floor*, not a minimum anyone should aim for. Micro-cuts are legitimate — a window
//: under H3's trained band renders at the minimum with the buffer trimmed away, which is why
//: `styles.css` deliberately draws no warning on the short end.
export const MIN_WINDOW_SECONDS = 0.5;

//: The shortest window a split can halve, which is twice the floor and nothing more interesting.
export const SPLIT_MINIMUM_SECONDS = 2 * MIN_WINDOW_SECONDS;

// -- The timeline controls that need a selection ----------------------------------------------
//
// One sentence for all three, because it is one situation: the Director pressed a control that
// operates on *the selected shot* while nothing is selected. `#split-shot` got this sentence in
// 2026-08-21; `#duplicate-shot` and `#delete-shot` sat beside it still returning on `!shot` and
// saying nothing, which is the defect that thread was opened about rather than a variant of it.
//
// The verb is substituted rather than the whole sentence rewritten per control, so the three
// cannot drift into three different accounts of the same state — and the second half, the gesture
// that fixes it, is identical for all of them because the fix genuinely is the same click.
//
// `#add-shot` is deliberately **not** here: it needs no selection, and its one refusal (no
// project) already speaks through `requireProject`.
export const NO_SHOT_SELECTED_REFUSAL =
  "No shot is selected, so there is nothing to {verb}. Click a clip on the timeline first.";

//: The refusal one selection-hungry control says, or `""` when it has its shot and may proceed.
//: Pure and shared, so a fourth control added later cannot invent a fourth wording.
export function noShotSelectedRefusal(shot, verb) {
  return shot ? "" : NO_SHOT_SELECTED_REFUSAL.replace("{verb}", verb);
}

// -- ✂ Split, when the window is too short to halve -------------------------------------------
//
// `#split-shot` refused a window under a second and said **nothing at all** — the same shape as
// the report that started this thread, a control that appears to do nothing. This is its
// sentence, and it explains the arithmetic rather than scolding: a 0.5 s window is a real thing
// the Director creates deliberately, and the reason it cannot be split is that halving it lands
// under the floor every drag in this workspace already stops at.
export const SPLIT_NO_SHOT_REFUSAL = NO_SHOT_SELECTED_REFUSAL.replace("{verb}", "split");
export const SPLIT_TOO_SHORT_REFUSAL =
  "{shot} is {seconds}s long. A split halves the window, so each half would be {half}s — under " +
  "the {minimum}s floor a shot window can be dragged to, which is why there is no cut to make " +
  "here. Drag the window out past {least}s first, or add a shot beside this one.";

//: Seconds as this refusal prints them: enough places for a 0.002 s edit to be visible, with no
//: trailing zeroes to make a round number look like a measurement.
function splitSeconds(value) {
  return String(Math.round(Number(value) * 1000) / 1000);
}

// The whole of one split: whether it may happen, what it says when it may not, and the two
// windows it produces. Pure, so the rule is executed by the contract rather than read out of a
// one-line click handler.
export function splitShotPlan(project, shot) {
  if (!shot) return { ok: false, refusal: noShotSelectedRefusal(shot, "split"), halves: [] };
  const duration = Number(shot.duration) || 0;
  if (duration < SPLIT_MINIMUM_SECONDS) {
    return {
      ok: false,
      refusal: SPLIT_TOO_SHORT_REFUSAL
        .replace("{shot}", shotLabel(project, shot.id))
        .replace("{seconds}", splitSeconds(duration))
        .replace("{half}", splitSeconds(duration / 2))
        .replace("{minimum}", splitSeconds(MIN_WINDOW_SECONDS))
        .replace("{least}", splitSeconds(SPLIT_MINIMUM_SECONDS)),
      halves: [],
    };
  }
  // Deliberately un-quantised, and identical to the arithmetic this handler used before the
  // refusal was added to it: the second half starts exactly where the first one ends, so the cut
  // the split makes is exact rather than exact-to-a-microsecond. `exactSeconds` is for the
  // gestures that answer a Director's number; this one answers a division.
  const half = duration / 2;
  const start = Number(shot.start) || 0;
  return {
    ok: true,
    refusal: "",
    halves: [
      { start, duration: half },
      { start: start + half, duration: half },
    ],
  };
}

//: How long after one press on an edge a second press on the *same* edge is the same gesture.
//: 400 ms is the platform's own double-click window on Windows, and the value is here rather
//: than inline so the rule can be executed.
export const EDGE_DOUBLE_CLICK_MS = 400;

//: How far the pointer may wander between a press on a resize handle and its release and still
//: leave that press standing as the first half of a double-click. Three pixels: a real
//: double-click on a 7 px handle does not travel, and a hand that jitters a pixel between the
//: two clicks is still double-clicking.
export const EDGE_DRAG_SLOP_PX = 3;

// Whether the press that started this gesture is still available to pair with the next one.
//
// A press on a resize handle starts *both* gestures -- the drag and the first half of a
// double-click -- and only the release can tell them apart. Until this existed the press was
// remembered across a drag, and `doubleEdgePress` measures from the *first* press: a 300 ms edge
// drag followed by re-grabbing the same edge 100 ms later fell inside the window above and ran
// the gap fill instead of starting the second drag, stretching the shot to its neighbour.
//
// `travelPx` is the furthest the pointer got from where it went down, not where it ended: a drag
// that wandered out and came back has still been a drag.
export function edgePressSurvivesDrag(travelPx, slop = EDGE_DRAG_SLOP_PX) {
  return Math.abs(Number(travelPx) || 0) <= slop;
}

// Whether this press on a resize handle completes a double-click.
//
// **Hand-rolled rather than a `dblclick` listener, and this is not a stylistic choice.** The
// clip's own `pointerdown` re-renders the SHOTS track, which replaces every clip node in the
// document -- so by the time the second click of a real double-click happens, the element the
// first one landed on no longer exists, and the browser has no common target to dispatch
// `dblclick` at. A `dblclick` handler on the clip is therefore dead code in this panel, and it
// is dead in a way no offline harness can see: the listener is bound, the selector matches, and
// the event simply never arrives. It was, until a real browser was pointed at it.
export function doubleEdgePress(last, press, windowMs = EDGE_DOUBLE_CLICK_MS) {
  if (!last || !press || !press.shotId || !press.edge) return false;
  if (last.shotId !== press.shotId || last.edge !== press.edge) return false;
  const elapsed = Number(press.at) - Number(last.at);
  return elapsed >= 0 && elapsed <= windowMs;
}

export const GAP_FILL_NO_GAP =
  "{shot} already meets its neighbour on that side — there is no gap there to close.";
export const GAP_FILL_TOAST = "{shot} extended by {seconds}s to close the gap.";
//: What the far side of the gap is called when there is no shot there.
export const GAP_FILL_SONG_HEAD = "the start of the song";
export const GAP_FILL_SONG_TAIL = "the end of the song";

// The plan for one double-click: which edge moves, where to, and how far. Pure; the caller
// writes it.
//
// `edge` is "left" or "right". The far side is the nearest neighbour on that side that does
// not already overlap this shot -- or the song's own head/tail when there is none, which is
// what makes the gesture work on the first and last clip in the plan.
//
// **Both shots at the resulting cut are checked**, not only the one being stretched.
// `timeline.snap_cut_plan` rules that a cut belongs to the two shots that share it, and this
// gesture creates exactly such a shared cut where there was empty song. The stretched shot is
// checked first so its own sentence is the one read when both are protected -- the order
// `shot_write_refusal` and `populate` both use.
export function gapFillPlan(project, shotId, edge) {
  const shots = project?.shots || [];
  const shot = shots.find((item) => item?.id === shotId);
  if (!shot) return { ok: false, refusal: "" };
  const ordered = [...shots].sort((a, b) => a.start - b.start);
  const end = shot.start + shot.duration;
  const refuse = (refusal) => ({ ok: false, refusal });
  // The neighbour is the *adjacent* clip in song order, never "the nearest one that does not
  // already overlap". Searching for a non-overlapping neighbour is how this gesture would
  // swallow a shot whole: a right edge already overlapping the next clip would skip past it
  // and stretch to the one after, deleting a shot from the picture without deleting it from
  // the plan. Adjacency is what the Director sees, and an overlapping neighbour simply means
  // there is no gap here.
  const index = ordered.findIndex((item) => item.id === shot.id);
  let boundary;
  let neighbour = null;
  if (edge === "right") {
    neighbour = ordered[index + 1] || null;
    boundary = neighbour ? neighbour.start : Number(project?.song?.duration);
  } else {
    neighbour = index > 0 ? ordered[index - 1] : null;
    boundary = neighbour ? neighbour.start + neighbour.duration : 0;
  }
  if (!Number.isFinite(boundary)) return refuse(GAP_FILL_NO_GAP.replace("{shot}", shotLabel(project, shot.id)));
  const gap = exactSeconds(edge === "right" ? boundary - end : shot.start - boundary);
  if (gap <= GAP_EPSILON_SECONDS) {
    return refuse(GAP_FILL_NO_GAP.replace("{shot}", shotLabel(project, shot.id)));
  }
  const blocked = cutMoveRefusal(project, shot) || cutMoveRefusal(project, neighbour);
  if (blocked) return refuse(blocked);
  const window = edge === "right"
    ? { start: exactSeconds(shot.start), duration: exactSeconds(boundary - shot.start) }
    : { start: exactSeconds(boundary), duration: exactSeconds(end - boundary) };
  return {
    ok: true,
    refusal: "",
    edge,
    gap,
    shotId: shot.id,
    neighbourId: neighbour?.id || null,
    against: neighbour ? shotLabel(project, neighbour.id) : (edge === "right" ? GAP_FILL_SONG_TAIL : GAP_FILL_SONG_HEAD),
    ...window,
  };
}

// ---- C. snap an edge to the playhead --------------------------------------------------------

//: How near, in *screen pixels*, an edge must come to the playhead before it snaps. Pixels and
//: not seconds, so the gesture feels identical at every zoom: 0.4 s of pull at 6 px/s would
//: swallow a whole short shot, and at 64 px/s would be unreachably fine.
export const PLAYHEAD_SNAP_PIXELS = 8;
//: The playhead's own name and sentence, now read by the "Snap to" selector's playhead row rather
//: than by a button of its own -- the Director's ruling of 2026-08-24 replaced that button with one
//: control over every kind. **The magnet itself is unchanged**: same 8 px, same decline while the
//: song plays, same `playheadSnap` below. Only where it is switched has moved.
export const PLAYHEAD_SNAP_HELP =
  "Dragging a shot edge within a few pixels of the playhead lands it exactly on the playhead, " +
  "and the neighbouring shot's edge follows so the plan stays contiguous. Park the playhead " +
  "where you want the cut, then drag the cut to it. Off while the song is playing — a moving " +
  "playhead is not a target.";
export const PLAYHEAD_SNAP_TOAST = "Cut moved to the playhead at {seconds}s.";
export const BOUNDARY_COLLAPSE_REFUSAL =
  "Moving that cut to the playhead would leave {shot} with no length at all.";

// Whether a proposed edge position should land on the playhead instead. Pure over numbers so
// the tolerance rule is executed rather than read.
export function playheadSnap({
  seconds,
  playhead,
  pixelsPerSecond,
  enabled = true,
  playing = false,
  tolerancePixels = PLAYHEAD_SNAP_PIXELS,
} = {}) {
  const free = { snapped: false, seconds: exactSeconds(seconds) };
  if (!enabled || playing) return free;
  if (!Number.isFinite(seconds) || !Number.isFinite(playhead)) return free;
  if (!(pixelsPerSecond > 0)) return free;
  if (Math.abs(seconds - playhead) * pixelsPerSecond > tolerancePixels) return free;
  return { snapped: true, seconds: exactSeconds(playhead) };
}


// ---- C2. snap an edge to the song itself: voiceless gaps and measured beats ------------------
//
// Story 8.3. Before this, a dragged edge snapped to exactly one thing — the playhead — and both
// of the song's own opinions about where a cut belongs were out of reach of the gesture: the
// beats Story 8.2 drew could not be landed on, and the voiceless gaps the batch "Snap cuts"
// button aims for were reachable only by rewriting the whole plan at once.
//
// **Nothing here decides where a cut belongs.** `timeline.py` does, on the server, for the drag
// and the button alike, and `GET /timeline/snap-targets` serves the answer. Everything below is
// arithmetic over the seconds it was handed: which of them is nearest, and whether it is near
// enough. A gap rule re-derived here would be the second snapper `timeline.py` names as this
// codebase's own recurring defect — and it would differ, because the button does not snap to
// lyric-word edges, it clamps into gaps by a clearance this file has never heard of.

// The three kinds, and the order they settle a tie in. Distance decides first and almost always
// alone; rank is only consulted when two targets are the *same* distance from the edge, which on
// real numbers means they are the same instant reached two ways.
//
// **Playhead over gap over beat**, and the argument runs the same way each time: the playhead is
// where the Director's own hand put it, so it outranks anything measured; a gap is where the plan
// snapper says a cut belongs — the strongest editorial claim the song makes — and a beat is a
// reference mark. Identical to the precedence `beatMarkerPlan` already uses when a beat and an
// onset compete for one pixel: the stronger claim wins, and it is stated rather than emergent.
//
// **Every kind must appear in `SNAP_TARGET_RANK`**, and a test asserts it rather than trusting it:
// a kind missing from this table compares `undefined > 2`, which is false, so it would silently
// lose every tie it entered instead of failing anywhere a reader could see.
export const SNAP_TARGET_KINDS = { playhead: "playhead", gap: "gap", beat: "beat" };
export const SNAP_TARGET_RANK = { beat: 1, gap: 2, playhead: 3 };

// What the Director is told a cut moved to. One sentence per kind, because "Cut moved to the
// playhead" said of a beat is a false report of what the application just did.
export const SNAP_TARGET_TOASTS = {
  playhead: PLAYHEAD_SNAP_TOAST,
  gap: "Cut moved to a voiceless moment at {seconds}s — the same second Snap cuts would choose.",
  beat: "Cut moved to the beat at {seconds}s.",
};

// What an undone snap is called, per kind, and the second half of the same argument
// `SNAP_TARGET_TOASTS` makes: "snapping the cut to the playhead" offered as the Undo for a cut
// that landed on a beat names a gesture the Director never made. It reaches the Undo control's
// tooltip **and** its accessible name, so a screen reader hears the wrong target too.
//
// No `{seconds}`: the Undo names a gesture, not a landing. "Undo snapping the cut to the beat at
// 41.203s" describes the state being left rather than the one being returned to.
//
// A fourth kind in Epic 10 or 11 is a line here and nothing else -- `UNDO_GESTURES` is built from
// this table rather than restating it.
export const SNAP_TARGET_UNDO = {
  playhead: "snapping the cut to the playhead",
  gap: "snapping the cut to a voiceless moment",
  beat: "snapping the cut to the beat",
};

// How a snap's kind is spelled when it is recorded as an undoable gesture. Prefixed rather than
// bare so a kind name can never collide with one of the other gestures' keys, and so a reader of
// a stack entry can still tell that the gesture was a snap.
export const UNDO_SNAP_PREFIX = "snap:";

// The gesture name `applySnappedCut` records, from the kind `edgeSnap` actually found. An
// unrecognised kind records the bare `snap`, whose sentence is the playhead's -- the same
// fallback the toast on that path takes, and for the same reason: an imprecise report of a write
// beats a silent one.
export function undoSnapGesture(kind) {
  return SNAP_TARGET_UNDO[kind] ? `${UNDO_SNAP_PREFIX}${kind}` : "snap";
}

// Which kinds a drag may land on, as a **set of kind names**. One argument for the whole
// question, deliberately, rather than a boolean per kind.
//
// The Director's ruling: what is being chosen is *which set of snap points this drag snaps to*,
// and a control that isolates a set is not the same shape as three switches that happen to sit
// beside each other. A `beats: true, gaps: true` signature would encode the number of kinds into
// every caller and every test, so adding a fourth would edit all of them -- and it would put the
// choice's structure in the pure function rather than in the control that owns it.
//
// So nothing below knows how many kinds exist or how the Director picked them. `null` and
// `undefined` mean **every kind**, which is both the honest default for a pure function and the
// behaviour while no selection has been stored; an empty set means none, and a drag with none is
// the freehand drag this application made before any of this existed.
//
// **Always a copy, never the caller's own Set.** `app.js` holds one live set that the selector
// mutates, and handing that object through would make a plan resolved at `pointerdown` change its
// mind halfway through a drag if a kind were toggled -- a gesture whose rules moved under it.
export function snapKindSet(kinds = null) {
  if (kinds === null || kinds === undefined) return new Set(Object.values(SNAP_TARGET_KINDS));
  if (kinds instanceof Set) return new Set(kinds);
  return new Set(Array.isArray(kinds) ? kinds : [kinds]);
}

// The widest a target may reach, in *screen pixels*. The playhead magnet's own number, and it is
// a ceiling rather than a starting point -- raising it above `PLAYHEAD_SNAP_PIXELS` is an
// Ask First change, because two magnets on one gesture with two different reaches would feel
// like one magnet that is sometimes broken.
export const DRAG_SNAP_PIXELS = PLAYHEAD_SNAP_PIXELS;

// ...and how much of the room between two neighbouring targets one of them may claim.
//
// **This is the story's central clause held as arithmetic.** One target on its own can safely
// claim the whole 8 px: there is nowhere else to land. Beats are 440 targets on a 3-minute song --
// measured 2.44 a second on a real track (2026-08-24), which is 16.4 px apart at 40 px/s,
// **6.5 px apart at the 16 px/s default** and 2.5 px at the 6 px/s floor. A flat 8 px radius at
// the default would capture every pixel between every pair of beats, and placing a cut off the
// beat would become impossible -- breaking the one thing this story promises it will not do.
//
// A third of the local spacing on each side leaves a third of it as dead zone at every zoom, and
// it makes snapping *fade out* as the targets crowd rather than becoming absolute: at 6 px/s the
// radius is 0.83 px, which is a magnet that has effectively stopped, which is the honest answer
// when the Director cannot see the difference between two beats anyway.
//
// **The playhead is capped by this too, and review iteration 1 is why.** It was left on a flat
// 8 px on the reading that "one target is safe", which is true of a playhead alone and false of a
// playhead parked among beats: measured between two beats 0.41 s apart at 16 px/s, 33% of the span
// was free with beats alone and **0%** once a playhead sat in the middle of it. All three kinds are
// active by default and a Director parks the playhead where they are working, so that was the
// default configuration. Lowering a tolerance to keep the dead zone is what the clause above
// always required; only *raising* one past 8 px is an Ask First.
export const DRAG_SNAP_SPACING_FRACTION = 1 / 3;

// The drag's targets, resolved once. **Once per drag, not once per pointer move.**
//
// `renderTimeline` runs on every `pointermove` of an edge drag -- which is why Story 8.2 had to
// guard the marker band's rebuild -- and a three-minute song carries several hundred targets.
// Sorting them, de-duplicating them and measuring every one's local spacing sixty times a second
// would reintroduce exactly the cost that guard exists to prevent. So this is the expensive half,
// done at `pointerdown`, and `edgeSnap` below is the cheap half that runs per move.
//
// `targets` is the route's whole report -- `{gaps, beats, ...}` -- or null, which is a project
// whose targets have not been read, or a read that failed. Null, absent, empty and disabled all
// produce the same empty plan, and an empty plan is a drag that behaves exactly as it did before
// this story existed.
//
// **The playhead is folded in here, as a point like any other.** It is the one target that is a
// live number rather than a measured list, and keeping it outside was the defect review iteration
// 1 found: outside, it neither took the spacing cap nor crowded the beats around it. Inside, one
// pass answers both -- it is capped by its neighbours and it caps them -- and `edgeSnap` has a
// single list to scan instead of a list plus an exception.
//
// Each point carries **its own** tolerance, in seconds, because the spacing that caps it is local:
// beats crowd in a chorus and spread over an outro, and one number for the whole song would be
// the wrong number for most of it. The scale they were measured in is carried out with them, so a
// caller can tell that a zoom has invalidated them rather than having to remember.
export function dragSnapPlan({
  targets = null,
  playhead = null,
  pixelsPerSecond = TIMELINE_ZOOM_BASE,
  enabledKinds = null,
  tolerancePixels = DRAG_SNAP_PIXELS,
  spacingFraction = DRAG_SNAP_SPACING_FRACTION,
} = {}) {
  const scale = Number(pixelsPerSecond);
  const empty = { points: [], reach: 0, pixelsPerSecond: scale, playhead: null, counts: {} };
  if (!Number.isFinite(scale) || scale <= 0) return { ...empty, pixelsPerSecond: 0 };
  const room = Math.min(Number(tolerancePixels) || 0, DRAG_SNAP_PIXELS);
  if (!(room > 0)) return empty;
  const fraction = Number(spacingFraction);
  if (!Number.isFinite(fraction) || fraction <= 0) return empty;
  // **A kind left out of the set is left out of the plan, not filtered out of the result.** The
  // spacing that caps every tolerance is measured over the points that are actually in play, so
  // a set with beats out of it gives the surviving gap targets the full reach they deserve
  // rather than a reach still crowded by beats nobody can land on.
  const enabled = snapKindSet(enabledKinds);
  // One second, one kind. A beat measured at the same instant a gap target sits on is one place
  // to land, not two, and the stronger claim names it -- `SNAP_TARGET_RANK`'s reason.
  const claimed = new Map();
  const claim = (value, kind) => {
    // `typeof === "number"`, not a coercion: a `null` in a malformed array coerces to a target
    // at second zero, which is a magnet at the head of the song that nothing measured.
    if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return;
    const at = exactSeconds(value);
    const held = claimed.get(at);
    if (held === undefined || SNAP_TARGET_RANK[kind] > SNAP_TARGET_RANK[held]) claimed.set(at, kind);
  };
  const take = (list, kind) => {
    if (!enabled.has(kind)) return;
    for (const value of Array.isArray(list) ? list : []) claim(value, kind);
  };
  take(targets?.gaps, SNAP_TARGET_KINDS.gap);
  take(targets?.beats, SNAP_TARGET_KINDS.beat);
  if (enabled.has(SNAP_TARGET_KINDS.playhead)) claim(playhead, SNAP_TARGET_KINDS.playhead);
  const ordered = [...claimed.entries()]
    .map(([seconds, kind]) => ({ seconds, kind }))
    .sort((first, second) => first.seconds - second.seconds);
  const points = ordered.map((point, index) => {
    // The nearer of its two neighbours: a target with a beat 2 px to its left and open song to
    // its right may not claim 8 px leftward on the strength of the open side.
    const before = index > 0 ? point.seconds - ordered[index - 1].seconds : Infinity;
    const after = index + 1 < ordered.length ? ordered[index + 1].seconds - point.seconds : Infinity;
    const spacing = Math.min(before, after);
    const capped = Number.isFinite(spacing) ? Math.min(room, spacing * scale * fraction) : room;
    return { ...point, tolerancePixels: capped, tolerance: capped / scale };
  });
  const counts = {};
  for (const point of points) counts[point.kind] = (counts[point.kind] || 0) + 1;
  return {
    points,
    // The widest any point's tolerance can be, in seconds. `edgeSnap` scans outward from the edge
    // and stops here, which is what keeps a per-move search bounded on a song with hundreds of
    // targets rather than walking all of them.
    reach: room / scale,
    // What this plan was measured in and against, so a caller can notice that a zoom or a seek has
    // made it stale. Carried rather than re-derived: a plan resolved at 16 px/s whose tolerances
    // are read at 64 px/s claims 10.7 px of pull, above a ceiling this file calls an Ask First.
    pixelsPerSecond: scale,
    playhead: enabled.has(SNAP_TARGET_KINDS.playhead) && Number.isFinite(Number(playhead))
      ? exactSeconds(playhead)
      : null,
    // Per kind rather than a field each, so nothing here counts the kinds either.
    counts,
  };
}

// Where a dragged edge actually lands. `playheadSnap`'s contract, extended: the same
// `{snapped, seconds}` a caller already reads, plus `kind` so the report of what happened can
// name the right thing.
//
// **The nearer target wins, whichever kind it is**, and every kind is asked the same question by
// the same function: `playheadSnap` decides whether an edge is within N screen pixels of an
// instant, which is the whole of the playhead magnet's rule and now the whole of every target's.
// It was generalised rather than copied -- there is still exactly one implementation of "near
// enough", and the playhead's own behaviour is what it always was when it is the only target
// in play.
//
// **The plan is the only source of what may be landed on.** No kind filter here, no playhead
// argument, no scale: all three are decided when the plan is resolved, so a caller cannot pass a
// plan built one way and a filter built another and get an answer belonging to neither. That
// mismatch was real -- a plan built with every kind, snapped with `['playhead']`, used to answer
// `kind: "beat"`.
//
// **Off every target is a real answer.** Nothing here refuses, clamps, colours or reports; an
// edge released between targets comes back `snapped: false` at the second it was released, and
// the caller writes it as the freehand drag always has.
//
// Snapping declines entirely while the master is playing, matching the playhead magnet: an edge
// cannot be lined up against anything while the thing it is being lined up against is moving,
// and a cut that lands somewhere different depending on how long the drag took is not a target.
export function edgeSnap({ seconds, plan = null, playing = false } = {}) {
  const value = Number(seconds);
  const free = { snapped: false, seconds: exactSeconds(seconds), kind: "" };
  if (!Number.isFinite(value)) return free;
  const scale = Number(plan?.pixelsPerSecond);
  const points = plan?.points || [];
  if (playing || !Number.isFinite(scale) || scale <= 0 || !points.length) return free;
  let best = null;
  const consider = (point) => {
    // The one implementation of "near enough", asked of this target exactly as it has always been
    // asked of the playhead, at this target's own capped tolerance.
    const pull = playheadSnap({
      seconds: value,
      playhead: point.seconds,
      pixelsPerSecond: scale,
      tolerancePixels: point.tolerancePixels,
    });
    if (!pull.snapped) return;
    const distance = Math.abs(value - point.seconds);
    if (best === null
      || distance < best.distance - GAP_EPSILON_SECONDS
      || (distance <= best.distance + GAP_EPSILON_SECONDS
        && SNAP_TARGET_RANK[point.kind] > SNAP_TARGET_RANK[best.kind])) {
      best = { seconds: pull.seconds, kind: point.kind, distance };
    }
  };
  const reach = Number(plan?.reach) || 0;
  // Binary search for where this edge falls among the sorted points, then out from there until
  // the widest a point's tolerance could possibly be is behind us. Bounded by `reach` rather
  // than by the list, so a per-move call costs a handful of comparisons on any song.
  let low = 0;
  let high = points.length;
  while (low < high) {
    const middle = (low + high) >> 1;
    if (points[middle].seconds < value) low = middle + 1;
    else high = middle;
  }
  for (let index = low - 1; index >= 0 && value - points[index].seconds <= reach; index -= 1) {
    consider(points[index]);
  }
  for (let index = low; index < points.length && points[index].seconds - value <= reach; index += 1) {
    consider(points[index]);
  }
  return best === null ? free : { snapped: true, seconds: best.seconds, kind: best.kind };
}

// Which measurement a read of `GET /timeline/snap-targets` belongs to -- **the whole read, both
// halves**: the marks the band draws and the seconds a drag may land on. The one fetch is keyed on
// this, so it is re-read exactly when the answer changes and never otherwise -- the route hashes
// the whole master to decide whether the analysis is current, which is why nothing may ask it on a
// timer.
//
// **Why this key rather than `songEnvelopeIdentity`, now that one read serves both.** It is a
// strict superset: literally that function's answer with three more fields appended, so it changes
// whenever the envelope's key would and cannot fail to notice something the narrower one would
// have. What it costs is a re-read on a first transcription, which is correct -- that is exactly
// when the gap half appears -- and on a duration correction. More often, never less.
//
// It is `songEnvelopeIdentity` plus the three things the *gap* half depends on and the envelope's
// key does not: the word and span counts, and the song's own duration. A first transcription, or a
// re-transcription, changes neither the song file nor the analysis fingerprint and changes every
// gap target there is. The duration is in it because the outermost gap target is
// `duration - SNAP_CLEARANCE_SECONDS`, so a Song whose length was corrected without the file being
// replaced would otherwise keep a target measured against the old end of the track.
//
// Deliberately **not** keyed on the shot plan. The targets are computed over the song rather than
// over the plan's span (see `drag_snap_targets`), so editing a window cannot move one -- which is
// what lets this be one read on a load path instead of a re-read after every drag.
export function snapTargetsIdentity(projectId, song = null) {
  const id = projectId || "";
  if (!id || !song?.path) return `${id}::`;
  const words = Array.isArray(song?.lyric_words) ? song.lyric_words.length : 0;
  const spans = Array.isArray(song?.vocal_spans) ? song.vocal_spans.length : 0;
  return `${songEnvelopeIdentity(id, song)}:${words}:${spans}:${song?.duration || 0}`;
}


// ---- C3. the "Snap to" selector: which set of points a drag lands on -------------------------
//
// The Director's ruling of 2026-08-24, mid-implementation: *"there should definitely be a dropdown
// selector or something to help isolate what set of snap points dragging snaps to."*
//
// **One control answers "what does dragging snap to".** It replaces the playhead magnet's own
// button rather than sitting beside it, because two controls answering one question is how a
// Director ends up opening both to find out. The playhead's *behaviour* is untouched -- still
// `playheadSnap`, still 8 px, still declining while the master plays; only where it is switched
// has moved.
//
// **It deliberately does not absorb `#beat-markers`.** Markers are drawing and snap targets are
// dragging: a Director may want to see the beats without landing on them, or land on beats they
// have hidden. Folding the two together is an Ask First in this story's spec.
//
// Everything below is text and structure over a set of kind names, and nothing in it counts them:
// the control draws whatever `SNAP_TARGET_ORDER` lists, and `app.js` writes markup from the plan.
//
// **What adding a fourth kind actually costs, stated honestly**, because an earlier draft of these
// comments said "one line in `SNAP_TARGET_ORDER`" and that was not true. It is one entry in each of
// the tables a kind needs -- `SNAP_TARGET_KINDS`, `SNAP_TARGET_RANK`, `SNAP_TARGET_ORDER`,
// `SNAP_TARGET_LABELS`, `SNAP_TARGET_SHORT`, `SNAP_TARGET_NOTES`, `SNAP_TARGET_HELP` and
// `SNAP_TARGET_TOASTS` -- plus wherever its seconds come from: a `take(...)` line in
// `dragSnapPlan` for a served list, or a `claim(...)` line for a live number like the playhead,
// and the matching key on the server's report. What it does **not** cost is a signature change, a
// per-kind boolean anywhere, or an edit to `edgeSnap`, `app.js` or any caller -- which is the part
// the set-shaped argument bought. Completeness tests hold `RANK` and `LABELS` to the order list,
// because a kind missing from `RANK` silently loses every tie rather than failing.
export const SNAP_SELECT_CONTROL = "#snap-targets";
export const SNAP_SELECT_SUMMARY = "#snap-targets-summary";
export const SNAP_SELECT_LIST = "#snap-target-kinds";
export const SNAP_SELECT_LABEL = "Snap to";
//: What the control says when the Director has switched every kind off. A sentence rather than an
//: empty space: "Snap to:" with nothing after it reads as a control that failed to draw, and this
//: is a real and deliberate state -- dragging is entirely freehand and that is a choice.
export const SNAP_SELECT_NONE = "nothing";
//: ...and what it puts after a kind that is ticked and has nothing to land on. Short, because
//: it sits in a toolbar; parenthesised, because it qualifies the kind rather than replacing it.
export const SNAP_SELECT_EMPTY_KIND = "(none)";
export const SNAP_SELECT_HELP =
  "Which points a dragged shot edge lands on. Any combination, including none. Snapping is an " +
  "assist and never a rule: an edge put down between targets stays exactly where it was " +
  "released, and nothing warns or refuses. The pull shrinks as targets crowd together, so there " +
  "is always room to place a cut deliberately off them, and nothing snaps while the song plays. " +
  "Showing the beat marks is a separate switch — you can snap to beats you have hidden.";

//: The kinds, in the order the control lists them, and the one place their number is written
//: down. Strongest editorial claim first, which is also `SNAP_TARGET_RANK`'s order.
export const SNAP_TARGET_ORDER = [
  SNAP_TARGET_KINDS.playhead, SNAP_TARGET_KINDS.gap, SNAP_TARGET_KINDS.beat,
];
export const SNAP_TARGET_LABELS = {
  playhead: "Playhead",
  gap: "Phrase gaps",
  beat: "Beats",
};
//: The same three for the summary line, where the control has one row of a toolbar to say what
//: dragging will do. Lower case and shorter, because it is read as a phrase and not as a list of
//: headings: `Snap to: playhead · gaps · beats`.
export const SNAP_TARGET_SHORT = { playhead: "playhead", gap: "gaps", beat: "beats" };
//: One short line each, drawn under the row's own name. What the kind *is* and where it comes
//: from -- the thing a Director cannot get from the word "gaps" alone is that these are the same
//: seconds the batch Snap cuts button chooses, which is the whole argument for the feature.
//
//: **The "needs a transcribed song" / "needs an analysed song" clauses were taken out of these
//: two**, and that is the whole of this change said in one place. They were true of some songs
//: and said of every song: a Director looking at a fully measured track was told its beats needed
//: an analysed song, and a Director looking at a track nobody had measured was told exactly the
//: same thing. A condition stated unconditionally is not a state, it is decoration. What the row
//: is *actually* missing now comes from `SNAP_TARGET_ABSENT` below, said only when it is missing.
export const SNAP_TARGET_NOTES = {
  playhead: "Where you parked the play marker.",
  gap: "The voiceless moments Snap cuts chooses.",
  beat: "The beats the song analysis measured.",
};
//: ...and the paragraph behind it, on the row's `title`, for a Director who went looking. The
//: playhead's is the sentence its own button used to carry, so the magnet's explanation moved with
//: its switch rather than being rewritten shorter and losing half of what it said.
export const SNAP_TARGET_HELP = {
  playhead: PLAYHEAD_SNAP_HELP,
  gap: "The seconds `timeline.py` picks inside each voiceless stretch — far enough inside that " +
    "neither shot's edge sits on a syllable. Exactly what the batch Snap cuts button lands a cut " +
    "on, computed once on the server so a drag and that button can never disagree. A song that " +
    "has not been transcribed has none, and this row says so: the other kinds keep working.",
  beat: "Every beat the song analysis measured, not the thinned set drawn on the waveform — you " +
    "can land on a beat whose mark is hidden. A song that has not been analysed has none, and " +
    "this row says so and offers the measurement: the other kinds keep working meanwhile.",
};

// ---- C3a. a row that cannot pull, and the one it can do something about ---------------------
//
// **Epic 8's headline finding, in one sentence:** the server computes twelve absence reasons and
// serves `measured`/`analysed` on every targets read, and none of it reached the Director
// anywhere. An un-analysed song and a machine with no ffmpeg looked identical on screen, and all
// five real projects had a song and no analysis, because the route that measures one had no
// caller in the interface at all.
//
// **Why here and not on the beat marker band.** Story 8.2 ruled that absence is *silence* for the
// markers, and that ruling stands. The distinction it drew is the one this relies on: a named
// refusal belongs where a consumer genuinely *needs* the envelope, not where one merely draws it.
// A Director opening this selector is asking what a drag will do, and a row that cannot pull is
// answering that question rather than raising an alarm.
//
// **Absence is not an error.** A song nobody has analysed is the normal state of every project
// that predates the measurement. `--red` is errors and `--amber` is caution; a missing
// measurement is neither, so the words are plain and the tokens are inert. Nothing here disables
// a tick either: a Director may switch beats on *before* measuring, and the row still says what
// it is currently worth.

//: Which served flag each kind's targets come from. A kind that is absent from this table needs
//: no measurement at all and can never be unavailable -- the playhead is one, which is why the
//: matrix says it is unaffected by every combination below.
//:
//: The flags are read rather than the lists' lengths, because `drag_snap_targets` deliberately
//: distinguishes *unmeasured* from *measured and voiced throughout*: a song with no silent moment
//: anywhere has no gap targets and has still been transcribed, and telling the Director to
//: transcribe it would be a lie about a song they already transcribed.
export const SNAP_TARGET_EVIDENCE = { gap: "measured", beat: "analysed" };

//: What one row's evidence amounts to. **Three values, because two would flatten the middle one:**
//: a kind that can pull, a kind that is *known* to have nothing, and a kind this browser has not
//: been told about. Only the middle one is a claim about the song, and only the middle one earns
//: an action.
export const SNAP_ROW_READY = "ready";
export const SNAP_ROW_ABSENT = "absent";
export const SNAP_ROW_UNKNOWN = "unknown";

//: What a row says when its kind has nothing to offer. Named thing, named reason, and -- where
//: the fix lives somewhere else in this application -- where it lives, because a Director told
//: "there are no phrase gaps" and nothing else has been given a dead end rather than an answer.
export const SNAP_TARGET_ABSENT = {
  gap: "This song has not been transcribed, so there are no phrase gaps to land on. " +
    "Analyze structure, on the Song page, is what measures them.",
  beat: "This song has not been analysed, so there are no beats to land on.",
};
//: ...and what a row says when **nothing has been read yet**, which is not the same thing and must
//: not be spelled like it. A targets read that has not landed, or one that was refused, is not
//: evidence of absence -- but silence in its place is how this change would trade one falsehood
//: for another, because the row would then state no prerequisite at all. So the prerequisite the
//: old `SNAP_TARGET_NOTES` stated unconditionally is stated here **conditionally**: only while
//: this browser genuinely does not know.
export const SNAP_TARGET_NEEDS = {
  gap: "Needs a transcribed song; this one has not been read yet.",
  beat: "Needs an analysed song; this one has not been read yet.",
};
//: The three ways a project can have nothing to measure, told apart rather than flattened. The
//: server tells the last two apart in `SONG_ANALYSIS_WITHOUT_SONG`'s own comment -- "no song at
//: all" and "a generated Song whose render has not landed" -- and a browser that called an
//: unrendered song "no song" would be telling the Director to import a track they have already
//: asked for.
export const SNAP_TARGET_NO_PROJECT =
  "No project is open, so there is nothing to measure.";
export const SNAP_TARGET_UNSONGED =
  "This project has no song, so there is nothing to measure yet.";
export const SNAP_TARGET_UNRENDERED =
  "This song has not been rendered yet, so there is no audio to measure.";

//: Which kinds this application can produce itself, and the action name the click site matches.
//: **Beats only, deliberately.** Phrase gaps come from transcription, which is its own act on its
//: own page with its own cost; offering a button here that would not help is worse than naming
//: where the fix actually is. A kind absent from this table simply carries no action.
export const SNAP_ANALYZE_ACTION = "analyze-song";
export const SNAP_TARGET_REMEDY = { beat: SNAP_ANALYZE_ACTION };
//: A row's action button, by kind. **Per row rather than one fixed id**, because a second entry in
//: `SNAP_TARGET_REMEDY` would otherwise draw two buttons carrying the same id and leave the second
//: unreachable to every `querySelector` in `app.js` -- a defect that arrives with a one-line table
//: edit and shows up nowhere near it.
export const snapActionControl = (kind) => `#snap-action-${kind}`;
//: **The naming, settled rather than left to collide.** The Song page's `#analyze-song` reads
//: *Analyze structure* and runs a **transcription**; this one reads *Analyze song* and runs a
//: **measurement**. They are different acts on different pages with different costs, and the two
//: ids were one character apart. `#snap-action-<kind>` is named for the row it belongs to instead,
//: so neither id can be mistaken for the other -- and the words stay in one vocabulary throughout:
//: the row says *analysed*, the button says *Analyze song*, the toast says *Song analysed*.
export const SNAP_ANALYZE_LABEL = "Analyze song";
//: The house's own running copy, from the UX spine: `Analyzing song…`. Elapsed nothing, percentage
//: nothing -- a measurement whose progress this application cannot see does not get a bar.
export const SNAP_ANALYZE_RUNNING = "Analyzing song…";
export const SNAP_ANALYZE_HELP =
  "Measure this song now: levels, onsets, beats and an estimated tempo. It reads the audio and " +
  "writes the measurement beside the project; no shot, cut or take is touched, and nothing is " +
  "rendered. It takes a moment on a full-length track.";
export const SNAP_ANALYZE_RUNNING_HELP =
  "The measurement is running. One at a time — this cannot be started again until it answers.";
//: What lands when it does. A count rather than a reassurance, because the count is the thing the
//: Director can check against the marks that just appeared on the waveform -- and **only when
//: there is one**: the read that would supply it can itself be refused, and "0 beats to snap to"
//: after a measurement that succeeded is this application inventing a number it does not have.
export const SNAP_ANALYZE_DONE = "Song analysed: {beats} to snap to.";
export const SNAP_ANALYZE_DONE_UNCOUNTED =
  "Song analysed. The measurement has not been read back yet, so the beats are not on screen.";
//: `1 beat`, not `1 beats`. The count is the whole content of the sentence, so getting its grammar
//: wrong is the one part of it a Director is guaranteed to notice.
export function snapBeatCount(beats) {
  const count = Number.isFinite(beats) ? Math.max(0, Math.trunc(beats)) : 0;
  return `${count} ${count === 1 ? "beat" : "beats"}`;
}

// The control's whole state, decided here so `app.js` writes markup and nothing else: which rows
// to draw, which are ticked, and the one line the summary says.
//
// `active` is normalised on the way through -- a kind this build does not have is dropped rather
// than drawn, which is what makes a stored selection from an older or newer browser session
// harmless. See `storedSnapKinds`, which is the other half of that.
//
// **`served` is the second argument and it decides three things per row**: whether the kind has
// anything to offer, the sentence it says when it does not, and whether this application holds
// the route that fixes it. It is the whole served targets body -- `measured` and `analysed` come
// straight off the wire, from `read_timeline_snap_targets` -- plus the Song, plus whether a
// measurement is running right now.
//
// **Four states per measured kind, and the third is the one a careless read loses.**
//
// * **no song** -- there is nothing to measure and no button that could change that.
// * **a song, and no targets read yet** -- *say nothing*. A read that has not landed, or one that
//   was refused, is not evidence of absence, and a row that claimed "not analysed" for the
//   duration of a request would be a false sentence that flickers. Silence is what the loaders do
//   with a refused read everywhere else in this feature.
// * **read, and the flag is false** -- the row cannot pull, and it says why.
// * **read, and the flag is true** -- the row reads exactly as it did before this existed.
//
// The action is offered only on the third of those, and only for a kind in `SNAP_TARGET_REMEDY`:
// a button on a row that is already working is a re-measurement nobody asked for.
export function snapSelectorPlan(activeKinds = null, served = null) {
  const enabled = snapKindSet(activeKinds);
  const targets = served?.targets || null;
  const project = Boolean(served?.project);
  const song = served?.song || null;
  const running = Boolean(served?.analysing);
  // Whether one kind has anything to offer, the sentence when it has not, and whether this is
  // something the *browser* knows or merely has not been told. Nothing here names a kind: a
  // fourth kind that needs a measurement is one entry in `SNAP_TARGET_EVIDENCE` and one in each
  // of the two sentence tables, which is the bargain the rest of this control already makes.
  //
  // **Three states, not two, and `unknown` is the one a careless read loses.** `absent` is a
  // claim -- "there is nothing here" -- and this browser is only entitled to make it from
  // evidence. A read that has not landed, one that was refused, and a served body from an older
  // build that does not carry the flag at all are none of them evidence: `undefined` is not
  // `false`, and a row answering "has not been analysed" for a flag the server never sent would be
  // asserting something nobody said. Unknown rows stay available, state their prerequisite, and
  // offer no action.
  //
  // **One value rather than a pair of booleans**, because `known && !available` and `!available`
  // were the same predicate -- there is no state where a row is unavailable and unknown -- so the
  // offer below was written with a term nothing could ever make false, and a mutation of it went
  // unnoticed by every test. `available` and `known` are still on the row for its readers; the
  // decision is made once, here, on the state itself.
  const evidence = (kind) => {
    const flag = SNAP_TARGET_EVIDENCE[kind];
    const absent = (reason) => ({ state: SNAP_ROW_ABSENT, reason });
    if (!flag) return { state: SNAP_ROW_READY, reason: "" };
    if (!project) return absent(SNAP_TARGET_NO_PROJECT);
    if (!song) return absent(SNAP_TARGET_UNSONGED);
    // A generated Song whose render has not landed: asked for, named, and with no audio on disk
    // yet. Not the same state as no song, and the Director's next move differs for each.
    if (!song.path) return absent(SNAP_TARGET_UNRENDERED);
    const flagged = targets ? targets[flag] : undefined;
    if (flagged === undefined || flagged === null) {
      return { state: SNAP_ROW_UNKNOWN, reason: SNAP_TARGET_NEEDS[kind] || "" };
    }
    if (flagged) return { state: SNAP_ROW_READY, reason: "" };
    return absent(SNAP_TARGET_ABSENT[kind] || "");
  };
  // The house control record, one key short: `reason` is missing on purpose. In every existing
  // case -- `markReadyControl`, `approvalControl` -- a control's reason explains why the *button*
  // is dead. This button is alive; it is the *row* that cannot pull, so the reason is the row's,
  // and a copy on the button would be two owners of one sentence.
  //
  // Offered only on a row that is **known** to have nothing to offer -- `SNAP_ROW_ABSENT` and
  // neither of the other two: a button on a row that works is a re-measurement nobody asked for,
  // and a button on a row this browser has not been told about is a guess. The song's own path
  // gates it too -- a measurement needs audio, and the three absences that are not about the
  // measurement are exactly the ones with none.
  const remedy = (kind, verdict) => {
    const action = SNAP_TARGET_REMEDY[kind];
    const offer = action && verdict.state === SNAP_ROW_ABSENT && Boolean(song?.path);
    if (!offer) {
      return { shown: false, disabled: true, action: "", label: SNAP_ANALYZE_LABEL, title: "" };
    }
    return {
      shown: true,
      // **Read as unavailable while one runs, never `disabled` in the DOM.** A browser blurs a
      // focused element the moment it is disabled and sends the next Tab to the top of the
      // document -- so the very press that starts the measurement would cost a keyboard Director
      // their place. `app.js` applies this as `aria-disabled`, which announces the same state and
      // keeps focus, and the click site carries the guard that actually refuses the second press.
      disabled: running,
      action,
      label: running ? SNAP_ANALYZE_RUNNING : SNAP_ANALYZE_LABEL,
      title: running ? SNAP_ANALYZE_RUNNING_HELP : SNAP_ANALYZE_HELP,
    };
  };
  const kinds = SNAP_TARGET_ORDER.map((kind) => {
    const verdict = evidence(kind);
    return {
      kind,
      label: SNAP_TARGET_LABELS[kind] || kind,
      note: SNAP_TARGET_NOTES[kind] || "",
      help: SNAP_TARGET_HELP[kind] || "",
      checked: enabled.has(kind),
      // Availability never touches `checked`. A kind switched on while its measurement is missing
      // stays switched on: the tick is what the Director wants a drag to land on, and the row
      // below it is what that is currently worth.
      // Both derived from the one verdict, so a reader can ask either question and neither can
      // disagree with what the row was actually drawn from.
      available: verdict.state !== SNAP_ROW_ABSENT,
      known: verdict.state !== SNAP_ROW_UNKNOWN,
      state: verdict.state,
      reason: verdict.reason,
      action: remedy(kind, verdict),
    };
  });
  const active = kinds.filter((row) => row.checked).map((row) => row.kind);
  // **The summary marks what cannot pull, and this change is not finished without it.** The whole
  // argument against the old `SNAP_TARGET_NOTES` was that a condition stated unconditionally is
  // decoration -- and a collapsed selector reading `Snap to: playhead · gaps · beats` over
  // a song nobody has measured makes exactly that claim, to the Director who never opens the panel.
  //
  // The ticked kind is still named rather than dropped. It is the Director's own selection and the
  // control has one line to report it; taking a kind out of that line would silently reverse a
  // choice they made and can still see ticked in the panel. `(none)` after it says what the drag
  // will actually do, which is what the summary is for.
  const named = kinds
    .filter((row) => row.checked)
    .map((row) => {
      const short = SNAP_TARGET_SHORT[row.kind] || row.kind;
      return row.available ? short : `${short} ${SNAP_SELECT_EMPTY_KIND}`;
    })
    .join(" · ");
  return {
    kinds,
    active,
    any: active.length > 0,
    // The state, in words, on the control itself. Never a count and never a colour: "2 selected"
    // does not tell a Director what a drag is about to do, and a lit button says even less.
    summary: `${SNAP_SELECT_LABEL}: ${active.length ? named : SNAP_SELECT_NONE}`,
  };
}

// One stored session value, read back as a selection. **Three states, and the third is the one a
// careless read loses.**
//
// * **absent** -- `undefined`, `null`, or anything that is not an array -- means *every kind*.
//   That is the default-on asymmetry the other view toggles use, and it is what a first-ever run
//   and a session saved before this feature both carry. Answered as `null` here, so the caller
//   keeps its own default rather than being handed a list this function invented.
// * **an empty array** means the Director switched everything off on purpose. Freehand dragging
//   is a real choice and it has to survive a reload.
// * **a populated array** is exactly that subset.
//
// `session.snapTargets || ALL` would flatten the first two together in the *other* direction and
// happen to work -- `[]` is truthy -- which is precisely why this is a named function with a test
// rather than an expression somebody has to reason about at a glance.
//
// Unknown names are dropped rather than thrown on: a session written by an older build may name a
// kind that has gone, and one written by a newer build may name a kind this one does not have.
// Either way the selection is what this build can honour of what was asked for.
export function storedSnapKinds(value) {
  if (!Array.isArray(value)) return null;
  return value.filter((kind) => SNAP_TARGET_ORDER.includes(kind));
}

// The whole selection a stored session implies, migration included. `null` still means "nothing
// was stored, keep the every-kind default"; anything else is the selection to adopt.
//
// **`playheadSnap: false` is honoured, and review iteration 1 is why.** That key was the playhead
// magnet's own switch before this selector replaced it, and deleting its restore line with nothing
// in its place silently switched the magnet back on for every Director who had turned it off --
// a preference reversed without being asked, on the next load, with nothing on screen to say so.
// A session carrying it and no `snapTargets` is therefore read as "every kind except the
// playhead", which is what that Director had.
//
// The migration is deliberately one-way and one-shot: the moment a selection is stored, that key
// stops being consulted, so a Director who ticks the playhead back on is not overruled by it on
// the next load. Nothing rewrites or deletes the old key -- a session store is not a migration
// journal, and leaving it costs one dead field.
export function snapKindsFromSession(session = null) {
  const stored = storedSnapKinds(session?.snapTargets);
  if (stored !== null) return stored;
  if (session?.playheadSnap === false) {
    return SNAP_TARGET_ORDER.filter((kind) => kind !== SNAP_TARGET_KINDS.playhead);
  }
  return null;
}
// Move the cut at one shot's edge to `seconds`, carrying the shot that shares it.
//
// This is the half that keeps the plan contiguous, and it is why snapping is not simply the
// existing drag with a magnet on it: the freehand right-edge drag changes one duration and
// leaves the next shot's start where it was, which is how the Director's plan came to hold
// four sub-frame gaps. A cut is one instant belonging to two shots, so both windows move.
//
// A neighbour counts as sharing the cut when it meets this edge within
// `BOUNDARY_TOLERANCE_SECONDS` -- assembly's own idea of "the same boundary written twice".
// A neighbour further off than that is on the other side of a real gap or overlap, and this
// gesture does not silently close one; that is what the double-click is for.
export function boundaryMovePlan(project, shotId, edge, seconds) {
  const shots = project?.shots || [];
  const shot = shots.find((item) => item?.id === shotId);
  if (!shot) return { ok: false, refusal: "", windows: [] };
  const at = exactSeconds(seconds);
  const end = shot.start + shot.duration;
  const shared = shots.find((item) => item?.id !== shot.id && (edge === "right"
    ? Math.abs(item.start - end) <= BOUNDARY_TOLERANCE_SECONDS
    : Math.abs(item.start + item.duration - shot.start) <= BOUNDARY_TOLERANCE_SECONDS));
  const blocked = cutMoveRefusal(project, shot) || cutMoveRefusal(project, shared);
  if (blocked) return { ok: false, refusal: blocked, windows: [] };
  const windows = [];
  if (edge === "right") {
    windows.push({ id: shot.id, start: exactSeconds(shot.start), duration: exactSeconds(at - shot.start) });
    if (shared) {
      windows.push({
        id: shared.id,
        start: at,
        duration: exactSeconds(shared.start + shared.duration - at),
      });
    }
  } else {
    windows.push({ id: shot.id, start: at, duration: exactSeconds(end - at) });
    if (shared) {
      windows.push({ id: shared.id, start: exactSeconds(shared.start), duration: exactSeconds(at - shared.start) });
    }
  }
  // Zero or negative only. **Not** a minimum length: short windows are legitimate and are
  // being made more so, so nothing here may refuse one for being short -- a window with no
  // length at all is a different thing, and it is not a window.
  const collapsed = windows.find((window) => window.duration <= 0);
  if (collapsed) {
    return {
      ok: false,
      refusal: BOUNDARY_COLLAPSE_REFUSAL.replace("{shot}", shotLabel(project, collapsed.id)),
      windows: [],
    };
  }
  return { ok: true, refusal: "", windows, sharedId: shared?.id || null };
}

// ---- A. undo and redo -----------------------------------------------------------------------
//
// **A snapshot stack over the shot list, replayed through `PUT /shots`.** The manifest is one
// atomic JSON document and the shot list is one field of it, so a snapshot of that field *is*
// a complete description of the plan at a moment -- which is what makes the cheap design the
// principled one here. A command history would have to carry an inverse for every gesture, and
// each inverse would be a second implementation of the rule its gesture already encodes.
//
// The whole safety argument is the revision stamp, and it is deliberately not a new rule:
//
// * **Never undo something that was never applied.** An entry is pushed when the server
//   confirms the write, never when the gesture is made. A refused save leaves nothing to undo.
// * **Never clobber a state the server changed underneath.** The undo write carries the
//   revision the stack is valid against as `PUT /shots`'s optimistic-concurrency token, so a
//   render landing, a reconcile writing `latest_output`, or any other writer moving
//   `updated_at` makes the server refuse it with `PROJECT_CHANGED_REFUSAL` -- the same 409
//   every stale timeline save already gets. The client pre-flights the same comparison so the
//   button can say so before it is pressed, and the stack is dropped rather than left holding
//   entries that would replay over somebody else's work.
// * **Never resurrect a shot whose render is in flight.** A snapshot taken before the render
//   carries that shot's pre-render `status`, and `_require_in_flight_status_kept` refuses a
//   body that walks an in-flight status back. Nothing here re-implements that check.
//
// Redo falls out of the same machinery: undoing displaces a state, and the displaced state is
// pushed onto a second stack governed by exactly the same rule.

export const UNDO_LABEL = "Undo";
export const REDO_LABEL = "Redo";
export const UNDO_ICON = "↶";
export const REDO_ICON = "↷";
//: How many gestures back the stack reaches. A plan's shot list is a few tens of kilobytes;
//: forty of them is nothing, and forty gestures is far more than one sitting's worth of
//: mis-clicks.
export const UNDO_DEPTH = 40;

export const UNDO_EMPTY = "Nothing to undo — timeline edits made in this session appear here.";
export const REDO_EMPTY = "Nothing to redo.";
export const UNDO_HELP = "Undo {what}. Ctrl+Z. Only the shot list is restored — takes on disk are never touched.";
export const REDO_HELP = "Redo {what}. Ctrl+Shift+Z.";

//: `app.PROJECT_CHANGED_REFUSAL`, byte for byte (a contract test holds them together). The
//: sentence the server answers a stale `PUT /shots` with, said here *before* the request so a
//: Director reads the same words whichever side notices first.
export const PROJECT_CHANGED_REFUSAL =
  "Project changed since it was loaded; refresh before replacing it";
export const UNDO_PROJECT_MOVED =
  `${PROJECT_CHANGED_REFUSAL}. Undo refuses rather than revert a change it did not make.`;

//: What each covered gesture is called when the button names what it will undo.
export const UNDO_GESTURES = {
  add: "adding a shot",
  split: "the split",
  duplicate: "the duplicate",
  delete: "the delete",
  move: "moving a shot",
  resize: "resizing a shot",
  gapfill: "closing the gap",
  // The bare `snap` is what an unrecognised kind falls back to, and it reads as the playhead's
  // sentence because the playhead is the target this gesture had before the song had any others.
  snap: SNAP_TARGET_UNDO.playhead,
  // One entry per snap kind, spread from `SNAP_TARGET_UNDO` rather than spelled out again: a
  // branch per kind here is the thing that went stale when Story 8.3 added two of them and left
  // one sentence behind.
  ...Object.fromEntries(
    Object.entries(SNAP_TARGET_UNDO).map(([kind, what]) => [`${UNDO_SNAP_PREFIX}${kind}`, what]),
  ),
  edit: "the last shot edit",
};
export const UNDO_GESTURE_FALLBACK = "edit";

export function undoGestureLabel(kind) {
  return UNDO_GESTURES[kind] || UNDO_GESTURES[UNDO_GESTURE_FALLBACK];
}

// Whether either button can be pressed, and what it says it will do. One function for both, so
// the two can never disagree about the revision rule.
//
// `busy` is `app.js`'s `shotWriteInFlight` -- "" for none, or which automated write holds the
// read-to-save window open. Its two sentences are the existing ones, verbatim, because an
// undo landing in the middle of an expansion is the same hazard a hand edit is.
export function undoControl(entries, {
  revision = null,
  projectRevision = null,
  busy = "",
  redo = false,
} = {}) {
  const label = redo ? REDO_LABEL : UNDO_LABEL;
  const shut = (title) => ({ disabled: true, label, what: "", title });
  const held = entries || [];
  if (!held.length) return shut(redo ? REDO_EMPTY : UNDO_EMPTY);
  if (busy) return shut(busy === "assistant" ? ASSISTANT_EDIT_BLOCKED : SHOT_EXPANSION_EDIT_BLOCKED);
  // Both revisions known and different: something wrote the project that this stack does not
  // account for. Refused rather than replayed -- see the note above.
  if (revision === null || projectRevision === null || revision !== projectRevision) {
    return shut(UNDO_PROJECT_MOVED);
  }
  const what = undoGestureLabel(held[held.length - 1]?.kind);
  return {
    disabled: false,
    label,
    what,
    title: (redo ? REDO_HELP : UNDO_HELP).replace("{what}", what),
  };
}

// ------------------------------------------------------------------------------------------
// The over-render offset and the Monitor's decisions (spec-monitor-and-over-render). Takes
// are rendered ~half a second longer than their windows; these decide which slice of the
// take the timeline's window shows. `effectiveOffset` is the client's one copy of the rule
// the server's assembly route resolves from the same two fields -- a contract test holds
// the two together, because a Monitor previewing one slice while assembly cuts another
// would make the fine-tune a lie.
// ------------------------------------------------------------------------------------------

export function effectiveOffset(shot) {
  return (Number(shot?.latest_take_lead) || 0) + (Number(shot?.trim_nudge) || 0);
}

// The nudge control's whole decision: shown only when there is a take to tune, stepped in
// frames by the workspace, floored so the cut can never reach before the take begins. The
// upper bound is the take's own length, which only the server measures -- assembly refuses
// an overrun with the numbers, and the Monitor simply shows the last frame.
export function trimNudgeControl(shot) {
  const lead = Number(shot?.latest_take_lead) || 0;
  const nudge = Number(shot?.trim_nudge) || 0;
  return {
    shown: Boolean(shot?.latest_output),
    lead,
    nudge,
    offset: lead + nudge,
    minNudge: -lead,
  };
}

// ------------------------------------------------------------------------------------------
// Whether a move-drag on the timeline carries this shot's take with it. The Director's ask,
// 2026-08-21:
//
//     "when I said Trim nudge I was talking specifically about the section in the Shots info
//     panel that lets us nudge the clip along the timeline. When dragging in the timeline
//     though it would just move the window over the clip but keep the clip aligned where it
//     belongs with the music. Perhaps a lock/unlock from timeline toggle in the shots info
//     panel may be useful next to that nudge input so that dragging a b-roll clip would be
//     easier (default locked)."
//
// Both behaviours already existed and were on the wrong gestures: the *left edge* of a rendered
// clip has moved `start` with `trim_nudge` compensating since 2026-08-20 -- which is exactly
// "the window moves over the take" -- while the whole-clip move wrote `start` alone, carrying
// the take with it. This makes the move behave like the edge by default, and the toggle is what
// puts the old behaviour back for one shot when the Director wants it.
//
// **Session state, per shot, never persisted and never sent.** Held in `app.js` as a set of shot
// ids, on the precedent of the two line mutes, the snap magnet and the seed randomize toggle --
// and decided that way for a sharper reason of its own. What is durable about a b-roll clip is
// where it sits, and where it sits is `start`/`trim_nudge`, which are persisted fields this
// gesture writes. The unlock itself is a working mode -- "let me drag this one freely" -- and a
// *persisted* unlock would be a trap: it would sit on a lip-sync shot for ever, silently letting
// a drag months later pull the take off the words it was rendered against, which is the exact
// failure the Director asked for this to prevent. Session-only fails closed, every time.
//
// Per shot rather than one flag for the workspace, which is where this differs from the seed
// randomizer: unlocking is dangerous on the *next* shot, so it must not leak to it.
export const TAKE_ANCHOR_CONTROL = "take-anchor";
export const TAKE_ANCHOR_LABEL = "Locked to the music";
//: Says what each state does, and says plainly that this is not the shot lock two rows above it
//: -- two controls with "lock" in the name, one of which refuses sweeps and one of which changes
//: what a drag does, is exactly the pair a Director would otherwise have to find out by trying.
export const TAKE_ANCHOR_HELP =
  "Locked: dragging this clip along the timeline slides its window over the take, and the take " +
  "goes on playing against the same seconds of the song — a lip-sync take stays on its own " +
  "words. Unlocked: the take travels with the window, which is what you want when repositioning " +
  "b-roll. Either way nothing is prevented; a window dragged off what its take covers turns " +
  "amber. This is not the shot lock above, and it lasts for this session only.";

// The toggle's whole decision, and the drag's. `held` is the one rule both read: a shot with no
// take has nothing to hold still, so the move behaves exactly as it did before this existed.
//
// `shown` is `trimNudgeControl`'s own -- the toggle is drawn inside the trim-nudge row, so a shot
// with no take shows neither, which is the honest answer for a control that would do nothing.
export function takeAnchorControl(shot, unlocked = false) {
  const shown = Boolean(shot?.latest_output);
  return {
    shown,
    held: shown && !unlocked,
    control: TAKE_ANCHOR_CONTROL,
    label: TAKE_ANCHOR_LABEL,
    help: TAKE_ANCHOR_HELP,
  };
}

// What a shot's `trim_nudge` becomes when its window's `start` moves from `from` to `to`.
//
// **The whole anchoring rule, in one function, for every gesture that moves any shot's start.**
// The Director's ruling, 2026-08-21:
//
//     "Well we dont want to slide the take next to the one we are adjusting either, rather move
//     its windows edge while both clips stay in place, same for double click, those gestures
//     should only slide the window bounds but leave the clip position intact."
//
// This supersedes the reasoning that had the compensation on the shot *under the hand* only.
// The principle is about the take, not about which clip the pointer is on: a take's anchor is
// `start - lead - nudge`, the song second its first frame plays at, so *any* uncompensated write
// to `start` slides that shot's take off the music -- and a Director snapping one cut to a beat
// did not ask for the take on the other side of it to move. Written as a function over numbers
// rather than as a line repeated at each site, because "repeated at each site" is exactly how it
// came to be applied at two of the four and not at the other two.
//
// Three answers, and only the first writes anything:
//
// * **A take, and locked.** The nudge moves by exactly the seconds the window moved, so
//   `start - lead - nudge` comes out unchanged. This is what the lock means.
// * **No take.** Nothing to anchor -- there is no take whose position could be preserved -- so
//   the window moves alone, exactly as it did before any of this existed.
// * **Unlocked.** The Director has said this clip is being repositioned deliberately; the take
//   travels with the window, which is what a b-roll reposition wants.
//
// `unlocked` is the *moving shot's own* lock, never the lock of whatever clip the gesture was
// aimed at. When a drag on shot A moves shot B's start it is B's take that must stay on the
// music, so it is B's toggle that decides -- and B's toggle is the one a Director would go and
// untick if they wanted B's take to travel.
//
// `nudge` is what `trim_nudge` was *at* `from`, passed in rather than read off the shot: a drag
// mutates the live shot on every `pointermove`, so the shot's current nudge is the previous
// frame's answer and compounding onto it would multiply the compensation by the number of mouse
// events. It defaults to the shot's own field for the callers that write once.
//
// **`exactSeconds`, never the frame grid.** Windows step to 1/24 s; a window that did not *start*
// on the grid moves by an off-grid amount, and re-gridding the compensation rounds it to a
// different number than the window moved by. A browser measured a 1.608 s move written as a
// 1.625 s nudge on 2026-08-21, leaving the take 17 ms off the music with every offline assertion
// green. The compensation is not its own gesture; it is exactly what the window did, with float
// noise trimmed.
export function anchoredNudge(shot, { from, to, nudge = null, unlocked = false } = {}) {
  const was = nudge === null ? (Number(shot?.trim_nudge) || 0) : (Number(nudge) || 0);
  if (!takeAnchorControl(shot, unlocked).held) return was;
  const moved = exactSeconds(Number(to) - Number(from));
  // A window whose new position is not a number has not moved anywhere. Without this the sum is
  // `NaN`, and a `NaN` nudge is a shot the whole panel then draws and assembles from as garbage;
  // there is no version of this where writing it is better than leaving the nudge alone.
  if (!Number.isFinite(moved)) return was;
  return exactSeconds(was + moved);
}

// The shot whose window holds this moment of the song, or null over a gap. Later starts
// win a boundary tie, matching assembly's cumulative grid where a boundary frame belongs
// to the clip it opens.
export function monitorShotAt(project, seconds) {
  let found = null;
  for (const shot of project?.shots || []) {
    if (seconds >= shot.start && seconds < shot.start + shot.duration) {
      if (!found || shot.start > found.start) found = shot;
    }
  }
  return found;
}

// What the Monitor shows at one moment: a take (and where inside it), or an honest
// placeholder -- never a stale frame from another shot. `takeTime` folds the effective
// offset in, so a fresh song-audio take previews the exact song seconds it was
// conditioned on and a nudged one previews the nudged slice assembly will cut. `muted`
// is the acceptance flag inverted: an accepted clip's own audio plays over the master in
// preview, exactly as assembly will mix it -- one decision on both sides.
//
// A take with a render in flight is its own kind. It plays exactly as a settled take does --
// same file, same slice, same mix -- because it is the only evidence the Director has and
// `latest_output` still points at it; what changes is that the view now carries the sentence
// saying so, and `monitorShowsTake` keeps both kinds on screen. The alternative, deciding a
// take was too stale to show, would blank the picture and lose the very comparison the
// re-render is being judged by.
export function monitorState(project, seconds) {
  const shot = monitorShotAt(project, seconds);
  if (!shot) return { kind: "gap", shot: null, takeTime: 0, label: "No shot under the playhead", muted: true };
  if (!shot.latest_output) {
    return { kind: "no-take", shot, takeTime: 0, label: "This shot has no rendered take yet", muted: true };
  }
  const view = {
    kind: "take",
    shot,
    takeTime: Math.max(0, seconds - shot.start + effectiveOffset(shot)),
    label: "",
    muted: !shot.mix_take_audio,
  };
  if (!shotRenderInFlight(shot)) return view;
  return { ...view, kind: MONITOR_PREVIOUS_TAKE, label: TAKE_DISPLACED_BY_RENDER };
}

// The kind of a take the Monitor shows while a newer render is in flight, and the two kinds
// that put a picture on the screen. Named because `syncMonitor` and the stylesheet's
// `.showing-take` both have to agree that a displaced take is still shown -- a surface that
// tested `kind === "take"` would black out the only take there is.
export const MONITOR_PREVIOUS_TAKE = "previous-take";
export const MONITOR_TAKE_KINDS = ["take", MONITOR_PREVIOUS_TAKE];

export function monitorShowsTake(view) {
  return MONITOR_TAKE_KINDS.includes(view?.kind);
}

// ------------------------------------------------------------------------------------------
// The takes strip: every clip this shot's render history produced, and which of them the shot
// is pointing at. Decided here rather than in the inspector's template, for `shotPromptCell`'s
// reason -- the strip's whole job is to make one claim per row, and the row that made the
// wrong one was written as a ternary inside a template literal.
//
// The claim that was wrong: while a newer render was in flight, the displaced take's row read
// `Current`, which is an affirmative statement that this is the shot's answer. It is not; it
// is the previous take, and something else is about to take its place. The row says `Previous`
// then, and a pending row is appended so the strip shows the take that is coming rather than
// leaving the Director to infer it from a border colour in another panel.
// ------------------------------------------------------------------------------------------

// A take file and its `-audio` sibling are one take. The pair lands from one job and the strip
// must not offer the same take twice.
const takeKey = (file) => String(file || "").replace("-audio.mp4", ".mp4");

// The chips a row carries where an action would be. `Use` is the only one that is a verb,
// because it is the only row you can do anything to.
export const TAKE_USE_CHIP = "Use";
export const TAKE_CURRENT_CHIP = "Current";
export const TAKE_PREVIOUS_CHIP = "Previous";
export const TAKE_PENDING_CHIP = "Rendering";
// The pending row's own text, in place of a filename it does not have yet.
export const TAKE_PENDING_ROW = "not landed yet";

export function takesStripRows(project, shot) {
  const files = [];
  const seen = new Set();
  // The job each take came out of, kept beside the filename. Two takes of one shot differ only
  // in a serial buried in a path the row has to truncate -- `…-h3-reference_00001-audio.mp4`
  // against `…_00002-audio.mp4` -- so a Director choosing between them was choosing between two
  // identical-looking lines (the Director's report, 2026-08-21). The job record already holds
  // the two facts that actually tell them apart: the seed it was rendered at, and when it
  // landed. Both are carried here as raw values; the panel formats the time, because a locale
  // string is a rendering decision and this function is the one the contract executes.
  const provenance = new Map();
  for (const job of project?.jobs || []) {
    if (job.kind !== "h3" || job.target_id !== shot?.id) continue;
    for (const file of job.output_files || []) {
      if (!file.endsWith(".mp4")) continue;
      if (seen.has(takeKey(file))) continue;
      seen.add(takeKey(file));
      files.push(file);
      provenance.set(file, job);
    }
  }
  const state = shotRenderState(shot);
  const rows = files.map((file, index) => {
    const current = Boolean(shot?.latest_output) && takeKey(shot.latest_output) === takeKey(file);
    const displaced = current && state.inFlight;
    const job = provenance.get(file);
    return {
      file,
      pending: false,
      current,
      displaced,
      text: `Take ${index + 1} · ${file.split("/").pop()}`,
      // Null rather than 0 when the record does not carry one: 0 is a seed a render can
      // genuinely have used, so it may not double as "unknown".
      seed: Number.isFinite(job?.seed) ? job.seed : null,
      // When the take landed, not when it was queued -- `updated_at` moves when the job
      // settles. "" when the record predates the field, which draws nothing rather than an
      // Invalid Date.
      at: job?.updated_at || job?.created_at || "",
      chip: current ? (displaced ? TAKE_PREVIOUS_CHIP : TAKE_CURRENT_CHIP) : TAKE_USE_CHIP,
      // Only the row the shot already points at is unusable; a displaced row is still that
      // row, and pointing the shot back at a take it is already pointing at does nothing.
      disabled: current,
      className: [current ? "current" : "", displaced ? "displaced" : ""].filter(Boolean).join(" "),
      title: displaced ? `${file} — ${TAKE_DISPLACED_BY_RENDER}` : file,
    };
  });
  if (state.inFlight) {
    rows.push({
      file: "",
      pending: true,
      current: false,
      displaced: false,
      text: `Take ${files.length + 1} · ${TAKE_PENDING_ROW}`,
      seed: null,
      at: "",
      chip: TAKE_PENDING_CHIP,
      disabled: true,
      className: "pending",
      title: state.note,
    });
  }
  return { rows, inFlight: state.inFlight, takes: files.length };
}

// The take-audio acceptance control: shown only with a take to accept, checked from the
// persisted flag. Default unchecked -- "only the main music track and accepted audio
// from videos would come through" is the Director's rule, and nothing infers acceptance.
export function takeAudioControl(shot) {
  return {
    shown: Boolean(shot?.latest_output),
    checked: Boolean(shot?.mix_take_audio),
  };
}

// The newest finished export, read from the job records rather than a field of its own: an
// export *is* a completed local job's output, jobs append in submission order, and a second
// copy of "the latest" is a copy that can lie. The URL is the existing project-media route,
// which serves Range requests, so the player this feeds can scrub.
export function latestAssemblyExport(project) {
  const jobs = project?.jobs || [];
  for (let index = jobs.length - 1; index >= 0; index -= 1) {
    const job = jobs[index];
    if (
      job.kind === "post" && !job.prompt_id && job.status === "complete"
      && (job.output_files || []).length
    ) {
      return {
        path: job.output_files[0],
        url: `/api/projects/${project.id}/media/${job.output_files[0]}`,
        jobId: job.id,
      };
    }
  }
  return null;
}

// The shot statuses a reconciliation tick is allowed to move, and the only ones. The report is a
// snapshot that can be a request older than a click the Director just made, and the whole-list
// shots save writes every field it holds -- so a stale `draft` patched over a fresh `ready` here
// would be re-asserted onto the server by the next drag. Restricting the patch to the render
// path's own transitions (queued/running -> complete/error) makes a stale snapshot harmless: it
// can only decline to move a shot, never march one backwards.
const RENDER_IN_FLIGHT_SHOT_STATUSES = ["queued", "running"];
const RENDER_SETTLED_SHOT_STATUSES = ["complete", "error"];

// Apply one poll answer to the project the Director is looking at, in place, and say what moved.
//
// A patch, deliberately not a `loadProject`: the Director may be mid-keystroke in the shot
// inspector or mid-paste in the song context editors, and a full reload every two seconds is the
// editor-wiping defect this application keeps having to fix. So only render-facing fields move,
// each under its own guard:
//
// * jobs are merged by id -- a known job takes the report's status/outputs/error *and its
//   recorded timing* unless it is already terminal locally (a settled job never regresses under a
//   stale snapshot), an unknown one is appended, and a local job the report has never heard of is
//   KEPT: it was submitted after the snapshot was taken, and dropping it would stop the very
//   polling that will see it.
//
//   The timing is merged here for the same reason it is recorded at all. `RenderStatusReport.jobs`
//   is a list of whole `RenderJob`s, so the measurement is on the wire on the very tick the render
//   settles -- and copying only status/outputs/error left the queue panel's "Took" column empty
//   for every render a Director actually watches finish, with a tooltip saying no timing was ever
//   taken for it, about a job measured 200 ms earlier. Nothing repaired it either: polling stands
//   itself down when the last job settles and never calls `loadProject`, so the blank stood until
//   the next project switch. That is the one surface this whole workstream feeds, blank at the
//   only moment anyone is looking at it.
// * shots move only along the render path -- see the status lists above.
// * an asset adopts a landed file and never un-lands one: `path` is patched only when the report
//   has one and it differs, because `"" over file` is what a stale snapshot says about an upload
//   it predates.
// * the song's audio is adopted only when the loaded Song's `prompt_id` matches the report's --
//   the browser's copy of `apply_job_history`'s guard, for the browser's copy of the race.
//
// Returns `{jobs, shots, assets, song, settled}`: which surfaces need repainting, plus every job
// this answer settled, so the caller can say out loud that a render finished -- the silence that
// caused a double render is the defect this whole path exists to end.
export function applyRenderStatus(project, report) {
  const changes = { jobs: false, shots: false, assets: false, song: false, settled: [] };
  if (!project || !report) return changes;
  const held = new Map((project.jobs || []).map((job) => [job.id, job]));
  for (const job of report.jobs || []) {
    const known = held.get(job.id);
    if (!known) {
      project.jobs = [...(project.jobs || []), job];
      changes.jobs = true;
      continue;
    }
    if (TERMINAL_JOB_STATUSES.includes(known.status)) continue;
    const same = known.status === job.status && known.error === job.error
      && known.render_seconds === job.render_seconds
      && known.render_seconds_source === job.render_seconds_source
      && known.render_frames === job.render_frames
      && JSON.stringify(known.output_files || []) === JSON.stringify(job.output_files || []);
    if (same) continue;
    if (RENDER_SETTLED_SHOT_STATUSES.includes(job.status)) changes.settled.push(job);
    known.status = job.status;
    known.output_files = job.output_files || [];
    known.error = job.error || "";
    // The measurement, adopted with the settle that produced it. `|| default` rather than a
    // straight copy so a report from a server older than the instrumentation cannot write
    // `undefined` into a cell that formats numbers.
    known.render_seconds = job.render_seconds || 0;
    known.render_seconds_source = job.render_seconds_source || "";
    known.render_frames = job.render_frames || 0;
    changes.jobs = true;
  }
  for (const entry of report.shots || []) {
    const shot = (project.shots || []).find((item) => item.id === entry.shot_id);
    if (!shot) continue;
    const moved = RENDER_SETTLED_SHOT_STATUSES.includes(entry.status)
      && RENDER_IN_FLIGHT_SHOT_STATUSES.includes(shot.status);
    if (!moved) continue;
    // A new take displaces the previous take's review -- the mirror of the server's own rule,
    // for the copy of the shot this client keeps drawing until the next full load.
    if (entry.latest_output && entry.latest_output !== shot.latest_output) shot.latest_review = null;
    shot.status = entry.status;
    if (entry.latest_output) shot.latest_output = entry.latest_output;
    changes.shots = true;
  }
  for (const entry of report.assets || []) {
    const asset = (project.assets || []).find((item) => item.id === entry.asset_id);
    if (!asset || !entry.path || asset.path === entry.path) continue;
    asset.path = entry.path;
    changes.assets = true;
  }
  if (
    report.song?.path
    && project.song
    && project.song.prompt_id === report.song.prompt_id
    && project.song.path !== report.song.path
  ) {
    project.song.path = report.song.path;
    changes.song = true;
  }
  return changes;
}

// What a settled render says for itself, named the way the Director knows its target. One
// sentence per job, decided here rather than in the poll loop, so the wording is executed by the
// contract tests -- a completion that reads as an error, or an error that reads as good news, is
// the kind of inversion only an executed string catches.
//
// The shot half goes through `jobTarget`, and that is the fix rather than a tidy-up. A render can
// outlive its shot -- a populate mid-render replaces `project.shots` wholesale and mints new ids
// -- and this toast used to call `shotLabel` directly, which for an id no shot has returns the
// bare `shot_9f2c…` string. So the completion of a long H3 render could arrive as "Render
// complete: shot_9f2c4b1e0a77 is ready", naming a shot the Director cannot find anywhere on the
// timeline, exactly as the queue row did until 2026-08-22. `jobTarget` already owns that
// question, says "shot no longer on the plan" in the one place it is worded, and covers `ltx` as
// well as `h3`.
export function renderSettledToast(project, job) {
  let name = job.target_id || job.kind;
  if (job.kind === "flux" || job.kind === "multiview") {
    name = (project?.assets || []).find((asset) => asset.id === job.target_id)?.name || "an asset";
  } else if (JOB_KINDS_TARGETING_A_SHOT.includes(job.kind)) {
    name = jobTarget(project, job).label;
  } else if (job.kind === "music") {
    name = project?.song?.title || "the song";
  }
  if (job.status === "error") {
    return `Render failed for ${name}: ${job.error || "ComfyUI reported an execution error"}`;
  }
  return `Render complete: ${name} is ready`;
}

// -----------------------------------------------------------------------------------------
// The timeline's viewport: the zoom scale, and the scroll offset that makes a real plan
// reachable.
//
// The Director's report, 2026-08-20: "I cant scroll left or right and i see what i think is a
// zoom slider that isnt functional." Both halves were true and neither was what it looked
// like. The slider is `#master-volume`, a *working* volume control sitting in the transport
// row, taken for a zoom because the timeline had no zoom slider at all -- so one is added
// rather than the volume one repurposed. And the tracks really were in a scrollable box; its
// horizontal scrollbar was laid out 61px below the bottom of the window, where the panel's
// `overflow: hidden` put it permanently out of reach. `styles.css` carries that half.
//
// Everything below is arithmetic on purpose: pixels and seconds, no DOM. That is what lets the
// anchor rule be *executed* by the offline contract rather than only read -- and a scroll that
// does not scroll is exactly the class of defect a stub DOM cannot see.

// The scale bounds every zoom control clamps to. One spelling, because three controls
// disagreeing about how far in you may go is a defect that only shows on the one you did not
// try.
export const TIMELINE_ZOOM_MIN = 6;
export const TIMELINE_ZOOM_MAX = 64;
// What the label calls 100%. The scale the timeline opens at and the divisor the percentage is
// read against are the same number by construction.
export const TIMELINE_ZOOM_BASE = 16;
// One press of the +/- buttons.
export const TIMELINE_ZOOM_STEP = 1.25;
// The label gutter every track carries, and therefore the offset in every pixel<->second
// conversion on this timeline. `.track { grid-template-columns: 90px 1fr }` in the stylesheet.
export const TIMELINE_LABEL_WIDTH = 90;
// The slider's integer travel. `<input type="range">` steps in integers, and 1000 notches over
// a 10.7x range is finer than a pixel of thumb travel, so the control reads as continuous.
export const TIMELINE_ZOOM_SLIDER_MAX = 1000;

export function clampTimelineZoom(pixelsPerSecond) {
  const value = Number(pixelsPerSecond);
  if (!Number.isFinite(value)) return TIMELINE_ZOOM_BASE;
  return Math.min(Math.max(value, TIMELINE_ZOOM_MIN), TIMELINE_ZOOM_MAX);
}

// Slider position for a scale, and the scale for a position -- a *logarithmic* pair, so one
// notch is the same proportional change at 6 px/s as at 64. A linear mapping spends four
// fifths of its travel above 100% and leaves the readable half of the range unpickable.
export function zoomSliderValue(pixelsPerSecond) {
  const span = Math.log(TIMELINE_ZOOM_MAX / TIMELINE_ZOOM_MIN);
  const at = Math.log(clampTimelineZoom(pixelsPerSecond) / TIMELINE_ZOOM_MIN);
  return Math.round((at / span) * TIMELINE_ZOOM_SLIDER_MAX);
}

export function zoomFromSlider(value) {
  const raw = Number(value);
  const position = Number.isFinite(raw)
    ? Math.min(Math.max(raw, 0), TIMELINE_ZOOM_SLIDER_MAX)
    : 0;
  const span = Math.log(TIMELINE_ZOOM_MAX / TIMELINE_ZOOM_MIN);
  return clampTimelineZoom(
    TIMELINE_ZOOM_MIN * Math.exp((position / TIMELINE_ZOOM_SLIDER_MAX) * span)
  );
}

export function zoomLabelText(pixelsPerSecond) {
  return `${Math.round((clampTimelineZoom(pixelsPerSecond) / TIMELINE_ZOOM_BASE) * 100)}%`;
}

export const TIMELINE_ZOOM_ANCHORS = { playhead: "playhead", centre: "centre" };

// Where the viewport should sit after a zoom. **Never zero**: re-scaling a 30-shot plan back to
// the head of the song every time is its own defect, and the one the buttons had.
//
// The anchor is the playhead when the playhead is on screen, and the viewport centre otherwise.
// The reason is what the Director is looking at. The playhead is the timeline's subject -- the
// Monitor above plays the shot under it -- so while it is visible it is the frame being judged
// and it must not move. Scrolled away to a later section it is not on screen at all, and holding
// an off-screen second fixed would move everything the Director *is* reading; there the centre
// of the visible band is the honest invariant. Ctrl+wheel keeps its own third anchor, the
// pointer, because for that gesture the pointer is by definition the thing of interest.
//
// The returned `scrollLeft` has no upper clamp: assigning past the end is clamped by the
// browser against the content it has just laid out, which is the only reading of the new width
// that is not a guess.
export function zoomViewport({
  scrollLeft = 0,
  viewportWidth = 0,
  pixelsPerSecond = TIMELINE_ZOOM_BASE,
  toPixelsPerSecond = TIMELINE_ZOOM_BASE,
  playheadSeconds = 0,
  labelWidth = TIMELINE_LABEL_WIDTH,
} = {}) {
  const from = clampTimelineZoom(pixelsPerSecond);
  const to = clampTimelineZoom(toPixelsPerSecond);
  const left = Number.isFinite(scrollLeft) ? Math.max(0, scrollLeft) : 0;
  const width = Number.isFinite(viewportWidth) && viewportWidth > 0 ? viewportWidth : 0;
  const playhead = Number.isFinite(playheadSeconds) ? Math.max(0, playheadSeconds) : 0;
  const playheadX = labelWidth + playhead * from;
  const onScreen = width > 0 && playheadX >= left && playheadX <= left + width;
  const anchorSeconds = onScreen
    ? playhead
    : Math.max(0, (left + width / 2 - labelWidth) / from);
  // How far into the viewport the anchor sits now, kept exactly there afterwards.
  const offset = onScreen ? playheadX - left : width / 2;
  return {
    pixelsPerSecond: to,
    anchor: onScreen ? TIMELINE_ZOOM_ANCHORS.playhead : TIMELINE_ZOOM_ANCHORS.centre,
    anchorSeconds,
    scrollLeft: Math.max(0, labelWidth + anchorSeconds * to - offset),
  };
}

export const TIMELINE_WHEEL_ACTIONS = { zoom: "zoom", scroll: "scroll", native: "native" };

// What one wheel notch over the tracks should do. **The plain wheel scrolls along the song**,
// which is the timeline convention in every editing application the Director already uses, and
// the direct answer to "I cant scroll left or right": the axis this panel is about is time.
//
// - Ctrl (or Cmd) zooms about the pointer, the gesture every editor teaches.
// - Shift is the escape hatch back to vertical, because the plain wheel is taken. It inverts the
//   browser's own shift-is-horizontal habit on purpose: inside a timeline, horizontal is the
//   unmodified gesture, so the modifier has to mean the other one or vertical has no wheel at
//   all. The four tracks are five fixed rows deep and the scrollbar is right there, so this is a
//   fallback rather than a daily gesture.
// - A horizontal delta -- a trackpad swipe -- is already asking for horizontal and gets it.
// - With nothing to scroll horizontally the wheel is handed straight back to the browser, so a
//   short plan that fits its box behaves exactly as any other page does.
//
// The earlier rule here was "hijack deltaY only when there is no vertical overflow", and it was
// measured wrong in the browser: at 1600x1100 the tracks overflowed their box by *four pixels*,
// which was enough to hand the wheel back and leave the gesture as dead as it was before.
export function timelineWheelPlan({
  deltaX = 0,
  deltaY = 0,
  ctrlKey = false,
  metaKey = false,
  shiftKey = false,
  canScrollX = false,
  canScrollY = false,
} = {}) {
  const still = { action: TIMELINE_WHEEL_ACTIONS.native, delta: 0, scrollX: 0, scrollY: 0 };
  if (ctrlKey || metaKey) {
    return { action: TIMELINE_WHEEL_ACTIONS.zoom, delta: deltaY, scrollX: 0, scrollY: 0 };
  }
  const move = (x, y) => ({ action: TIMELINE_WHEEL_ACTIONS.scroll, delta: 0, scrollX: x, scrollY: y });
  if (shiftKey && deltaY) return canScrollY ? move(0, deltaY) : still;
  if (!canScrollX) return still;
  if (deltaX) return move(deltaX, 0);
  if (deltaY) return move(deltaY, 0);
  return still;
}

// ---- Beat markers over the timeline waveform -------------------------------------------------
//
// Story 8.1 measured the song into an envelope carrying beats and onsets in absolute seconds and
// nothing showed them: the Director cuts against a waveform that draws amplitude and nothing
// else, inferring where the beat falls from the shape of the peaks. This is the drawing half.
//
// Everything below is arithmetic, `zoomViewport`'s rule one section up: seconds and pixels, no
// DOM. `app.js` positions and writes, and re-derives none of it.

// The band the marks are drawn into, and the toggle that shows them. Constants rather than
// literals, `VRAM_EJECT_CONTROL`'s reason exactly: an id spelled once here and once in the
// markup is an id that drifts, and with it here the contract test holds both to one string.
export const BEAT_MARKERS_BAND = "#beat-band";
export const BEAT_MARKERS_CONTROL = "#beat-markers";
export const BEAT_MARKERS_LABEL = "Beat markers";
// The band draws two kinds and only one of them is a snap target, which is a thing the Director
// can see and could not otherwise learn: measured on this project's own masters, onsets run
// 2.9–4.1 a second against 2.0–2.1 beats, so at the 16 px/s default they sit ~5.6 px apart and the
// local-spacing cap would give them a ~1.9 px pull — a target in name only. Rather than promote
// them, the band says what each kind is for. A tick the Director cannot land on is fine; a tick
// they cannot land on and were never told about is the contradiction.
export const BEAT_MARKERS_HELP =
  "Draws the beats and onsets the song analysis measured over the master waveform. Beats are " +
  "snap targets; onsets are reference marks, showing where the transients are without being " +
  "somewhere a cut can land. Display only — showing or hiding them changes nothing about any " +
  "shot and writes nothing to the project. A song that has not been analysed, or one replaced " +
  "since it was, simply draws nothing.";

// The two kinds, and the class each is drawn with. **The class is decided here**, not in the
// template, because it is the other half of the placement decision: a beat is a taller, heavier
// rule and an onset a short thin tick, which is what tells them apart by height and weight rather
// than by hue. Both take inert tokens. Standing law 7 closes the palette at six accents with fixed
// meanings -- `--cyan` already means *approved* and `--blue` is reserved permanently for
// transitions and reactive bindings -- and a marker is a reference mark, not a state.
export const BEAT_MARKER_KINDS = { beat: "beat", onset: "onset" };
export const BEAT_MARKER_CLASSES = { beat: "beat-mark", onset: "onset-mark" };

// How close two marks of the same kind may come before the further one is dropped, and how close
// an onset may come to a *kept beat* before the onset is dropped.
//
// **Measured, on a real 3-minute track analysed by this application on 2026-08-24:** 440 beats and
// 332 onsets, 772 marks in all. That is 2.44 beats and 1.84 onsets a second -- an average onset
// every 0.54 s, nowhere near the 0.07 s floor the picker enforces, so the picker's floor is the
// wrong number to design against. What those densities become on screen:
//
//   * 40 px/s -- a beat every 16.4 px, an onset every 21.7 px: comfortable, nothing is dropped.
//   * 16 px/s (the default) -- 6.5 px and 8.7 px: dense but legible, and both kinds survive.
//   *  6 px/s (the floor)   -- 2.5 px between beats. Against a 2 px mark that is ~80% ink, which
//      is a solid bar and not a reference mark, however short it is drawn.
//
// So the same-kind gap is what stops the far end of the zoom being a wall, and it is deliberately
// larger than the clearance: a clearance wide enough to thin the band would also wipe every onset
// out at the default zoom, where beats already sit 6.5 px apart. The clearance's only job is that
// two marks never land on the same pixel, and when they compete **the beat wins** -- it is the
// stronger of the two claims, and the one a cut is placed against.
export const BEAT_MARKER_MIN_GAP_PIXELS = 6;
export const BEAT_MARKER_CLEARANCE_PIXELS = 2;

// Where every mark goes, and whether there are any at all.
//
// `envelope` takes a **report carrying a measurement** -- the timeline's own merged read
// `{gaps, beats, measured, analysed, envelope}`, or `GET /song/envelope`'s `{present, reason,
// envelope}` -- or a bare envelope, because absence has a named reason for every way it can happen
// and **every one of them draws the same nothing here**. Absence is silence: no error, no toast,
// no refusal text, and a timeline identical to the one this application drew before the feature
// existed. The named refusal with its `[Analyze song]` action belongs where a consumer genuinely
// *needs* an envelope; beat markers do not need one, they simply do not draw. Nothing here reads
// `reason`, so a reason this file has never heard of draws exactly the same nothing as the ones
// it has.
//
// **The unwrap is a key test, never a duck test, and that is load-bearing.** The merged read
// carries *two* `beats` lists and they are not the same list: the top-level one is
// `drag_snap_targets`' window-filtered, NaN-stripped set of seconds a cut may land on, and the
// measurement's own is every beat the analysis found. A plan that read the report as though it
// were a bare envelope would draw the drag's targets as marks -- marks that look right and are a
// different set. So a report is recognised by the keys only a report has, and only then is
// `envelope` taken out of it.
//
// Offsets are `seconds * pixelsPerSecond` -- the same expression the clips are laid out with,
// deliberately, and *not* a fraction of `trackWidth`. Below `900 / pixelsPerSecond` the waveform
// canvas stretches to its 900px floor while the clips stay on their seconds, so the waveform and
// the clips already disagree down there. That is pre-existing and is not fixed here; what this
// settles is which of the two a mark agrees with, and the answer is the clips, because a beat
// marker exists to say where to put a cut.
//
// `trackWidth` and `duration` both trim, and both are needed. The track has that same 900px floor,
// so on a short song at a low zoom the drawn width runs past the end of the audio -- and a mark
// out there would sit on silence that was never measured. A mark is drawn only where the two agree
// there is still song.
//
// The two lists are assumed ascending, which is what `audio.py` writes. Given anything else the
// thinning under-drops rather than mis-places: every mark still lands on its own second.
export function beatMarkerPlan({
  envelope = null,
  pixelsPerSecond = TIMELINE_ZOOM_BASE,
  trackWidth = 0,
  duration = 0,
  enabled = true,
  minimumGap = BEAT_MARKER_MIN_GAP_PIXELS,
  clearance = BEAT_MARKER_CLEARANCE_PIXELS,
} = {}) {
  const empty = { markers: [], beats: 0, onsets: 0 };
  if (!enabled) return empty;
  const scale = Number(pixelsPerSecond);
  const width = Number(trackWidth);
  if (!Number.isFinite(scale) || scale <= 0) return empty;
  if (!Number.isFinite(width) || width <= 0) return empty;
  const report = envelope && typeof envelope === "object" ? envelope : null;
  if (!report) return empty;
  // A carrier is anything shaped like a reply rather than like a measurement. `envelope` is the
  // merged read's own key and is present on it even when there is nothing to draw; `gaps` and
  // `present` are the other two replies' unmistakable marks. A bare envelope has none of the
  // three, so it is passed through as itself.
  const carrier = "envelope" in report || "gaps" in report || "present" in report;
  const measured = carrier ? (report.envelope || null) : report;
  if (!measured || typeof measured !== "object") return empty;
  const positive = (value) => (Number.isFinite(Number(value)) && Number(value) > 0 ? Number(value) : 0);
  const song = positive(duration);
  const limit = song ? Math.min(width, song * scale) : width;
  const gap = positive(minimumGap);
  const clear = positive(clearance);
  // `typeof === "number"` rather than a coercion, because this file's own producer writes real
  // numbers and `allow_nan=False` on the write side guarantees they are finite: coercing would
  // quietly turn a `null` in a malformed array into a mark at second zero.
  const placed = (times, kind) => (Array.isArray(times) ? times : [])
    .filter((seconds) => typeof seconds === "number" && Number.isFinite(seconds) && seconds >= 0)
    .map((seconds) => ({
      kind,
      seconds,
      left: seconds * scale,
      className: BEAT_MARKER_CLASSES[kind],
    }))
    .filter((mark) => mark.left < limit);
  // Thinned to a minimum spacing, first-wins, so the result is a deterministic function of the
  // measurement rather than of which end it was scanned from. `against` is the marks this kind
  // yields to -- ascending, so the cursor only ever moves forward.
  const spaced = (marks, room, against = []) => {
    const kept = [];
    let cursor = 0;
    for (const mark of marks) {
      while (cursor < against.length && against[cursor].left <= mark.left - clear) cursor += 1;
      if (cursor < against.length && against[cursor].left < mark.left + clear) continue;
      const previous = kept[kept.length - 1];
      if (previous && mark.left - previous.left < room) continue;
      kept.push(mark);
    }
    return kept;
  };
  const beats = spaced(placed(measured.beats, BEAT_MARKER_KINDS.beat), gap);
  const onsets = spaced(placed(measured.onsets, BEAT_MARKER_KINDS.onset), gap, beats);
  // Onsets first in the markup, so a beat paints over an onset that survived beside it rather than
  // under it -- the clearance keeps the two off the same pixel, not out of the same neighbourhood.
  return { markers: [...onsets, ...beats], beats: beats.length, onsets: onsets.length };
}

// Which song a measurement belongs to. **Half of `snapTargetsIdentity`, which is what the one read
// is actually keyed on** -- kept as its own function because it is the half that answers "is this
// the same measurement of the same file", and because separating it is what makes the superset
// argument above checkable rather than asserted.
//
// The **fingerprint** is in the key, not only the path. A measurement retaken over the same file --
// the Director pressing Analyze again, a re-render landing on the same output name -- changes the
// fingerprint and nothing else, and keyed on the path alone it would never be re-read at all.
export function songEnvelopeIdentity(projectId, song = null) {
  const id = projectId || "";
  const path = song?.path || "";
  if (!id || !path) return `${id}::`;
  return `${id}:${path}:${song?.analysis?.song_fingerprint || ""}`;
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
  // `updated_at` is the revision this list was edited against; the server refuses the save
  // when it is stale (409) instead of silently overwriting later work — the 2026-08-19
  // revert, where one background save from a tab loaded earlier reverted 32 prompts at once.
  saveShots: (id, shots, updated_at = null) => request(`/api/projects/${id}/shots`, { method: "PUT", headers: jsonHeaders, body: JSON.stringify(updated_at ? { shots, updated_at } : { shots }) }),
  // The Director's section marks (Intro/Verse/Chorus...), replaced whole like the shot
  // list and for its reason. The server sorts and refuses overlaps.
  saveSections: (id, sections) => request(`/api/projects/${id}/sections`, { method: "PUT", headers: jsonHeaders, body: JSON.stringify({ sections }) }),
  // Read every section's shared look out of the Treatment and the Style Bible. Report first,
  // apply on confirm -- `snapCuts`' two-stage shape and `populate`'s at bottom -- and
  // `overwrite` is the *separate* consent for replacing a look the Director wrote by hand.
  // `plan` is the report being confirmed, echoed back whole. The server mints a `plan_id` over
  // the report it emitted and refuses a confirm it cannot match, so the looks that land are the
  // looks that were read in the confirm dialog — and the confirming call asks no model at all.
  fillSectionLooks: (id, { confirmApply = false, overwrite = false, plan = null } = {}) => request(`/api/projects/${id}/sections/fill-looks`, { method: "POST", headers: jsonHeaders, body: JSON.stringify({ confirm_apply: confirmApply, overwrite, plan }) }),
  uploadSong: (id, data) => request(`/api/projects/${id}/songs/upload`, { method: "POST", body: data }),
  // Its own route, and it carries only the two context fields: the audio, the duration and the
  // provenance are not editable text, so nothing that could overwrite them is on the wire.
  saveSongContext: (id, context) => request(`/api/projects/${id}/song/context`, { method: "PUT", headers: jsonHeaders, body: JSON.stringify(context) }),
  // Recovery for one context field. No body, exactly as the document restore has none: the kept
  // version lives on the server and nothing the client could send is the authority on it.
  restoreSongContext: (id, field) => request(`/api/projects/${id}/song/context/${field}/restore`, { method: "POST" }),
  // The vocal type's one door, deliberately not folded into `saveSongContext` or `saveProject`.
  // The whole-project PUT re-adopts the stored value and can never write this field, which is
  // what stops an ordinary save from un-declaring the cast; and keeping it off the context body
  // means the route that can rewrite the lyric sheet and the route that declares the cast are two
  // different doors.
  saveVocalType: (id, vocal_type) => request(`/api/projects/${id}/song/vocal-type`, { method: "PUT", headers: jsonHeaders, body: JSON.stringify({ vocal_type }) }),
  // The flag is the Director's acknowledgement, so it is passed through rather than
  // hardcoded: a caller that never showed SONG_CHANGE_CONSEQUENCE must not claim it did.
  removeSong: (id, confirmed = false) => request(`/api/projects/${id}/song?confirm_song_replacement=${confirmed ? "true" : "false"}`, { method: "DELETE" }),
  // Whisper over the track, the sheet's [Tag] blocks timed against it, the Sections row
  // filled from the measurement. First run transcribes (minutes, CPU); the words are kept
  // on the Song so every later run is instant.
  alignLyrics: (id, body = {}) => request(`/api/projects/${id}/song/align-lyrics`, { method: "POST", headers: jsonHeaders, body: JSON.stringify(body) }),
  // **`GET /song/envelope` is deliberately not here.** It still exists, unchanged, and still
  // serves a whole measurement to anyone who wants one -- but this browser is not one of them:
  // `snapTargets` below carries the part the timeline draws beside the seconds a drag lands on,
  // from one computation on the server. Two client reads of one measurement is what let the band
  // and the drag describe different states, and re-adding an entry here is how that comes back.
  //
  // Measure the song again, now, because the Director asked. `force=True` on the server, so it
  // re-measures rather than skipping an analysis it considers current -- the whole difference
  // between this and the measurement that rides a song import. It answers the **Project**, not
  // the measurement: that is read back through `snapTargets` below, which is what keeps a
  // multi-megabyte measurement off every route that returns a Project.
  //
  // Four refusals a caller must be ready for, all of them the server's own sentence: **422** with
  // no song or no audio yet, **404** for an unknown project or a song file that is gone, and
  // **502** carrying a named analysis reason -- no ffmpeg on this machine is the one that made
  // this route worth wiring, because it was previously indistinguishable on screen from a song
  // nobody had got round to measuring. `errorMessage` renders every one of them into the thrown
  // Error, so the click site shows what the server said rather than a status code.
  analyzeSong: (id) => request(`/api/projects/${id}/song/analyze`, { method: "POST" }),
  // Every second a dragged shot edge may land on: the voiceless-gap targets `timeline.py` itself
  // chooses -- the same seconds the batch "Snap cuts" button lands a cut on -- and the beats the
  // song analysis measured. Computed server-side so the drag and the button can never hold two
  // opinions about where a cut belongs. Absence of either half is a 200 carrying the half that
  // exists, so a caller reads the lists and never a status code.
  //
  // **And the measurement the waveform draws, under `envelope`**, from the same read: the beats
  // and onsets, plus the two small per-band arrays AD-26's band selector will want. Not the
  // per-frame series -- `bands`, `rms`, `peak` and `flux` are 98% of the sidecar and nothing here
  // reads a byte of them. `null` when the song has no measurement, which is `analysed: false`
  // said in the shape the band consumes.
  //
  // Read once on a load path and **never behind a timer**: reaching the beats hashes the master
  // to decide whether the measurement still describes it, which is a multi-megabyte read to
  // answer a question whose answer changes only when the song does.
  snapTargets: (id) => request(`/api/projects/${id}/timeline/snap-targets`),
  // One pointer moves: `output` switches among the shot's own takes (provenance is its
  // job history), `asset_id` attaches a video asset as the shot's clip.
  selectTake: (projectId, shotId, body) => request(`/api/projects/${projectId}/shots/${shotId}/select-take`, { method: "POST", headers: jsonHeaders, body: JSON.stringify(body) }),
  // Deletion, each with its own server-side guard: the project asks for its confirm flag,
  // the asset refuses while cited, the job settles its record as it cancels on ComfyUI.
  deleteProject: (id) => request(`/api/projects/${id}?confirm_delete=true`, { method: "DELETE" }),
  deleteAsset: (projectId, assetId) => request(`/api/projects/${projectId}/assets/${assetId}`, { method: "DELETE" }),
  // The way through that refusal, and never around it: this moves citations and deletes
  // nothing. `confirm_apply` false is a report the route refuses to save on, so the two-stage
  // shape is the server's rule and not this function's manners.
  replaceAssetCitations: (projectId, assetId, replacementId, confirmApply) => request(`/api/projects/${projectId}/assets/${assetId}/replace-citations`, { method: "POST", headers: jsonHeaders, body: JSON.stringify({ replacement_id: replacementId, confirm_apply: confirmApply }) }),
  cancelJob: (projectId, jobId) => request(`/api/projects/${projectId}/jobs/${jobId}`, { method: "DELETE" }),
  // Every open render at once. `confirm_cancel` is the acknowledgement itself, server-enforced --
  // sent true only after the dialog `cancelAllPlan` writes, exactly as `confirm_gpu` is. The reply
  // is the report, not the project, so the caller reloads: twenty-six settles moved twenty-six
  // shots and a patch of the last one would leave the other twenty-five stale on screen.
  cancelOpenJobs: (projectId) => request(`/api/projects/${projectId}/jobs?confirm_cancel=true`, { method: "DELETE" }),
  uploadAsset: (id, data) => request(`/api/projects/${id}/assets/upload`, { method: "POST", body: data }),
  generateMusic: (id, body) => request(`/api/projects/${id}/generate/music`, { method: "POST", headers: jsonHeaders, body: JSON.stringify(body) }),
  generateSongPlanner: (id, body) => request(`/api/projects/${id}/generate/songplanner`, { method: "POST", headers: jsonHeaders, body: JSON.stringify(body) }),
  generateFlux: (id, body) => request(`/api/projects/${id}/generate/flux`, { method: "POST", headers: jsonHeaders, body: JSON.stringify(body) }),
  generateMultiview: (projectId, assetId, body) => request(`/api/projects/${projectId}/assets/${assetId}/multiview`, { method: "POST", headers: jsonHeaders, body: JSON.stringify(body) }),
  // AI Mod: one instruction, one new asset beside the source. The server wraps a plain
  // sentence in the workflow's own prompting form; a full structured prompt travels verbatim.
  editAsset: (projectId, assetId, body) => request(`/api/projects/${projectId}/assets/${assetId}/edit`, { method: "POST", headers: jsonHeaders, body: JSON.stringify(body) }),
  analyzeAsset: (projectId, assetId) => request(`/api/projects/${projectId}/assets/${assetId}/analyze`, { method: "POST" }),
  // The anchor's one door. Deliberately not folded into `saveProject`: the whole-project PUT
  // re-adopts the stored anchor and can never write this field, which is what stops an ordinary
  // save from blanking it.
  saveConsistencyPrompt: (projectId, assetId, consistency_prompt) => request(`/api/projects/${projectId}/assets/${assetId}/consistency-prompt`, { method: "PUT", headers: jsonHeaders, body: JSON.stringify({ consistency_prompt }) }),
  // The character slot's one door, on the anchor's own argument: the whole-project PUT re-adopts
  // the stored slot per asset id and can never write this field, so no ordinary save can un-slot
  // the cast and leave every `(S1)` in the sheet resolving to nothing.
  saveCharacterSlot: (projectId, assetId, character_slot) => request(`/api/projects/${projectId}/assets/${assetId}/character-slot`, { method: "PUT", headers: jsonHeaders, body: JSON.stringify({ character_slot }) }),
  // The sampling bundle's one door, on the same argument as the two above: the whole-project PUT
  // re-adopts the stored profile and can never write this field, so a stale tab cannot reassert
  // the bundle it was holding and spend a batch on a graph nobody selected. The reply is the whole
  // project, which is what lets the select repaint from the server rather than from the click.
  saveSamplingProfile: (projectId, profile) => request(`/api/projects/${projectId}/sampling-profile`, { method: "PUT", headers: jsonHeaders, body: JSON.stringify({ profile }) }),
  // The display name's one door, on the same argument as the two above — and here the hazard runs
  // the other way: `name` is required, so a stale tab does not omit it, it *reasserts* it, and one
  // ordinary save would undo a rename. The whole-project PUT re-adopts the stored name per asset
  // id, so this is the only call that can change one. Answers `{project, name, previous, prompts,
  // maps, message}` rather than a bare project: a rename has consequences that are invisible in
  // the manifest, and the message is where they are said.
  renameAsset: (projectId, assetId, name) => request(`/api/projects/${projectId}/assets/${assetId}/name`, { method: "PUT", headers: jsonHeaders, body: JSON.stringify({ name }) }),
  analyzeLatestTake: (projectId, shotId) => request(`/api/projects/${projectId}/shots/${shotId}/analyze-latest`, { method: "POST" }),
  compileTimeline: (id, body) => request(`/api/projects/${id}/timeline/compile`, { method: "POST", headers: jsonHeaders, body: JSON.stringify(body) }),
  // A GET, and nothing is cached from it: readiness is derived from the prompts on every call, so
  // the only stale answer possible is one this client held on to.
  readiness: (id) => request(`/api/projects/${id}/readiness`),
  generateH3: (projectId, shotId, body = {}) => request(`/api/projects/${projectId}/shots/${shotId}/generate/h3`, { method: "POST", headers: jsonHeaders, body: JSON.stringify(body) }),
  // Generate All (FR-4): one POST, the server submits per shot through the identical
  // single-shot gates and reports what queued and what was skipped, each by name.
  // confirm_gpu is the acknowledgement itself — sent true only after the confirm dialog.
  generateBatch: (projectId, body) => request(`/api/projects/${projectId}/generate/batch`, { method: "POST", headers: jsonHeaders, body: JSON.stringify(body) }),
  // Re-open one settled shot for another take. Its own route and no body at all: the shots write
  // is the generic full-project-shaped one, and sending a whole shot list to say "render this
  // again" is how a stale client silently reverts every other shot in the plan. Nothing is
  // rendered by this and no GPU time is spent -- the re-opened shot is queued like any other.
  renderAgain: (projectId, shotId) => request(`/api/projects/${projectId}/shots/${shotId}/render-again`, { method: "POST" }),
  // Both bodyless on purpose, like every purpose-built shot action: approval is the one field
  // the route writes, and it writes it from its own manifest rather than from anything a client
  // could put on the wire.
  approveTake: (projectId, shotId) => request(`/api/projects/${projectId}/shots/${shotId}/approve`, { method: "POST" }),
  unapproveTake: (projectId, shotId) => request(`/api/projects/${projectId}/shots/${shotId}/unapprove`, { method: "POST" }),
  // The two sides of a shot's first render, and neither carries a body. The shots write would have
  // done this too -- it is the only thing that could, which is why no shot could reach a render
  // through the interface at all -- but it takes the whole shot list, so a request meaning "I have
  // decided to render this one" would also reassert every prompt, window and lock this client
  // happens to be holding. Nothing is rendered by either and no GPU time is spent.
  markShotReady: (projectId, shotId) => request(`/api/projects/${projectId}/shots/${shotId}/mark-ready`, { method: "POST" }),
  markShotDraft: (projectId, shotId) => request(`/api/projects/${projectId}/shots/${shotId}/mark-draft`, { method: "POST" }),
  directorChat: (id, body) => request(`/api/projects/${id}/director/chat`, { method: "POST", headers: jsonHeaders, body: JSON.stringify(body) }),
  // Asset Fill (the Director's stage 3): the Stage Manager assesses the library and queues
  // one Flux render per proposal. confirm_gpu is the acknowledgement, server-enforced.
  fillAssets: (id, count) => request(`/api/projects/${id}/assets/fill`, { method: "POST", headers: jsonHeaders, body: JSON.stringify({ count, confirm_gpu: true }) }),
  // Populate Timeline (the Director's stage 4): destructive by design and doubly guarded —
  // the button shows the warning, and the server refuses without confirm_replace in the
  // same words. Nothing is rendered by it; the shots land as drafts.
  populateTimeline: (id, confirmReplace) => request(`/api/projects/${id}/timeline/populate`, { method: "POST", headers: jsonHeaders, body: JSON.stringify({ confirm_replace: confirmReplace }) }),
  // Report first, apply on confirm. `confirmApply` false is a read: the route does not save
  // and answers with no project at all, which is how "nothing was written" reaches the client
  // as a fact rather than as a promise. The flag is passed through rather than hardcoded, for
  // `generateBatch`'s reason -- a caller that never showed the report must not claim it did.
  snapCuts: (id, tolerance, confirmApply = false) => request(`/api/projects/${id}/timeline/snap-cuts`, { method: "POST", headers: jsonHeaders, body: JSON.stringify({ tolerance, confirm_apply: confirmApply }) }),
  // Its own route, and it carries no body: expansion is not a chat turn. The whole input the
  // model sees is derived on the server from the project itself, so there is nothing here for a
  // message to travel in — and nothing that could queue a render.
  expandShots: (id, focus = "story") => request(`/api/projects/${id}/director/expand?focus=${focus}`, { method: "POST" }),
  // Pass two, one shot. No body for `expandShots`' reason and one more: everything the specialist
  // needs is already on the shot, so there is nothing here a stale client could assert. The reply
  // is not the project -- it is the project *plus* whether the answer was applied, what the format
  // checker said, and the text itself, because a refused prompt is something the Director reads
  // and judges rather than something to throw away.
  expandShotPrompt: (projectId, shotId) => request(`/api/projects/${projectId}/shots/${shotId}/expand-prompt`, { method: "POST" }),
  // Pass two, the whole plan: one model call per shot, on the server, judged per shot. No body --
  // every shot in the plan is swept, including the ones nothing can be written to, so there is no
  // selection for this to carry. The reply is the whole project with the per-shot report in its
  // notices. Nothing is rendered by it and no GPU time is spent.
  expandPlanPrompts: (id) => request(`/api/projects/${id}/shots/expand-prompts`, { method: "POST" }),
  // Assistant ProducerBot. The body carries the Director's own words and the shots they selected,
  // and the selection is the turn's consent: the server refuses a tool call naming anything else,
  // so this is the one place the scope of an assistant turn is decided. No render is queued by it
  // and no GPU time is spent -- the reply is the whole project, with a per-shot report in its
  // notices.
  assistantFill: (id, body) => request(`/api/projects/${id}/assistant/fill`, { method: "POST", headers: jsonHeaders, body: JSON.stringify(body) }),
  job: (projectId, jobId) => request(`/api/projects/${projectId}/jobs/${jobId}`),
  // AD-1's poll endpoint: one GET reconciles every open job against ComfyUI's /queue (fetched
  // once per tick) and /history (only for jobs the queue no longer holds), and answers with the
  // fixed jobs+states shape `applyRenderStatus` patches in. Never a per-job fan-out from here.
  renderStatus: (id) => request(`/api/projects/${id}/render-status`),
  // Assembly. One field, and one only: which preset to build. Every other input is the
  // manifest's own -- approved takes, snapshotted windows, the master song -- so there is
  // nothing else here a stale client could assert. Synchronous by design; the reply carries
  // the settled job, the preset it built and the measured export. Nothing is queued on
  // ComfyUI and no GPU time is spent.
  assemble: (id, preset = EXPORT_PRESET_DEFAULT) => request(`/api/projects/${id}/assemble`, { method: "POST", headers: jsonHeaders, body: JSON.stringify({ preset }) }),
  workflows: () => request("/api/workflows"),
  // Machine-scoped, so neither call carries a project id. The GET is what refreshes the
  // after-the-fact report; the PUT is the only thing that changes the setting, and the server
  // answers both with the same shape so nothing here has to merge two views of one value.
  vramEject: () => request("/api/vram-eject"),
  setVramEject: (enabled) => request("/api/vram-eject", { method: "PUT", headers: jsonHeaders, body: JSON.stringify({ enabled }) }),
};
