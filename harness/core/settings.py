"""Optional, zero-dependency configuration for Harness².

Settings live next to the deployed plugin:

* ``harness.toml`` is preferred and parsed with :mod:`tomllib` on Python 3.11+;
* ``harness.json`` is the fallback on Python 3.10 and is also read when no TOML
  file exists.

Malformed files and invalid values never raise. They produce warnings and the
documented default is used for the offending key. All valid values are merged
over a fresh copy of :data:`DEFAULT_SETTINGS`.
"""

from __future__ import annotations

import copy
import json
import math
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib as _tomllib
except ImportError:  # Python 3.10
    _tomllib = None  # type: ignore[assignment]


DEFAULT_SETTINGS: dict[str, dict[str, Any]] = {
    "rules": {
        "max_bytes": 20000,
    },
    "briefing": {
        "max_chars": 7000,
        "max_skills": 8,
        "max_lessons": 5,
    },
    "skills": {
        "max_per_session": 2,
        "min_quality": 0.55,
    },
    "memory": {
        "prune_days": 365,
        "candidate_days": 30,
        "max_lessons": 2000,
    },
    "hooks": {
        "stop_timeout": 90,
        "post_tool_use_timeout": 5,
    },
    "project": {
        "isolate_memory": True,
        "project_skills_only": True,
    },
    "routing": {
        "enabled": True,
        "auto_apply": True,
        "max_skills": 5,
        "max_tools": 6,
    },
}

# section -> key -> (kind, minimum, maximum)
_SPEC: dict[str, dict[str, tuple[str, float | int | None, float | int | None]]] = {
    "rules": {
        "max_bytes": ("int", 1, None),
    },
    "briefing": {
        "max_chars": ("int", 1, None),
        "max_skills": ("int", 0, None),
        "max_lessons": ("int", 0, None),
    },
    "skills": {
        "max_per_session": ("int", 0, None),
        "min_quality": ("number", 0.0, 1.0),
    },
    "memory": {
        "prune_days": ("int", 0, None),
        "candidate_days": ("int", 0, None),
        "max_lessons": ("int", 0, None),
    },
    "hooks": {
        "stop_timeout": ("int", 1, None),
        "post_tool_use_timeout": ("int", 1, None),
    },
    "project": {
        "isolate_memory": ("bool", None, None),
        "project_skills_only": ("bool", None, None),
    },
    "routing": {
        "enabled": ("bool", None, None),
        "auto_apply": ("bool", None, None),
        "max_skills": ("int", 0, None),
        "max_tools": ("int", 0, None),
    },
}


def _defaults() -> dict[str, dict[str, Any]]:
    return copy.deepcopy(DEFAULT_SETTINGS)


def _plugin_dir(plugin_dir: Path | str | None) -> Path:
    if plugin_dir is not None:
        return Path(plugin_dir).expanduser()
    configured = os.environ.get("HARNESS_PLUGIN_DIR")
    if configured:
        return Path(configured).expanduser()
    # harness/core/settings.py -> the plugin root two levels up.
    return Path(__file__).resolve().parents[2]


def _load_document(root: Path, warnings: list[str]) -> tuple[dict[str, Any], str]:
    """Read the preferred config file. Returns (document, source label)."""
    toml_path = root / "harness.toml"
    json_path = root / "harness.json"

    if toml_path.is_file() and _tomllib is not None:
        try:
            with toml_path.open("rb") as handle:
                document = _tomllib.load(handle)
        except Exception as exc:  # malformed TOML must never crash a hook
            warnings.append(
                f"could not parse harness.toml ({exc}); using default settings"
            )
            return {}, "harness.toml"
        if not isinstance(document, dict):
            warnings.append("harness.toml must contain a table; using default settings")
            return {}, "harness.toml"
        return document, "harness.toml"

    if toml_path.is_file() and _tomllib is None:
        # Python 3.10 cannot parse TOML without a third-party dependency. The
        # documented fallback is JSON, so try that before giving up.
        if json_path.is_file():
            warnings.append(
                "harness.toml requires Python 3.11+; using harness.json instead"
            )
        else:
            warnings.append(
                "harness.toml requires Python 3.11+; using default settings"
            )
            return {}, "harness.toml"

    if json_path.is_file():
        try:
            text = json_path.read_text(encoding="utf-8-sig")
            document = json.loads(text)
        except Exception as exc:
            warnings.append(
                f"could not parse harness.json ({exc}); using default settings"
            )
            return {}, "harness.json"
        if not isinstance(document, dict):
            warnings.append(
                "harness.json must contain an object; using default settings"
            )
            return {}, "harness.json"
        return document, "harness.json"

    return {}, ""


