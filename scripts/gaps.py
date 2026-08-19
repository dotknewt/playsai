#!/usr/bin/env python3
"""Find studio albums you're missing from artists already in your library.

For each artist (ranked by how many of their albums you own, or chosen with
--artist), fetches their full album discography from MusicBrainz and diffs it
against your beets library — by MusicBrainz release-group ID when your
library is MB-tagged, with a fuzzy title fallback otherwise.

Reads library.db read-only; makes at most one MusicBrainz request per second
(their rate-limit policy).

Examples:
  python3 scripts/gaps.py --db ~/.config/beets/library.db --artists 5
  python3 scripts/gaps.py --db library.db --artist "Stereolab"

The default for --db can also be set via a .env file (PLAYSAI_DB) — see
scripts/_env.py. A CLI flag always overrides the .env value.
"""

import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

import _env

USER_AGENT = "playsai/0.1 (https://github.com/dotknewt/playsai)"
MB_ROOT = "https://musicbrainz.org/ws/2"


def mb_get(path, params):
    params = dict(params, fmt="json")
    url = f"{MB_ROOT}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def norm_title(title):
    title = title.lower()
    title = re.sub(r"[\(\[].*?[\)\]]", "", title)  # drop (deluxe), [remaster], ...
    title = re.sub(r"[^\w\s]", "", title)
    return re.sub(r"\s+", " ", title).strip()


def load_library(db_path):
    """Return (artists, owned_rgids, owned_titles).

    artists: list of (albumartist, mb_albumartistid, n_albums) sorted by
    n_albums desc. owned_* are per-artist-key dicts of sets.
    """
    con = sqlite3.connect(f"file:{urllib.parse.quote(db_path)}?mode=ro", uri=True)
    try:
        rows = con.execute(
            """
            SELECT albumartist, mb_albumartistid, album, mb_releasegroupid
            FROM albums WHERE albumartist != ''
            UNION
            SELECT albumartist, mb_albumartistid, album, mb_releasegroupid
            FROM items WHERE albumartist != '' AND album != ''
            """
        ).fetchall()
    finally:
        con.close()

    counts, rgids, titles, mbids = {}, {}, {}, {}
    for artist, artist_mbid, album, rgid in rows:
        key = artist.lower()
        counts.setdefault(key, set()).add(norm_title(album))
        titles.setdefault(key, set()).add(norm_title(album))
        if rgid:
            rgids.setdefault(key, set()).add(rgid)
        if artist_mbid and key not in mbids:
            mbids[key] = (artist, artist_mbid)

    artists = sorted(
        (
            (mbids[key][0], mbids[key][1], len(albums))
            for key, albums in counts.items()
            if key in mbids
        ),
        key=lambda a: a[2],
        reverse=True,
    )
    return artists, rgids, titles


def discography(artist_mbid, include_secondary):
    """All official studio album release-groups for an artist."""
    albums, offset = [], 0
    while True:
        data = mb_get(
            "release-group",
            {"artist": artist_mbid, "type": "album", "limit": 100, "offset": offset},
        )
        groups = data.get("release-groups", [])
        for rg in groups:
            if rg.get("secondary-types") and not include_secondary:
                continue  # skip live albums, compilations, remix albums, ...
            albums.append(
                {
                    "id": rg["id"],
                    "title": rg["title"],
                    "date": rg.get("first-release-date", ""),
                }
            )
        offset += len(groups)
        if offset >= data.get("release-group-count", 0) or not groups:
            break
        time.sleep(1.1)
    return albums


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=_env.get("DB"),
                     required=_env.get("DB") is None,
                     help="path to beets library.db (or set PLAYSAI_DB)")
    ap.add_argument("--artists", type=int, default=5,
                    help="check your top N artists by owned-album count "
                         "(default 5; each artist costs >=1 MB request)")
    ap.add_argument("--artist", action="append", default=[],
                    help="check only this artist (repeatable, name match)")
    ap.add_argument("--include-secondary", action="store_true",
                    help="also list live albums, compilations, soundtracks...")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = ap.parse_args()

    artists, owned_rgids, owned_titles = load_library(args.db)
    if not artists:
        sys.exit("no MusicBrainz-tagged album artists found in the library.\n"
                 "Run `beet mbsync` / re-import with autotagging first.")

    if args.artist:
        wanted = {a.lower() for a in args.artist}
        targets = [a for a in artists if a[0].lower() in wanted]
        missing_names = wanted - {a[0].lower() for a in targets}
        for name in sorted(missing_names):
            print(f"  ! not in library (or missing MBID): {name}", file=sys.stderr)
    else:
        targets = artists[: args.artists]

    report = []
    for i, (name, mbid, n_owned) in enumerate(targets):
        if i:
            time.sleep(1.1)
        try:
            disco = discography(mbid, args.include_secondary)
        except Exception as exc:
            print(f"  ! {name}: {exc}", file=sys.stderr)
            continue
        key = name.lower()
        have_ids = owned_rgids.get(key, set())
        have_titles = owned_titles.get(key, set())
        missing = [
            a for a in disco
            if a["id"] not in have_ids and norm_title(a["title"]) not in have_titles
        ]
        missing.sort(key=lambda a: a["date"])
        report.append(
            {"artist": name, "owned": n_owned, "total": len(disco),
             "missing": missing}
        )

    if args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
        return

    for entry in report:
        print(f"\n{entry['artist']} — you own {entry['owned']} of "
              f"{entry['total']} studio albums")
        if not entry["missing"]:
            print("  complete!")
        for album in entry["missing"]:
            date = album["date"][:4] or "????"
            print(f"  [{date}] {album['title']}")


if __name__ == "__main__":
    main()
