"""Produce the Python SDK's request for a TypeScript session's live comparison.

Only model/map metadata crosses this boundary; no database credentials or row values are input.
The private controller uses this same SDK factory when issuing its verification request.
"""

from __future__ import annotations

import json
import sys

import sde
from sde.testing.loader import model_from_neutral


def main() -> None:
    body = json.load(sys.stdin)
    model = model_from_neutral(body["model"])
    placement = sde.load_map(body["map"], model=model)
    request = sde.verification_request(
        placement,
        group=body["group"],
        project_id=body["project_id"],
        request_id=body["request_id"],
        requested_at=body["requested_at"],
    )
    print(json.dumps(request.as_record()))


if __name__ == "__main__":
    main()
