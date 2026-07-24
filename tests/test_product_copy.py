"""Regression tests for the public REXLiTE AI product language."""

from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "custom_components" / "rexlite"


def _load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _string_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for child in value.values() for text in _string_values(child)]
    if isinstance(value, list):
        return [text for child in value for text in _string_values(child)]
    return []


class ProductCopyTests(unittest.TestCase):
    """Keep Home Assistant-facing copy business-oriented and consistent."""

    def test_english_translation_matches_source_strings(self) -> None:
        self.assertEqual(
            _load_json(INTEGRATION / "strings.json"),
            _load_json(INTEGRATION / "translations" / "en.json"),
        )

    def test_user_facing_copy_does_not_expose_engineering_terms(self) -> None:
        forbidden_terms = (
            "Agent",
            "Gateway",
            "WebSocket",
            "API",
            "Token",
            "權杖",
            "閘道",
            "通道",
            "串接",
            "內部網址",
        )
        for filename in ("strings.json", "translations/en.json", "translations/zh-Hant.json"):
            values = _string_values(_load_json(INTEGRATION / filename))
            for text in values:
                with self.subTest(filename=filename, text=text):
                    self.assertFalse(
                        any(term in text for term in forbidden_terms),
                        f"Public copy exposes an engineering term: {text}",
                    )


if __name__ == "__main__":
    unittest.main()
