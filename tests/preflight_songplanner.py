"""Manual live audit of the SongPlanner adapter against ComfyUI ``/object_info``.

Not pytest-collected (like ``smoke_h3_app.py``). The validation rules live in
``tests/preflight.py`` — shared with ``tests/preflight_h3_ultra.py`` and with the
offline fixture checks in ``tests/test_workflows.py`` — and this script is the
SongPlanner caller: the graphs under audit, and nothing else. Run from the repo
root with a live, user-managed ComfyUI (never started or stopped here):

    uv run python tests/preflight_songplanner.py [base_url] [--record]

``--record`` merges the audited ``/object_info`` subset into
``tests/fixtures/object_info.json`` only when the audit found zero problems, and
only ever adds to what is recorded there. No generation is submitted.
"""

from __future__ import annotations

import sys

from preflight import parse_arguments, repo_src_on_path, run_audit

repo_src_on_path()

# Imported after `repo_src_on_path()` on purpose: run as a script, `src` is not
# importable until that call puts it on the path.
from music_video_producer.workflows import (
    build_songplanner_invented_payload,
    build_songplanner_known_lyrics_payload,
)


def audit_payloads() -> list[tuple[str, dict]]:
    """The graphs under audit, validated separately so neither masks the other."""
    invented = build_songplanner_invented_payload(
        idea="preflight idea", genre_hint="", duration=120, seed=0, prefix="preflight"
    )
    known = build_songplanner_known_lyrics_payload(
        idea="preflight idea",
        genre_hint="",
        lyrics="[verse]\npreflight",
        duration=120,
        seed=0,
        prefix="preflight",
    )
    return [("invented", invented), ("known-lyrics", known)]


def main() -> None:
    base_url, record = parse_arguments(sys.argv[1:])
    run_audit(audit_payloads(), base_url=base_url, record=record)


if __name__ == "__main__":
    main()
