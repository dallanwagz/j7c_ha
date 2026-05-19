#!/usr/bin/env python3
"""Quality-scale checklist guard.

Loads ``custom_components/atorch_ble/quality_scale.yaml`` (or a path passed as
argv[1]) and exits non-zero if any rule with ``status: todo`` lacks a
``target`` field or carries a target version already in the past relative to
the integration's current version in ``manifest.json``.

This is intentionally lenient: it does not validate against HA Core's
canonical rule set — that is the responsibility of HA Core's
``script.quality_scale`` validator, which is invoked separately in CI. This
helper exists only to prevent ``todo`` rot.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

DEFAULT_QS_PATH = Path("custom_components/atorch_ble/quality_scale.yaml")
DEFAULT_MANIFEST_PATH = Path("custom_components/atorch_ble/manifest.json")


def _parse_version(s: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in s.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def main(argv: list[str]) -> int:
    qs_path = Path(argv[1]) if len(argv) > 1 else DEFAULT_QS_PATH
    manifest_path = (
        Path(argv[2]) if len(argv) > 2 else DEFAULT_MANIFEST_PATH
    )

    with qs_path.open() as f:
        data = yaml.safe_load(f)

    rules = data.get("rules")
    if not isinstance(rules, dict):
        print(f"{qs_path}: missing or malformed 'rules' map", file=sys.stderr)
        return 2

    current_version: tuple[int, ...] | None = None
    if manifest_path.exists():
        with manifest_path.open() as f:
            current_version = _parse_version(json.load(f).get("version", "0"))

    errors: list[str] = []
    for name, val in rules.items():
        if isinstance(val, str):
            status = val
            target = None
        elif isinstance(val, dict):
            status = val.get("status")
            target = val.get("target")
        else:
            errors.append(f"{name}: unrecognized rule shape ({type(val).__name__})")
            continue

        if status not in {"done", "exempt", "todo"}:
            errors.append(f"{name}: unknown status '{status}'")
            continue

        if status == "todo":
            if not target:
                errors.append(f"{name}: status=todo without target version")
            elif current_version is not None:
                tgt = _parse_version(str(target))
                if tgt < current_version:
                    errors.append(
                        f"{name}: target {target} is older than current "
                        f"version {'.'.join(map(str, current_version))}"
                    )

    if errors:
        print("Quality-scale checklist errors:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    todos = sum(
        1
        for v in rules.values()
        if (v if isinstance(v, str) else v.get("status")) == "todo"
    )
    done = sum(
        1
        for v in rules.values()
        if (v if isinstance(v, str) else v.get("status")) == "done"
    )
    exempt = sum(
        1
        for v in rules.values()
        if (v if isinstance(v, str) else v.get("status")) == "exempt"
    )
    print(
        f"{qs_path}: {len(rules)} rules ({done} done, {exempt} exempt, {todos} todo) — OK"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
