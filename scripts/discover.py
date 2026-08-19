#!/usr/bin/env python3
"""Discover artists you don't have yet, seeded from your beets library.

Reads library.db directly (read-only — safe on a large, live library),
ranks your artists by play count (falling back to track count), asks a
recommendation source for artists similar to your top seeds, filters out
everyone already in your library, and prints a ranked shortlist.

Sources:
  * deezer  (default) — no API key required.
  * lastfm  — richer similarity graph; needs --lastfm-key (free API key).

Examples:
  python3 scripts/discover.py --db ~/.config/beets/library.db
  python3 scripts/discover.py --db library.db --seeds 15 --limit 40
  python3 scripts/discover.py --db library.db --artist "Boards of Canada"
  python3 scripts/discover.py --db library.db --source lastfm --lastfm-key KEY
"""

import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"


def api_get(url, params):
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}?{qs}", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def norm(name):
    """Normalize an artist name for duplicate detection."""
    name = name.lower().strip()
    name = re.sub(r"^the\s+", "", name)
    name = re.sub(r"[^\w\s]", "", name)
    return re.sub(r"\s+", " ", name)


def library_artists(db_path):
    """Return {normalized_name: (display_name, plays, tracks)}."""
    con = sqlite3.connect(f"file:{urllib.parse.quote(db_path)}?mode=ro", uri=True)
    try:
        rows = con.execute(
            """
            SELECT CASE WHEN i.albumartist != '' THEN i.albumartist ELSE i.artist END
                       AS name,
                   COUNT(*) AS tracks,
                   COALESCE(SUM(CAST(a.value AS INTEGER)), 0) AS plays
            FROM items i
            LEFT JOIN item_attributes a
                   ON a.entity_id = i.id AND a.key = 'play_count'
            GROUP BY name
            """
        ).fetchall()
    finally:
        con.close()
    artists = {}
    for name, tracks, plays in rows:
        if not name:
            continue
        key = norm(name)
        if key in artists:
            old_name, old_plays, old_tracks = artists[key]
            artists[key] = (old_name, old_plays + plays, old_tracks + tracks)
        else:
            artists[key] = (name, plays, tracks)
    return artists


def similar_deezer(artist_name, limit):
    """Related artists from Deezer's public (keyless) API."""
    found = api_get("https://api.deezer.com/search/artist", {"q": artist_name})
    matches = found.get("data") or []
    if not matches:
        return []
    artist_id = matches[0]["id"]
    related = api_get(f"https://api.deezer.com/artist/{artist_id}/related", {})
    return [a["name"] for a in (related.get("data") or [])[:limit]]


def similar_lastfm(artist_name, limit, api_key):
    data = api_get(
        "https://ws.audioscrobbler.com/2.0/",
        {
            "method": "artist.getsimilar",
            "artist": artist_name,
            "autocorrect": 1,
            "api_key": api_key,
            "format": "json",
            "limit": limit,
        },
    )
    similar = data.get("similarartists", {}).get("artist", [])
    return [a["name"] for a in similar]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True, help="path to beets library.db")
    ap.add_argument("--seeds", type=int, default=10,
                    help="number of top library artists to seed with (default 10)")
    ap.add_argument("--artist", action="append", default=[],
                    help="seed with this artist instead of your top ones "
                         "(repeatable)")
    ap.add_argument("--limit", type=int, default=25,
                    help="max recommendations to print (default 25)")
    ap.add_argument("--per-seed", type=int, default=20,
                    help="similar artists fetched per seed (default 20)")
    ap.add_argument("--source", choices=["deezer", "lastfm"], default="deezer")
    ap.add_argument("--lastfm-key", help="Last.fm API key (for --source lastfm)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = ap.parse_args()

    if args.source == "lastfm" and not args.lastfm_key:
        ap.error("--source lastfm requires --lastfm-key")

    owned = library_artists(args.db)
    if not owned:
        sys.exit("no artists found in the library — is --db pointing at library.db?")

    if args.artist:
        seeds = [(name, 1) for name in args.artist]
    else:
        # Rank by plays when play counts exist, otherwise by track count.
        ranked = sorted(owned.values(), key=lambda a: (a[1], a[2]), reverse=True)
        seeds = [(name, plays + tracks) for name, plays, tracks in ranked[: args.seeds]]

    # score[candidate] accumulates rank-weighted votes across seeds.
    scores = defaultdict(float)
    voters = defaultdict(set)
    display_names = {}
    for seed_name, _weight in seeds:
        try:
            if args.source == "deezer":
                names = similar_deezer(seed_name, args.per_seed)
            else:
                names = similar_lastfm(seed_name, args.per_seed, args.lastfm_key)
        except Exception as exc:  # network hiccups shouldn't kill the whole run
            print(f"  ! {seed_name}: {exc}", file=sys.stderr)
            continue
        for rank, cand in enumerate(names):
            key = norm(cand)
            if key in owned:
                continue
            scores[key] += (len(names) - rank) / len(names)
            voters[key].add(seed_name)
            display_names[key] = cand
        time.sleep(0.3)  # be polite to the API

    results = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[: args.limit]
    out = [
        {
            "artist": display_names[key],
            "score": round(score, 2),
            "because_of": sorted(voters[key]),
        }
        for key, score in results
    ]

    if args.json:
        json.dump(out, sys.stdout, indent=2)
        print()
        return

    print(f"\nSeeded from: {', '.join(name for name, _ in seeds)}\n")
    for i, rec in enumerate(out, 1):
        because = ", ".join(rec["because_of"][:3])
        print(f"{i:3}. {rec['artist']:<35} (score {rec['score']:>5}; "
              f"similar to {because})")


if __name__ == "__main__":
    main()
