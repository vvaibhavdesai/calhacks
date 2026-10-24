"""A06 stage 3: per-vendor nightly close orchestration (LangGraph + PostgreSQL
checkpoints).

  checkpoints.py   PostgresSaver wiring for the restricted app role
  recorder.py      flight-recorder events, the effect ledger, crash points
  reconcile_day.py day-scoped, incremental use of A03's matching engine
  terms.py         the terms-resolution interface (stubbed onto A05 until
                   app/terms_resolution.py lands)
  explain.py       optional Claude case explanation (proposal only)
  vendor_graph.py  the per-vendor LangGraph workflow (C04 milestones)
  coordinator.py   parent run: start/claim/lease/fan-out/summarize/resume

Agents only read and propose. Every money-moving step (settle, approve,
release, post) is existing deterministic code behind its existing gates.
"""
