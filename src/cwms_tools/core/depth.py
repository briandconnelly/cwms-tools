"""Parse CWMS depth-tagged sub-location ids into structured depth metadata.

Depth-indexed water-quality sensors hang off a parent "string" location with
cryptic ids whose suffix encodes the sensor's depth (see cwms-overview.md §6.2):

    GWLW_S1-D3,0ft   -> 3.0 ft       BECR-D042,5m -> 42.5 m
    GWLW_S1-D13,0ft  -> 13.0 ft      UBLW_S1-D21,0ft -> 21.0 ft

The convention is `<parent>-D<depth><unit>` where the depth uses a COMMA as the
decimal separator (a CWMS/European quirk) and the unit is `ft` or `m`. The
trailing `,0ft` reads as "every sensor is at 0 ft" unless you know the comma is
a decimal point — which is exactly the guesswork issue #27 removes by exposing
`{value, unit}` instead of the raw tag.
"""

from __future__ import annotations

import re
from typing import Any

#: `-D<digits>[,<digits>]<unit>` as the id SUFFIX (cwms-overview.md §6.2) — comma
#: is the decimal separator; unit is ft/m; anchored to end so an id that merely
#: *contains* a `-D…ft/m` substring isn't mistaken for a depth-tagged sensor.
_DEPTH_RE = re.compile(r"-D(\d+(?:,\d+)?)(ft|m)$", re.IGNORECASE)

#: Feet per meter, for ordering sensors whose tags mix units.
_FEET_PER_METER = 3.280839895


def parse_depth(location_name: str) -> dict[str, Any] | None:
    """Return `{"value": float, "unit": "ft"|"m"}` for a depth-tagged id, else None.

    The comma in the tag is treated as a decimal point, so `D3,0ft` is 3.0 ft and
    `D042,5m` is 42.5 m. Returns None for ids with no parseable depth tag.
    """
    match = _DEPTH_RE.search(location_name)
    if match is None:
        return None
    value = float(match.group(1).replace(",", "."))
    return {"value": value, "unit": match.group(2).lower()}


def depth_sort_key(location_name: str) -> tuple[int, float]:
    """Sort key ordering depth-tagged ids shallow→deep (in meters).

    Non-depth ids sort last (so a profile keeps real sensors first). Mixed ft/m
    tags are normalized to meters so the ordering is physically correct.
    """
    depth = parse_depth(location_name)
    if depth is None:
        return (1, 0.0)
    meters = depth["value"] / _FEET_PER_METER if depth["unit"] == "ft" else depth["value"]
    return (0, meters)
