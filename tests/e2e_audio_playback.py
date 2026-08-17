from __future__ import annotations

import json
import math
import struct
import sys
import wave
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8766"
ARTIFACT_DIR = Path("test-artifacts")
ARTIFACT_DIR.mkdir(exist_ok=True)
AUDIO_PATH = ARTIFACT_DIR / "browser-playback-fixture.wav"


def write_fixture() -> None:
    sample_rate = 22050
    seconds = 2
    with wave.open(str(AUDIO_PATH), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        frames = bytearray()
        for index in range(sample_rate * seconds):
            value = int(8000 * math.sin(2 * math.pi * 440 * index / sample_rate))
            frames.extend(struct.pack("<h", value))
        target.writeframes(frames)


def main() -> None:
    write_fixture()
    options = webdriver.EdgeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1440,1000")
    options.set_capability("ms:loggingPrefs", {"browser": "ALL"})
    driver = webdriver.Edge(options=options)
    wait = WebDriverWait(driver, 15)
    result: dict[str, object] = {}
    try:
        driver.get(BASE_URL)
        wait.until(EC.visibility_of_element_located((By.ID, "project-dialog")))
        driver.find_element(By.CSS_SELECTOR, "#project-form input[name=name]").send_keys(
            "Audio Playback Browser QA"
        )
        driver.find_element(By.CSS_SELECTOR, "#project-form button[type=submit]").click()
        wait.until(lambda browser: "Audio Playback Browser QA" in browser.find_element(By.ID, "project-select").text)

        driver.find_element(By.ID, "song-file").send_keys(str(AUDIO_PATH.resolve()))
        wait.until(lambda browser: browser.find_element(By.ID, "import-title").get_attribute("value"))
        driver.find_element(By.ID, "import-song").click()
        wait.until(lambda browser: browser.find_element(By.ID, "song-source").text == "IMPORTED")

        driver.refresh()
        wait.until(lambda browser: browser.find_element(By.ID, "song-source").text == "IMPORTED")

        audio = wait.until(EC.presence_of_element_located((By.ID, "master-audio")))
        wait.until(lambda browser: browser.execute_script("return arguments[0].readyState", audio) >= 2)
        driver.find_element(By.ID, "global-play").click()
        wait.until(lambda browser: browser.execute_script("return !arguments[0].paused && arguments[0].currentTime > 0.05", audio))
        result = {
            "imported": True,
            "survived_reload": True,
            "playing": not driver.execute_script("return arguments[0].paused", audio),
            "current_time": driver.execute_script("return arguments[0].currentTime", audio),
            "severe_console_errors": [
                entry for entry in driver.get_log("browser") if entry["level"] == "SEVERE"
            ],
        }
        driver.save_screenshot(str(ARTIFACT_DIR / "audio-playback.png"))
    finally:
        driver.quit()
    (ARTIFACT_DIR / "audio-playback-browser-log.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, indent=2))
    if not result.get("playing") or result.get("severe_console_errors"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
