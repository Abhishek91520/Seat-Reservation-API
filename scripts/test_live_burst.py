"""scripts/test_live_burst.py: Verify burst benchmark passes with 0 5xx
while /live is actively polling in a browser.
"""

import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

BASE_URL = "http://127.0.0.1:8000"


def run_live_burst():
    print("Launching Playwright headless browser to load /live dashboard...")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        # Open live page with a mock show or without show to start polling
        page.goto(f"{BASE_URL}/live")
        time.sleep(1.0)
        print("[+] /live loaded in headless Chromium.")

        print("Starting burst benchmark while browser is active...")
        cmd = [
            sys.executable,
            "scripts/burst.py",
            BASE_URL,
            "--seats",
            "200",
            "--users",
            "500",
            "--hot",
            "5",
            "--concurrency",
            "50",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        print("Burst output:\n", result.stdout)
        if result.stderr:
            print("Burst stderr:\n", result.stderr)

        assert result.returncode == 0, f"Burst failed with exit code {result.returncode}"
        assert "Zero 5xx guarantee holds (5xx errors = 0 across all scenarios)" in result.stdout
        assert "BURST VERIFICATION COMPLETE: ALL PASS [OK]" in result.stdout

        browser.close()
    print("\n[SUCCESS] /live polling during burst completed with 0 5xx and ALL PASS!")


if __name__ == "__main__":
    run_live_burst()
