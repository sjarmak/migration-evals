"""Shared ISO-date coercion for the runner, report, and contamination split.

The runner (``model_cutoff_date`` / repo ``created_at``), the report
latency/cost aggregator, and the contamination split each consumed the
same untrusted shape — ``None``, an existing ``date``/``datetime``, or an
ISO-8601 string — and each carried a near-identical private coercer.
They had drifted: two passed a ``datetime`` straight through despite
promising ``date | None``. :func:`parse_iso_date` is the single helper;
it narrows a ``datetime`` to its calendar ``date`` so the return type is
honest.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

__all__ = ["parse_iso_date"]


def parse_iso_date(raw: Any) -> date | None:
    """Coerce a value to a calendar :class:`date`, or ``None`` if unusable.

    Accepts ``None`` (→ ``None``), an existing ``date`` (returned as-is),
    a ``datetime`` (narrowed to its ``.date()``), or a string whose
    leading ``YYYY-MM-DD`` parses via :meth:`date.fromisoformat`. Any
    other type, an empty string, or an unparseable string yields
    ``None``. ``datetime`` is checked before ``date`` because it is a
    subclass of ``date``.
    """
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None
