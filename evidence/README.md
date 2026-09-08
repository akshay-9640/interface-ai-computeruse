# Evidence index

`example_artifact.json` is the capability artifact produced by the discovery
run below (same content as `artifacts_store/open_member_subaccount.v1.json`,
copied here for convenience).

Each `sessions/<run>/` directory has `run.jsonl` (structured, redacted event
log -- for discovery runs this includes the LLM's own stated reasoning per
step), `screenshots/`, and `result.json` (the final structured outcome).

| Run | What it shows |
|---|---|
| `discovery-20260908T200156-082130` | **The real, genuine LLM-driven discovery run** (Claude, via the Anthropic API, tool-calling against the live mock app). Goal: look up member 10001, open a new Savings sub-account with a $100.00 deposit, stop at the confirmation screen. 7 recorded steps, no escalation needed. Produced `example_artifact.json`. |
| `replay-20260908T200243-0d30dd` | Deterministic replay of that artifact, **`--confirm`**: finalizes the (irreversible) sub-account and returns the new account number as a typed output. No LLM involved. |
| `replay-20260908T200252-7a8ce9` | Replay with `member_id=99999` (doesn't exist): hits the target app's own "not found" page and returns a clean `business_outcome` result (`member_not_found`) -- not a crash, per assignment glossary. |
| `replay-20260908T200440-37508c` | **Human escalation & handoff demo.** `member_id=10002` is flagged "restricted" in the mock app; replay treats this as a hard failure it cannot resolve alone and pauses, handing control of the *same live browser session* (via CDP) to a human operator (`src/escalation/operator.py`). The operator enters a supervisor PIN the automation was never given, submits the override, and hands control back. Replay detects the alternate completion path and finishes with `status: success`, `escalation_used: true`. The PIN itself is redacted everywhere in this run's logs (`control.json`, `run.jsonl`) -- only the fact that a PIN was entered into the "Supervisor PIN:" field is recorded, never its value. |

Reproduce any of these with the commands in `README.md`'s "Demo path" section
(the mock app's in-memory state resets between runs via `POST /_reset`).
