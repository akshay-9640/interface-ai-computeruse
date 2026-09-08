# Design Write-Up

## 1. Architecture

Single Python process, no queues or services -- discovery and replay are both
just "drive a Playwright browser, talk to a target app, write structured
evidence." The target is `src/mockapp`, **Meridian Core**: a Flask app I wrote
to stand in for a legacy credit-union servicing console (Section 4's "proxy
target"). It's server-rendered, table-laid-out, has no test IDs, and
deliberately exercises the runtime conditions Section 1 calls out: a
not-found result, a validation error, a permission denial requiring supervisor
override, a session timeout, a transient "system busy" failure, and a
confirm-before-finalize step for the one irreversible action (opening a
sub-account). I built my own target instead of using a public demo site so I
could get a genuinely hostile-but-controllable surface without fighting a
third party's terms, rate limits, or a moving target app.

The pipeline has five stages, each a separate module with one job:

1. **Perception** (`src/perception/snapshot.py`) turns a live page into a
   numbered list of interactive elements -- role, accessible name, form field
   name, current value -- computed the way a screen reader would, not via CSS.
2. **Discovery** (`src/agent/discover.py`) runs the observe/decide/act loop: a
   Claude tool-calling agent sees the numbered list (+ a screenshot) and picks
   one action per turn from a small, closed tool set.
3. **Artifact building** (`src/agent/artifact_builder.py`) converts a
   successful run's step trace into a typed `CapabilityArtifact`.
4. **Replay** (`src/replay/engine.py`) re-drives the same app from a saved
   artifact with no LLM involved.
5. **Escalation** (`src/escalation/`) is the cross-cutting seam both discovery
   and replay call into when they can't safely continue.

Key trade-off: I used **one process per run**, not a service with a job queue.
The brief explicitly says not to prematurely build scaling infrastructure, and
a single process is simpler to reason about and to grade. The seam that would
turn this into a service is narrow and already exists: `run_discovery()` and
`replay()` are both plain functions that return a structured result; wrapping
them behind an HTTP endpoint or a queue worker is mechanical, not architectural.

Locator targeting is the one piece of logic discovery and replay share
directly (`src/perception/locate.py`), not just conceptually: both call the
same `resolve()` function against the same candidate list format. That's a
deliberate choice -- if I'd hand-written a separate "execute this recorded
step" path for replay, discovery could silently rely on interaction patterns
replay doesn't support. Because they share code, whatever worked during
discovery is mechanically guaranteed to be replayable.

## 2. Artifact schema

A `CapabilityArtifact` (`src/artifacts/schema.py`) is closer to a typed
function signature than a script:

- `input_params` / `outputs`: typed, described, with an example value each --
  what a calling agent (or a human reviewer) passes in and gets back.
- `steps`: each one has a **primary locator + ranked fallbacks**, not a single
  selector. A locator candidate is one of `role_name` (real ARIA accessible
  name -- resolved via Playwright's `get_by_role`), `label_adjacent` (a
  legacy-table-form pattern: find a label cell by exact text, then step to the
  adjacent `<td>`'s control -- see below), `text`, or `nth_of_role` as a last
  resort. Which strategies even get proposed for an element depends on *where*
  its name came from (`ElementSnapshot.name_source`): a name borrowed from a
  neighboring table cell can't be found via `get_by_role`, because the browser's
  real accessibility tree doesn't know about it. Treating "how do we know this
  element's name" and "how do we find it again" as the same fact, tracked
  end-to-end from perception through to the artifact, is what makes the
  locators actually work on a legacy, label-less table form rather than just
  looking robust on paper. (I hit this as a real bug during testing: a naive
  version that only used `role_name` silently resolved to a `<td>` label
  instead of its input, and `.fill()` threw. `label_adjacent` -- an XPath step
  from the label cell to its sibling cell's control -- was the fix.)
- `error_handlers` is a **separate, declarative layer**, deliberately not
  baked into individual steps -- see Section 3.
- `checkpoint`: a success assertion, captured from what the LLM itself said
  proved the goal was reached (`finish_success`'s `checkpoint_text`).
- `confirmation_step_id` + `risk_level`/`requires_confirmation`: see Section 6.
- `provenance`: model, discovery run ID, timestamp, and a `reviewed` flag
  (defaults `false`) -- an artifact records that it hasn't been human-reviewed
  yet rather than silently implying it has.

**Parameterization.** The discovery run is driven with concrete example values
(a real member ID, a real deposit amount). `artifact_builder.build_artifact`
walks the recorded trace and replaces any typed/selected value that exactly
matches a declared parameter's example with `{param_name}` in that step's
`value_template`. The CLI's `discover` command also checks, after building the
artifact, that every declared parameter actually appears somewhere in the
templates -- if the LLM typed the value in a slightly different format than
instructed, this fails loudly instead of silently shipping a capability that
ignores the caller's input. This is a deliberately narrow slice of what a full
"canonicalization" pass (optional stretch goal 8) would do; see Section 4 and
Cuts.

## 3. Determinism & error handling

Replay resolves each step's locator with the same `resolve()` function
discovery used, walking primary -> fallbacks with a bounded per-candidate
timeout, so "stable element/control targeting" isn't a separate reimplementation.

The more interesting half of 3.3 is the error taxonomy. Runtime errors
(not-found, validation, permission denial, session timeout, a transient
backend blip) are a property of the **target app**, not of any one recorded
flow -- every capability recorded against Meridian Core can hit a not-found
page, whether or not its own discovery run happened to see one. So
`error_handlers` live partly outside any single artifact, in
`src/policy/error_taxonomy.yaml` keyed by `app_name`, and partly on the
artifact itself; `src/replay/errors.py` merges them (artifact-level entries
override app defaults by `handler_id`). This is also a deliberate hook for
reuse across tenants running the same vendor product -- see Section 4.

Each handler is a `(match_text, classification, recovery)` triple, checked
against the resulting page's heading + body text after every step:

- **`business_outcome`** (member not found, invalid deposit amount): replay
  stops immediately and returns a clean structured result -- `status:
  "business_outcome"`, a label, and whatever the handler declares as outputs.
  Never conflated with a crash.
- **`recoverable`** (the mock app's simulated "system busy" -- member `10003`
  fails its first confirm attempt, then succeeds): the *same step* is retried
  up to `max_retries` times, with a short wait for `wait_and_retry`. This only
  works because the busy page happens to re-offer the identical control (same
  accessible name) -- a deliberate mock-app design choice so retry means "redo
  the same step," not "somehow reconstruct the pre-error state."
- **`hard_failure`** (permission denied, session expired): replay stops and
  either escalates to a human (Section 5) or returns a structured failure with
  the failing step, what was expected, and what was observed.

Result shape (`src/replay/result.py`) is a three-way discriminated status
(`success` / `business_outcome` / `failure`), not a boolean, specifically
because the glossary is right that conflating "no such member" with a crash is
the most common mistake here.

**The confirm-screen checkpoint problem.** This capability's happy path stops
one screen short of an irreversible action by design (Section 6), so its
recorded checkpoint text ("Confirm New Sub-Account") only describes that safe
state. When `confirm=True` and the gated final step actually runs, the page
moves *past* that checkpoint to a state the LLM never saw and so never
authored a checkpoint string for. Rather than inventing app-specific knowledge
in the harness, replay verifies success **structurally** in that case: if at
least one declared output can be extracted from the resulting page, that's
proof positive of a genuine terminal state. The same logic underpins resuming
after a human escalation resolves via a path the recorded steps never
anticipated (Section 5).

Secondarily, on UI drift: because locators are role/accessible-name based
rather than CSS, small layout/styling changes don't break resolution; the
`nth_of_role` fallback and per-step `locator_fallbacks` give some slack for
moderate structural changes, but a genuinely redesigned flow still needs
re-discovery -- that's out of scope here (see Cuts).

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** The seam is `src/perception` + `src/perception/locate.py`:
everything above them (the artifact schema, the replay engine, the discovery
loop's tool contract) only ever talks about elements with a role, an
accessible name, and a locator strategy -- never CSS, never pixel coordinates
as a primary strategy. To extend to a genuinely worse legacy web surface
(framesets, deeply nested tables with no labels at all), I'd add more
`name_source`/locator strategies to `snapshot.py` and `locate.py` (e.g. OCR'd
text-proximity for a canvas-rendered grid) without touching the artifact
schema or replay engine at all -- they already only consume the
`LocatorCandidate` contract. For a native desktop app, `perception.capture()`
and `locate.resolve()` would be reimplemented against an OS accessibility API
(e.g. UI Automation on Windows, AT-SPI on Linux) instead of `page.evaluate()`
and Playwright locators, but would still produce the same `ElementSnapshot`
shape and the same `LocatorCandidate` strategies conceptually (role+name is a
first-class concept in OS accessibility trees too) -- the rest of the pipeline
would not need to know the difference.

**Multi-tenant reuse.** `TargetSpec` separates `app_name` (the *product*, e.g.
`meridian_core`) from `tenant_id` (a specific institution's instance, currently
unused since this project only implements one tenant). The error taxonomy is
already keyed by `app_name`, not by artifact -- so a shared vendor product's
known error signatures (its "record not found" page, its permission-denial
copy) are authored once and every capability recorded against any tenant
running that product inherits them for free, without re-recording. For a
capability itself, the natural per-tenant override point is exactly the same
mechanism error-handler overriding already uses: a tenant-specific artifact
version could override individual `steps[].locator` entries (a rebranded
tenant might rename a button) while inheriting everything else, keyed by
`(capability_id, tenant_id)` falling back to `(capability_id, None)` --
un-implemented here, but the schema doesn't block it (`version` + `target.tenant_id`
already exist as the join keys). **Drift detection** would mean replaying a
capability's steps against a new tenant/version and recording which locators
resolved via which strategy/confidence; a string of `nth_of_role`-only
resolutions (the lowest-confidence fallback) across many capabilities for one
tenant is the signal that tenant's build has drifted and needs re-discovery --
the `LocatorCandidate.confidence` field and the trace's `strategy_used` logging
already carry the raw data this would consume, but I did not build the
aggregation/alerting on top of it (see Cuts).

## 5. Escalation & handoff

**Detect.** Two triggers: the discovery agent calls an explicit `escalate`
tool when it judges it can't safely continue (prompted to prefer this over
guessing on anything unexpected or irreversible-looking); replay escalates
automatically on a `hard_failure` classification or an unhandled execution
error, gated by `CapabilityArtifact.escalate_on_hard_failure` (default `true`).

**Take control of the live session, not a fresh one.** Every browser session
(`src/runtime/browser_session.py`) launches Chromium with a CDP debugging
port. On escalation, the automation process writes a small `control.json`
(`src/escalation/control.py`) into the run's evidence directory: who's in
control, the CDP port, why it stopped, and context (goal/capability, current
step, a screenshot). It then blocks, polling that file. The operator CLI
(`src/escalation/operator.py`) reads it and calls
`playwright.chromium.connect_over_cdp()` against the **same port** -- a second
Playwright connection into the exact browser/page the automation was driving,
not a new one. It shows the same numbered-element view discovery uses, so a
human can `click <index>` / `type <index> <text>` / `select <index> <value>`
using the same perception + locator-resolution code. Every human action is
appended to `control.json`'s `human_actions` list *and* the run's evidence log.
`resume completed|abandoned` flips `controller` back to `agent`, unblocking
the paused process.

**Hand back and resume.** The automation process re-observes the page after
handback. If the human's actions already reached a terminal state via a path
the recorded steps never anticipated (the supervisor-override demo: a
permission-denied page has its own form, submitting it jumps straight to the
success page, bypassing every remaining recorded step), the structural
output-then-checkpoint check described in Section 3 catches it and the run
finishes as a success with `escalation_used: true` and a
`completed_via_human_operator` label. Otherwise, execution resumes from the
next recorded step, picking up wherever the human left the page -- appropriate
for a lighter intervention (e.g. dismissing something unexpected) rather than
a full alternate completion path.

**What's mocked, deliberately** (per the brief's scope note): the operator
console is a CLI, not a graphical co-browsing UI. What's real: the same live
session, an auditable control-transfer record, and a human genuinely typing a
credential (a supervisor PIN) the automation was never given -- see Section 6.

## 6. Safety

**Allowlist** (`src/policy/allowlist.yaml`, enforced by
`src/policy/guardrails.PolicyEngine`): domains, route prefixes, and permitted
action types, checked before *every* navigation and before every action type,
by both discovery and replay -- not just documented as a convention. It's an
explicit `raise`, not a prompt instruction the model could ignore: I confirmed
this matters during testing, since the system prompt alone ("don't click
Confirm") is advisory, but `PolicyEngine.is_risky()` blocking the actual
`click()` call is not.

**Safe vs. risky.** `risky_name_patterns` + `risky_action_types` in the
allowlist flag any click whose target's accessible name suggests an
irreversible action ("Confirm", "Submit Override", "Delete", ...). Two
independent guardrails compound here: (1) during *discovery*, the harness
blocks the LLM from executing a risky click at all (it gets a policy-blocked
tool result back and is told to call `finish_success` instead) -- the model
never decides, on its own, to perform the irreversible step; (2) the one
genuinely risky step in this capability (finalizing the sub-account) is added
to the artifact by the harness after discovery ends, marked as
`confirmation_step_id`, and replay refuses to execute it unless the *caller*
passes `confirm=True`. A capability with a risky terminal step defaults to
`safe/reversible` behavior (stop and report `pending_confirmation`) and only
performs the irreversible action on explicit, per-invocation opt-in -- this is
the "block, require confirmation, or flag" choice from 3.4, and I chose
"require confirmation" specifically because "block" would make the capability
useless and "flag" (execute-then-warn) is the wrong order of operations for
something irreversible.

**The supervisor-PIN design.** The mock app's permission-denied path can only
be completed with a PIN that is never given to the LLM, never placed in the
allowlist config, and never referenced anywhere the discovery agent can see.
The corresponding `/override` route is explicitly excluded from
`allowed_route_prefixes`. This means the automation is *structurally* incapable
of bypassing a permission denial, not just instructed not to -- the only path
through it is the human escalation channel in Section 5.

**Redaction.** `src/policy/guardrails.redact`/`redact_field` run over every
string written to the evidence log (`src/observability/logger.py` applies this
to every event, recursively) and over sensitive `ParamSpec` values before
they're logged. Patterns cover SSN-shaped strings, PIN-labeled numbers, and
password fields; sensitive field names (`password`, `supervisor_pin`, `ssn`,
...) are fully masked rather than pattern-matched. This is deliberately
conservative -- false positives are cheap, a leaked credential in a log
directory is not.

**Limits.** The redaction patterns are regex heuristics, not a real PII
classifier -- they'd miss anything not shaped like the patterns I anticipated.
The allowlist is a single static YAML file, not scoped per-tenant or
per-capability. And the risky-action classifier is a substring match on
accessible names, which is exactly as fragile as it sounds; a production
version would want this declared per-step in the artifact itself (authored at
review time) rather than inferred from element text at run time.

## 7. Cuts

- **Only one target surface implemented.** Section 3.7 explicitly doesn't
  require a second surface; I designed the perception/locator seam to extend
  to a legacy web app or desktop app (Section 4) but didn't build either.
- **No automatic re-authentication on session expiry.** The `session_expired`
  error handler is declared and classified (`hard_failure`) but not wired to
  actually re-run `login()` and resume -- it surfaces as a failure (or
  escalates) today. Auth is deliberately treated as external to any capability
  (Section 3's harness-level `login()`), so the natural fix is a small
  "re-authenticate and retry the current step once" recovery action alongside
  `retry_once`/`wait_and_retry`/`dismiss_and_continue`.
- **Canonicalization is literal-substitution, not pattern-mining.** Turning
  `10001` into `{member_id}` only works because the harness is told the exact
  example value up front; it doesn't generalize routes
  (`/members/12345` -> `/members/{id}`) the way stretch goal 8 describes. That
  generalization is a natural next step given the `LocatorCandidate`/
  `value_template` machinery is already in place.
- **No confidence scoring or approval gating on artifacts** (stretch goal),
  beyond the per-locator-candidate `confidence` field, which is populated but
  not aggregated or surfaced anywhere.
- **No multi-run stability signal** (stretch goal) -- I verified each scenario
  manually multiple times during development but didn't build automated N-run
  flakiness reporting.
- **The operator console is a CLI**, per the brief's explicit scope note --
  see Section 5 for what's real underneath it.
- **Drift detection across tenants is designed, not implemented** -- see
  Section 4; the raw signal (`strategy_used`, `confidence`) is already logged
  per step, but nothing aggregates it into a drift alert.
- **What I'd build next, in order:** re-auth recovery; a second target surface
  (to prove the perception seam actually generalizes, not just on paper);
  drift detection from the existing per-step locator-confidence signal;
  route/value canonicalization for real cross-tenant reuse.
