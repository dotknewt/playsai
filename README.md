# playsai — smart playlists & music discovery for beets

A toolkit for driving playlists out of a (large) beets `library.db` with the
[smartplaylist](https://beets.readthedocs.io/en/stable/plugins/smartplaylist.html)
plugin, plus scripts that use the same database to **discover music you don't
have yet**.

```
config/config.yaml              # drop-in beets config: plugins + 13 smart playlists
scripts/discover.py             # "artists like my favorites that I don't own" (Deezer/Last.fm)
scripts/gaps.py                 # albums missing from artists you already collect (MusicBrainz)
scripts/mixtape.py              # weighted-random mixes with artist spacing (beets query API)
scripts/fingerprint_parallel.py # AcoustID fingerprints, N fpcalc workers, 1 db writer
```

The discovery scripts read `library.db` **read-only**, so they are safe to
run against a live library. `fingerprint_parallel.py` is the one writer in
the repo — it batches its writes through a single process (see below).

## Quick start

```sh
pip install -r requirements.txt          # beets + pylast

# Merge config/config.yaml into your ~/.config/beets/config.yaml
# (at minimum: the plugins, types, and smartplaylist sections), then:
beet splupdate                           # generates every playlist as .m3u
```

`smartplaylist.auto` is on by default, so after the first `splupdate` the
playlists regenerate themselves whenever the database changes — imports,
`beet modify`, `beet lastimport`, and so on.

## The playlists

| Playlist | Query idea |
| --- | --- |
| `recently-added` | `added:-4w..` — everything imported in the last month |
| `fresh-favorites` | recent imports you already rated 8+ |
| `favorites` | `rating:8..10` across the whole library |
| `never-played` | `play_count:0` **or** the field was never set (union query) |
| `rediscover` | played 5+ times, but not once in the last 6 months |
| `genre-*` / `mood-chill` | genre tags from lastgenre, moods you hand-tag |
| `decade-1990s` / `-2000s` | `year:1990..1999` |
| `favorite-albums` | an `album_query`, so whole albums instead of tracks |
| `short-and-sweet` | `length:..180` |

These are all just [beets queries](https://beets.readthedocs.io/en/stable/reference/query.html)
— edit them freely. Three mechanics worth knowing, because they unlock most
enhancements:

1. **Flexible attributes.** Any `beet modify key=value <query>` invents a new
   field. `mood=chill`, `energy=high`, `context=running`, `wishlist=1` — tag
   once, and a one-line playlist definition turns it into a self-maintaining
   playlist.
2. **The `types` plugin.** Declaring `rating: int` / `last_played: date`
   makes range queries (`rating:8..10`) and date math (`last_played:..-6m`)
   work on those invented fields. Without it they're compared as strings.
3. **Union queries.** A YAML list under `query:` is an OR of queries, e.g.
   `never-played` combines `play_count:0` with `^play_count::.` (regex
   negation ≈ "field unset").

### Getting play/rating data in

The `rediscover`, `favorites`, and `never-played` playlists are only as good
as the listening data behind them:

* **`beet lastimport`** pulls per-track play counts from a Last.fm account.
* **mpdstats plugin** — if you listen through MPD, it maintains
  `play_count`, `skip_count`, `last_played`, and even an auto-`rating` live.
* Otherwise, rate manually as you listen: `beet modify rating=9 title:...`.

## Discovering new music

### Similar artists you don't own — `scripts/discover.py`

Ranks your library's artists by play count (falling back to track count),
asks Deezer (no API key needed) or Last.fm for similar artists, drops
everyone you already have, and prints a ranked shortlist with the reasoning:

```
$ python3 scripts/discover.py --db ~/.config/beets/library.db
  1. Autechre       (score 1.85; similar to Aphex Twin, Boards of Canada)
  2. Squarepusher   (score 1.70; similar to Aphex Twin, Boards of Canada)
  3. Jeff Buckley   (score 1.00; similar to Radiohead)
  ...
```

Useful flags: `--artist "Boards of Canada"` to seed from specific artists
instead of your top ones, `--seeds N` / `--limit N`, `--json` for piping,
`--source lastfm --lastfm-key KEY` for Last.fm's similarity graph.

### Albums you're missing — `scripts/gaps.py`

For artists you already collect, diffs your library against their full
MusicBrainz discography (by release-group ID when your library is MB-tagged,
fuzzy title match otherwise):

```
$ python3 scripts/gaps.py --db library.db --artist Radiohead
Radiohead — you own 2 of 10 studio albums
  [1994] The Bends
  [2007] In Rainbows
  ...
```

By default only official studio albums count; `--include-secondary` adds
live albums, compilations, and soundtracks. Respects MusicBrainz's 1
request/second rate limit.

### Weighted-random mixes — `scripts/mixtape.py`

What declarative smartplaylist queries can't do: weighted shuffling
(higher-rated and long-unplayed tracks are favored), no artist repeated
within N tracks, and a target duration. Takes any beets query as the pool:

```
$ python3 scripts/mixtape.py --db library.db --out daily.m3u --minutes 90
$ python3 scripts/mixtape.py --db library.db --out chill.m3u \
      --query 'genre:ambient' --minutes 45 --spacing 5
```

Run it from cron for a fresh "daily mix" every morning.

## Fast AcoustID fingerprinting — `scripts/fingerprint_parallel.py`

`beet fingerprint` (the [chroma plugin](https://beets.readthedocs.io/en/stable/plugins/chroma.html))
decodes one track at a time, so fingerprinting a large library takes days.
And running several `beet` processes against one `library.db` makes them
fight over SQLite's single write lock. This script does the same work with
the bottlenecks removed: N parallel `fpcalc` processes decode audio, while
one thread stores results into the `acoustid_fingerprint` field in batched
transactions — every core busy, exactly one database writer.

```
$ python3 scripts/fingerprint_parallel.py --db ~/.config/beets/library.db --jobs 8
fingerprinting 12480 tracks with 8 fpcalc workers
  310/12480 done, 2 failed, 9.8 tracks/s, ETA 20.7 min
```

Needs Chromaprint's CLI: `apt install libchromaprint-tools` (Debian/Ubuntu)
or `brew install chromaprint` (macOS). Worth knowing:

* **Resumable.** Only tracks without a stored fingerprint are selected, so
  rerun after Ctrl-C (which flushes what finished), a crash, or new imports
  and it continues where it stopped.
* **Same output as beets.** It stores the identical fingerprint string the
  chroma plugin would, so afterwards `beet submit` pushes them to AcoustID
  without re-decoding and chroma reuses them during matching.
* `--query 'added:-1m..'` restricts to any beets query; `--force`
  re-fingerprints; `--write` also writes tags into the audio files;
  `--limit 50` for a trial run.
* Safe alongside the read-only scripts here, but don't run it during a
  `beet import` — two writers serialize on SQLite's lock at best.

## Automation

* smartplaylist already regenerates on database changes (`auto: yes`).
* Cron a daily mix and a weekly discovery report:

  ```cron
  0 6 * * *  python3 ~/playsai/scripts/mixtape.py --db ~/.config/beets/library.db --out ~/Music/playlists/daily-mix.m3u --minutes 90
  0 8 * * 1  python3 ~/playsai/scripts/discover.py --db ~/.config/beets/library.db --json > ~/Music/discover-weekly.json
  ```

* The **hook plugin** (already enabled) can chain actions, e.g. re-run
  lastgenre or push playlists to a device after every import:

  ```yaml
  hook:
    hooks:
      - event: import
        command: echo "imported into {lib.path}"
  ```

## Ideas for extending further

* **Genre hygiene** — `beet lastgenre` gives every album a canonical genre so
  the `genre-*` playlists stay meaningful; add `genres: true` for
  multi-genre tags.
* **BPM/tempo playlists** — the `autobpm` plugin computes BPM locally
  (librosa); then `query: 'bpm:170..'` is a running playlist.
* **Export to streaming** — the `spotify` plugin turns any beets query into a
  Spotify playlist (`beet spotify -m open <query>`), handy for checking out
  `discover.py` results before buying.
* **Wishlist workflow** — pipe `discover.py --json` / `gaps.py --json` into a
  file, and after acquiring albums, tag imports with
  `beet modify wishlist_hit=1 album:...` to feed a "new discoveries" playlist.
* **Duplicates & health** — `beet duplicates`, `beet missing`, and the `web`
  plugin (browse/query the library from a browser) all run off the same db.
* **ListenBrainz** — scrobble there and periodically import listens to keep
  `play_count`/`last_played` fresh without Last.fm.
