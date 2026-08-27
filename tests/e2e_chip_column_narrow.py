"""Supplement to the chip-column QA: the narrow clip, before and after, and four chips on it.

The narrow clip is the reason for the amendment, so two things are looked at rather than reasoned
about: what the 40px clip looked like under the *old* rule (chip absolutely placed, prompt with
its plain 9px inset) against what it looks like now, and whether four chips -- the arrangement the
old "three abreast" rule could not express at all -- fit a 40px clip.

The fixture is `e2e_chip_column.py`'s own -- imported rather than restated, so the two scripts
cannot drift into measuring different clips (the same reason `e2e_song_analysis.py` borrows
`e2e_timeline_edit.py`'s helpers). Run `e2e_chip_column.py` first; this is its supplement, not a
substitute for it.

**No GPU is spent and nothing reaches `/prompt`.** ComfyUI is pointed at a dead port and never
contacted.

Run from the repo root -- it starts and proves its own server, and takes no base URL::

    uv run --with selenium python tests/e2e_chip_column_narrow.py [--port 8782]

Assumes: nothing listening on the port, Microsoft Edge and its WebDriver installed, and
`music_video_producer` importable from this checkout's `src/`. ComfyUI does not need to be
running.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from e2e_chip_column import INJECT, MEASURE, dead_port, seed
from e2e_support import (
    ManagedServer,
    artifact_dir,
    console_gate,
    edge_driver,
    report,
    settle,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

NAME = "chip-column-narrow"
NARROW = "shot_02"
WIDE = "shot_01"

#: Everything this change touched, undone in the live page: the chip positions itself again and the
#: prompt goes back to the plain 9px inset it had on every clip.
OLD_LOOK = """
const style = document.createElement('style');
style.id = 'old-look';
style.textContent = '.clip-chips { display: contents; }'
  + '.clip-fx { position: absolute; right: 14px; bottom: 4px; }'
  + '.shot-clip[data-chips] .clip-prompt { padding-right: 9px; }';
document.head.append(style);
"""


def shoot(driver, shot_id: str, label: str) -> None:
    driver.find_element(
        By.CSS_SELECTOR, f'#shots-track .shot-clip[data-shot-id="{shot_id}"]'
    ).screenshot(str(artifact_dir() / f"{NAME}-{label}.png"))


def main() -> None:
    port = 8782
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    result: dict[str, object] = {}
    os.environ["MVP_COMFY_ROOT"] = str(Path(tempfile.mkdtemp(prefix="mvp-chip2-comfy-")))
    os.environ["MVP_COMFY_URL"] = f"http://127.0.0.1:{dead_port()}"
    os.environ["MVP_LLM_EJECT_BEFORE_RENDER"] = "false"
    os.environ["MVP_LLM_BASE_URL"] = ""

    with ManagedServer(port, label=NAME) as server:
        result["server_identity"] = server.evidence
        result["data_root"] = str(server.data_root)
        project_id = seed(server.base_url)
        driver = edge_driver()
        wait = WebDriverWait(driver, 25)
        try:
            driver.get(server.base_url)
            wait.until(EC.presence_of_element_located((By.ID, "project-select")))
            wait.until(lambda b: b.find_element(
                By.CSS_SELECTOR, f'#project-select option[value="{project_id}"]')).click()
            wait.until(lambda b: b.find_element(By.ID, "project-select").get_attribute("value")
                       == project_id)
            driver.find_element(By.CSS_SELECTOR, '[data-panel="timeline"]').click()
            settle(driver, "#shots-track")

            driver.execute_script(OLD_LOOK)
            result["narrow_old"] = driver.execute_script(MEASURE, NARROW)
            shoot(driver, NARROW, "01-narrow-old-rule")
            shoot(driver, WIDE, "02-wide-old-rule")
            driver.execute_script("document.getElementById('old-look').remove();")
            result["narrow_new"] = driver.execute_script(MEASURE, NARROW)
            shoot(driver, NARROW, "03-narrow-new-rule")
            shoot(driver, WIDE, "04-wide-new-rule")

            # Four chips on the 40px clip -- the arrangement three abreast could never hold.
            driver.execute_script(INJECT, NARROW, ["\u2713", "\u2691", "\u25cf"])
            four = driver.execute_script(MEASURE, NARROW)
            result["narrow_four_chips"] = four
            shoot(driver, NARROW, "05-narrow-four-chips")
            assert len(four["chips"]) == 4, four
            for chip in four["chips"]:
                assert not chip["escaped"], chip
            driver.execute_script(
                "document.querySelectorAll('.clip-fx[data-experiment]')"
                ".forEach((n) => n.remove());"
                "document.querySelectorAll('.shot-clip[data-chips]').forEach((clip) => {"
                "  clip.dataset.chips = String(clip.querySelectorAll('.clip-fx').length); });")
            console_gate(driver, NAME, result)
        finally:
            driver.quit()
    report(NAME, result)
    print(json.dumps(sorted(str(p) for p in artifact_dir().glob(f"{NAME}-*.png")), indent=2))


if __name__ == "__main__":
    main()
