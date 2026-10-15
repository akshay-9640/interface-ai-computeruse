"""Public live demo: a web console that runs the EXISTING CLI commands, unchanged.

    gunicorn -w 1 --threads 8 -b 0.0.0.0:$PORT demo.server:app

What a visitor can do (no LLM anywhere, so no API key and no per-visit cost):
  - run 4 replay scenarios of the saved capability artifact (`manage.py replay`)
    against a private Meridian Core instance on 127.0.0.1 inside the container
  - in the escalation scenario, act as the supervisor: the console pipes the exact
    README operator commands into the real `manage.py operator`, which attaches to
    the SAME paused browser over CDP
  - browse the saved discovery run (the real Claude tool-calling session that
    produced the artifact) with the model's own reasoning per step

Guardrails for a public URL: one run at a time, per-IP rate limit, hard timeouts,
mock state reset before every run, an abandoned escalation is handed back as
"abandoned" automatically, and only the last few demo runs are kept on disk.
Single gunicorn worker on purpose: run state lives in this process.
"""
from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from collections import defaultdict, deque
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_from_directory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.escalation.control import ControlChannel  # noqa: E402

PY = sys.executable
MOCK_URL = "http://127.0.0.1:5001"
EVIDENCE = ROOT / "evidence" / "sessions"
ARTIFACT = ROOT / "artifacts_store" / "open_member_subaccount.latest.json"

RUN_TIMEOUT_S = 150          # whole replay, including an escalation pause
OPERATOR_GRACE_S = 90        # escalated and nobody clicks "act as supervisor": hand back as abandoned
RATE_LIMIT = (10, 600)       # runs per IP per 10 minutes
KEEP_DEMO_RUNS = 25

SCENARIOS = {
    "dry_run": {
        "title": "Replay to the confirmation screen",
        "params": {"member_id": "10001", "account_type": "Savings", "initial_deposit": "100.00"},
        "confirm": False,
        "expect": "Stops at the gated, irreversible step: business_outcome / pending_confirmation.",
    },
    "confirm": {
        "title": "Replay with confirm=True",
        "params": {"member_id": "10001", "account_type": "Savings", "initial_deposit": "100.00"},
        "confirm": True,
        "expect": "Executes the risky final click and extracts the new account number.",
    },
    "not_found": {
        "title": "Member does not exist",
        "params": {"member_id": "99999", "account_type": "Savings", "initial_deposit": "100.00"},
        "confirm": False,
        "expect": "The error taxonomy turns the not-found page into a clean business_outcome, not a crash.",
    },
    "escalation": {
        "title": "Restricted member, human escalation",
        "params": {"member_id": "10002", "account_type": "Checking", "initial_deposit": "100.00"},
        "confirm": True,
        "expect": "Hard failure, so replay pauses and hands the live browser to a human supervisor.",
    },
}

# The README's operator path, verbatim. The PIN goes to the browser only; the
# operator CLI redacts it before anything is logged.
OPERATOR_SCRIPT = "type 0 4242\nselect 1 Checking\nclick 2\nresume completed\n"

app = Flask(__name__, template_folder="templates", static_folder="static")
COMMITTED = {p.name for p in EVIDENCE.iterdir()} if EVIDENCE.exists() else set()
LOCK = threading.Lock()
RUNS: dict[str, "Run"] = {}
CURRENT: dict[str, "Run | None"] = {"run": None}
HITS: dict[str, deque] = defaultdict(deque)
MOCK: dict[str, "subprocess.Popen | None"] = {"proc": None}


# ------------------------------------------------------------------ mock app

