"""Session establishment is treated as a concern OUTSIDE any individual capability
artifact -- like a service-account credential a production deployment would already
have provisioned per tenant, not something the LLM types in or that a recorded flow
re-does on every replay. Both discovery and replay call `login()` once, before their
respective step lists run; neither the LLM nor the artifact's steps ever see or
handle the password (assignment 3.4: never persist secrets)."""
from __future__ import annotations

import os

from playwright.sync_api import Page

MOCKAPP_USERNAME = "operator1"
MOCKAPP_PASSWORD = os.environ.get("MOCKAPP_PASSWORD", "letmein123")


def login(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/login", wait_until="networkidle")
    page.get_by_role("textbox").nth(0).fill(MOCKAPP_USERNAME)
    # the password field's accessible role is also "textbox" in most a11y trees;
    # target it via the input type instead, since that's stable and non-secret.
    page.locator('input[type="password"]').fill(MOCKAPP_PASSWORD)
    page.get_by_role("button", name="Sign In").click()
    page.wait_for_load_state("networkidle")
