#!/usr/bin/env python3
"""Manual, real-HTTP verification of A06 stage 3 (nightly close orchestration).

*** NOT an automated test suite. *** Same convention as
scripts/verify_a07_payouts.py: real HTTP calls against a running `uvicorn`
and a real Postgres, hand-worked expectations from
scripts/dev_fixtures_a06_orchestration.py (NOT B01), one PASS/FAIL line per
check, non-zero exit on any failure. Every scenario uses its own festival
id, so runs are independent.

This script also starts and kills real WORKER PROCESSES
(`python -m worker.orchestration`), because the orchestration runs in the
worker, not in the API:

  S1  Happy path + isolated holds: five vendors; three complete (incl. a
      zero-payout vendor-retained-cash vendor); Gamma held by a 15%/10%
      contract conflict case at TERMS_RESOLVED; Epsilon held for a
      finance-lead approval above the $5,000/day automatic limit. Resume
      refused while the case is open; resolve + approve through the
      existing endpoints; resume (idempotent key) -> run COMPLETED with
      hand-checked numbers, balanced books, passing close checks and a
      complete flight recorder.
  S2  Worker crash mid-node: worker 1 dies (os._exit, no cleanup) right
      after Alpha's release committed and before the checkpoint; worker 2
      dies right after Beta's rail submission was accepted and before the
      checkpoint; worker 3 picks the run up after the lease expires ->
      completed with exactly one payout / rail record / outbox intent /
      payout journal / settlement / commission journal per vendor.
  S3  External SIGKILL: the worker hangs right after Alpha's commission
      posting committed; the script `kill -9`s it; a new worker resumes ->
      no duplicate journal entries.
  S4  LLM timeout and S5 invalid structured output (local stub at
      ANTHROPIC_BASE_URL, no spend): visible, retryable failure rows and
      events; the held vendor stays held (not failed), others complete.
  S6  (only with VERIFY_LIVE_CLAUDE=1 and ANTHROPIC_API_KEY set) one real
      Claude explanation with code-verified citations, under a $0.10 run
      budget; reports the recorded spend.

Usage:
    1. Postgres migrated to head; scripts/seed_admin.py run.
    2. API: uvicorn app.main:app on API_BASE_URL (app dir services/api).
    3. DATABASE_URL=<owner url, fixtures/assertions>
       WORKER_DATABASE_URL=<encore_app url, for the spawned workers>
       API_BASE_URL=http://127.0.0.1:8031 ADMIN_EMAIL=... ADMIN_PASSWORD=...
       AUTH_JWT_SECRET=... DOCUMENT_STORAGE_ROOT=...
       uv run python scripts/verify_a06_orchestration.py
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "services", "api"))
sys.path.insert(0, os.path.dirname(__file__))

from app.accounting import unbalanced_entry_count  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app.db import get_session_factory, make_engine  # noqa: E402
from app.models import User  # noqa: E402
from app.models_ledger import BusinessEvent, JournalEntry, OutboxIntent, Payout  # noqa: E402
from app.models_orchestration import (  # noqa: E402
    AgentCaseExplanation,
    OrchestrationRun,
    OrchestrationStep,
)
from app.models_payout_dispatch import RailSimulatorRecord  # noqa: E402
from app.models_reconciliation import MatchGroup  # noqa: E402
from app.models_settlement import Settlement  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

import dev_fixtures_a06_orchestration as fx  # noqa: E402

API_BASE_URL = os.environ.get("API_BASE_URL", "http://127.0.0.1:8031")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "LocalDevPassw0rd!")
WORKER_DATABASE_URL = os.environ.get("WORKER_DATABASE_URL", os.environ.get("DATABASE_URL", ""))
LOG_DIR = os.environ.get("VERIFY_LOG_DIR", "/tmp/encore-graph-verify")
LEASE_SECONDS = 4
RUN = uuid.uuid4().hex[:8]
D1 = fx.D1

_failures: list[str] = []
_passes: list[str] = []
_Session = None


def check(label: str, condition: bool, detail: object = "") -> None:
    if condition:
        _passes.append(label)
        print(f"PASS  {label}")
    else:
        _failures.append(f"{label} -- {detail}")
        print(f"FAIL  {label} -- {detail}")


def db():
    return _Session()


def festival(name: str) -> str:
    return f"gcmf-2026-a06verify-{RUN}-{name}"


def seed(name: str) -> tuple[str, dict]:
    fest = festival(name)
    with db() as s:
        ids = fx.seed(s, fest, f"{RUN}-{name}")
    return fest, ids


def count(model, *where) -> int:
    with db() as s:
        return s.execute(select(func.count()).select_from(model).where(*where)).scalar_one()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def login(email: str, password: str) -> dict:
    r = httpx.post(f"{API_BASE_URL}/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, f"login {email}: {r.status_code} {r.text}"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


class Api:
    def __init__(self, headers: dict) -> None:
        self.c = httpx.Client(base_url=API_BASE_URL, headers=headers, timeout=60)

    def post(self, path: str, body: dict | None = None, **headers) -> httpx.Response:
        return self.c.post(path, json=body or {}, headers=headers)

    def get(self, path: str, **params) -> httpx.Response:
        return self.c.get(path, params=params)

    def start(self, fest: str, key: str | None = None, **kw) -> httpx.Response:
        body = {"festival_id": fest, "business_date": D1.isoformat(), **kw}
        return self.post(
            "/api/v1/orchestration/runs", body, **({"Idempotency-Key": key} if key else {})
        )

    def run(self, run_id: str) -> dict:
        return self.get(f"/api/v1/orchestration/runs/{run_id}").json()

    def vendor(self, run_id: str, vendor_id: str) -> dict:
        return self.get(f"/api/v1/orchestration/runs/{run_id}/vendors/{vendor_id}").json()

    def resume(self, run_id: str, vendor_id: str, key: str | None = None) -> httpx.Response:
        return self.post(
            f"/api/v1/orchestration/runs/{run_id}/vendors/{vendor_id}/resume",
            {"note": "verified the decision"},
            **({"Idempotency-Key": key} if key else {}),
        )


def wait_run(api: Api, run_id: str, statuses: set[str], timeout: float = 90) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = api.run(run_id)
        if last.get("status") in statuses and not last.get("needs_work"):
            return last
        time.sleep(0.5)
    return last


# ---------------------------------------------------------------------------
# Worker processes
# ---------------------------------------------------------------------------


class Worker:
    def __init__(self, name: str, extra_env: dict | None = None) -> None:
        os.makedirs(LOG_DIR, exist_ok=True)
        env = dict(os.environ)
        # The key reaches a worker only when a scenario passes it explicitly.
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_BASE_URL", None)
        env.update(
            {
                "DATABASE_URL": WORKER_DATABASE_URL,
                "WORKER_ID": f"verify-{RUN}-{name}",
                "ORCHESTRATION_LEASE_SECONDS": str(LEASE_SECONDS),
                "ORCHESTRATION_POLL_SECONDS": "0.5",
                "PYTHONPATH": os.pathsep.join(
                    [
                        os.path.join(ROOT, "services", "api"),
                        os.path.join(ROOT, "services", "worker"),
                        os.path.join(ROOT, "packages", "domain"),
                        os.path.join(ROOT, "packages", "contracts"),
                    ]
                ),
                **(extra_env or {}),
            }
        )
        self.name = name
        self.log_path = os.path.join(LOG_DIR, f"worker-{RUN}-{name}.log")
        self.log = open(self.log_path, "w")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "worker.orchestration"],
            cwd=ROOT,
            env=env,
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )

    def wait_exit(self, timeout: float = 60) -> int | None:
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def kill9(self) -> int:
        self.proc.send_signal(signal.SIGKILL)
        return self.proc.wait(timeout=10)

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.log.close()


# ---------------------------------------------------------------------------
# Local Anthropic stub (S4/S5): no network, no spend
# ---------------------------------------------------------------------------

_stub_mode = {"mode": "timeout", "calls": 0}


class _Stub(BaseHTTPRequestHandler):
    def log_message(self, *a) -> None:  # noqa: D401
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", 0))
        self.rfile.read(length)
        _stub_mode["calls"] += 1
        if _stub_mode["mode"] == "timeout":
            time.sleep(4)
        body = {
            "id": "msg_stub",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-5-5",
            "content": [{"type": "text", "text": "Sure! The vendor is held because... (not JSON)"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 12},
        }
        data = json.dumps(body).encode()
        try:
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


def start_stub() -> str:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}"


# ---------------------------------------------------------------------------
# Shared assertions
# ---------------------------------------------------------------------------


def assert_no_duplicates(tag: str, fest: str, ids: dict) -> None:
    dupes = {}
    for name, vid in ids.items():
        settlements = count(Settlement, Settlement.vendor_id == vid)
        payouts = count(Payout, Payout.vendor_id == vid)
        rail = count(RailSimulatorRecord, RailSimulatorRecord.vendor_id == vid)
        with db() as s:
            payout = s.execute(select(Payout).where(Payout.vendor_id == vid)).scalar_one_or_none()
            pkey = payout.idempotency_key if payout else None
            pid = payout.id if payout else None
        outbox = count(OutboxIntent, OutboxIntent.idempotency_key == pkey) if pkey else 0
        pay_journal = (
            count(JournalEntry, JournalEntry.entry_key.like(f"PAYOUT:{pid}%")) if pid else 0
        )
        commission = count(JournalEntry, JournalEntry.entry_key.like(f"COMMISSION:{vid}:%"))
        groups = count(
            MatchGroup, MatchGroup.vendor_id == vid, MatchGroup.match_type == "order_capture"
        )
        expected_payout = 1 if fx.EXPECTED[name]["payout"] else 0
        got = (settlements, payouts, rail, outbox, pay_journal, commission)
        want = (1, expected_payout, expected_payout, expected_payout, expected_payout, 1)
        if got != want:
            dupes[name] = {"got": got, "want": want, "order_groups": groups}
    check(
        f"{tag}: exactly one settlement/payout/rail record/outbox intent/payout journal/"
        "commission journal per vendor",
        not dupes,
        dupes,
    )
    with db() as s:
        bank_groups = s.execute(
            select(func.count())
            .select_from(MatchGroup)
            .where(MatchGroup.festival_id == fest, MatchGroup.match_type == "bank_deposit")
        ).scalar_one()
    check(f"{tag}: one bank-deposit match group", bank_groups == 1, bank_groups)
    with db() as s:
        unbalanced = unbalanced_entry_count(s, fest)
    check(f"{tag}: every journal entry balances", unbalanced == 0, unbalanced)


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def s1_happy_path_and_holds(admin: Api, nobody: Api) -> None:
    fest, ids = seed("s1")
    r = nobody.start(fest)
    check("S1: start without run_reconciliation -> 403", r.status_code == 403, r.status_code)
    r = admin.start(fest, explain_cases=False, key=f"s1-start-{RUN}")
    check(
        "S1: start -> 202 with status_url",
        r.status_code == 202 and "status_url" in r.json(),
        r.text,
    )
    run_id = r.json()["run_id"]
    r2 = admin.start(fest, explain_cases=False, key=f"s1-start-{RUN}")
    check(
        "S1: same Idempotency-Key + payload -> 200, same run",
        r2.status_code == 200 and r2.json()["run_id"] == run_id,
        r2.text,
    )
    r3 = admin.start(fest, max_concurrency=1, key=f"s1-start-{RUN}")
    check("S1: same key, different payload -> 409", r3.status_code == 409, r3.status_code)
    r4 = admin.start(fest)
    check(
        "S1: second run for the same festival/date -> 409 naming the run",
        r4.status_code == 409 and r4.json()["detail"]["run_id"] == run_id,
        r4.text,
    )

    w = Worker("s1")
    try:
        run = wait_run(admin, run_id, {"WAITING_REVIEW", "COMPLETED"})
        check(
            "S1: run -> WAITING_REVIEW (two vendors held)", run["status"] == "WAITING_REVIEW", run
        )
        check(
            "S1: vendor counts 3 COMPLETED / 2 HELD",
            run.get("vendor_counts") == {"COMPLETED": 3, "HELD": 2},
            run.get("vendor_counts"),
        )
        with db() as s:
            got = {
                n: (st.assessed_fee_cents, st.payout_cents)
                for n in ("Alpha", "Beta", "Delta")
                for st in [
                    s.execute(select(Settlement).where(Settlement.vendor_id == ids[n])).scalar_one()
                ]
            }
        want = {n: (fx.EXPECTED[n]["fee"], fx.EXPECTED[n]["payout"]) for n in got}
        check("S1: Alpha/Beta/Delta fee+payout match hand-worked figures", got == want, got)
        for n in ("Alpha", "Beta"):
            v = admin.vendor(run_id, ids[n])
            p = admin.get(f"/api/v1/payouts/{v['payout_id']}").json()
            check(
                f"S1: {n} REPORTED and its simulated payout CONFIRMED for the right amount",
                v["milestone"] == "REPORTED"
                and p["state"] == "CONFIRMED"
                and p["amount_cents"] == fx.EXPECTED[n]["payout"],
                (v["milestone"], p.get("state"), p.get("amount_cents")),
            )
        d = admin.vendor(run_id, ids["Delta"])
        check(
            "S1: Delta (vendor-retained cash, payout 0) skipped payout, posted and reported",
            d["status"] == "COMPLETED" and d["payout_id"] is None and d["statement_id"],
            d,
        )

        g = admin.vendor(run_id, ids["Gamma"])
        case_id = (g.get("hold_reasons") or [{}])[0].get("case_id")
        check(
            "S1: Gamma HELD at TERMS_RESOLVED by an open contract-conflict case",
            g["status"] == "HELD" and g["held_at"] == "TERMS_RESOLVED" and case_id,
            g,
        )
        check(
            "S1: Gamma's thread is paused at a LangGraph interrupt (checkpoint)",
            (g.get("checkpoint") or {}).get("waiting_for_human") is True,
            g.get("checkpoint"),
        )
        check(
            "S1: Gamma case impact = $4.00 (15% vs 10% of $80.00)",
            g["cases"][0]["financial_impact_cents"] == fx.EXPECTED["Gamma"]["case_impact"],
            g["cases"],
        )
        check(
            "S1: Gamma not settled while held",
            count(Settlement, Settlement.vendor_id == ids["Gamma"]) == 0,
        )
        stm = admin.get("/api/v1/statements", vendor_id=ids["Gamma"]).json()["items"]
        check(
            "S1: held vendor still gets a HELD statement (statement does not wait for the human)",
            len(stm) >= 1 and stm[-1]["status"] == "HELD",
            [x["status"] for x in stm],
        )
        e = admin.vendor(run_id, ids["Epsilon"])
        reason = (e.get("hold_reasons") or [{}])[0]
        check(
            "S1: Epsilon HELD for finance_lead approval ($5,100 > $5,000/day)",
            e["status"] == "HELD" and reason.get("required_capability") == "finance_lead",
            e,
        )
        ep = admin.get(f"/api/v1/payouts/{e['payout_id']}").json()
        check("S1: automatic gate did not approve Epsilon (DRAFT)", ep["state"] == "DRAFT", ep)

        r = admin.resume(run_id, ids["Gamma"])
        check(
            "S1: resume refused while the case is OPEN (422 hold_not_cleared)",
            r.status_code == 422 and r.json()["detail"]["code"] == "hold_not_cleared",
            r.text,
        )
        r = admin.resume(run_id, ids["Alpha"])
        check("S1: resume of a completed vendor -> 422", r.status_code == 422, r.status_code)

        r = admin.post(
            f"/api/v1/cases/{case_id}/resolve",
            {"decision": "edit", "commission_bps": 1000, "resolution_note": "10% per producer"},
        )
        check(
            "S1: human resolves the case (10%) via existing endpoint", r.status_code == 200, r.text
        )
        r = admin.post(f"/api/v1/payouts/{e['payout_id']}/approve", {"note": "finance lead"})
        check(
            "S1: finance lead approves Epsilon via existing endpoint",
            r.status_code == 200 and r.json()["state"] == "APPROVED",
            r.text,
        )
        key = f"s1-resume-gamma-{RUN}"
        r1 = admin.resume(run_id, ids["Gamma"], key=key)
        r2 = admin.resume(run_id, ids["Gamma"], key=key)
        check(
            "S1: resume -> 202; same key replay -> 200 same command",
            r1.status_code == 202
            and r2.status_code == 200
            and r1.json()["command_id"] == r2.json()["command_id"],
            (r1.text, r2.text),
        )
        r = admin.resume(run_id, ids["Epsilon"])
        check("S1: resume Epsilon -> 202", r.status_code == 202, r.text)

        run = wait_run(admin, run_id, {"COMPLETED"})
        check("S1: run COMPLETED after both resumes", run["status"] == "COMPLETED", run)
        with db() as s:
            gs = s.execute(
                select(Settlement).where(Settlement.vendor_id == ids["Gamma"])
            ).scalar_one()
        check(
            "S1: Gamma settled at the human-chosen 10%: fee $8.00, payout $72.00",
            (gs.commission_bps_used, gs.assessed_fee_cents, gs.payout_cents) == (1000, 800, 7200),
            (gs.commission_bps_used, gs.assessed_fee_cents, gs.payout_cents),
        )
        for n in ("Gamma", "Epsilon"):
            v = admin.vendor(run_id, ids[n])
            p = admin.get(f"/api/v1/payouts/{v['payout_id']}").json()
            check(
                f"S1: {n} paid (simulated) after resume",
                p["state"] == "CONFIRMED" and p["amount_cents"] == fx.EXPECTED[n]["payout"],
                p,
            )
        pool = admin.get(f"/api/v1/cash-pool/{fest}").json()
        paid = sum(fx.EXPECTED[n]["payout"] for n in fx.EXPECTED)
        check(
            "S1: cash pool: reserved = paid total, available + reserved = funded",
            pool["reserved_cents"] == paid
            and pool["available_cents"] + pool["reserved_cents"] == fx.CASH_POOL_CENTS,
            pool,
        )
        tb = admin.get("/api/v1/journal/trial-balance", festival_id=fest).json()
        check("S1: trial balance balanced", tb.get("balanced") is True, tb.get("balanced"))
        cc = admin.get(
            "/api/v1/close/checks", festival_id=fest, business_date=D1.isoformat()
        ).json()
        check("S1: daily-close hard checks all pass", all(cc["hard"].values()), cc["hard"])
        assert_no_duplicates("S1", fest, ids)

        events = admin.get(f"/api/v1/orchestration/runs/{run_id}/events").json()["items"]
        types = {e["event_type"] for e in events}
        needed = {
            "orchestration.run_queued",
            "orchestration.run_claimed",
            "orchestration.node_started",
            "orchestration.node_completed",
            "orchestration.vendor_held",
            "orchestration.resume_requested",
            "orchestration.vendor_resumed",
            "orchestration.run_status_changed",
            "settlement.calculated",
            "case.opened",
            "case.resolved",
        }
        check("S1: flight recorder has the run's lifecycle", needed <= types, needed - types)
        orch_events = [e for e in events if e["event_type"].startswith("orchestration.node")]
        check(
            "S1: node events carry graph + langgraph versions",
            orch_events
            and all(
                e["versions"]
                and e["versions"].get("graph_version")
                and e["versions"].get("langgraph")
                for e in orch_events
            ),
            orch_events[:1],
        )
        resumed = [e for e in events if e["event_type"] == "orchestration.vendor_resumed"]
        check(
            "S1: resume events name the human actor and command",
            len(resumed) == 2 and all(e["actor_id"] and e["command_id"] for e in resumed),
            resumed,
        )
        blob = json.dumps(events)
        check(
            "S1: no secrets/prompts in the recorder",
            "sk-ant" not in blob and "SYSTEM_PROMPT" not in blob and "<case>" not in blob,
        )
    finally:
        w.stop()


def s2_crash_mid_node(admin: Api) -> None:
    fest, ids = seed("s2")
    r = admin.start(fest, max_concurrency=1)
    run_id = r.json()["run_id"]
    marker = os.path.join(LOG_DIR, f"crash-{RUN}-s2")
    w1 = Worker(
        "s2-dies-after-release",
        {
            "ORCHESTRATION_CRASH_POINTS": f"pay:after_release@{ids['Alpha']}",
            "ORCHESTRATION_CRASH_MARKER": marker,
        },
    )
    code = w1.wait_exit(60)
    w1.stop()
    check("S2: worker 1 died at Alpha pay:after_release (exit 137)", code == 137, code)
    with db() as s:
        p = s.execute(select(Payout).where(Payout.vendor_id == ids["Alpha"])).scalar_one()
        state1 = p.state
    check(
        "S2: Alpha's release committed (DISPATCH_PENDING), no rail submission yet",
        state1 == "DISPATCH_PENDING"
        and count(RailSimulatorRecord, RailSimulatorRecord.vendor_id == ids["Alpha"]) == 0,
        state1,
    )
    w2 = Worker(
        "s2-dies-after-dispatch",
        {"ORCHESTRATION_CRASH_POINTS": f"pay:after_dispatch@{ids['Beta']}"},
    )
    code = w2.wait_exit(60)
    w2.stop()
    check(
        "S2: worker 2 reclaimed after lease expiry and died at Beta pay:after_dispatch",
        code == 137,
        code,
    )
    check(
        "S2: Beta accepted by the rail once before the crash",
        count(RailSimulatorRecord, RailSimulatorRecord.vendor_id == ids["Beta"]) == 1,
    )
    with db() as s:
        claims = s.get(OrchestrationRun, run_id).claim_count
    w3 = Worker("s2-recovers")
    try:
        run = wait_run(admin, run_id, {"WAITING_REVIEW", "COMPLETED"})
        check(
            "S2: worker 3 finished the run (WAITING_REVIEW: Gamma/Epsilon held)",
            run["status"] == "WAITING_REVIEW",
            run,
        )
        check(
            "S2: run was claimed three times (lease reclaim)",
            run["claim_count"] == claims + 1 == 3,
            run["claim_count"],
        )
        for n in ("Alpha", "Beta", "Delta"):
            v = admin.vendor(run_id, ids[n])
            check(f"S2: {n} COMPLETED after recovery", v["status"] == "COMPLETED", v["status"])
        assert_no_duplicates("S2", fest, {n: ids[n] for n in ("Alpha", "Beta", "Delta")})
        with db() as s:
            recovered = s.execute(
                select(func.count())
                .select_from(BusinessEvent)
                .where(
                    BusinessEvent.run_id == run_id,
                    BusinessEvent.event_type == "orchestration.vendor_recovered",
                )
            ).scalar_one()
            dup_steps = s.execute(
                select(OrchestrationStep.vendor_id, OrchestrationStep.step, func.count())
                .where(OrchestrationStep.run_id == run_id)
                .group_by(OrchestrationStep.vendor_id, OrchestrationStep.step)
                .having(func.count() > 1)
            ).all()
            starts = s.execute(
                select(func.count())
                .select_from(BusinessEvent)
                .where(
                    BusinessEvent.run_id == run_id,
                    BusinessEvent.vendor_id == ids["Alpha"],
                    BusinessEvent.event_type == "orchestration.node_started",
                    BusinessEvent.payload["node"].astext == "pay",
                )
            ).scalar_one()
        check(
            "S2: recoveries recorded (Alpha and Beta continued from checkpoint)",
            recovered == 2,
            recovered,
        )
        check("S2: effect ledger has no duplicate steps", not dup_steps, dup_steps)
        check("S2: the interrupted 'pay' node is visibly retried (2 starts)", starts == 2, starts)
    finally:
        w3.stop()


def s3_external_sigkill(admin: Api) -> None:
    fest, ids = seed("s3")
    run_id = admin.start(fest, max_concurrency=1).json()["run_id"]
    marker = os.path.join(LOG_DIR, f"hang-{RUN}-s3")
    if os.path.exists(marker):
        os.remove(marker)
    w1 = Worker(
        "s3-hangs",
        {
            "ORCHESTRATION_CRASH_POINTS": f"after:post@{ids['Alpha']}",
            "ORCHESTRATION_CRASH_MODE": "hang",
            "ORCHESTRATION_CRASH_MARKER": marker,
        },
    )
    deadline = time.time() + 60
    while time.time() < deadline and not os.path.exists(marker):
        time.sleep(0.2)
    reached = os.path.exists(marker)
    commission_before = count(
        JournalEntry, JournalEntry.entry_key.like(f"COMMISSION:{ids['Alpha']}:%")
    )
    code = w1.kill9()
    w1.stop()
    check(
        "S3: worker hung after Alpha's commission posting committed; SIGKILLed",
        reached and code == -signal.SIGKILL and commission_before == 1,
        (reached, code, commission_before),
    )
    w2 = Worker("s3-recovers")
    try:
        run = wait_run(admin, run_id, {"WAITING_REVIEW", "COMPLETED"})
        check("S3: new worker resumed the run", run["status"] == "WAITING_REVIEW", run["status"])
        check("S3: Alpha COMPLETED", admin.vendor(run_id, ids["Alpha"])["status"] == "COMPLETED")
        assert_no_duplicates("S3", fest, {n: ids[n] for n in ("Alpha", "Beta", "Delta")})
    finally:
        w2.stop()


def _llm_failure(admin: Api, name: str, mode: str, expect_status: str) -> None:
    fest, ids = seed(name)
    stub = start_stub()
    _stub_mode["mode"], _stub_mode["calls"] = mode, 0
    run_id = admin.start(fest, explain_cases=True, budget_usd=0.25).json()["run_id"]
    w = Worker(
        name,
        {
            "ANTHROPIC_API_KEY": "stub-key-not-a-secret",
            "ANTHROPIC_BASE_URL": stub,
            "ORCHESTRATION_LLM_TIMEOUT_SECONDS": "1",
        },
    )
    try:
        run = wait_run(admin, run_id, {"WAITING_REVIEW", "COMPLETED", "PARTIALLY_COMPLETE"})
        g = admin.vendor(run_id, ids["Gamma"])
        expl = (g.get("cases") or [{}])[0].get("explanations") or []
        tag = name.upper()
        check(
            f"{tag}: run continues; Gamma HELD (not FAILED) despite the model {mode}",
            run["status"] == "WAITING_REVIEW" and g["status"] == "HELD",
            (run["status"], g["status"]),
        )
        check(
            f"{tag}: explanation recorded as {expect_status}, retryable, no text shown",
            len(expl) == 1
            and expl[0]["status"] == expect_status
            and expl[0]["retryable"] is True
            and expl[0]["explanation"] is None,
            expl,
        )
        check(f"{tag}: the stub was actually called once", _stub_mode["calls"] == 1, _stub_mode)
        events = admin.get(
            f"/api/v1/orchestration/runs/{run_id}/events", vendor_id=ids["Gamma"]
        ).json()["items"]
        failed = [e for e in events if e["event_type"] == "agent.case_explanation_failed"]
        check(
            f"{tag}: visible retryable failure event with model/prompt version",
            len(failed) == 1
            and failed[0]["payload"]["retryable"] is True
            and failed[0]["versions"].get("prompt_version")
            and failed[0]["versions"].get("model_requested"),
            failed,
        )
        check(
            f"{tag}: other vendors unaffected (3 completed)",
            run.get("vendor_counts") == {"COMPLETED": 3, "HELD": 2},
            run.get("vendor_counts"),
        )
        r = admin.run(run_id)
        check(
            f"{tag}: budget reservation released after the failed call",
            r["reserved_usd"] == 0 and r["spent_usd"] <= 0.001,
            (r["reserved_usd"], r["spent_usd"]),
        )
    finally:
        w.stop()


def _load_key_file() -> None:
    """VERIFY_ANTHROPIC_KEY_FILE=<gitignored .env>: read ANTHROPIC_API_KEY into
    this process only. Never printed or written anywhere."""
    path = os.environ.get("VERIFY_ANTHROPIC_KEY_FILE")
    if not path or os.environ.get("ANTHROPIC_API_KEY"):
        return
    with open(path) as fh:
        for line in fh:
            if line.startswith("ANTHROPIC_API_KEY="):
                os.environ["ANTHROPIC_API_KEY"] = line.split("=", 1)[1].strip()


def s6_live_claude(admin: Api) -> None:
    _load_key_file()
    if not (os.environ.get("VERIFY_LIVE_CLAUDE") == "1" and os.environ.get("ANTHROPIC_API_KEY")):
        print("SKIP  S6: live Claude (needs VERIFY_LIVE_CLAUDE=1 + key or key file)")
        return
    fest, ids = seed("s6")
    run_id = admin.start(fest, explain_cases=True, budget_usd=0.10).json()["run_id"]
    w = Worker("s6", {"ANTHROPIC_API_KEY": os.environ["ANTHROPIC_API_KEY"]})
    try:
        run = wait_run(admin, run_id, {"WAITING_REVIEW", "COMPLETED"}, timeout=180)
        g = admin.vendor(run_id, ids["Gamma"])
        expl = (g.get("cases") or [{}])[0].get("explanations") or []
        ok = [x for x in expl if x["status"] == "SUCCEEDED"]
        check(
            "S6: real Claude explanation SUCCEEDED with only verified citations",
            ok and all(c["verified"] for c in ok[0]["citations"]) and ok[0]["explanation"],
            expl,
        )
        check("S6: the case is still OPEN (proposal only)", g["cases"][0]["status"] == "OPEN")
        r = admin.run(run_id)
        check(
            "S6: spend recorded and within the $0.10 budget",
            0 < r["spent_usd"] <= 0.10 and r["reserved_usd"] == 0,
            r["spent_usd"],
        )
        if ok:
            print(f"      model_served={ok[0]['model_served']} spent_usd={r['spent_usd']:.5f}")
            print(f"      explanation: {ok[0]['explanation'][:300]!r}")
        with db() as s:
            row = (
                s.execute(select(AgentCaseExplanation).where(AgentCaseExplanation.run_id == run_id))
                .scalars()
                .first()
            )
            print(f"      usage: in={row.input_tokens} out={row.output_tokens}")
        check(
            "S6: run not blocked by the explanation",
            run["status"] == "WAITING_REVIEW",
            run["status"],
        )
    finally:
        w.stop()


def ensure_nobody() -> str:
    email = f"a06-nobody-{RUN}@example.com"
    with db() as s:
        s.add(User(email=email, hashed_password=hash_password("A06Nobody!pass")))
        s.commit()
    return email


def main() -> int:
    global _Session
    make_engine()
    _Session = get_session_factory()
    admin = Api(login(ADMIN_EMAIL, ADMIN_PASSWORD))
    nobody = Api(login(ensure_nobody(), "A06Nobody!pass"))
    print(f"verify_a06_orchestration: run={RUN} api={API_BASE_URL} logs={LOG_DIR}")

    s1_happy_path_and_holds(admin, nobody)
    s2_crash_mid_node(admin)
    s3_external_sigkill(admin)
    _llm_failure(admin, "s4", "timeout", "TIMEOUT")
    _llm_failure(admin, "s5", "invalid", "INVALID_OUTPUT")
    s6_live_claude(admin)

    print(f"\n{len(_passes)} passed, {len(_failures)} failed")
    for f in _failures:
        print(f"  FAILED: {f}")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
