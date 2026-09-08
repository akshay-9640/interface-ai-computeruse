"""A live browser session usable by discovery, replay, AND a human operator attaching
mid-run. The session is launched with a CDP debugging port; anyone who knows that
port (read from the run's control-state file) can attach a second Playwright
connection to the SAME running browser/page via connect_over_cdp -- this is the seam
that makes "take control of the live session, not a fresh one" (3.6) real rather than
simulated. See src/escalation/control.py for the handoff protocol built on top of
this."""
from __future__ import annotations

import socket
from dataclasses import dataclass

from playwright.sync_api import sync_playwright, Playwright, Browser, BrowserContext, Page


def find_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@dataclass
class BrowserSession:
    playwright: Playwright
    browser: Browser
    context: BrowserContext
    page: Page
    cdp_port: int
    _owns_browser: bool = True

    @classmethod
    def launch(cls, headless: bool = True) -> "BrowserSession":
        port = find_free_port()
        pw = sync_playwright().start()
        browser = pw.chromium.launch(
            headless=headless,
            args=[f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1"],
        )
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        return cls(playwright=pw, browser=browser, context=context, page=page, cdp_port=port)

    @classmethod
    def attach(cls, cdp_port: int) -> "BrowserSession":
        """Used by the operator CLI to join an already-running session (same
        browser process, same page) rather than opening a fresh one."""
        pw = sync_playwright().start()
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else context.new_page()
        return cls(playwright=pw, browser=browser, context=context, page=page, cdp_port=cdp_port, _owns_browser=False)

    def close(self) -> None:
        """Only actually tears down the browser process if this session launched
        it. An attached operator session must NOT close the shared browser out
        from under the agent process."""
        try:
            if self._owns_browser:
                self.context.close()
                self.browser.close()
            # else: an attached (operator) session must not call browser.close() --
            # it does not own the underlying process, only the CDP connection.
        finally:
            self.playwright.stop()
