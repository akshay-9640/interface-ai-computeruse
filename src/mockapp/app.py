"""
Meridian Core -- a fictional legacy credit-union servicing console.

This is the "proxy target" for the assignment: a deliberately old-school,
server-rendered, table-heavy internal app with no test IDs and no API,
standing in for the kind of back-office screen the real system has to
automate. It intentionally exercises the runtime conditions Section 1/3.3
of the brief call out:

  - "record not found"          -> search for a member ID that doesn't exist
  - validation error            -> submit the new-sub-account form with a bad deposit amount
  - permission denial           -> member 10002 is "restricted", needs supervisor override
  - unexpected confirmation step -> opening a sub-account requires a second, explicit
                                    "confirm" screen (an interstitial, not a native JS dialog,
                                    so it's automatable without fighting browser dialogs)
  - session/timeout expiry      -> the session cookie expires after MOCKAPP_SESSION_TIMEOUT_SECONDS
  - transient slowness/error    -> member 10003 ("flaky") fails the first confirm attempt
                                    per session with a "system busy" error, then succeeds on retry

No real PII, no real credentials, no external network calls. All data is in-memory
and resets when the process restarts.
"""
from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field

from flask import Flask, request, redirect, url_for, session, render_template, flash

app = Flask(__name__)
app.secret_key = os.environ.get("MOCKAPP_SECRET_KEY", "dev-only-not-a-real-secret")
app.config["TEMPLATES_AUTO_RELOAD"] = True

SESSION_TIMEOUT_SECONDS = int(os.environ.get("MOCKAPP_SESSION_TIMEOUT_SECONDS", "900"))
OPERATOR_USERNAME = "operator1"
OPERATOR_PASSWORD = "letmein123"  # noqa: this is a mock app, not a real credential
SUPERVISOR_OVERRIDE_PIN = "4242"  # intentionally NOT given to the automation agent


@dataclass
class SubAccount:
    account_number: str
    account_type: str
    balance: float


@dataclass
class Member:
    member_id: str
    first_name: str
    last_name: str
    status: str  # "normal" | "restricted"
    accounts: list[SubAccount] = field(default_factory=list)


def _seed_members() -> dict[str, Member]:
    return {
        "10001": Member(
            "10001", "Alice", "Rivera", "normal",
            [SubAccount("CHK-10001-01", "Checking", 2340.12),
             SubAccount("SAV-10001-01", "Savings", 8902.55)],
        ),
        "10002": Member(
            "10002", "Marcus", "Chen", "restricted",
            [SubAccount("CHK-10002-01", "Checking", 512.00)],
        ),
        "10003": Member(
            "10003", "Priya", "Natarajan", "normal",
            [SubAccount("SAV-10003-01", "Savings", 150.00)],
        ),
    }


MEMBERS: dict[str, Member] = _seed_members()
# per-session counter so member 10003's confirm step fails exactly once per session
_FLAKY_ATTEMPTS: dict[str, int] = {}


def _require_login():
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    login_time = session.get("login_time", 0)
    if time.time() - login_time > SESSION_TIMEOUT_SECONDS:
        session.clear()
        return redirect(url_for("login", expired=1))
    session["login_time"] = time.time()  # sliding expiry
    return None


