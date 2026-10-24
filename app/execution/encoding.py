"""Bounded JSON normalization for idempotency, NOT a JCS/signing codec."""

import hashlib
import json
import math
from typing import Any

from app.execution.errors import InvalidCommand

MAX_JSON_BYTES = 65_536
MAX_JSON_DEPTH = 20
MAX_JSON_NODES = 4096


def _validate(value: Any, budget: list[int], depth: int = 0) -> None:
    budget[1] -= 1
    if depth > MAX_JSON_DEPTH or budget[1] < 0:
        raise InvalidCommand()
    if value is None or type(value) is bool:
        budget[0] -= 5
    elif type(value) is str:
        # Reject huge strings before encoding/copying them. Each intermediate
        # scalar allocation is bounded, as is total traversal and final output.
        if len(value) > budget[0]:
            raise InvalidCommand()
        try:
            budget[0] -= len(json.dumps(value, ensure_ascii=False).encode())
        except UnicodeError as exc:
            raise InvalidCommand() from exc
    elif type(value) is int:
        if abs(value) > 2**53 - 1:
            raise InvalidCommand()
        budget[0] -= len(str(value))
    elif type(value) is float:
        if not math.isfinite(value):
            raise InvalidCommand()
        budget[0] -= len(str(value))
    elif type(value) in (dict, list):
        if len(value) > MAX_JSON_NODES:
            raise InvalidCommand()
        budget[0] -= 2 + len(value) * 2
        if type(value) is dict:
            for key, item in value.items():
                if type(key) is not str:
                    raise InvalidCommand()
                _validate(key, budget, depth + 1)
                _validate(item, budget, depth + 1)
        else:
            for item in value:
                _validate(item, budget, depth + 1)
    else:
        raise InvalidCommand()
    if budget[0] < 0:
        raise InvalidCommand()


def json_bytes(value: Any) -> bytes:
    _validate(value, [MAX_JSON_BYTES, MAX_JSON_NODES])
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    except (ValueError, TypeError, UnicodeError) as exc:
        raise InvalidCommand() from exc
    if len(encoded) > MAX_JSON_BYTES:
        raise InvalidCommand()
    return encoded


def copied_object(value: Any) -> dict[str, Any]:
    if type(value) is not dict:
        raise InvalidCommand()
    return dict(json.loads(json_bytes(value)))


def digest(value: Any) -> str:
    return hashlib.sha256(b"stage-command-json-v1\0" + json_bytes(value)).hexdigest()
