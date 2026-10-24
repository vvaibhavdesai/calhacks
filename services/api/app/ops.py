"""A10 operational signals: stalled runs, outbox backlog, UNKNOWN payouts,
Claude spend and worker liveness.

Everything here is READ-ONLY. It reports conditions a human must look at;
it never retries, resends, reconciles or cancels anything. In particular an
UNKNOWN payout is an alert, not a trigger: C06 says an unknown result is
reconciled, never blindly resent, and that reconciliation is a deliberate
human/API action (POST /api/v1/payouts/{id}/reconcile), not something a
monitoring probe should do.

`collect_signals` is the single source for both `GET /api/v1/ops/alerts`
(JSON for humans and the UI) and `GET /metrics` (Prometheus text for a
scraper), so the two can never disagree.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.limits import spent_last_24h_usd
from app.config import Settings
from app.models_ledger import OutboxIntent, Payout, Run

ACTIVE_RUN_STATES = ("QUEUED", "RUNNING")


@dataclass
class Alert:
    code: str
    severity: str  # "critical" | "warning"
    count: int
    detail: str


@dataclass
class Signals:
    checked_at: datetime
    runs_active: int
    runs_stalled: int
    outbox_pending: int
    outbox_pending_stale: int
    outbox_oldest_pending_age_seconds: float | None
    outbox_unknown: int
    outbox_failed: int
    payouts_unknown: int
    payouts_recovery_required: int
    claude_spent_24h_usd: float
    claude_daily_budget_usd: float
    worker_up: bool | None  # None = not configured, so not checked
    alerts: list[Alert] = field(default_factory=list)


def _probe_worker(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - operator URL
            if resp.status != 200:
                return False
            return json.loads(resp.read() or b"{}").get("status") == "ok"
    except Exception:  # noqa: BLE001 - any failure means "not live"
        return False


def collect_signals(db: Session, settings: Settings, *, now: datetime | None = None) -> Signals:
    now = now or datetime.now(UTC)
    stalled_before = now - timedelta(minutes=settings.ops_stalled_run_minutes)
    stale_before = now - timedelta(minutes=settings.ops_outbox_stale_minutes)

    runs_active = db.scalar(
        select(func.count()).select_from(Run).where(Run.status.in_(ACTIVE_RUN_STATES))
    )
    runs_stalled = db.scalar(
        select(func.count())
        .select_from(Run)
        .where(Run.status.in_(ACTIVE_RUN_STATES), Run.updated_at < stalled_before)
    )

    outbox_by_status = dict(
        db.execute(select(OutboxIntent.status, func.count()).group_by(OutboxIntent.status)).all()
    )
    outbox_pending_stale = db.scalar(
        select(func.count())
        .select_from(OutboxIntent)
        .where(OutboxIntent.status == "PENDING", OutboxIntent.created_at < stale_before)
    )
    oldest_pending = db.scalar(
        select(func.min(OutboxIntent.created_at)).where(OutboxIntent.status == "PENDING")
    )

    payouts_unknown = db.scalar(
        select(func.count()).select_from(Payout).where(Payout.state == "UNKNOWN")
    )
    payouts_recovery = db.scalar(
        select(func.count()).select_from(Payout).where(Payout.recovery_required.is_(True))
    )

    worker_up = (
        _probe_worker(settings.ops_worker_health_url) if settings.ops_worker_health_url else None
    )

    s = Signals(
        checked_at=now,
        runs_active=int(runs_active or 0),
        runs_stalled=int(runs_stalled or 0),
        outbox_pending=int(outbox_by_status.get("PENDING", 0)),
        outbox_pending_stale=int(outbox_pending_stale or 0),
        outbox_oldest_pending_age_seconds=(
            (now - oldest_pending).total_seconds() if oldest_pending else None
        ),
        outbox_unknown=int(outbox_by_status.get("UNKNOWN", 0)),
        outbox_failed=int(outbox_by_status.get("FAILED", 0)),
        payouts_unknown=int(payouts_unknown or 0),
        payouts_recovery_required=int(payouts_recovery or 0),
        claude_spent_24h_usd=round(spent_last_24h_usd(db, now=now), 4),
        claude_daily_budget_usd=settings.claude_daily_budget_usd,
        worker_up=worker_up,
    )

    if s.runs_stalled:
        s.alerts.append(
            Alert(
                "STALLED_RUNS",
                "warning",
                s.runs_stalled,
                f"runs QUEUED/RUNNING with no update for > {settings.ops_stalled_run_minutes} min",
            )
        )
    if s.outbox_pending_stale or s.outbox_pending > settings.ops_outbox_backlog_max:
        s.alerts.append(
            Alert(
                "OUTBOX_BACKLOG",
                "warning",
                s.outbox_pending,
                f"{s.outbox_pending_stale} PENDING intent(s) older than "
                f"{settings.ops_outbox_stale_minutes} min; backlog threshold "
                f"{settings.ops_outbox_backlog_max}",
            )
        )
    if s.outbox_unknown:
        s.alerts.append(
            Alert(
                "OUTBOX_UNKNOWN",
                "critical",
                s.outbox_unknown,
                "outbox intents with UNKNOWN delivery result: reconcile, never resend",
            )
        )
    if s.payouts_unknown:
        s.alerts.append(
            Alert(
                "UNKNOWN_PAYOUTS",
                "critical",
                s.payouts_unknown,
                "payouts in UNKNOWN state: reconcile via POST /api/v1/payouts/{id}/reconcile; "
                "never resend",
            )
        )
    if s.payouts_recovery_required:
        s.alerts.append(
            Alert(
                "PAYOUT_RECOVERY_REQUIRED",
                "critical",
                s.payouts_recovery_required,
                "payouts flagged recovery_required (e.g. lost cancel race)",
            )
        )
    if s.claude_spent_24h_usd >= 0.8 * s.claude_daily_budget_usd:
        s.alerts.append(
            Alert(
                "CLAUDE_BUDGET",
                "critical" if s.claude_spent_24h_usd >= s.claude_daily_budget_usd else "warning",
                1,
                f"estimated Claude spend ${s.claude_spent_24h_usd:.2f} of "
                f"${s.claude_daily_budget_usd:.2f} rolling-24h budget",
            )
        )
    if s.worker_up is False:
        s.alerts.append(Alert("WORKER_DOWN", "critical", 1, "worker health probe failed"))
    return s


def signals_to_json(s: Signals) -> dict:
    return {
        "status": "alerting" if s.alerts else "ok",
        "checked_at": s.checked_at.isoformat(),
        "alerts": [a.__dict__ for a in s.alerts],
        "signals": {
            "runs_active": s.runs_active,
            "runs_stalled": s.runs_stalled,
            "outbox_pending": s.outbox_pending,
            "outbox_pending_stale": s.outbox_pending_stale,
            "outbox_oldest_pending_age_seconds": s.outbox_oldest_pending_age_seconds,
            "outbox_unknown": s.outbox_unknown,
            "outbox_failed": s.outbox_failed,
            "payouts_unknown": s.payouts_unknown,
            "payouts_recovery_required": s.payouts_recovery_required,
            "claude_spent_24h_usd_estimate": s.claude_spent_24h_usd,
            "claude_daily_budget_usd": s.claude_daily_budget_usd,
            "worker_up": s.worker_up,
        },
    }


def signals_to_prometheus(s: Signals) -> str:
    def g(name: str, help_: str, value: float | int | None) -> list[str]:
        if value is None:
            return []
        return [f"# HELP {name} {help_}", f"# TYPE {name} gauge", f"{name} {float(value):g}"]

    lines: list[str] = []
    lines += g("encore_runs_active", "Runs in QUEUED/RUNNING.", s.runs_active)
    lines += g("encore_runs_stalled", "Active runs with no recent update.", s.runs_stalled)
    lines += g("encore_outbox_pending", "Outbox intents PENDING.", s.outbox_pending)
    lines += g(
        "encore_outbox_pending_stale",
        "PENDING intents older than threshold.",
        s.outbox_pending_stale,
    )
    lines += g(
        "encore_outbox_oldest_pending_age_seconds",
        "Age of the oldest PENDING intent.",
        s.outbox_oldest_pending_age_seconds,
    )
    lines += g("encore_outbox_unknown", "Outbox intents with UNKNOWN result.", s.outbox_unknown)
    lines += g("encore_outbox_failed", "Outbox intents FAILED.", s.outbox_failed)
    lines += g("encore_payouts_unknown", "Payouts in UNKNOWN state.", s.payouts_unknown)
    lines += g(
        "encore_payouts_recovery_required",
        "Payouts flagged recovery_required.",
        s.payouts_recovery_required,
    )
    lines += g(
        "encore_claude_spend_24h_usd",
        "Estimated Claude spend, rolling 24h.",
        s.claude_spent_24h_usd,
    )
    lines += g(
        "encore_claude_daily_budget_usd",
        "Configured rolling-24h budget.",
        s.claude_daily_budget_usd,
    )
    if s.worker_up is not None:
        lines += g("encore_worker_up", "Worker health probe (1 = live).", int(s.worker_up))
    lines += g("encore_alerts_active", "Number of active alerts.", len(s.alerts))
    return "\n".join(lines) + "\n"
