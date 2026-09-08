"""The (mocked, per Section 3.6's scope note) human operator surface. This is
deliberately a CLI, not a graphical co-browsing console -- the brief explicitly
allows mocking the operator UI as long as "the handoff mechanism and the
control-transfer model" are real. What's real here: this process attaches to the
exact same live Chromium instance the automation was driving (via CDP,
BrowserSession.attach), so actions taken here happen in the SAME session, not a
fresh one; every action is logged into the shared control-state file AND the run's
evidence log; and handing back control is an explicit, auditable step.
"""
from __future__ import annotations

import sys
from pathlib import Path

from src.escalation.control import ControlChannel
from src.perception.snapshot import capture
from src.perception import locate
from src.runtime.browser_session import BrowserSession


def run_operator_session(run_dir: str) -> None:
    control_path = Path(run_dir) / "control.json"
    if not control_path.exists():
        print(f"No control.json found in {run_dir} -- is there a paused run to attach to?")
        sys.exit(1)

    control = ControlChannel(control_path)
    state = control.read()
    if state.controller != "operator":
        print(f"Control is currently held by '{state.controller}', not waiting on an operator. Nothing to do.")
        return

    print("=" * 72)
    print("HUMAN OPERATOR CONSOLE (mock)")
    print(f"Reason for escalation: {state.reason}")
    print(f"Context: {state.context}")
    print("Attaching to the LIVE session the automation was using (same browser, same page)...")
    print("=" * 72)

    session = BrowserSession.attach(state.cdp_port)
    try:
        while True:
            snapshot = capture(session.page)
            print("\n--- current page ---")
            print(snapshot.to_numbered_text())
            print("\nCommands: click <index> | type <index> <text...> | select <index> <value> | "
                  "state | screenshot | resume [completed|abandoned] | quit")
            try:
                raw = input("operator> ").strip()
            except EOFError:
                raw = "resume abandoned"
            if not raw:
                continue
            parts = raw.split(maxsplit=2)
            cmd = parts[0].lower()

            if cmd == "quit":
                print("Exiting without handing control back (run stays paused).")
                return

            if cmd == "state":
                continue

            if cmd == "screenshot":
                p = Path(run_dir) / "screenshots" / f"operator_{len(snapshot.elements)}.png"
                session.page.screenshot(path=str(p), full_page=True)
                print(f"Saved {p}")
                continue

            if cmd == "resume":
                resolution = parts[1] if len(parts) > 1 else "completed"
                control.hand_back(resolution)
                print(f"Control handed back to automation (resolution={resolution}).")
                return

            if cmd in ("click", "type", "select") and len(parts) >= 2:
                try:
                    index = int(parts[1])
                except ValueError:
                    print("Usage: click <index> | type <index> <text> | select <index> <value>")
                    continue
                element = next((e for e in snapshot.elements if e.index == index), None)
                if element is None:
                    print(f"No element with index {index}.")
                    continue
                candidates = element.locator_candidates()
                try:
                    resolved = locate.resolve(session.page, candidates[0], candidates[1:])
                except Exception as e:
                    print(f"Could not resolve element {index}: {e}")
                    continue
                try:
                    if cmd == "click":
                        resolved.locator.click()
                        control.append_human_action({"action": "click", "element": element.name})
                    elif cmd == "type":
                        text = parts[2] if len(parts) > 2 else ""
                        resolved.locator.fill(text)
                        control.append_human_action({"action": "type", "element": element.name, "text": text})
                    elif cmd == "select":
                        value = parts[2] if len(parts) > 2 else ""
                        resolved.locator.select_option(value=value)
                        control.append_human_action({"action": "select", "element": element.name, "value": value})
                    session.page.wait_for_load_state("networkidle", timeout=5000)
                except Exception as e:
                    print(f"Action failed: {e}")
                continue

            print("Unrecognized command.")
    finally:
        session.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m src.escalation.operator <run_dir>")
        sys.exit(1)
    run_operator_session(sys.argv[1])
