"""Operator CLI for the demo clock (app/demo_clock.py).

    uv run python -m worker.clock show
    uv run python -m worker.clock set 2026-10-17T08:59 --note "night 1 close"
    uv run python -m worker.clock advance 5m
    uv run python -m worker.clock offset 7d
    uv run python -m worker.clock resume

`set` freezes the clock at an instant, so a demo is deterministic: nothing
moves until the next `set`/`advance`. A bare date-time without an offset is
read as festival-local (America/Los_Angeles), because that is the timezone
every business date in this product is expressed in; append `Z` or an
offset to give a UTC instant instead.

Inert when APP_ENV=production or DEMO_CLOCK_MODE=off -- the mutators refuse
loudly rather than pretending the clock moved, the same way
ORCHESTRATION_CRASH_POINTS is ignored in production.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime

_DURATION = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*([smhdw]?)\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(raw: str) -> float:
    """ "90", "5m", "2h", "3d", "-1h" -> seconds."""
    match = _DURATION.match(raw)
    if match is None:
        raise argparse.ArgumentTypeError(f"not a duration: {raw!r} (try 90, 5m, 2h, 3d)")
    return float(match.group(1)) * _UNIT_SECONDS[match.group(2).lower()]


def parse_instant(raw: str):
    from app.demo_clock import FESTIVAL_TZ

    text = raw.strip().replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:  # pragma: no cover - argparse reports it
        raise argparse.ArgumentTypeError(f"not an ISO-8601 instant: {raw!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=FESTIVAL_TZ)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m worker.clock")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show", help="print the current clock")
    p_set = sub.add_parser("set", help="freeze the clock at an instant")
    p_set.add_argument("instant", type=parse_instant)
    p_set.add_argument("--note")
    p_adv = sub.add_parser("advance", help="move the clock forward (e.g. 5m, 2h, 1d)")
    p_adv.add_argument("duration", type=parse_duration)
    p_off = sub.add_parser("offset", help="run at wall clock plus an offset")
    p_off.add_argument("duration", type=parse_duration)
    sub.add_parser("resume", help="go back to the wall clock")
    args = parser.parse_args(argv)

    from app import demo_clock
    from app.orchestration.recorder import session

    actor = f"cli:worker.clock:{os.environ.get('USER', 'unknown')}"[:100]
    with session() as db:
        try:
            if args.command == "show":
                state = demo_clock.read_state(db)
            elif args.command == "set":
                state = demo_clock.freeze_at(db, args.instant, actor=actor, note=args.note)
            elif args.command == "advance":
                state = demo_clock.advance(db, args.duration, actor=actor)
            elif args.command == "offset":
                state = demo_clock.set_offset(db, args.duration, actor=actor)
            else:
                state = demo_clock.resume_wall_clock(db, actor=actor)
        except demo_clock.DemoClockDisabledError as exc:
            print(json.dumps({"error": str(exc)}), file=sys.stderr)
            return 2
        print(json.dumps(state.as_dict(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