@app.route("/", methods=["GET"])
def index():
    guard = _require_login()
    if guard:
        return guard
    return redirect(url_for("search"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    expired = request.args.get("expired")
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == OPERATOR_USERNAME and password == OPERATOR_PASSWORD:
            session.clear()
            session["logged_in"] = True
            session["login_time"] = time.time()
            session["sid"] = str(uuid.uuid4())
            return redirect(url_for("search"))
        error = "Invalid username or password."
    return render_template("login.html", error=error, expired=expired)


@app.route("/logout", methods=["GET"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/search", methods=["GET"])
def search():
    guard = _require_login()
    if guard:
        return guard
    query = request.args.get("q", "").strip()
    results = []
    not_found = False
    if query:
        member = MEMBERS.get(query)
        if member:
            results = [member]
        else:
            not_found = True
    return render_template("search.html", query=query, results=results, not_found=not_found)


@app.route("/members/<member_id>", methods=["GET"])
def member_detail(member_id):
    guard = _require_login()
    if guard:
        return guard
    member = MEMBERS.get(member_id)
    if not member:
        return render_template("not_found.html", member_id=member_id), 404
    return render_template("member_detail.html", member=member)


@app.route("/members/<member_id>/new-subaccount", methods=["GET", "POST"])
def new_subaccount(member_id):
    guard = _require_login()
    if guard:
        return guard
    member = MEMBERS.get(member_id)
    if not member:
        return render_template("not_found.html", member_id=member_id), 404

    if member.status == "restricted":
        return render_template("permission_denied.html", member=member, override_error=None)

    validation_error = None
    if request.method == "POST":
        account_type = request.form.get("account_type", "")
        deposit_raw = request.form.get("initial_deposit", "")
        try:
            deposit = float(deposit_raw)
        except ValueError:
            deposit = None

        if account_type not in ("Checking", "Savings"):
            validation_error = "Please choose an account type."
        elif deposit is None or deposit < 25.00:
            validation_error = "Initial deposit must be a number of at least $25.00."

        if not validation_error:
            return render_template(
                "confirm_subaccount.html",
                member=member, account_type=account_type, deposit=deposit,
            )

    return render_template("new_subaccount.html", member=member, error=validation_error)


@app.route("/members/<member_id>/new-subaccount/confirm", methods=["POST"])
def confirm_subaccount(member_id):
    guard = _require_login()
    if guard:
        return guard
    member = MEMBERS.get(member_id)
    if not member:
        return render_template("not_found.html", member_id=member_id), 404

    account_type = request.form.get("account_type", "")
    try:
        deposit = float(request.form.get("deposit", "0"))
    except ValueError:
        deposit = 0.0

    sid = session.get("sid", "anon")
    if member_id == "10003":
        attempts = _FLAKY_ATTEMPTS.get(sid, 0)
        _FLAKY_ATTEMPTS[sid] = attempts + 1
        if attempts == 0:
            return render_template(
                "system_busy.html", member=member, account_type=account_type, deposit=deposit
            ), 503

    prefix = "CHK" if account_type == "Checking" else "SAV"
    account_number = f"{prefix}-{member_id}-{len(member.accounts) + 1:02d}"
    member.accounts.append(SubAccount(account_number, account_type, deposit))
    return render_template(
        "success.html", member=member, account_number=account_number,
        account_type=account_type, deposit=deposit,
    )


@app.route("/members/<member_id>/new-subaccount/override", methods=["POST"])
def override_subaccount(member_id):
    """Supervisor-override path. Deliberately NOT reachable by the automation's
    policy allowlist / not exposed to the LLM agent -- see policy/allowlist.yaml
    and REPORT.md 'Safety'. Only a human operator, who knows the PIN out of band,
    is expected to use this during an escalation handoff."""
    guard = _require_login()
    if guard:
        return guard
    member = MEMBERS.get(member_id)
    if not member:
        return render_template("not_found.html", member_id=member_id), 404

    pin = request.form.get("supervisor_pin", "")
    if pin != SUPERVISOR_OVERRIDE_PIN:
        return render_template(
            "permission_denied.html", member=member, override_error="Incorrect supervisor PIN."
        )

    account_type = request.form.get("account_type", "Checking")
    deposit = 25.00
    prefix = "CHK" if account_type == "Checking" else "SAV"
    account_number = f"{prefix}-{member_id}-{len(member.accounts) + 1:02d}"
    member.accounts.append(SubAccount(account_number, account_type, deposit))
    return render_template(
        "success.html", member=member, account_number=account_number,
        account_type=account_type, deposit=deposit, via_override=True,
    )


@app.route("/_reset", methods=["POST"])
def reset_state():
    """Test-only helper so replay demos start from a clean slate. Not part of the
    automated surface (not in the allowlist's permitted routes for the agent)."""
    global MEMBERS, _FLAKY_ATTEMPTS
    MEMBERS = _seed_members()
    _FLAKY_ATTEMPTS = {}
    return {"ok": True}


if __name__ == "__main__":
    port = int(os.environ.get("MOCKAPP_PORT", "5001"))
    app.run(host="127.0.0.1", port=port, debug=False)
