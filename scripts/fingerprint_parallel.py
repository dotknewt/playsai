#!/usr/bin/env python3
"""Compute Chromaprint/AcoustID fingerprints with parallel fpcalc workers.

`beet fingerprint` (the chroma plugin) decodes one track at a time, so a big
backlog takes days. This script produces the exact same result — the
fingerprint string beets stores in each item's `acoustid_fingerprint` field —
but runs N `fpcalc` processes in parallel and funnels every database write
through this single process in batched transactions. SQLite allows only one
writer at a time, so this shape saturates all your cores without several
`beet` processes fighting over the library lock.

Resumable by construction: only tracks with no stored fingerprint are
selected, so rerunning after Ctrl-C, a crash, or new imports continues where
the last run stopped. An interrupt flushes everything already computed.

Once fingerprints are stored, the chroma commands reuse them instead of
re-decoding: `beet submit` sends them straight to AcoustID (that command
needs the chroma plugin enabled, pyacoustid installed, and an AcoustID API
key). Import-time matching still decodes files fresh — chroma fingerprints
the file on disk during import, before it is in the library.

Requires Chromaprint's fpcalc binary on $PATH:
  Debian/Ubuntu: apt install libchromaprint-tools
  macOS:         brew install chromaprint

--db can also come from a PLAYSAI_DB entry in a .env file (current
directory, then the repo root) — see scripts/_env.py. A CLI flag always
overrides the .env value.

Examples:
  python3 scripts/fingerprint_parallel.py --db ~/.config/beets/library.db
  python3 scripts/fingerprint_parallel.py --db library.db --jobs 8 \\
      --query 'added:-1m..'
  python3 scripts/fingerprint_parallel.py --db library.db --write
"""

import argparse
import concurrent.futures
import os
import shutil
import signal
import subprocess
import sys
import time

import _env

try:
    from beets.dbcore.query import InvalidQueryError
    from beets.library import Library
except ImportError:
    sys.exit("beets is required: pip install beets")


def run_fpcalc(fpcalc, path, length, timeout):
    """Fingerprint one file. Returns (fingerprint, error), one of them None."""
    try:
        os.stat(path)
    except FileNotFoundError:
        return None, "file does not exist"
    except OSError as exc:
        return None, f"cannot access file: {exc.strerror}"
    cmd = [fpcalc]
    if length:
        cmd += ["-length", str(length)]
    cmd.append(path)
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"fpcalc timed out after {timeout:g}s"
    except OSError as exc:
        return None, f"could not run fpcalc: {exc}"
    if proc.returncode:
        # stderr may quote the filename, which need not be valid UTF-8
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        return None, (f"fpcalc exited {proc.returncode}"
                      + (f": {detail[-1]}" if detail else ""))
    # Plain output is DURATION=... / FINGERPRINT=... lines; the FINGERPRINT
    # value is the same compressed string chroma stores via pyacoustid.
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("FINGERPRINT="):
            fp = line[len("FINGERPRINT="):].strip()
            if fp:
                return fp, None
    return None, "no FINGERPRINT in fpcalc output"


