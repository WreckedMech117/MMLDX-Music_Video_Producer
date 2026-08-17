---
title: "Product Brief: Music Video Producer"
status: draft
created: 2026-08-16
updated: 2026-08-16
---

# Product Brief: Music Video Producer

## Executive Summary

Music Video Producer turns a song into a finished AI music video entirely on the machine it runs on. It is a production editor — song, treatment, assets, timeline, queue — sitting on top of a portable ComfyUI installation that the user already owns and already controls. Nothing leaves the building: no cloud render service, no subscription, no API quota, no third party deciding what may be generated.

The problem it solves is not "generate a video clip." That is solved, badly, in a dozen places. The problem is everything around the clip: keeping one character recognizably the same person across forty shots, holding those shots in sync with a real song, remembering which seed produced the take that worked, and getting from a song to an assembled video without hand-driving a node graph several hundred times. ComfyUI can render anything and remembers nothing about your production. This application is the memory and the structure.

As of 2026-08-16 it is no longer theoretical. A shot was submitted from the application, rendered through live ComfyUI on local hardware, and returned a 90-frame clip with synchronized audio, verified by `ffprobe`. The central risk — that the pipeline had never actually run end to end — is retired. What remains is not "does it work" but "does it work for a whole song, and is it pleasant enough to use for the length of a whole song."

## The Problem

Making an AI music video today means operating a node graph as if it were an instrument, and doing it by hand, once per shot.

The specific pains, in the order they bite:

- **Continuity collapses.** Generate the same character twice and you get two different people. Every existing workflow treats each render as an independent event, so the burden of identity falls on the operator's prompt discipline and luck.
- **Nothing remembers anything.** ComfyUI's output folder is a pile of files. Which seed made the good take? Which prompt? Which of the four `_00003` files was the one you approved? The information exists briefly, in the operator's head, and then does not.
- **The song is not in the loop.** Shots have to land on the music. Driving a graph shot by shot means timing is reconstructed mentally, then verified by rendering, which is the most expensive possible way to find out a cut is wrong.
- **The work does not scale to a song's length.** A three-minute video is thirty to forty shots. Hand-operating a graph forty times, tracking forty sets of provenance, is where projects die.
- **The alternatives take the work off your machine.** Cloud tools solve some of this by owning your pipeline, your content policy, and your recurring bill.

The cost of the status quo is simple: videos do not get finished.

## The Solution

A local production editor that treats a music video as a project with structure, not a folder of renders.

- **A song is the spine.** Import a master or generate one; every shot's timing is measured in seconds against it.
- **A treatment becomes a shot plan.** A locally-hosted LLM turns creative direction into an editable treatment, style bible, and timed shots. It creates records; it never spends GPU time on its own.
- **Characters are promoted, not re-rolled.** An approved character image becomes a multiview reference sheet, and that sheet is what downstream shots refer to. Continuity becomes a data relationship instead of a prompting skill.
- **Provenance is the point.** Every render stores its prompt ID, seed, workflow, target, and output path in a recoverable project manifest. A take can be found, compared, and rebuilt later.
- **Completion is not approval.** A finished render becomes `latest_output`. Only an explicit editorial decision makes it `approved_output`. The software never decides a shot is good.
- **The user owns the renderer.** The application checks and uses the ComfyUI server; it never starts, stops, or interrupts it.

The intended shape of use is two-mode: a **guided wizard** carries a new production through the early, structural decisions — song, treatment, characters, initial shot plan — and then hands off to a **professional editing surface** where every shot is individually tunable by eye. Fast to start, deep to finish. `[ASSUMPTION]` The wizard is for starting a production, not a permanent mode; experienced use lives in the editor.

## What Makes This Different

Honestly: the differentiator is not a model and not an algorithm. Anyone can call the same ComfyUI nodes.

- **It is local and unconditional.** No account, no quota, no content policy, no price change. On owned hardware, the marginal cost of a take is electricity.
- **It is a production tool, not a generator.** The competition generates clips. This holds a project: song, timing, continuity, provenance, approval state, recovery.
- **It respects the user's existing installation.** It adapts to the ComfyUI the user already runs and never seizes control of it, so the same machine stays usable for everything else.
- **Honesty is a design constraint.** The project's documentation refuses to claim unverified capability, and the UI disables controls whose backends are not real. This is unusual, and it is why the roadmap can be trusted.

The moat is execution and fit, not technology. Stated plainly so nobody plans around a moat that is not there.

## Who This Serves

