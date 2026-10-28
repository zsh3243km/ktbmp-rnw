
import logging
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

BASE_URL = "https://control.katabump.com"
LOGIN_URL = f"{BASE_URL}/auth/login"

EMAIL = os.getenv("KATABUMP_EMAIL", "").strip()
PASSWORD = os.getenv("KATABUMP_PASSWORD", "")
SERVER_ID = os.getenv("SERVER_ID", "").strip() or "d1712508"

HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"
TIMEOUT_MS = int(os.getenv("TIMEOUT_MS", "20000"))
ARTIFACT_DIR = Path(os.getenv("ARTIFACT_DIR", "artifacts"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("katabump-renew")


def validate_config():
    if not EMAIL:
        raise ValueError("KATABUMP_EMAIL is not configured.")

    if not PASSWORD:
        raise ValueError("KATABUMP_PASSWORD is not configured.")

    if not re.fullmatch(r"[A-Za-z0-9_-]+", SERVER_ID):
        raise ValueError("SERVER_ID contains unsupported characters.")

    if not 1000 <= TIMEOUT_MS <= 120000:
        raise ValueError("TIMEOUT_MS must be between 1000 and 120000.")

    if urlparse(BASE_URL).scheme != "https":
        raise ValueError("The control panel must use HTTPS.")


def save_artifacts(page, label="failure"):
    try:
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

        page.screenshot(
            path=str(ARTIFACT_DIR / f"{label}.png"),
            full_page=True,
        )

        page_text = page.locator("body").inner_text(timeout=5000)

        (ARTIFACT_DIR / f"{label}.txt").write_text(
            page_text[:20000],
            encoding="utf-8",
        )

        log.info("Diagnostic artifacts saved.")

    except Exception as exc:
        log.warning("Could not save diagnostic artifacts: %s", exc)


def find_visible(locator):
    try:
        count = min(locator.count(), 10)

        for index in range(count):
            candidate = locator.nth(index)

            if candidate.is_visible() and candidate.is_enabled():
                return candidate

    except Exception:
        pass

    return None


def login(page):
    log.info("Opening the login page.")

    response = page.goto(
        LOGIN_URL,
        wait_until="domcontentloaded",
        timeout=60000,
    )

    if response and response.status >= 400:
        raise RuntimeError(
            f"Login page returned HTTP {response.status}."
        )

    username = find_visible(
        page.locator(
            'input[name="username"], '
            'input[name="email"], '
            'input[type="email"], '
            'input[autocomplete="username"]'
        )
    )

    password = find_visible(
        page.locator(
            'input[name="password"], '
            'input[type="password"], '
            'input[autocomplete="current-password"]'
        )
    )

    if username is None or password is None:
        raise RuntimeError(
            "Login fields were not found. "
            "The login page structure may have changed."
        )

    username.fill(EMAIL)
    password.fill(PASSWORD)

    submit = find_visible(
        page.locator(
            'button[type="submit"], input[type="submit"]'
        )
    )

    if submit is None:
        raise RuntimeError("The login submit button was not found.")

    log.info("Submitting login credentials.")
    submit.click()

    try:
        page.wait_for_url(
            lambda url: (
                urlparse(url).hostname == "control.katabump.com"
                and "/auth/login" not in url
            ),
            timeout=TIMEOUT_MS,
        )

    except PlaywrightTimeoutError:
        if "/auth/login" in page.url:
            save_artifacts(page, "login_failure")

            raise RuntimeError(
                "Login could not be verified. Check the credentials, "
                "verification challenges, and login flow."
            )

    if urlparse(page.url).hostname != "control.katabump.com":
        raise RuntimeError("Unexpected hostname after login.")

    log.info("Login navigation completed.")


def renew_server(page):
    server_url = f"{BASE_URL}/server/{SERVER_ID}"

    log.info("Opening server %s.", SERVER_ID)

    response = page.goto(
        server_url,
        wait_until="domcontentloaded",
        timeout=60000,
    )

    if response and response.status >= 400:
        raise RuntimeError(
            f"Server page returned HTTP {response.status}."
        )

    if urlparse(page.url).hostname != "control.katabump.com":
        raise RuntimeError("Unexpected hostname on the server page.")

    if "/auth/login" in page.url:
        raise RuntimeError("The session is not authenticated.")

    page.locator("body").wait_for(state="visible")

    renew_pattern = re.compile(r"^(Renew|Renew now)$", re.I)

    renew_button = find_visible(
        page.get_by_role("button", name=renew_pattern)
    )

    if renew_button is None:
        renew_button = find_visible(
            page.get_by_role("link", name=renew_pattern)
        )

    if renew_button is None:
        log.info(
            "No enabled renewal action was found. "
            "No renewal action was performed."
        )
        save_artifacts(page, "renewal_not_available")
        return 0

    log.info("Clicking the renewal action.")
    renew_button.click()

    dialog = page.get_by_role("dialog")

    try:
        dialog.wait_for(state="visible", timeout=3000)

    except PlaywrightTimeoutError:
        dialog = None

    if dialog is not None:
        confirm = find_visible(
            dialog.get_by_role(
                "button",
                name=re.compile(
                    r"^(Confirm|Confirm renewal|Yes, renew)$",
                    re.I,
                ),
            )
        )

        if confirm is not None:
            log.info("Confirming the renewal in the dialog.")
            confirm.click()

    success_pattern = re.compile(
        r"renewal successful|server renewed|renewed successfully",
        re.I,
    )

    try:
        page.get_by_text(success_pattern).first.wait_for(
            state="visible",
            timeout=TIMEOUT_MS,
        )

        log.info("The dashboard displayed a renewal success message.")
        return 0

    except PlaywrightTimeoutError:
        log.warning(
            "No recognized success message appeared. "
            "The renewal result could not be verified."
        )

        save_artifacts(page, "renewal_unverified")
        return 3


def main():
    try:
        validate_config()

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=HEADLESS)

            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                accept_downloads=False,
            )

            page = context.new_page()
            page.set_default_timeout(TIMEOUT_MS)

            try:
                login(page)
                return renew_server(page)

            except Exception:
                log.exception("The renewal workflow failed.")
                save_artifacts(page, "failure")
                return 1

            finally:
                context.close()
                browser.close()

    except Exception:
        log.exception("Configuration or browser startup failed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
