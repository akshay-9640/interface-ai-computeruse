# Computer-Use Automation System

A small, real implementation of the interface.ai take-home: an LLM-driven agent
discovers a UI flow once, against a deliberately legacy-styled mock banking
back-office app, and compiles it into a typed, versioned, deterministically
replayable **capability artifact**. Replay drives the same app again with no LLM
in the loop, with explicit error handling and a human-in-the-loop escalation path
for anything replay can't safely resolve on its own.

See [`REPORT.md`](REPORT.md) for the design write-up (architecture, artifact
schema, determinism/error handling, heterogeneity & multi-tenant story,
escalation model, safety guardrails, and what was cut).

## Live demo

A public, no-LLM version of the demo path below runs as a small web console
(`demo/`): visitors replay the saved capability against a private Meridian Core
instance, watch each step and screenshot, and play the human supervisor in the
escalation scenario (the console pipes the exact operator commands from step 3
into the real `manage.py operator`, attached to the same paused browser). It also
shows the saved discovery run, with Claude's stated reasoning per step, and the
artifact JSON. Nothing calls an LLM, so it needs no API key and costs nothing per visit.

There is also a **recorded, static version** (`site/`, deployed on Vercel via
`vercel.json`): the same console, playing back real runs of every scenario
(event log, screenshots, result, and the human handoff) on their recorded
timeline. It loads instantly and needs no server. To refresh the recordings,
start the live demo locally and run `python -m demo.record`.

Run it locally:

```bash
pip install -r requirements-demo.txt
playwright install chromium
gunicorn -w 1 --threads 8 -b 127.0.0.1:8080 demo.server:app   # open http://127.0.0.1:8080
```

Deploy it: the repo has a `Dockerfile` and a Render blueprint (`render.yaml`).
On Render, New > Blueprint > this repo, free plan, no environment variables.
Guardrails for a public URL: one run at a time, a per-IP rate limit, hard
timeouts, mock state reset before every run, an unanswered escalation is handed
back as "abandoned" after 90 seconds, and only the last 25 demo runs are kept.

## What's in here

```
src/mockapp/       "Meridian Core" -- the target app (Flask, server-rendered,
                    table layout, no test IDs; simulates not-found, validation
                    errors, permission denial, session timeout, a transient
                    "system busy" failure, and a confirm-before-finalize step)
src/perception/     Turns a live Playwright page into a numbered list of
                    interactive elements + ranked, CSS-free locator candidates
src/agent/          The LLM discovery loop (Claude, tool-calling) + the
                    step-trace -> CapabilityArtifact builder
src/artifacts/      The capability artifact schema (Pydantic) + JSON store
src/replay/         The deterministic replay engine + error-taxonomy matching
src/policy/         Allowlist/guardrails, redaction, and the app's default
                    error taxonomy (error_taxonomy.yaml, allowlist.yaml)
src/escalation/     Control-transfer protocol + the (CLI-mocked) operator console
src/runtime/        Shared browser-session (CDP-attachable) + auth helpers
src/cli/            `discover` / `replay` / `operator` / `list` commands
tests/              Unit tests for the parts that don't need a browser
evidence/           Saved runs (see below) -- discovery, replay, and escalation
```

## Setup

Requires Python 3.11+ and the Playwright Chromium browser.

```bash
pip install -r requirements.txt
playwright install chromium   # skip if already installed on your system
cp .env.example .env
# edit .env and set ANTHROPIC_API_KEY (only needed for `discover`; `replay`
# and the mock app itself never call an LLM and work with no key at all)
```

Start the target app (leave running in its own terminal):

```bash
python -m src.mockapp.app
# -> Meridian Core listening on http://127.0.0.1:5001
```

Run the tests (no browser, no API key, no running mock app required):

```bash
python -m pytest tests/ -q
```

## Demo path

All commands below assume the mock app is running at `http://127.0.0.1:5001`
(the default) and are run from the repo root.

### 1. Discover a capability (real LLM run)

```bash
python manage.py discover \
  --member-id 10001 --account-type Savings --deposit 100.00
```

This drives a live Claude tool-calling agent against Meridian Core: it logs in
(handled by the harness, not the LLM -- see REPORT.md), searches for the
member, opens the "new sub-account" form, fills it out, submits, and stops at
the confirmation screen (it is instructed never to click the final, irreversible
"Confirm and Open Account" button itself). On success it saves a capability
artifact to `artifacts_store/open_member_subaccount.v1.json` (and
`...latest.json`), and the full step-by-step run -- including the model's own
stated reasoning for each action and a screenshot per step -- is written to
`evidence/sessions/discovery-<timestamp>-<id>/`.

Add `--headed` to watch it in a visible browser window (requires a display).

### 2. Replay the capability deterministically (no LLM)

Dry run -- reaches the confirmation screen and stops (the risky final step is
gated):

```bash
python manage.py replay \
  --param member_id=10001 --param account_type=Savings --param initial_deposit=100.00
```

Full run -- also finalizes the (irreversible) account creation and returns the
new account number:

```bash
python manage.py replay --confirm \
  --param member_id=10001 --param account_type=Savings --param initial_deposit=100.00
```

Hit a business outcome instead of a crash (member doesn't exist):

```bash
python manage.py replay \
  --param member_id=99999 --param account_type=Savings --param initial_deposit=100.00
```

### 3. Trigger and resolve a human escalation

Member `10002` is flagged "restricted" -- Meridian Core blocks a normal
operator from opening a sub-account for them at all. Replay treats this as a
hard failure it cannot safely resolve alone (see REPORT.md, "Safety") and
escalates to a human operator on the **same live browser session**, rather than
failing outright:

```bash
python manage.py replay --confirm \
  --param member_id=10002 --param account_type=Checking --param initial_deposit=100.00
```

It will print something like:

```
[ESCALATION] Replay paused at step 'step_04_click'.
  Reason: Member is restricted; requires supervisor override (see escalation path).
  Run:   python manage.py operator --run-dir evidence/sessions/replay-<...>
```

In another terminal, attach as the operator:

```bash
python manage.py operator --run-dir evidence/sessions/replay-<...>
```

The operator console shows the live page (same session, same browser -- not a
fresh one) as a numbered element list. To complete the supervisor override (the
PIN is `4242`, known only to a human supervisor -- the automation is never
given it):

```
operator> type 0 4242
operator> select 1 Checking
operator> click 2
operator> resume completed
```

The paused `replay` process detects the page is now in a terminal success state
reached via this alternate path, extracts the new account number, and exits
with `status: success`, `escalation_used: true`.

### 4. List saved capabilities (agent-facing catalog)

```bash
python manage.py list
```

## Resetting the mock app's state

The mock app keeps its data in memory. To reset it between demo runs:

```bash
curl -X POST http://127.0.0.1:5001/_reset
```

(This route is intentionally excluded from the automation's own allowlist --
see `src/policy/allowlist.yaml`.)

## Notes

- `MOCKAPP_SESSION_TIMEOUT_SECONDS` (default 900) controls the mock app's
  session/auth timeout, used to exercise the "session expired mid-replay"
  error-taxonomy entry.
- Every run's evidence directory (`evidence/sessions/<run>/`) contains
  `run.jsonl` (structured, redacted event log), `screenshots/`, and
  `result.json`. Nothing sensitive is ever written to these -- see
  `src/policy/guardrails.py:redact` and REPORT.md, "Safety".
