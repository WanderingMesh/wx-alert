"""Text normalization shared by configuration parsing and alert rendering."""

from __future__ import annotations

from typing import Any


def clean_optional(value: str | None) -> str | None:
    """Strip a string and collapse an empty result to None.

    Configuration files routinely carry blank-but-present values. Treating
    "" and None identically means callers only need one absence check.
    """
    if value is None:
        return None

    value = value.strip()
    return value or None


def clean_field(value: Any, fallback: str) -> str:
    """Convert an arbitrary alert field to normalized text with a fallback.

    NWS omits fields freely and occasionally supplies non-string values, so
    every read goes through here rather than assuming a type.
    """
    if value is None:
        return fallback

    text = str(value).strip()
    return text if text else fallback
