from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8766"


def request(path: str, *, method: str = "GET", payload: dict | None = None) -> dict:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {} if body is None else {"Content-Type": "application/json"}
    req = urllib.request.Request(f"{BASE_URL}{path}", data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"{method} {path}: {error.code} {error.read().decode()}") from error


def main() -> None:
    project = request("/api/projects", method="POST", payload={"name": "H3 Text Smoke QA"})
    shot = {
        "start": 0,
        "duration": 4,
        "prompt": (
            "A cinematic medium shot of a solo singer under one amber warehouse light, "
            "subtle breathing and a slow camera push, stable face and background, one continuous take."
        ),
        "mode": "text",
        "asset_ids": [],
        "seed": 260816,
        "status": "ready",
    }
    project = request(
        f"/api/projects/{project['id']}/shots",
        method="PUT",
        payload={"shots": [shot]},
    )
    shot_id = project["shots"][0]["id"]
    job = request(
        f"/api/projects/{project['id']}/shots/{shot_id}/generate/h3",
        method="POST",
        payload={"width": 768, "height": 448, "steps": 4},
    )
    started = time.monotonic()
    while time.monotonic() - started < 1200:
        current = request(f"/api/projects/{project['id']}/jobs/{job['id']}")
        if current["status"] in {"complete", "error", "cancelled"}:
            print(
                json.dumps(
                    {
                        "project_id": project["id"],
                        "shot_id": shot_id,
                        "job_id": job["id"],
                        "prompt_id": current["prompt_id"],
                        "status": current["status"],
                        "outputs": current["output_files"],
                        "error": current["error"],
                        "elapsed_seconds": round(time.monotonic() - started, 1),
                    },
                    indent=2,
                )
            )
            raise SystemExit(0 if current["status"] == "complete" else 1)
        time.sleep(8)
    raise TimeoutError("H3 smoke job did not reach a terminal state within 20 minutes")


if __name__ == "__main__":
    main()
