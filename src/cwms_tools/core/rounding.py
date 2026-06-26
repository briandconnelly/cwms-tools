"""Round float values to a fixed number of significant figures at the
serialization boundary (issue #45).

Unit conversion (notably °C↔°F, m↔ft) lands on binary floats whose shortest
round-trip repr carries IEEE-754 noise — a water temperature stored natively in
°F surfaces as ``68.55000000000001``. CWMS exposes no sensor-precision metadata,
so consumers can't infer the right rounding themselves. Shortest-round-trip repr
does not help: ``68.55000000000001`` already *is* the shortest round-trip for
that double, so actual rounding is required.

This rounds to **significant figures** (not fixed decimals) because parameters
span huge magnitude ranges — temperatures ~20, flows in thousands of cfs, volts
~12 — where a fixed decimal count rounds some wrong. Six sig figs sits well below
any plausible sensor resolution, so no real signal is lost while the 1e-14 tails
disappear.

`round_floats` is applied at the two serialization sinks (the Pydantic
`CompactDumpMixin` serializer for the MCP surface, and `cli.render.emit` for the
CLI) so both surfaces share one source of truth. It operates on already-built
payloads, never mutating in place, so upstream domain values and any derived math
stay exact.

Carve-outs: latitude/longitude (and bbox bounds) are angular — 6 sig figs would
cost ~100 m of citation-grade precision — and cost fields span millions, where 6
sig figs rounds to the nearest 10k. Those keys pass through verbatim. The bug is
specifically unit-converted *measurement* values, which is what remains rounded.
"""

from __future__ import annotations

import math
from typing import Any

#: Default significant figures. Below any plausible sensor resolution (a
#: thermistor resolves ~0.1 °C), so the rounding only removes conversion noise.
DEFAULT_SIG_FIGS = 6

#: Keys whose float values pass through unrounded. Compared after normalizing
#: case and hyphen/underscore, so both the snake_case declared model fields
#: (`published_latitude`) and the hyphenated raw-CDA passthrough keys
#: (`published-latitude`) match the same entry.
#:
#: - Coordinates (incl. the `south`/`west`/`north`/`east` bbox bounds) are
#:   angular: 6 sig figs on a CONUS longitude (~ -118.24) costs ~100 m.
#: - Cost fields span millions, where 6 sig figs rounds to the nearest 10k.
_UNROUNDED_KEYS = frozenset(
    {
        "latitude",
        "longitude",
        "published_latitude",
        "published_longitude",
        "south",
        "west",
        "north",
        "east",
        "federal_cost",
        "non_federal_cost",
        "federal_o_and_m_cost",
        "non_federal_o_and_m_cost",
    }
)


def _normalize_key(key: str) -> str:
    return key.replace("-", "_").lower()


def _round_sig(value: float, sig_figs: int) -> float:
    """Round one float to `sig_figs` significant figures.

    Non-finite values (NaN, ±inf) are returned unchanged — they have no
    meaningful significant-figure form and `log10` would raise on them. Zero is
    normalized to ``+0.0`` (callers route exact zero here only via `round_floats`,
    which already normalizes, but keep this self-consistent for direct callers).
    """
    if value == 0.0:
        return 0.0
    if not math.isfinite(value):
        return value
    # digits-after-the-decimal-point for `round`; negative for magnitudes >= 10**sig_figs.
    digits = sig_figs - 1 - math.floor(math.log10(abs(value)))
    return round(value, digits)


def round_floats(obj: Any, *, sig_figs: int = DEFAULT_SIG_FIGS, _key: str | None = None) -> Any:
    """Recursively round every float in `obj` to `sig_figs` significant figures.

    Returns a new structure; never mutates `obj`. Only floats are touched —
    ``None``, ints, ``bool`` (an int subclass, explicitly excluded), strings,
    and timestamps pass through. Floats under a coordinate/money key
    (`_UNROUNDED_KEYS`) also pass through. Idempotent: rounding an
    already-rounded payload is a no-op.
    """
    # bool must be checked before float: `isinstance(True, int)` is True and
    # bool is not a float, but guard explicitly so the intent is unmistakable.
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        # Normalize signed zero first, ahead of the carve-out: `-0.0` is not
        # meaningful precision and `json.dumps(-0.0)` emits the noisy "-0.0".
        # Doing this before the key check covers exempt floats (e.g. a
        # `latitude` of -0.0) too.
        if obj == 0.0:
            return 0.0
        exempt = _key is not None and _normalize_key(_key) in _UNROUNDED_KEYS
        return obj if exempt else _round_sig(obj, sig_figs)
    if isinstance(obj, dict):
        return {k: round_floats(v, sig_figs=sig_figs, _key=k) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        # Propagate the parent key so an excluded key holding a list of values
        # stays excluded; entering a dict resets the key context.
        return [round_floats(v, sig_figs=sig_figs, _key=_key) for v in obj]
    return obj


__all__ = ["DEFAULT_SIG_FIGS", "round_floats"]
