#!/usr/bin/env python3
"""Stub for generating the TypeScript client/types from the Pydantic/OpenAPI contracts.

NOT IMPLEMENTED YET. C03 (shared records, commands and API contract) is still a
DRAFT in CONTRACTS.md -- schemas are not frozen, so there is nothing real to
generate beyond the minimal C01 types in contracts/common.py. This stub exists so
CI has a stable command to call once generation is implemented, and so the
intended workflow is documented:

    uv run python packages/contracts/scripts/generate_ts_types.py \
        --openapi services/api/openapi.json \
        --out apps/web/src/generated

Future implementation should:
  1. Export the FastAPI app's OpenAPI schema (services/api/app/main.py already
     exposes this at /openapi.json once the app is running, or via
     `app.openapi()` offline).
  2. Run a generator (e.g. openapi-typescript) against that schema.
  3. Write generated, never-hand-edited TypeScript under apps/web/src/generated.

Running this script today performs a no-op success exit so it can be wired into
CI ("contracts-validate") ahead of the real implementation, without claiming any
business schema has been generated.
"""

import sys


def main() -> int:
    print(
        "generate_ts_types.py: stub only -- C03 schemas are still DRAFT. "
        "Nothing generated. See module docstring for the intended workflow."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
