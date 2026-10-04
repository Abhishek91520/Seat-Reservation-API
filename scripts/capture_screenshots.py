#!/usr/bin/env python3
"""scripts/capture_screenshots.py: Capture visual screenshots of /live dashboard in Playwright.

Generates screenshots at:
- 390x844 (mobile) light mode
- 390x844 (mobile) dark mode
- 1440x900 (desktop) light mode
- 1440x900 (desktop) dark mode
Verifies document.documentElement.scrollWidth <= window.innerWidth at 390px.
"""

import os
import time

import httpx
from playwright.sync_api import sync_playwright

BASE_URL = "http://127.0.0.1:8000"
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "admin-secret-token")
SCREENSHOTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "screenshots"
)


def setup_demo_show():
    client = httpx.Client(base_url=BASE_URL, timeout=10.0)
    admin_headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    seats = (
        [f"A{i}" for i in range(1, 21)]
        + [f"B{i}" for i in range(1, 21)]
        + [f"C{i}" for i in range(1, 21)]
    )
    r = client.post(
        "/shows",
        json={
            "name": "Oppenheimer 70mm Special Screening",
            "price_paise": 75000,
            "per_user_limit": 4,
            "seats": seats,
        },
        headers=admin_headers,
    )
    assert r.status_code == 201, f"Failed to create show: {r.text}"
    show_id = r.json()["id"]

    # Book some seats to show realistic mixed state (available, confirmed)
    token_resp = client.post("/auth/token", json={"user_id": "screen_user_1"})
    token = token_resp.json()["access_token"]
    user_headers = {"Authorization": f"Bearer {token}"}
    client.post(
        "/reservations",
        json={
            "show_id": show_id,
            "seats": ["A1", "A2", "A3"],
            "idempotency_key": "shot_key_1",
        },
        headers=user_headers,
    )

    token_resp2 = client.post("/auth/token", json={"user_id": "screen_user_2"})
    token2 = token_resp2.json()["access_token"]
    client.post(
        "/reservations",
        json={
            "show_id": show_id,
            "seats": ["B5", "B6"],
            "idempotency_key": "shot_key_2",
        },
        headers={"Authorization": f"Bearer {token2}"},
    )

    return show_id


def capture_all():
    os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
    show_id = setup_demo_show()
    print(f"Created demo show: {show_id}")

    targets = [
        {"name": "mobile_light", "width": 390, "height": 844, "color_scheme": "light"},
        {"name": "mobile_dark", "width": 390, "height": 844, "color_scheme": "dark"},
        {"name": "desktop_light", "width": 1440, "height": 900, "color_scheme": "light"},
        {"name": "desktop_dark", "width": 1440, "height": 900, "color_scheme": "dark"},
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        for t in targets:
            context = browser.new_context(
                viewport={"width": t["width"], "height": t["height"]},
                color_scheme=t["color_scheme"],
            )
            page = context.new_page()
            url = f"{BASE_URL}/live?show={show_id}"
            page.goto(url)
            page.wait_for_selector("#dashboard", state="visible")
            time.sleep(1.2)  # Wait for first poll to complete and DOM cells to paint

            # Verify no horizontal scroll at mobile width
            scroll_width = page.evaluate("document.documentElement.scrollWidth")
            inner_width = page.evaluate("window.innerWidth")
            print(f"[{t['name']}] viewport width={inner_width}, scrollWidth={scroll_width}")
            assert scroll_width <= inner_width, (
                f"Horizontal scroll detected on {t['name']}: {scroll_width} > {inner_width}"
            )

            out_path = os.path.join(SCREENSHOTS_DIR, f"{t['name']}.png")
            page.screenshot(path=out_path)
            print(f"Saved screenshot: {out_path}")
            context.close()

        browser.close()
    print("\nAll 4 screenshots captured successfully without horizontal overflow!")


if __name__ == "__main__":
    capture_all()
