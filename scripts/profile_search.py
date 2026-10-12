"""Profile a cold and a warm office search against live CDA (#169). Dev-only.

Usage: uv run python scripts/profile_search.py NWDM "Fort Peck"
"""

from __future__ import annotations

import cProfile
import io
import pstats
import sys
import tempfile
import time
from pathlib import Path

from cwms_tools.core import places, session
from cwms_tools.core.cache import Cache, set_cache


def main(office: str, query: str) -> None:
    session.configure_session()
    set_cache(Cache(directory=Path(tempfile.mkdtemp(prefix="cwms-profile-"))))
    for label in ("cold", "warm"):
        prof = cProfile.Profile()
        t0 = time.perf_counter()
        prof.enable()
        resp = places.search_places(query, office=office, limit=1)
        prof.disable()
        print(f"== {label}: {time.perf_counter() - t0:.1f}s total_count={resp['total_count']}")
        out = io.StringIO()
        pstats.Stats(prof, stream=out).sort_stats("cumulative").print_stats(12)
        print(out.getvalue())


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
