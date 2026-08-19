#!/usr/bin/env python3
"""Generate a weighted-random mixtape (.m3u) from your beets library.

This covers what smartplaylist's declarative queries can't express:
  * weighted randomness — higher-rated tracks appear more often, tracks you
    haven't heard in a long time (or ever) get a staleness boost;
  * artist spacing — the same artist won't appear twice within N tracks;
  * a target duration instead of a track count.

Uses the beets Python API, so any beets query works as a filter:

  python3 scripts/mixtape.py --db library.db --out mix.m3u
  python3 scripts/mixtape.py --db library.db --out chill.m3u \\
      --query 'genre:ambient' --minutes 45
  python3 scripts/mixtape.py --db library.db --out gym.m3u \\
      --query 'bpm:120..' --minutes 60 --spacing 5

Defaults for --db and --music-dir can also be set via a .env file
(PLAYSAI_DB, PLAYSAI_MUSIC_DIR) — see scripts/_env.py. CLI flags always
override the .env value.
"""

import argparse
import random
import sys
import time

import _env

try:
    from beets import config as beets_config
    from beets.library import Library
except ImportError:
    sys.exit("beets is required: pip install beets")

SECONDS_PER_DAY = 86400.0


def track_weight(item, now):
    """Score a track for selection. Higher = more likely to be picked."""
    rating = item.get("rating")
    rating = float(rating) if rating not in (None, "") else 5.0  # neutral default

    last_played = item.get("last_played")
    if last_played in (None, ""):
        stale_days = 365.0  # never played: maximally stale
    else:
        stale_days = min(365.0, (now - float(last_played)) / SECONDS_PER_DAY)
    staleness = 0.25 + 0.75 * (stale_days / 365.0)  # 0.25 .. 1.0

    return max(0.05, rating) * staleness


def pick_tracks(items, target_seconds, spacing, rng, now):
    weights = [track_weight(it, now) for it in items]
    chosen, total, recent_artists = [], 0.0, []
    pool = list(zip(items, weights))

    while pool and total < target_seconds:
        # Weighted draw, retried a few times to honor artist spacing.
        for _attempt in range(10):
            item, w = rng.choices(pool, weights=[w for _, w in pool], k=1)[0]
            artist = (item.albumartist or item.artist).lower()
            if artist not in recent_artists:
                break
        pool.remove((item, w))
        chosen.append(item)
        total += item.length or 240.0
        recent_artists.append(artist)
        if len(recent_artists) > spacing:
            recent_artists.pop(0)
    return chosen, total


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=_env.get("DB"),
                     required=_env.get("DB") is None,
                     help="path to beets library.db (or set PLAYSAI_DB)")
    ap.add_argument("--out", required=True, help="output .m3u path")
    ap.add_argument("--query", default="", help="beets query to filter the pool "
                    "(e.g. 'genre:rock year:1990..')")
    ap.add_argument("--minutes", type=float, default=60.0,
                    help="target playlist length (default 60)")
    ap.add_argument("--spacing", type=int, default=3,
                    help="min tracks between repeats of an artist (default 3)")
    ap.add_argument("--music-dir", default=_env.get("MUSIC_DIR"),
                    help="music directory that library paths are relative to "
                         "(default: `directory` from your beets config, or "
                         "PLAYSAI_MUSIC_DIR)")
    ap.add_argument("--seed", type=int, help="random seed for reproducible mixes")
    args = ap.parse_args()

    music_dir = args.music_dir or beets_config["directory"].as_filename()
    lib = Library(args.db, directory=music_dir)
    items = list(lib.items(args.query))
    if not items:
        sys.exit(f"query matched nothing: {args.query!r}")

    rng = random.Random(args.seed)
    chosen, total = pick_tracks(items, args.minutes * 60.0, args.spacing, rng,
                                now=time.time())

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write("#EXTM3U\n")
        for item in chosen:
            fh.write(f"#EXTINF:{int(item.length or 0)},"
                     f"{item.artist} - {item.title}\n")
            fh.write(item.path.decode("utf-8", "surrogateescape") + "\n")

    print(f"wrote {args.out}: {len(chosen)} tracks, "
          f"{total / 60:.1f} min (pool was {len(items)} tracks)")


if __name__ == "__main__":
    main()
