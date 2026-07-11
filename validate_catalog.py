#!/usr/bin/env python3
"""Strict validator for device_catalog.json.

Mirrors the validation the Home Assistant integration applies at runtime
(custom_components/wican/catalog.py in meatpiHQ/ha-wican) — but where the
integration silently DROPS an invalid entry/field to protect installs,
this validator FAILS, so problems are caught in review instead of
shipping as silent no-ops.

Usage: python3 validate_catalog.py [path]   (default: device_catalog.json)
Exit code 0 = valid; 1 = problems found (listed on stdout).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Keep in sync with custom_components/wican/const.py in meatpiHQ/ha-wican.
SCHEMA_VERSION = 1
MAX_BYTES = 262144  # the integration refuses larger fetches
MAX_DEVICE_TYPES = 64
MAX_KEYWORDS = 8
MAX_KEYWORD_LENGTH = 32
MAX_MODEL_LENGTH = 64
MAX_SENSORS = 32
MAX_FIELD_LENGTH = 128

SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
FIRMWARE_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
STATUS_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

# Defined in integration code. These slugs are EXTEND-ONLY in the
# catalog: they may add sensors, but identity (model is informational
# only), hw_keywords, capability flags, and firmware stay code-defined.
RESERVED_SLUGS = {"wican", "wican_usb", "wican_pro", "meatpi"}
RESERVED_ALLOWED_FIELDS = {"model", "sensors"}

errors: list[str] = []


def err(msg: str) -> None:
    errors.append(msg)


def check_str(value: object, max_length: int, where: str, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            err(f"{where}: missing required string")
        return None
    if not isinstance(value, str) or not value.strip():
        err(f"{where}: must be a non-empty string, got {value!r}")
        return None
    if len(value.strip()) > max_length:
        err(f"{where}: longer than {max_length} chars")
        return None
    if value != value.strip():
        err(f"{where}: has leading/trailing whitespace")
    return value.strip()


def check_sensor(slug: str, index: int, raw: object) -> None:
    where = f"device_types.{slug}.sensors[{index}]"
    if not isinstance(raw, dict):
        err(f"{where}: must be an object")
        return
    key = check_str(raw.get("key"), MAX_FIELD_LENGTH, f"{where}.key", required=True)
    if key is not None and not STATUS_KEY_RE.match(key):
        err(f"{where}.key: {key!r} does not match {STATUS_KEY_RE.pattern}")
    check_str(raw.get("name"), MAX_FIELD_LENGTH, f"{where}.name")
    check_str(raw.get("device_class"), MAX_FIELD_LENGTH, f"{where}.device_class")
    check_str(raw.get("unit"), MAX_KEYWORD_LENGTH, f"{where}.unit")
    icon = raw.get("icon")
    if icon is not None:
        icon_str = check_str(icon, MAX_FIELD_LENGTH, f"{where}.icon")
        if icon_str is not None and not icon_str.startswith("mdi:"):
            err(f"{where}.icon: must start with 'mdi:'")
    diagnostic = raw.get("diagnostic")
    if diagnostic is not None and not isinstance(diagnostic, bool):
        err(f"{where}.diagnostic: must be a boolean")
    known = {"key", "name", "device_class", "unit", "icon", "diagnostic"}
    for extra in set(raw) - known:
        err(f"{where}: unknown field {extra!r}")


def check_device_type(slug: str, raw: object) -> None:
    where = f"device_types.{slug}"
    if not SLUG_RE.match(slug):
        err(f"{where}: slug does not match {SLUG_RE.pattern}")
    if not isinstance(raw, dict):
        err(f"{where}: must be an object")
        return
    if slug in RESERVED_SLUGS:
        for field in set(raw) - RESERVED_ALLOWED_FIELDS:
            err(
                f"{where}.{field}: not allowed on the reserved slug "
                f"{slug!r} — identity, capabilities, and firmware are "
                "code-defined; reserved entries may only add sensors"
            )
    check_str(raw.get("model"), MAX_MODEL_LENGTH, f"{where}.model", required=True)

    keywords = raw.get("hw_keywords")
    if keywords is not None:
        if not isinstance(keywords, list):
            err(f"{where}.hw_keywords: must be a list")
        else:
            if len(keywords) > MAX_KEYWORDS:
                err(f"{where}.hw_keywords: more than {MAX_KEYWORDS} keywords")
            seen: set[str] = set()
            for i, kw in enumerate(keywords):
                kw_str = check_str(kw, MAX_KEYWORD_LENGTH, f"{where}.hw_keywords[{i}]")
                if kw_str is None:
                    continue
                if kw_str != kw_str.lower():
                    err(f"{where}.hw_keywords[{i}]: must be lowercase")
                if kw_str.lower() in seen:
                    err(f"{where}.hw_keywords[{i}]: duplicate {kw_str!r}")
                seen.add(kw_str.lower())

    for flag in ("supports_obd_pids", "supports_gps"):
        value = raw.get(flag)
        if value is not None and not isinstance(value, bool):
            err(f"{where}.{flag}: must be a boolean")

    firmware = raw.get("firmware")
    if firmware is not None:
        if not isinstance(firmware, dict):
            err(f"{where}.firmware: must be an object or null")
        else:
            repo = check_str(firmware.get("repo"), MAX_FIELD_LENGTH, f"{where}.firmware.repo")
            if repo is not None and not FIRMWARE_REPO_RE.match(repo):
                err(f"{where}.firmware.repo: must look like 'owner/repo'")
            check_str(firmware.get("asset_pattern"), MAX_FIELD_LENGTH, f"{where}.firmware.asset_pattern")
            for extra in set(firmware) - {"repo", "asset_pattern"}:
                err(f"{where}.firmware: unknown field {extra!r}")

    sensors = raw.get("sensors")
    if sensors is not None:
        if not isinstance(sensors, list):
            err(f"{where}.sensors: must be a list")
        else:
            if len(sensors) > MAX_SENSORS:
                err(f"{where}.sensors: more than {MAX_SENSORS} sensors")
            keys: set[str] = set()
            for i, sensor in enumerate(sensors):
                check_sensor(slug, i, sensor)
                if isinstance(sensor, dict) and isinstance(sensor.get("key"), str):
                    if sensor["key"] in keys:
                        err(f"{where}.sensors[{i}]: duplicate key {sensor['key']!r}")
                    keys.add(sensor["key"])

    known = {"model", "hw_keywords", "supports_obd_pids", "supports_gps", "firmware", "sensors"}
    for extra in set(raw) - known:
        err(f"{where}: unknown field {extra!r}")


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "device_catalog.json")
    raw_bytes = path.read_bytes()
    if len(raw_bytes) > MAX_BYTES:
        err(f"{path}: {len(raw_bytes)} bytes exceeds the {MAX_BYTES}-byte fetch cap")

    try:
        document = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"FAIL: {path} is not valid UTF-8 JSON: {exc}")
        return 1

    if not isinstance(document, dict):
        err("document: must be a JSON object")
    else:
        if document.get("schema") != SCHEMA_VERSION:
            err(f"schema: must be {SCHEMA_VERSION}, got {document.get('schema')!r}")
        device_types = document.get("device_types")
        if not isinstance(device_types, dict):
            err("device_types: must be an object")
        else:
            if len(device_types) > MAX_DEVICE_TYPES:
                err(f"device_types: more than {MAX_DEVICE_TYPES} entries")
            if not device_types:
                err("device_types: must not be empty")
            for slug, entry in device_types.items():
                check_device_type(str(slug), entry)
        for extra in set(document) - {"schema", "_comment", "device_types"}:
            err(f"document: unknown top-level field {extra!r}")

    if errors:
        print(f"FAIL: {len(errors)} problem(s) in {path}:")
        for problem in errors:
            print(f"  - {problem}")
        return 1
    print(f"OK: {path} is a valid schema-{SCHEMA_VERSION} catalog")
    return 0


if __name__ == "__main__":
    sys.exit(main())
