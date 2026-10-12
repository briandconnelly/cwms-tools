"""Profile a cold and a warm office search against live CDA (#169). Dev-only.

Usage: uv run python scripts/profile_search.py NWDM "Fort Peck" [--no-profile]

cProfile inflates wall time (roughly 1.3x to 4x observed); pass --no-profile to
print only the true cold/warm totals.
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


def main(office: str, query: str, *, profile: bool = True) -> None:
    session.configure_session()
    with tempfile.TemporaryDirectory(prefix="cwms-profile-") as tmp:
        set_cache(Cache(directory=Path(tmp)))
        try:
            for label in ("cold", "warm"):
                prof = cProfile.Profile()
                t0 = time.perf_counter()
                if profile:
                    prof.enable()
                resp = places.search_places(query, office=office, limit=1)
                if profile:
                    prof.disable()
                print(
                    f"== {label}: {time.perf_counter() - t0:.1f}s total_count={resp['total_count']}"
                )
                if not profile:
                    continue
                out = io.StringIO()
                pstats.Stats(prof, stream=out).sort_stats("cumulative").print_stats(12)
                print(out.getvalue())
        finally:
            set_cache(None)  # closes the cache so the directory can be removed


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--no-profile"]
    main(args[0], args[1], profile="--no-profile" not in sys.argv)
