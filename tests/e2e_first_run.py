from __future__ import annotations

import json
import sys
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8766"
ARTIFACT_DIR = Path("test-artifacts")
ARTIFACT_DIR.mkdir(exist_ok=True)

options = webdriver.EdgeOptions()
options.add_argument("--headless=new")
options.add_argument("--window-size=1440,900")
options.set_capability("ms:loggingPrefs", {"browser": "ALL"})

driver = webdriver.Edge(options=options)
wait = WebDriverWait(driver, 15)
try:
    driver.get(BASE_URL)
    dialog = wait.until(EC.visibility_of_element_located((By.ID, "project-dialog")))
    assert dialog.get_attribute("open") is not None

    form = driver.find_element(By.ID, "project-form")
    form.find_element(By.NAME, "name").send_keys("First Run Browser QA")
    form.find_element(By.CSS_SELECTOR, "button[type='submit']").click()

    wait.until(lambda browser: browser.find_element(By.ID, "project-select").get_attribute("value"))
    wait.until(EC.invisibility_of_element_located((By.ID, "project-dialog")))
    wait.until(lambda browser: "First Run Browser QA" in browser.find_element(By.ID, "project-select").text)

    visited = []
    for button in driver.find_elements(By.CSS_SELECTOR, ".rail-item[data-panel]"):
        panel_name = button.get_attribute("data-panel")
        button.click()
        wait.until(lambda browser, name=panel_name: "active" in browser.find_element(By.ID, f"panel-{name}").get_attribute("class").split())
        visited.append(panel_name)

    assert visited == ["song", "treatment", "assets", "timeline", "queue"]

    logs = driver.get_log("browser")
    severe = [entry for entry in logs if entry.get("level") == "SEVERE"]
    driver.save_screenshot(str(ARTIFACT_DIR / "first-run.png"))
    (ARTIFACT_DIR / "first-run-browser-log.json").write_text(json.dumps(logs, indent=2), encoding="utf-8")
    assert not severe, severe
    print(json.dumps({"created": True, "visited": visited, "severe_console_errors": severe}, indent=2))
finally:
    driver.quit()