def _mock_alive() -> bool:
    try:
        with urllib.request.urlopen(f"{MOCK_URL}/login", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_mock() -> None:
    proc = MOCK["proc"]
    if proc is not None and proc.poll() is None and _mock_alive():
        return
    MOCK["proc"] = subprocess.Popen([PY, "-m", "src.mockapp.app"], cwd=ROOT,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        if _mock_alive():
            return
        time.sleep(0.25)
    raise RuntimeError("Meridian Core mock app did not start")


def reset_mock() -> None:
    req = urllib.request.Request(f"{MOCK_URL}/_reset", method="POST")
    urllib.request.urlopen(req, timeout=5).read()


# ------------------------------------------------------------------ runs

class Run:
    def __init__(self, scenario: str, ip: str):
        self.id = uuid.uuid4().hex[:10]
        self.scenario = scenario
        self.ip = ip
        self.started = time.time()
        self.finished: float | None = None
        self.run_dir: Path | None = None
        self.proc: subprocess.Popen | None = None
        self.error: str | None = None
        self.operator_used = False
        self.escalated_at: float | None = None

    def control(self) -> ControlChannel | None:
        if self.run_dir and (self.run_dir / "control.json").exists():
            return ControlChannel(self.run_dir / "control.json")
        return None

    def status(self) -> str:
        if self.error:
            return "error"
        if self.finished:
            return "done"
        ctl = self.control()
        if ctl:
            try:
                if ctl.read().controller == "operator":
                    return "escalated"
            except (json.JSONDecodeError, OSError):
                pass
        return "running"


def _start(run: Run) -> None:
    before = {p.name for p in EVIDENCE.iterdir()} if EVIDENCE.exists() else set()
    sc = SCENARIOS[run.scenario]
    cmd = [PY, "-u", "manage.py", "replay"]
    if sc["confirm"]:
        cmd.append("--confirm")
    for k, v in sc["params"].items():
        cmd += ["--param", f"{k}={v}"]
    run.proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def watch():
        try:
            while run.proc.poll() is None:
                if run.run_dir is None:
                    new = sorted(p for p in EVIDENCE.iterdir() if p.name not in before and p.name.startswith("replay-"))
                    if new:
                        run.run_dir = new[-1]
                if run.status() == "escalated":
                    run.escalated_at = run.escalated_at or time.time()
                    if time.time() - run.escalated_at > OPERATOR_GRACE_S and not run.operator_used:
                        run.control().hand_back("abandoned")  # visitor walked away; release the browser
                if time.time() - run.started > RUN_TIMEOUT_S:
                    run.proc.kill()
                    run.error = f"Timed out after {RUN_TIMEOUT_S}s."
                    break
                time.sleep(0.3)
            if run.run_dir is None:
                new = sorted(p for p in EVIDENCE.iterdir() if p.name not in before and p.name.startswith("replay-"))
                run.run_dir = new[-1] if new else None
        finally:
            run.finished = time.time()
            with LOCK:
                if CURRENT["run"] is run:
                    CURRENT["run"] = None
            _prune()

    threading.Thread(target=watch, daemon=True).start()


def _prune() -> None:
    demo_dirs = sorted((p for p in EVIDENCE.iterdir() if p.name not in COMMITTED), key=lambda p: p.stat().st_mtime)
    for old in demo_dirs[:-KEEP_DEMO_RUNS]:
        shutil.rmtree(old, ignore_errors=True)
    for rid in [rid for rid, r in RUNS.items() if r.run_dir and not r.run_dir.exists()]:
        RUNS.pop(rid, None)


def _client_ip() -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() or request.remote_addr or "?")


def _rate_limited(ip: str) -> bool:
    limit, window = RATE_LIMIT
    q = HITS[ip]
    now = time.time()
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(now)
    return False


# ------------------------------------------------------------------ evidence parsing

def _parse_input(raw):
    if isinstance(raw, dict):
        return raw
    try:
        return ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return {"raw": str(raw)}


def _events(run_dir: Path) -> list[dict]:
    f = run_dir / "run.jsonl"
    if not f.exists():
        return []
    out = []
    for line in f.read_text().splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        ev.pop("screenshot", None)  # absolute paths from the recording machine
        if ev.get("event") == "llm_decision":
            ev["input"] = _parse_input(ev.get("input"))
        out.append(ev)
    return out


def _shots(run_dir: Path) -> list[str]:
    d = run_dir / "screenshots"
    return sorted(p.name for p in d.glob("*.png")) if d.exists() else []


def _json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


# ------------------------------------------------------------------ routes

@app.get("/")
def index():
    return render_template("index.html", scenarios=SCENARIOS)


@app.get("/healthz")
def healthz():
    return {"ok": True, "mock": _mock_alive()}


@app.post("/api/run")
def api_run():
    scenario = (request.get_json(silent=True) or {}).get("scenario")
    if scenario not in SCENARIOS:
        abort(400, "unknown scenario")
    ip = _client_ip()
    with LOCK:
        cur = CURRENT["run"]
        if cur is not None:
            return jsonify(error="busy", message="Another visitor's run is in progress. Try again in a few seconds.",
                           current=cur.id), 409
        if _rate_limited(ip):
            return jsonify(error="rate_limited", message="That's a lot of runs. Take a breather and try again in a few minutes."), 429
        run = Run(scenario, ip)
        CURRENT["run"] = run
        RUNS[run.id] = run
    try:
        ensure_mock()
        reset_mock()
        _start(run)
    except Exception as exc:  # never leave the lock held
        run.error, run.finished = str(exc), time.time()
        with LOCK:
            CURRENT["run"] = None
        return jsonify(error="start_failed", message=str(exc)), 500
    return jsonify(id=run.id)


@app.get("/api/run/<rid>")
def api_run_state(rid):
    run = RUNS.get(rid) or abort(404)
    out = {"id": run.id, "scenario": run.scenario, "status": run.status(), "error": run.error,
           "elapsed": round((run.finished or time.time()) - run.started, 1), "operator_used": run.operator_used,
           "events": [], "shots": [], "result": None, "control": None}
    if run.run_dir and run.run_dir.exists():
        out["events"] = _events(run.run_dir)
        out["shots"] = _shots(run.run_dir)
        out["result"] = _json(run.run_dir / "result.json")
        ctl = _json(run.run_dir / "control.json")
        if ctl:
            out["control"] = {k: ctl.get(k) for k in ("controller", "reason", "human_actions", "resolution")}
    return jsonify(out)


@app.post("/api/run/<rid>/operator")
def api_operator(rid):
    run = RUNS.get(rid) or abort(404)
    if run.status() != "escalated" or run.operator_used:
        return jsonify(error="not_escalated", message="This run is not waiting for an operator."), 409
    run.operator_used = True
    proc = subprocess.run([PY, "manage.py", "operator", "--run-dir", str(run.run_dir)], cwd=ROOT,
                          input=OPERATOR_SCRIPT, capture_output=True, text=True, timeout=60)
    # The operator console's own screen output is NOT returned: it shows field values.
    # The audit trail that matters is control.json's human_actions, which is redacted.
    ctl = _json(run.run_dir / "control.json") or {}
    return jsonify(ok=proc.returncode == 0, human_actions=ctl.get("human_actions", []))


@app.get("/api/run/<rid>/shots/<name>")
def api_shot(rid, name):
    run = RUNS.get(rid) or abort(404)
    if not run.run_dir:
        abort(404)
    return send_from_directory(run.run_dir / "screenshots", name, max_age=3600)


@app.get("/api/discovery")
def api_discovery():
    runs = sorted(n for n in COMMITTED if n.startswith("discovery-"))
    if not runs:
        return jsonify(None)
    d = EVIDENCE / runs[-1]
    return jsonify(id=runs[-1], events=_events(d), shots=_shots(d), result=_json(d / "result.json"))


@app.get("/api/evidence/<run_id>/shots/<name>")
def api_evidence_shot(run_id, name):
    if run_id not in COMMITTED:  # only the committed, reviewed evidence is browsable this way
        abort(404)
    return send_from_directory(EVIDENCE / run_id / "screenshots", name, max_age=86400)


@app.get("/api/artifact")
def api_artifact():
    return jsonify(_json(ARTIFACT))


# Warm the mock app at boot so the first visitor does not wait for it.
threading.Thread(target=ensure_mock, daemon=True).start()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8080, debug=False, threaded=True)
