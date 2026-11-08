""".env.example says "every value has a safe default": copying it must not change behaviour.
It once set the old speech engine and turned the local language model off."""

from __future__ import annotations

import re
from pathlib import Path

from app.config import Settings

TEMPLATE = Path(__file__).resolve().parents[2] / ".env.example"


def test_every_template_value_is_the_code_default():
    fields = Settings.model_fields  # declared defaults (the test environment overrides some settings)
    checked = 0
    for line in TEMPLATE.read_text().splitlines():
        m = re.match(r"^LOCKEDIN_([A-Z0-9_]+)=(.*)$", line.strip())
        if not m or m.group(1).lower() not in fields:
            continue
        value = fields[m.group(1).lower()].default
        assert ("" if value is None else str(value).lower()) == m.group(2).lower(), f"LOCKEDIN_{m.group(1)}"
        checked += 1
    assert checked >= 20


def test_the_template_holds_no_secret_values():
    for line in TEMPLATE.read_text().splitlines():
        if re.match(r"^[A-Z_]*(API_KEY|TOKEN|SECRET)=", line):
            assert line.split("=", 1)[1].strip() == "", line.split("=", 1)[0]
