"""Strict JSON decoding with duplicate-key rejection."""

import json

from ..contracts import DatasetError, DatasetManifest


class JsonManifestDecoder:
    def decode(self, content: bytes) -> DatasetManifest:
        def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise DatasetError(f"DUPLICATE_JSON_KEY:{key}")
                result[key] = value
            return result

        def invalid_number(value: str) -> object:
            raise DatasetError(f"NONFINITE_JSON_NUMBER:{value}")

        text = content.decode("utf-8-sig")
        json.loads(text, object_pairs_hook=unique, parse_constant=invalid_number)
        return DatasetManifest.model_validate_json(text, strict=True)
