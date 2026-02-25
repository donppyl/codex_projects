#!/usr/bin/env python3
"""Assisted one-hour job application bot for LinkedIn and Indeed.

This script automates repetitive steps (login, search, opening apply flows) and
keeps the final submission step manual for safety and account compliance.

Usage:
    export LINKEDIN_EMAIL='you@example.com'
    export LINKEDIN_PASSWORD='your-password'
    export INDEED_EMAIL='you@example.com'
    export INDEED_PASSWORD='your-password'
    python job_autopilot.py --keywords "python developer" --location "Remote"
"""

from __future__ import annotations

import argparse
import os
import re
import time
from dataclasses import dataclass
from typing import Callable

from playwright.sync_api import BrowserContext, Error, Page, sync_playwright


@dataclass
class Config:
    keywords: str
    location: str
    resume_path: str | None
    max_linkedin: int
    max_indeed: int
    headless: bool
    wait_between_actions: float


def pause(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


def parse_args() -> Config:
    parser = argparse.ArgumentParser(
        description=(
            "Automate LinkedIn + Indeed search and assisted applications for jobs "
            "posted in the last hour."
        )
    )
    parser.add_argument("--keywords", required=True, help="Job keywords.")
    parser.add_argument("--location", default="Remote", help="Job location.")
    parser.add_argument("--resume", help="Path to resume file.")
    parser.add_argument(
        "--max-linkedin", type=int, default=10, help="Max LinkedIn applications."
    )
    parser.add_argument(
        "--max-indeed", type=int, default=10, help="Max Indeed applications."
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode (not recommended for MFA/captcha).",
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=0.6,
        help="Delay between actions to reduce anti-bot triggers.",
    )

    args = parser.parse_args()
    return Config(
        keywords=args.keywords,
        location=args.location,
        resume_path=args.resume,
        max_linkedin=args.max_linkedin,
        max_indeed=args.max_indeed,
        headless=args.headless,
        wait_between_actions=args.wait,
    )


def must_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing environment variable: {name}")
    return value


def safe_click(page: Page, selector: str) -> bool:
    try:
        locator = page.locator(selector).first
        if locator.count() == 0:
            return False
        locator.click(timeout=4000)
        return True
    except Error:
        return False


def with_retries(operation: Callable[[], bool], attempts: int = 3) -> bool:
    for _ in range(attempts):
        if operation():
            return True
        time.sleep(0.5)
    return False


def wait_for_manual_checkpoint(page: Page, note: str, timeout_seconds: int = 90) -> None:
    print(f"[ACTION NEEDED] {note}")
    print(
        f"Complete it in the opened browser window. Waiting up to {timeout_seconds} seconds..."
    )
    start = time.time()
    while time.time() - start < timeout_seconds:
        time.sleep(1)
        # Let user complete captcha/MFA/manual review.
        if "checkpoint" not in page.url and "challenge" not in page.url:
            return


def posted_within_hour(label: str) -> bool:
    text = label.lower().strip()
    if any(token in text for token in ["just posted", "today", "new"]):
        return True

    minute_match = re.search(r"(\d+)\s*minute", text)
    if minute_match:
        return int(minute_match.group(1)) <= 60

    hour_match = re.search(r"(\d+)\s*hour", text)
    if hour_match:
        return int(hour_match.group(1)) <= 1

    return False


def login_linkedin(page: Page) -> None:
    page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
    page.fill("#username", must_env("LINKEDIN_EMAIL"))
    page.fill("#password", must_env("LINKEDIN_PASSWORD"))
    page.click("button[type='submit']")
    page.wait_for_load_state("networkidle")

    if "checkpoint" in page.url or "challenge" in page.url:
        wait_for_manual_checkpoint(page, "Finish LinkedIn verification (MFA/captcha).")


def apply_linkedin(context: BrowserContext, cfg: Config) -> int:
    page = context.new_page()
    login_linkedin(page)

    query = (
        "https://www.linkedin.com/jobs/search/?"
        f"keywords={cfg.keywords.replace(' ', '%20')}&"
        f"location={cfg.location.replace(' ', '%20')}&"
        "f_LF=f_AL&f_TPR=r3600&sortBy=DD"
    )
    page.goto(query, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)

    cards = page.locator(".jobs-search-results__list-item")
    total = cards.count()
    print(f"[LinkedIn] Found {total} easy-apply cards in feed.")

    applied = 0
    for i in range(total):
        if applied >= cfg.max_linkedin:
            break
        card = cards.nth(i)
        card.scroll_into_view_if_needed()
        card.click()
        pause(cfg.wait_between_actions)

        posted_label = ""
        try:
            posted_label = page.locator(".job-details-jobs-unified-top-card__primary-description-container").inner_text(timeout=2000)
        except Error:
            pass

        if posted_label and not posted_within_hour(posted_label):
            continue

        if not with_retries(lambda: safe_click(page, "button.jobs-apply-button"), attempts=2):
            continue

        pause(cfg.wait_between_actions)

        if cfg.resume_path:
            for selector in [
                "input[type='file']",
                "input[name='file']",
            ]:
                try:
                    file_input = page.locator(selector).first
                    if file_input.count() > 0:
                        file_input.set_input_files(cfg.resume_path)
                except Error:
                    pass

        print("[LinkedIn] Review and submit this application manually.")
        page.wait_for_timeout(5000)
        applied += 1

        # Close modal if still open and continue.
        safe_click(page, "button[aria-label='Dismiss']")
        safe_click(page, "button[aria-label='Discard']")
        pause(cfg.wait_between_actions)

    print(f"[LinkedIn] Prepared {applied} applications.")
    page.close()
    return applied


def login_indeed(page: Page) -> None:
    page.goto("https://secure.indeed.com/account/login", wait_until="domcontentloaded")
    page.fill("input[type='email']", must_env("INDEED_EMAIL"))
    page.click("button[type='submit']")
    page.wait_for_timeout(1000)
    page.fill("input[type='password']", must_env("INDEED_PASSWORD"))
    page.click("button[type='submit']")
    page.wait_for_load_state("networkidle")

    if "captcha" in page.url or "auth" in page.url:
        wait_for_manual_checkpoint(page, "Finish Indeed verification (captcha/MFA).")


def apply_indeed(context: BrowserContext, cfg: Config) -> int:
    page = context.new_page()
    login_indeed(page)

    query = (
        "https://www.indeed.com/jobs?"
        f"q={cfg.keywords.replace(' ', '+')}&"
        f"l={cfg.location.replace(' ', '+')}&"
        "fromage=1&sort=date"
    )
    page.goto(query, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)

    cards = page.locator(".job_seen_beacon")
    total = cards.count()
    print(f"[Indeed] Found {total} cards in feed.")

    applied = 0
    for i in range(total):
        if applied >= cfg.max_indeed:
            break

        card = cards.nth(i)
        card.scroll_into_view_if_needed()

        age_text = ""
        try:
            age_text = card.inner_text(timeout=1000)
        except Error:
            pass

        if age_text and not posted_within_hour(age_text):
            continue

        card.click()
        pause(cfg.wait_between_actions)

        if not safe_click(page, "a:has-text('Apply now')") and not safe_click(
            page, "button:has-text('Apply now')"
        ):
            continue

        pause(cfg.wait_between_actions)

        if cfg.resume_path:
            for selector in ["input[type='file']", "input[name='resume']"]:
                try:
                    file_input = page.locator(selector).first
                    if file_input.count() > 0:
                        file_input.set_input_files(cfg.resume_path)
                except Error:
                    pass

        print("[Indeed] Review and submit this application manually.")
        page.wait_for_timeout(5000)
        applied += 1

        safe_click(page, "button[aria-label='Close']")
        safe_click(page, "button:has-text('Cancel')")

    print(f"[Indeed] Prepared {applied} applications.")
    page.close()
    return applied


def main() -> None:
    cfg = parse_args()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=cfg.headless)
        context = browser.new_context()
        try:
            linked_in_count = apply_linkedin(context, cfg)
            indeed_count = apply_indeed(context, cfg)
        finally:
            context.close()
            browser.close()

    print(
        "Done. Prepared applications: "
        f"LinkedIn={linked_in_count}, Indeed={indeed_count}."
    )


if __name__ == "__main__":
    main()
