"""Canonical message identity and strict JSON decoding."""

import json
from hashlib import sha256
from math import isfinite
from typing import NoReturn, cast

from pydantic import JsonValue

from .contracts_values import InitializationFailure, Value
from .validation import require


def canonical(request: Value) -> bytes:
    return json.dumps(
        request.model_dump(mode="json"),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def policy_digest(policy: Value) -> str:
    """Canonical digest for a trusted assembly's pre-approved policy DTO."""
    return sha256(canonical(policy)).hexdigest()


def decode_json(content: bytes) -> JsonValue:
    def pairs(items: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {}
        for key, value in items:
            require(key not in result, "INVALID_MESSAGE", f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    def constant(value: str) -> NoReturn:
        raise InitializationFailure(
            "INVALID_MESSAGE", f"Non-finite JSON value: {value}"
        )

    def finite_float(value: str) -> float:
        parsed = float(value)
        if not isfinite(parsed):
            constant(value)
        return parsed

    return cast(
        JsonValue,
        json.loads(
            content,
            object_pairs_hook=pairs,
            parse_constant=constant,
            parse_float=finite_float,
        ),
    )