**Primary: the Director — a musician who owns capable hardware and wants finished videos for their own songs.** Technical enough to run a portable ComfyUI and edit a `.env`; not interested in operating a node graph forty times per song. Wants control over the result at the shot level and wants the tool to hold everything they should not have to remember. Success for them is a finished video they would show people.

**Secondary, later: other musicians in the same position.** Own the GPU, own the songs, do not want a subscription or a content policy. This audience is a deliberate *later* — the tool is being built to work for one person first, and generalized only once it has actually produced something. `[ASSUMPTION]` Sharing means giving other people the software to run themselves, not hosting it for them.

Explicitly not served in this version: client and commercial production work, teams, and anyone who does not have local GPU capacity.

## Success Criteria

**The gating outcome**

- One complete music video for a real song, produced start to finish in this application — every stage exercised, assembled, watchable. Nothing else counts until this exists.

**Quality signals**

- A character is recognizably the same person across the shots of a finished video, judged by eye.
- Any take can be rebuilt or revised months later from what the project manifest recorded.

**Usability signals**

- Producing a video in the application is measurably faster than driving ComfyUI by hand for the same song. `[ASSUMPTION]` The honest measure is wall-clock time from song to assembled draft, compared once against a manual run — not a benchmark suite.
- A new production reaches its first rendered shot through guided steps, without the user reading documentation.
- Shot-level correction is direct: adjusting timing, prompt, or references for a single shot never requires redoing the surrounding work.

**Non-goals as measures.** Render quality is the model's business, not the application's. Speed of generation is the GPU's business. This tool is measured on structure, continuity, recoverability, and how it feels to operate for the length of a whole song.

## Scope

**In, for the first complete version**

- Song import and generation; treatment and style bible; asset generation and library; character promotion to multiview references.
- Shot planning against the song, with direct manipulation and per-shot tuning.
- Shot rendering through the verified H3 path, including reference-driven shots.
- Multiple takes per shot with comparison and explicit approval.
- Final assembly of approved takes into one song-synchronized video.
- A guided wizard covering a new production from song through first shots.
- Persistent provenance and recovery for everything above.

**Out, deliberately**

- Any cloud or hosted component; any account system; any telemetry.
- Multi-user, collaboration, and permissions.
- Client/commercial delivery features — invoicing, watermarking, client review links.
- Controlling ComfyUI's lifecycle, or bundling ComfyUI.
- Being a general video editor. It edits *this* pipeline's output, not arbitrary footage.
- Mobile and remote access. `[ASSUMPTION]` Local desktop browser only; the app binds to localhost by default.

**Known open boundary.** Finishing — LTX enhancement, SeedVR2, FILM interpolation, RTX VSR — is designed but unproven, and one real failure has already been diagnosed at the LTX dimension boundary. It stays in scope for the first complete version *only* if a standalone route that accepts an approved take proves out; otherwise the first complete video ships without upscaling. That decision is deferred, not dodged.

## Risks and Open Questions

- **The wizard contradicts a standing architectural decision.** `docs/ARCHITECTURE.md` commits to an "Operate / Command-Inspect editor" and explicitly rejects landing-page and dashboard surfaces. A guided wizard is a different interaction model. These need reconciling deliberately — most likely as a first-run production path that composes the existing workspaces rather than a parallel UI. This is the single most important unresolved design question in the brief.
- **Only one route has live evidence.** Text-only H3 is verified. Reference-driven shots, multiview promotion, Flux, and Music 3 are built and unit-tested but have not produced a real render from this application. Each is a separate unproven seam.
- **Scale is untested.** The verified render was one 3.75-second shot. A three-minute song is thirty to forty of them, and nothing yet exercises queueing, storage, or review at that volume.
- **Frame-grid handling is unproven off-grid.** The verified window was chosen to land exactly on H3's 17k+5 grid. The payload sends requested rather than aligned frames, so ordinary windows are untested.
- **Continuity is asserted, not measured.** There is no defined way to judge whether a character held across a video. `[ASSUMPTION]` It stays a by-eye judgment; the vision inspection records cues but does not score identity.

## Vision

In two to three years, the Director does not think about ComfyUI while making a video. They bring a song, describe what they want, shape the shots that matter, and the machine holds everything else — continuity, timing, provenance, assembly. The pipeline underneath is replaceable: models change every few months, and the project structure, the treatment, the shot plan, and the provenance survive them.

If it succeeds beyond that, it becomes the thing a musician with a GPU installs to make videos for their own songs — an owned instrument rather than a rented service, in a category that is otherwise moving quickly toward subscription and control.