def flush(lib, buffered, write_tags):
    """Store buffered (item, fingerprint) pairs; one transaction, then clear."""
    if not buffered:
        return
    for item, fp in buffered:
        item.acoustid_fingerprint = fp
        if write_tags:
            item.try_write()  # file I/O stays outside the db transaction
    # A Ctrl-C landing inside beets' Transaction.__enter__/__exit__ leaves its
    # per-thread tx stack or db lock in a state where a later transaction
    # silently never commits, or deadlocks. Hold SIGINT until the commit is
    # through; the pending interrupt is delivered right after the unmask.
    can_mask = hasattr(signal, "pthread_sigmask")
    if can_mask:
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
    try:
        with lib.transaction():
            for item, _fp in buffered:
                item.store()
        buffered.clear()
    finally:
        if can_mask:
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=_env.get("DB"),
                     required=_env.get("DB") is None,
                     help="path to beets library.db (or set PLAYSAI_DB "
                          "in .env — see scripts/_env.py)")
    ap.add_argument("--query", default="",
                    help="restrict to a beets query "
                         "(e.g. 'genre:rock' or 'path:/mnt/music/A')")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 2,
                    help="parallel fpcalc processes (default: CPU count)")
    ap.add_argument("--batch", type=int, default=100,
                    help="fingerprints stored per transaction (default 100)")
    ap.add_argument("--force", action="store_true",
                    help="re-fingerprint tracks that already have one")
    ap.add_argument("--write", action="store_true",
                    help="also write tags to the audio files themselves, "
                         "like `beet fingerprint` with import.write on")
    ap.add_argument("--length", type=int, default=0,
                    help="seconds of audio to decode per track "
                         "(fpcalc's default is 120; lower is faster)")
    ap.add_argument("--timeout", type=float, default=300.0,
                    help="per-file fpcalc timeout in seconds (default 300)")
    ap.add_argument("--fpcalc", default="fpcalc", help="fpcalc binary to use")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after N tracks (for a quick trial run)")
    args = ap.parse_args()
    if args.jobs < 1 or args.batch < 1:
        sys.exit("--jobs and --batch must be at least 1")

    fpcalc = shutil.which(args.fpcalc)
    if not fpcalc:
        sys.exit(f"{args.fpcalc!r} not found — install Chromaprint's CLI:\n"
                 "  Debian/Ubuntu: apt install libchromaprint-tools\n"
                 "  macOS:         brew install chromaprint")

    if not os.path.exists(args.db):
        # Library() would silently create a fresh empty db on a typoed path
        sys.exit(f"library database not found: {args.db}")
    lib = Library(args.db)
    try:
        items = list(lib.items(args.query))
    except InvalidQueryError as exc:
        sys.exit(f"invalid beets query: {exc}")
    if not args.force:
        # Filter by truthiness like chroma does, not with a SQL-side
        # '^acoustid_fingerprint::.' term: that also catches rows left NULL
        # by old library migrations, and a user query's comma-OR branches
        # can't bypass a Python-side filter.
        items = [it for it in items if not it.acoustid_fingerprint]

    # chroma won't fingerprint tracks without a duration (AcoustID needs it),
    # so mirror that instead of storing fingerprints `beet submit` can't use.
    no_duration = sum(1 for it in items if not it.length)
    items = [it for it in items if it.length]
    if no_duration:
        print(f"skipping {no_duration} tracks with no duration in the "
              "library (chroma can't use them either)", file=sys.stderr)
    if args.limit:
        items = items[:args.limit]
    if not items:
        if no_duration:
            print("nothing to do: only tracks without a duration remain")
        elif args.force:
            print("nothing to do: no matching tracks")
        else:
            print("nothing to do: every matching track already has a "
                  "fingerprint")
        return

    total = len(items)
    print(f"fingerprinting {total} tracks with {args.jobs} fpcalc workers")

    done = failed = 0
    buffered = []
    interrupted = False
    start = time.time()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs)
    try:
        futures = {
            pool.submit(run_fpcalc, fpcalc, os.fsdecode(item.path),
                        args.length, args.timeout): item
            for item in items
        }
        for future in concurrent.futures.as_completed(futures):
            item = futures[future]
            try:
                fp, err = future.result()
            except Exception as exc:  # one bad file must not kill the run
                fp, err = None, f"unexpected error: {exc}"
            done += 1
            if err:
                failed += 1
                print(f"\n  ! {os.fsdecode(item.path)}: {err}",
                      file=sys.stderr)
            else:
                buffered.append((item, fp))
                if len(buffered) >= args.batch:
                    flush(lib, buffered, args.write)
            rate = done / max(time.time() - start, 1e-9)
            eta = (total - done) / rate / 60.0
            print(f"\r  {done}/{total} done, {failed} failed, "
                  f"{rate:.1f} tracks/s, ETA {eta:.1f} min ",
                  end="", file=sys.stderr, flush=True)
        pool.shutdown()
    except KeyboardInterrupt:
        interrupted = True
        pool.shutdown(wait=False, cancel_futures=True)
        print("\ninterrupted — storing what finished; rerun to continue",
              file=sys.stderr)
    except BaseException:
        # Cancel the queued backlog: without this, the executor's atexit
        # hook would run fpcalc on every remaining track after the traceback.
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    finally:
        try:
            flush(lib, buffered, args.write)
        except Exception as exc:
            print(f"\ncould not store {len(buffered)} finished fingerprints "
                  f"({exc}); rerun to recompute them", file=sys.stderr)

    elapsed = time.time() - start
    print(f"\nstored {done - failed} fingerprints ({failed} failed) "
          f"in {elapsed / 60:.1f} min "
          f"({done / max(elapsed, 1e-9):.1f} tracks/s)")
    if interrupted:
        sys.exit(130)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
