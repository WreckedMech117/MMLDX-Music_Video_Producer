"""Manual live audit of the Krea multiview adapter against ComfyUI ``/object_info``.

Not pytest-collected (like ``preflight_songplanner.py`` and ``preflight_h3_ultra.py``).
The validation rules live in ``tests/preflight.py``; this script is the multiview
caller. Run from the repo root against a live, user-managed ComfyUI — never started
or stopped here, and nothing is submitted to ``/prompt``:

    uv run python tests/preflight_multiview.py [base_url] [--record]

Written while this adapter briefly grew a refine and a final sampling pass; those were
reverted the same day (see ``MULTIVIEW_STEPS`` in ``workflows.py``), and the audit is
kept because the adapter had never had one. It is the only check that puts the Krea and
KJ node classes — ``Krea2EditModelPatch``, ``Krea2EditGroundedEncode``,
``ImageResizeKJv2``, ``EmptySD3LatentImage`` — in front of a live schema at all; every
one of them is in ``UNRECORDED_CLASSES``, so the offline range check in
``tests/test_workflows.py`` skips them entirely.

``--record`` merges the audited subset into ``tests/fixtures/object_info.json``, and
only when the audit is clean. Recording here would add the six Krea/KJ classes the
offline range check currently lists in ``UNRECORDED_CLASSES``; that list is asserted
exactly in ``tests/test_workflows.py``, so a recording run has to be paired with an
edit there rather than left to be discovered.
"""

from __future__ import annotations

import sys

from preflight import (
    combo_options,
    fetch_object_info,
    parse_arguments,
    repo_src_on_path,
    run_audit,
)

repo_src_on_path()

# Imported after `repo_src_on_path()` on purpose: run as a script, `src` is not
# importable until that call puts it on the path.
from music_video_producer.workflows import MULTIVIEW_SAMPLER, build_multiview_payload


def audit_payloads(image_name: str) -> list[tuple[str, dict]]:
    """The one graph under audit.

    Both templates travel through the identical graph — the subject kind picks the
    prompt string and nothing else — so a second variant here would audit the same
    nodes with a different STRING literal in one of them. The prompt is a free-text
    input with no combo and no bounds, so that variant could not fail differently.

    ``image_name`` is passed in rather than made up because ``LoadImage.image`` is a
    combo over the files already in ComfyUI's input directory. The route uploads the
    source before it submits, so at submission time the name is always one the server
    has; an invented one fails the combo check for a reason the adapter does not have.
    """
    return [
        (
            "multiview",
            build_multiview_payload(
                image_name=image_name,
                prompt="preflight prompt",
                seed=0,
                prefix="preflight",
            ),
        )
    ]


def any_uploaded_image(object_info: dict) -> str:
    """One image name the live server offers ``LoadImage``, for the audit to stand in.

    Raises rather than falling back to a placeholder: with an empty input directory the
    combo has no valid value at all, and an audit that quietly substituted one would be
    reporting a graph nobody could submit as clean.
    """
    spec = object_info.get("LoadImage", {}).get("input", {}).get("required", {}).get("image")
    options = [option for option in (combo_options(spec) or []) if isinstance(option, str)]
    if not options:
        raise SystemExit(
            "FAIL: LoadImage offers no image to audit with. Upload any image to ComfyUI's "
            "input directory, or promote one asset through the app, and run this again."
        )
    return options[0]


def sampler_is_offered(object_info: dict) -> list[str]:
    """The sheet sampler, checked against KSampler's live option list.

    ``validate`` already checks the value the payload carries, so this is not the same
    check twice: it reads ``MULTIVIEW_SAMPLER`` directly, which is what catches a
    constant that was changed without the payload being rebuilt from it.
    """
    info = object_info.get("KSampler")
    if info is None:
        return ["KSampler is not registered, so the sheet sampler cannot be checked"]
    options = combo_options(info.get("input", {}).get("required", {}).get("sampler_name"))
    if not options:
        return ["KSampler.sampler_name publishes no readable option list"]
    if MULTIVIEW_SAMPLER not in options:
        return [f"sampler_name={MULTIVIEW_SAMPLER!r} is not offered by this server"]
    return []


def main() -> None:
    base_url, record = parse_arguments(sys.argv[1:])
    # Read once and handed to `run_audit` as its fetch, so the schema the image name is
    # chosen from is the same schema the graph is then validated against.
    object_info = fetch_object_info(base_url)
    run_audit(
        audit_payloads(any_uploaded_image(object_info)),
        base_url=base_url,
        record=record,
        checks=(sampler_is_offered,),
        fetch=lambda _base_url: object_info,
    )


if __name__ == "__main__":
    main()