def _coerce(
    section: str,
    key: str,
    value: Any,
    kind: str,
    minimum: float | int | None,
    maximum: float | int | None,
    default: Any,
    source: str,
    warnings: list[str],
) -> tuple[bool, Any]:
    """Validate one value, returning (accepted, converted_value)."""
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            warnings.append(
                f"{source}: {section}.{key} must be an integer; "
                f"using default {default!r}"
            )
            return False, default
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            warnings.append(
                f"{source}: {section}.{key} must be a number; "
                f"using default {default!r}"
            )
            return False, default
        if isinstance(value, float) and not math.isfinite(value):
            warnings.append(
                f"{source}: {section}.{key} must be a finite number; "
                f"using default {default!r}"
            )
            return False, default
    elif kind == "bool":
        if not isinstance(value, bool):
            warnings.append(
                f"{source}: {section}.{key} must be true or false; "
                f"using default {default!r}"
            )
            return False, default
    else:  # pragma: no cover - guard against a future typo in _SPEC
        return False, default

    if minimum is not None and value < minimum:
        warnings.append(
            f"{source}: {section}.{key} must be >= {minimum}; "
            f"using default {default!r}"
        )
        return False, default
    if maximum is not None and value > maximum:
        warnings.append(
            f"{source}: {section}.{key} must be <= {maximum}; "
            f"using default {default!r}"
        )
        return False, default

    if kind == "number":
        return True, float(value)
    if kind == "bool":
        return True, bool(value)
    return True, int(value)


def _validate(
    document: Mapping[str, Any], source: str, warnings: list[str]
) -> dict[str, dict[str, Any]]:
    settings = _defaults()
    for section, values in document.items():
        if section not in DEFAULT_SETTINGS:
            warnings.append(f"{source}: unknown section {section!r}; ignoring")
            continue
        if not isinstance(values, Mapping):
            warnings.append(
                f"{source}: {section} must be a table/object; using defaults"
            )
            continue
        spec = _SPEC[section]
        for key, value in values.items():
            if key not in spec:
                warnings.append(
                    f"{source}: unknown setting {section}.{key}; ignoring"
                )
                continue
            kind, minimum, maximum = spec[key]
            accepted, converted = _coerce(
                section,
                key,
                value,
                kind,
                minimum,
                maximum,
                DEFAULT_SETTINGS[section][key],
                source,
                warnings,
            )
            if accepted:
                settings[section][key] = converted
    return settings


def load_settings(
    plugin_dir: Path | str | None = None,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Load settings from ``plugin_dir``.

    Returns ``(settings, warnings)``. Missing files are not an error and simply
    return the defaults. The returned dictionary is safe for callers to mutate.
    """
    root = _plugin_dir(plugin_dir)
    warnings: list[str] = []
    try:
        document, source = _load_document(root, warnings)
    except OSError as exc:
        warnings.append(f"could not read settings from {root}: {exc}; using defaults")
        return _defaults(), warnings

    if not document:
        return _defaults(), warnings
    return _validate(document, source, warnings), warnings


def get_setting(
    settings: Mapping[str, Any], path: str, default: Any = None
) -> Any:
    """Read a dotted setting path, returning ``default`` when absent or invalid."""
    current: Any = settings
    for part in str(path).split("."):
        if not isinstance(current, Mapping) or part not in current:
            return default
        current = current[part]
    return current
