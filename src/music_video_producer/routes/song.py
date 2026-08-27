"""The master song: its audio, the context written around it, and its measurement.

`GET /song/envelope` is here. The frontend-contract test that asserts its `@app.get(...)`
decorator line appears verbatim now looks for that one literal across the package rather than
in `app.py`, which is what the assertion always meant. The read-time report it serves,
`song_envelope_report`, stays in `create_app` and reaches this module and the timeline's
snap-targets read through `RouterContext` -- one computation, two resources.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool

from ..app import (
    ALIGN_LYRICS_NOTHING_PLACED,
    ALIGN_LYRICS_SECTIONS_EXIST,
    ALIGN_LYRICS_TRANSCRIBE_FAILED,
    ALIGN_LYRICS_WITHOUT_SONG,
    ALIGN_LYRICS_WITHOUT_TAGS,
    RECOVERY_SLOT_SUFFIX,
    SONG_ANALYSIS_WITHOUT_SONG,
    SONG_CAPTION_FIELD,
    SONG_CAPTION_LIMIT,
    SONG_CONTEXT_FIELD_NAMES,
    SONG_CONTEXT_LABELS,
    SONG_CONTEXT_LIMITS,
    SONG_CONTEXT_RESTORE_REFUSAL,
    SONG_CONTEXT_WITHOUT_SONG,
    SONG_LYRICS_FIELD,
    SONG_LYRICS_LIMIT,
    AlignLyricsRequest,
    SongContextField,
    SongContextRequest,
    SongVocalTypeRequest,
    _browser_reported_duration,
    _copy_upload,
    _media_duration,
    _require_song_replacement_confirmation,
    _safe_filename,
    _song_context,
    logger,
)
from ..models import Project, Song, SongSection
from ..timeline import (
    align_lyric_blocks,
    lyric_blocks,
    proposed_sections_from_alignment,
    repair_sections,
)
from ..transcription import merge_vocal_spans
from .context import RouterContext


def register(ctx: RouterContext) -> None:
    """Register every route this module owns on the application it was handed.

    The context is unpacked into plain locals first -- `app` among them -- so
    every route below is registered by the same decorator, and closes over the
    same names, as it did when it was nested inside `create_app`. The move is
    the whole diff.
    """
    analyze_song_for_project = ctx.analyze_song_for_project
    app = ctx.app
    get_project = ctx.get_project
    get_project_for_update = ctx.get_project_for_update
    resolve_song_path = ctx.resolve_song_path
    settings = ctx.settings
    song_envelope_report = ctx.song_envelope_report
    store = ctx.store
    transcriber = ctx.transcriber

    @app.post("/api/projects/{project_id}/songs/upload", response_model=Project)
    async def upload_song(
        project_id: str,
        file: Annotated[UploadFile, File()],
        title: Annotated[str, Form()],
        duration: Annotated[float, Form()] = 0,
        confirm_song_replacement: Annotated[bool, Form()] = False,
        # The two things the Director already has about a finished track, carried into the fields
        # that exist for them. Both optional: an import that sends neither behaves exactly as every
        # import did before they existed. `caption` rather than a new "style" field because both
        # generation paths already use `caption` for precisely this — the sonic and stylistic
        # direction of the song — and a second field meaning the same thing would need its own
        # answer to which one the Director's context should believe.
        lyrics: Annotated[str, Form()] = "",
        caption: Annotated[str, Form()] = "",
    ) -> Project:
        project = get_project(project_id)
        # Before `_copy_upload`: a refusal must not have written anything, or it is not a
        # refusal. (The write itself no longer overwrites — see the index prefix below.)
        _require_song_replacement_confirmation(project, confirm_song_replacement)
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in {".wav", ".mp3", ".flac"}:
            raise HTTPException(status_code=415, detail="Song must be WAV, MP3, or FLAC")
        # Ahead of the copy for the same reason the confirmation gate is: an oversized lyric sheet
        # must not leave a written file and a half-done import behind it.
        song_lyrics = _song_context(lyrics, SONG_LYRICS_LIMIT, SONG_LYRICS_FIELD)
        song_caption = _song_context(caption, SONG_CAPTION_LIMIT, SONG_CAPTION_FIELD)
        songs_dir = store.media_dir(project_id) / "songs"
        songs_dir.mkdir(parents=True, exist_ok=True)
        filename = _safe_filename(file.filename or f"song{suffix}")
        # Songs used to be written under their own name, so a confirmed replacement whose
        # filename matched the previous song destroyed the very audio that makes "re-import
        # the same file" an undo — the promise `remove_song` documents. Assets avoid this
        # with an index prefix; songs now do too. The index advances past whatever name is
        # already taken rather than being derived from a count, so a file deleted by hand
        # cannot make a later import land on a name that still exists.
        index = 0
        target = songs_dir / f"{index:03d}-{filename}"
        while target.exists():
            index += 1
            target = songs_dir / f"{index:03d}-{filename}"
        _copy_upload(file, target, settings.max_upload_bytes)
        reported = _browser_reported_duration(duration)
        resolved_duration = reported if reported > 0 else _media_duration(target)
        project.song = Song(
            title=title.strip() or target.stem,
            source="imported",
            path=target.relative_to(store.project_dir(project_id)).as_posix(),
            duration=resolved_duration,
            lyrics=song_lyrics,
            caption=song_caption,
        )
        # **An open Director question, recorded here rather than answered (2026-08-23): should
        # this repair or warn about sections that outlive the song it replaces?** Nothing here
        # touches `project.sections`, so a layer marked out to 180 s survives a 150 s master.
        # The unrenderable half of that is closed — `timeline.layout_spans` clamps every span to
        # the song, so populate can no longer tile a window into seconds `workflows.
        # song_audio_window` refuses at submit — but the *boxes* still read 180 s in the
        # interface. Truncating them, dropping the ones wholly past the end, or warning and
        # leaving the Director's structure alone are three different editorial answers and this
        # route may not pick one on its own.
        #
        # Measured here, before the save, so the pointer and the Song it describes land in the
        # same manifest — a save between the two would leave a window in which the project has a
        # song and no analysis and a reader could cache that. On the threadpool because this is
        # an `async def`: a fifth of a second of numpy on the event loop would stall every request
        # in the process, which is exactly the "never blocks the interface" this has to keep.
        #
        # **A failed analysis does not fail the import.** The Song is already on disk and already
        # assigned; refusing here would throw away a completed upload over a measurement nobody
        # asked for. The reason is logged and the envelope endpoint reports the analysis absent,
        # which is what it would report anyway — the Project is otherwise exactly as it would be
        # if this call were not here.
        if reason := await run_in_threadpool(analyze_song_for_project, project_id, project):
            logger.warning("Song analysis skipped for %s: %s", project_id, reason)
        return store.save(project)

    @app.put("/api/projects/{project_id}/song/context", response_model=Project)
    def replace_song_context(project_id: str, request: SongContextRequest) -> Project:
        """Set the lyric sheet and style description of the Song this project already has.

        Correcting after the fact, so a Director who imported yesterday is not made to re-import a
        finished master to say what it is. The Song's audio is the one thing this must not touch:
        `path`, `duration`, `source` and `prompt_id` are never assigned here, and the two fields
        are written onto the *stored* Song rather than a rebuilt one, so there is no construction
        site where a provenance field could be defaulted away.

        Both fields are assigned from the body, exactly as `PUT /documents` assigns its text: an
        omitted field is a blank one. That is what makes clearing a wrong lyric sheet possible at
        all, and the client sends both every time. It is also why nothing here is a Song
        *replacement* — the timing spine is untouched, so `_require_song_replacement_confirmation`
        has nothing to protect and asking for an acknowledgement would be theatre.

        Both values are computed before either is assigned, so a refusal over the second field
        cannot leave the first one applied.

        Each field keeps the one version this save displaced, and only when the save genuinely
        displaces something. A save whose text equals the stored text writes no slot: the single
        slot is the whole protection, and spending it on a no-op would overwrite the recoverable
        version with a copy of the live one — destroying the thing it exists to protect, on the
        most likely accidental path there is, a Director opening the editor and clicking save.

        The two fields are independent. Editing the lyric sheet moves the lyric slot and leaves
        the style description's alone, because they are two separate pieces of work and one save
        button is an implementation detail of the screen rather than a fact about the text.

        **The write is a compare-and-swap**, for `restore_song_context`'s reason and with more at
        stake: this is the route that *fills* the single recovery slot, so a save laid over a
        newer manifest displaces the wrong stored text into it and the lyric sheet the Director
        thought was recoverable is the one that is gone. Both outcomes are a well-formed manifest
        carrying two strings, which is why nothing downstream can notice.
        """
        project, generation = get_project_for_update(project_id)
        if project.song is None:
            raise HTTPException(status_code=404, detail=SONG_CONTEXT_WITHOUT_SONG)
        submitted = {
            field: _song_context(
                getattr(request, field), SONG_CONTEXT_LIMITS[field], SONG_CONTEXT_FIELD_NAMES[field]
            )
            for field in SONG_CONTEXT_LABELS
        }
        for field, text in submitted.items():
            stored = getattr(project.song, field)
            # A no-op, and the one case where doing nothing is the whole feature. Note this
            # compares the *normalised* submission against stored text that was normalised the
            # same way on its own way in, so re-saving an untouched sheet is byte-equal here.
            if text == stored:
                continue
            setattr(project.song, f"{field}{RECOVERY_SLOT_SUFFIX}", stored)
            setattr(project.song, field, text)
        return store.save(project, if_generation=generation)

    @app.put("/api/projects/{project_id}/song/vocal-type", response_model=Project)
    def replace_song_vocal_type(
        project_id: str, request: SongVocalTypeRequest
    ) -> Project:
        """Declare who sings this track — the one writer of `Song.vocal_type`.

        The Director's ask (2026-08-21): "We should be able to select if the song is Instrumental,
        Female sung, Male sung, Duet, 3+, Choir… This would need to be done before treatment in
        the Song workspace so that the LLM system can account for all that." This route is the
        Song workspace's half; the per-line marks it unlocks are written into the lyric sheet by
        `PUT .../song/context`, because they *are* the lyric sheet.

        **Explicit, and therefore refusable and reversible.** Nothing infers this field: no route
        derives it from the lyric sheet's shape or from a library that happens to hold two
        characters, no vision inspection writes it, no tool schema exposes it to a model, and the
        generic full-project `PUT` re-adopts the stored value rather than trusting a body —
        `replace_consistency_prompt`'s rule, for the reason that route's docstring gives, and the
        sixth time that route has had to be defended against exactly this shape of field.
        `"unstated"` is a real value and re-declaring it is how a Director takes a declaration
        back.

        What it does **not** do is touch a shot, a section, or a character of the lyric sheet.
        Declaring Duet does not go tagging lines, and declaring Instrumental does not sweep
        `singing` to `not_singing` — see `models.INSTRUMENTAL_NOTE`. It is a statement about the
        song, read by the next plan; a sweep over work the Director already has is the silent bulk
        edit this codebase's report-then-confirm convention forbids, and `replace_default_setting`
        refuses it in the same words.

        Written onto the *stored* Song rather than a rebuilt one, `replace_song_context`'s rule:
        there is no construction site here where `path`, `duration`, `source` or `prompt_id` could
        be defaulted away by an edit that was only ever about one enum.
        """
        project = get_project(project_id)
        if project.song is None:
            raise HTTPException(status_code=404, detail=SONG_CONTEXT_WITHOUT_SONG)
        project.song.vocal_type = request.vocal_type
        return store.save(project)

    @app.post("/api/projects/{project_id}/song/align-lyrics", response_model=Project)
    def align_song_lyrics(project_id: str, request: AlignLyricsRequest) -> Project:
        """Hear the track, time the sheet's `[Tag]` blocks against it, fill the sections.

        The Director's ask (2026-08-20): "I did add the tags in the lyrics so that those
        would at least be clear... knowing where words are and arent is useful for knowing
        which Shots have words, when the cuts should happen, when the chorus and verses
        are." Three writes, all measured: `lyric_words` (every word Whisper hears, kept so
        nothing ever transcribes twice), `vocal_spans` (the singing-flag guard's evidence),
        and — when the plan has no sections, or `replace_sections` says to — the section
        boxes themselves, one per aligned block plus an Intro when the voice starts late,
        repaired by the same rules a populate proposal is.

        A sync `def`, deliberately: FastAPI runs it in the threadpool, and a CPU
        transcription of a whole track must not park the event loop for minutes.

        Prompts on the proposed sections are left empty — timing is measured, look is
        authored — and existing sections are never replaced without the flag: boxes the
        Director has dragged are their marks, not this route's.
        """
        project = get_project(project_id)
        if project.song is None or not project.song.path:
            raise HTTPException(status_code=422, detail=ALIGN_LYRICS_WITHOUT_SONG)
        if not lyric_blocks(project.song.lyrics):
            raise HTTPException(status_code=422, detail=ALIGN_LYRICS_WITHOUT_TAGS)
        if project.sections and not request.replace_sections:
            raise HTTPException(status_code=409, detail=ALIGN_LYRICS_SECTIONS_EXIST)
        words = project.song.lyric_words
        if not words or request.retranscribe:
            try:
                words = transcriber(resolve_song_path(project_id, project.song))
            except Exception as error:  # the dependency or the decode, named either way
                raise HTTPException(
                    status_code=502, detail=ALIGN_LYRICS_TRANSCRIBE_FAILED.format(error=error)
                ) from error
            project.song.lyric_words = words
            project.song.vocal_spans = merge_vocal_spans(words)
        aligned = align_lyric_blocks(project.song.lyrics, words)
        if not aligned:
            store.save(project)  # the transcription is still worth keeping
            raise HTTPException(status_code=422, detail=ALIGN_LYRICS_NOTHING_PLACED)
        project.sections = [
            SongSection(label=label, start=start, duration=length, prompt=prompt)
            for label, start, length, prompt in repair_sections(
                proposed_sections_from_alignment(aligned, project.song.duration),
                project.song.duration,
            )
        ]
        return store.save(project)

    @app.post("/api/projects/{project_id}/song/analyze", response_model=Project)
    def analyze_song_now(project_id: str) -> Project:
        """Measure the song again, now, because the Director asked.

        **The state this exists to leave.** Everything else in this story measures a song as a
        side effect of storing one, which means a measurement that failed, or one invalidated by a
        replaced file, was *terminal*: `SONG_ENVELOPE_SONG_CHANGED`, `SONG_ANALYSIS_WRITE_FAILED`
        and `SONG_ANALYSIS_FFMPEG_MISSING` all describe a condition the Director can fix — put the
        song back, free the disk, install ffmpeg — and then had no way at all to act on. The only
        remedy was to re-import the track, which is a destructive gesture behind a confirmation
        gate, to clear a derived cache file. This is the button that sentence implies.

        It is also `force`'s missing caller. That parameter existed for exactly this and nothing
        reached it, which is how a parameter becomes decoration; Treatment Story 16.2 calls this
        same entry point, skippably-by-fingerprint, from its own trigger.

        **`force=True`, so it always re-measures.** A Director who clicks this while the envelope
        is already current gets a fresh measurement rather than a silent no-op. That is the whole
        difference between this and the automatic path: the automatic path skips what is already
        done because it is not the Director asking, and this one is.

        **Not a poll, and not a job lane.** One request, one measurement, one answer — which is
        what keeps the frozen Never intact rather than bending it. There is no task record, nothing
        to come back and ask about, and nothing here that a client is expected to call on a timer.
        A sync `def`, so FastAPI runs it in the threadpool for the reason `align_song_lyrics` is
        one: 168 ms of ffmpeg and numpy has no business on the event loop.

        **What it does with no song**, stated because it is a decision and not an accident: it
        refuses, 422, naming the reason. `align_song_lyrics` is the sibling here and this follows
        it exactly. The distinction from the import path is worth being explicit about — there, the
        analysis is a bonus riding somebody else's request and may never fail it, so a failure is
        logged and swallowed. Here the analysis *is* the request, so a failure is the answer to it
        and is reported rather than hidden. The same split `align_song_lyrics` makes: a
        precondition nobody can measure past is 422, and a measurement that genuinely failed is
        502 carrying its own named reason.

        **A refusal changes nothing.** `analyze_project_song` mutates the Project only on success
        and never saves, and the save below is reached only when it returned no reason — so a
        failed re-analysis leaves the manifest exactly as it found it, which is the import path's
        discipline arriving at the same place by a different route.
        """
        project = get_project(project_id)
        song = project.song
        if song is None or not song.path:
            # Covers both "no song at all" and a generated Song whose render has not landed. There
            # is nothing on disk to measure in either case, and the sentence is true of both.
            raise HTTPException(status_code=422, detail=SONG_ANALYSIS_WITHOUT_SONG)
        # The containment-checked accessor's own 404 rather than a reason of this route's
        # invention: every other song route already answers exactly that for a file that is gone,
        # and a second spelling of it here would be a second thing to keep in step.
        try:
            resolve_song_path(project_id, song)
        except OSError as error:
            raise HTTPException(status_code=404, detail="Song media was not found") from error
        if reason := analyze_song_for_project(project_id, project, force=True):
            raise HTTPException(status_code=502, detail=reason)
        return store.save(project)

    @app.post(
        "/api/projects/{project_id}/song/context/{field}/restore", response_model=Project
    )
    def restore_song_context(project_id: str, field: SongContextField) -> Project:
        """Swap one song context field with the single version kept for it.

        A swap rather than a pop, matching the document restore exactly: the text being displaced
        becomes the kept version, so the restore is its own inverse and a mis-click costs nothing.
        The asymmetry would be the surprise — a Director who has used restore on the Treatment
        would reasonably expect the same click to behave the same way here.

        Nothing else about the Song is read or written. This route takes no body at all, so
        `path`, `duration`, `source` and `prompt_id` are not on the wire and cannot be defaulted
        away by it, which is the same guarantee the edit route makes.

        Nothing is appended to the chat thread, which is where this differs from the document
        restore deliberately. That thread is the audit trail of what the *Director* did to the two
        creative documents, and a Director reply can replace them without being asked; song
        context only ever changes when the human clicks save, so a system line about it would be
        the application narrating the human's own click back at them — and, since the thread is
        handed to the model on the next turn, doing so in the model's prompt.

        `None` in the slot means no save has ever displaced anything, and that refuses. `""` means
        a save displaced a blank, and that restores — a Director who pasted a sheet over an empty
        field has a real previous version, and telling them the blank is unrecoverable would be
        the conflation `Song`'s own docstring exists to avoid.

        An empty slot refuses with **409**, which is `restore_document`'s code for the identical
        question. It was 422 when this shipped, because the frozen matrix said so; the Director
        renegotiated it on 2026-08-18 rather than leave two restore routes answering "nothing was
        kept" with two different codes. Nothing about *which* states refuse moved with it — only
        the number — and a route test asserts the two restores stay equal, because the drift is
        what the change exists to close.

        **And the write is a compare-and-swap, which is `restore_document`'s guard and the same
        argument.** "Matching the document restore exactly" has to include how it is written or
        it is not matching: there is one kept version, this route both reads it and writes it,
        and two swaps that overlap leave the field where it started with the slot spent. What is
        at stake here is larger than a document's — an 8,000-character lyric sheet the Director
        pasted in from somewhere this application cannot reach — and a swap read from a stale
        copy is undetectable afterwards, because either outcome is a well-formed pair of
        strings. Refused rather than retried: see `RENDER_STATUS_SAVE_ATTEMPTS` for whose write
        may retry, and it is not a click on a specific field the Director is looking at.
        """
        project, generation = get_project_for_update(project_id)
        if project.song is None:
            raise HTTPException(status_code=404, detail=SONG_CONTEXT_WITHOUT_SONG)
        slot = f"{field}{RECOVERY_SLOT_SUFFIX}"
        previous = getattr(project.song, slot)
        if previous is None:
            raise HTTPException(
                status_code=409,
                detail=SONG_CONTEXT_RESTORE_REFUSAL.format(field=SONG_CONTEXT_LABELS[field]),
            )
        setattr(project.song, slot, getattr(project.song, field))
        setattr(project.song, field, previous)
        return store.save(project, if_generation=generation)

    @app.delete("/api/projects/{project_id}/song", response_model=Project)
    def remove_song(project_id: str, confirm_song_replacement: bool = False) -> Project:
        """Detach the project's Song. Removal is not destruction.

        Shots are left exactly as they are — a shot whose window no longer has a song
        behind it is still the Director's work — and no media is deleted. What "undo" means
        differs by source, so state it exactly rather than over-promising: an imported song's
        file stays under `media/songs/` and re-importing it restores the Song, while a
        generated song's audio lives in ComfyUI's output and stays listed on its render job's
        `output_files`, which is the only record tying that take to this project once the
        Song reference is gone.

        **"Shots are left exactly as they are" is a claim about a whole-manifest write**, and
        that is why the save is a compare-and-swap. This route changes one field and rewrites
        every other one from a copy read a moment earlier, so a `PUT /shots` that lands inside
        that moment is not merely lost — it is lost *through the sentence above*, the shot list
        reverting to whatever it held when the detach was clicked while the reply reports a
        clean removal. The direction reverses just as easily: a shot save that read before the
        detach landed puts the Song back. `save`'s `if_generation` closes it from this side, and
        `replace_shots` carries the same guard for the other; whichever request read first is
        refused with `SAVE_RACE_REFUSAL` and re-reads. Refused rather than retried, because a
        detach is a destructive decision the Director made about a Song they were looking at —
        see `RENDER_STATUS_SAVE_ATTEMPTS` for whose write may retry instead.
        """
        project, generation = get_project_for_update(project_id)
        if project.song is None:
            raise HTTPException(status_code=404, detail="This project has no song to remove")
        _require_song_replacement_confirmation(project, confirm_song_replacement)
        project.song = None
        return store.save(project, if_generation=generation)

    @app.get("/api/projects/{project_id}/song/envelope")
    def read_song_envelope(project_id: str) -> dict[str, Any]:
        """The Song Envelope, on its own endpoint. Read-only, and never part of a Project.

        **The whole measurement, for anyone who wants the whole thing — and the browser is no
        longer one of them.** `GET /timeline/snap-targets` carries the part the timeline draws
        beside the seconds a drag lands on, from one computation, because two client reads of one
        measurement is what let the band and the drag describe different states. This route is
        deliberately untouched by that change: it keeps its shape, its statuses and its
        absence-is-a-200 contract, and it is the documented read-only resource for a consumer
        outside this application. What it must not become again is a *second* path the browser
        takes to the same measurement.

        Its own endpoint because of the size: a three-minute envelope at 30 Hz with 8 bands is
        hundreds of kilobytes against a whole manifest of 110–190 KB, and the manifest rides a
        two-second poll. Embedding it in the Project response would multiply every poll by the
        length of the song — so no Project response carries it, here or anywhere.

        No `response_model`, deliberately: the envelope's arrays are the analysis's own recorded
        shape, and re-declaring them as a pydantic model here would be a second definition of the
        same thing to keep in step with `audio.py`, plus a validation pass over several thousand
        floats on every read for no guarantee that is not already true of a file this application
        wrote itself.

        A sync `def`, so FastAPI runs it in the threadpool: it hashes the whole song file to
        decide validity, and a multi-megabyte read has no business on the event loop.

        **Absence is a 200.** A project with no song, no analysis, a replaced song or a deleted
        sidecar all answer `{"present": false, "reason": …}`. None of those is an error and a 404
        would make consumers draw one. The only 404 here is the project itself not existing.
        """
        project = get_project(project_id)
        return song_envelope_report(project_id, project)
