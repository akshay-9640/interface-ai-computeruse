"""Record real runs of every scenario into site/ for the static (Vercel) demo.

    gunicorn -w 1 --threads 8 -b 127.0.0.1:8080 demo.server:app   # in one terminal
    python -m demo.record                                          # in another

Each scenario is executed for real through the live demo server (which runs the
unchanged `manage.py replay` / `manage.py operator`), then its event log, screenshots,
result and control-state are copied into site/data/. Nothing is edited or synthesized:
the static page only plays these files back on their recorded timeline.
"""
from __future__ import annotations

import json
import shutil
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from demo.server import ARTIFACT, COMMITTED, EVIDENCE, SCENARIOS, _events, _json, _shots  # noqa: E402

API = "http://127.0.0.1:8080"
SITE = ROOT / "site"
DATA = SITE / "data"


def call(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(f"{API}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read())


def wait_for(rid: str, statuses: set, timeout: float = 120) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        s = call("GET", f"/api/run/{rid}")
        if s["status"] in statuses:
            return s
        time.sleep(0.5)
    raise TimeoutError(f"run {rid} never reached {statuses}")


def shot_ts(name: str) -> float:
    return int(name.split("_", 1)[0]) / 1000  # RunLogger names screenshots <epoch ms>_<tag>.png


def copy_run(run_dir: Path, dest: Path) -> list:
    (dest / "shots").mkdir(parents=True, exist_ok=True)
    shots = []
    for n in _shots(run_dir):
        shutil.copy2(run_dir / "screenshots" / n, dest / "shots" / n)
        shots.append({"name": n, "ts": shot_ts(n)})
    return shots


def record(key: str) -> dict:
    print(f"recording {key} ...", flush=True)
    rid = call("POST", "/api/run", {"scenario": key})["id"]
    if key == "escalation":
        wait_for(rid, {"escalated"})
        time.sleep(1.0)
        call("POST", f"/api/run/{rid}/operator")
    final = wait_for(rid, {"done", "error"})
    run_dir = max((p for p in EVIDENCE.iterdir() if p.name.startswith("replay-")), key=lambda p: p.stat().st_mtime)
    dest = DATA / key
    shutil.rmtree(dest, ignore_errors=True)
    shots = copy_run(run_dir, dest)
    ctl = _json(run_dir / "control.json")
    rec = {
        "scenario": key,
        "title": SCENARIOS[key]["title"],
        "events": _events(run_dir),
        "shots": shots,
        "result": final["result"],
        "control": {k: ctl.get(k) for k in ("reason", "human_actions", "resolution")} if ctl else None,
        "elapsed": final["elapsed"],
        "source_run": run_dir.name,
    }
    (dest / "run.json").write_text(json.dumps(rec, indent=1))
    shutil.rmtree(run_dir, ignore_errors=True)  # the copy in site/ is the record; keep evidence/ clean
    print(f"  {rec['result']['status']} {rec['result'].get('label') or ''} in {rec['elapsed']}s, {len(shots)} screenshots")
    return rec


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    for key in SCENARIOS:
        record(key)

    # Discovery: the committed evidence of the real Claude run that produced the artifact.
    disc = sorted(n for n in COMMITTED if n.startswith("discovery-"))[-1]
    dest = DATA / "discovery"
    shutil.rmtree(dest, ignore_errors=True)
    shots = copy_run(EVIDENCE / disc, dest)
    (dest / "run.json").write_text(json.dumps(
        {"id": disc, "events": _events(EVIDENCE / disc), "shots": shots}, indent=1))

    shutil.copy2(ARTIFACT, DATA / "artifact.json")
    meta = {
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "scenarios": [{"key": k, "title": v["title"], "expect": v["expect"], "params": v["params"], "confirm": v["confirm"]}
                      for k, v in SCENARIOS.items()],
    }
    (DATA / "scenarios.json").write_text(json.dumps(meta, indent=1))
    print(f"wrote {DATA}")


if __name__ == "__main__":
    main()
