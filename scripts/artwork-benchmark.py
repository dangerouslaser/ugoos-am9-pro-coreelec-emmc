#!/usr/bin/env python3
# artwork-benchmark.py — workload-shaped benchmark for skin-widget artwork access.
#
# Models Kodi's typical artwork I/O pattern: many small/medium image files in a
# sharded directory tree, accessed as "open random file → read end-to-end → close".
# This is the pattern that drives skin-widget refresh latency, not raw 4K random I/O.
#
# Usage:
#   python3 artwork-benchmark.py --workdir /storage/art-bench
#   python3 artwork-benchmark.py --workdir /var/media/USB/art-bench
#
# Phases:
#   1. Populate — write N files in M shards (skipped if already populated and
#      --reuse is given; default re-populates so tests are reproducible).
#   2. Random whole-file read — drop caches, time `read-everything` of K random
#      files. Reports MB/s, files/sec, and p50/p95/p99 open-to-close latency.

import argparse
import os
import random
import shutil
import sys
import time


KB = 1024
MB = 1024 * 1024


# Distribution roughly matches a real Kodi artwork directory:
#   - thumbnails (small JPEG): 20-60 KB
#   - posters / clearlogos:    80-200 KB
#   - 1080p fanart / backdrop: 200-600 KB
#   - 4K fanart / hi-res keyart: 800 KB - 2 MB
SIZE_BUCKETS = [
    # (weight, min_bytes, max_bytes, label)
    (40, 20 * KB,  60 * KB,  "thumbnail"),
    (30, 80 * KB,  200 * KB, "poster"),
    (20, 200 * KB, 600 * KB, "fanart"),
    (10, 800 * KB, 2 * MB,   "fanart_4k"),
]


def pick_size(rng):
    total = sum(w for w, *_ in SIZE_BUCKETS)
    r = rng.randint(1, total)
    cum = 0
    for w, lo, hi, label in SIZE_BUCKETS:
        cum += w
        if r <= cum:
            return rng.randint(lo, hi), label
    return SIZE_BUCKETS[-1][1], SIZE_BUCKETS[-1][3]


def drop_caches():
    try:
        with open("/proc/sys/vm/drop_caches", "w") as f:
            f.write("3\n")
    except (PermissionError, FileNotFoundError):
        pass


def fmt_bytes(n):
    if n >= MB:
        return f"{n/MB:.2f} MB"
    if n >= KB:
        return f"{n/KB:.1f} KB"
    return f"{n} B"


def populate(workdir, n_files, n_shards, seed):
    """Create n_files files distributed across n_shards subdirectories."""
    rng = random.Random(seed)
    # Pre-generate a single MB of pseudo-random payload, then slice/repeat as
    # needed. Per-file random generation would dominate runtime on slow CPUs.
    blob = rng.randbytes(MB) if hasattr(rng, "randbytes") else \
           bytes(rng.randint(0, 255) for _ in range(MB))

    for s in range(n_shards):
        os.makedirs(os.path.join(workdir, f"{s:02x}"), exist_ok=True)

    paths = []
    total_bytes = 0
    sizes_by_label = {label: 0 for _, _, _, label in SIZE_BUCKETS}
    t0 = time.monotonic()
    for i in range(n_files):
        size, label = pick_size(rng)
        sizes_by_label[label] += 1
        shard = rng.randint(0, n_shards - 1)
        path = os.path.join(workdir, f"{shard:02x}", f"art_{i:05d}.bin")
        # Build the file content by repeating the blob (cheap and unique-ish)
        full, rem = divmod(size, MB)
        with open(path, "wb") as f:
            for _ in range(full):
                f.write(blob)
            if rem:
                f.write(blob[:rem])
        paths.append((path, size))
        total_bytes += size
    # Ensure data is on disk and metadata is too
    os.sync()
    t1 = time.monotonic()

    print(f"  populated {n_files} files in {n_shards} shards, "
          f"{fmt_bytes(total_bytes)} total, {t1-t0:.1f}s")
    print(f"  size mix: " + ", ".join(f"{k}={v}" for k, v in sizes_by_label.items()))
    return paths


def random_read_test(paths, n_reads, seed, read_block=64 * KB):
    """Open random files from `paths`, read end-to-end, close. Time each open-to-close."""
    drop_caches()
    rng = random.Random(seed)
    picks = [rng.choice(paths) for _ in range(n_reads)]

    latencies_us = []
    total_bytes = 0
    buf = bytearray(read_block)
    mv = memoryview(buf)

    t0 = time.monotonic()
    for path, expected_size in picks:
        op_t0 = time.monotonic()
        fd = os.open(path, os.O_RDONLY)
        try:
            n = 0
            while True:
                r = os.readinto(fd, mv) if hasattr(os, "readinto") else len(os.read(fd, read_block))
                if r <= 0:
                    break
                n += r
        finally:
            os.close(fd)
        op_t1 = time.monotonic()
        latencies_us.append((op_t1 - op_t0) * 1_000_000)
        total_bytes += n
    t1 = time.monotonic()

    elapsed = t1 - t0
    return {
        "elapsed_s": elapsed,
        "bytes": total_bytes,
        "files": n_reads,
        "mb_per_s": total_bytes / elapsed / MB,
        "files_per_s": n_reads / elapsed,
        "lat_us": sorted(latencies_us),
    }


def pct(sorted_list, p):
    if not sorted_list:
        return 0.0
    k = max(0, min(len(sorted_list) - 1, int(round(p / 100 * (len(sorted_list) - 1)))))
    return sorted_list[k]


def device_for_path(path):
    path = os.path.realpath(path)
    st_dev = os.stat(path).st_dev
    with open("/proc/mounts") as f:
        for line in f:
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                if os.stat(parts[1]).st_dev == st_dev:
                    return parts[0], parts[1]
            except OSError:
                continue
    return "?", "?"


def main():
    ap = argparse.ArgumentParser(description="Skin-widget artwork access benchmark.")
    ap.add_argument("--workdir", required=True,
                    help="Directory to populate and read from. Will be wiped unless --reuse.")
    ap.add_argument("--files", type=int, default=5000,
                    help="Number of files to populate (default: 5000).")
    ap.add_argument("--shards", type=int, default=16,
                    help="Number of subdirectory shards (default: 16, matching Kodi's hex sharding).")
    ap.add_argument("--reads", type=int, default=2000,
                    help="Number of random file reads in the timed phase (default: 2000).")
    ap.add_argument("--seed", type=int, default=0x4B0D1,
                    help="RNG seed for size mix + read order (default: 0x4B0D1).")
    ap.add_argument("--reuse", action="store_true",
                    help="Reuse existing files in workdir (skip populate phase if present).")
    args = ap.parse_args()

    workdir = os.path.abspath(args.workdir)
    dev, mnt = device_for_path(os.path.dirname(workdir) if not os.path.exists(workdir)
                               else workdir)

    print("=== skin-widget artwork benchmark ===")
    print(f"  workdir : {workdir}")
    print(f"  backed by: {dev} mounted at {mnt}")
    print(f"  files   : {args.files}  shards: {args.shards}")
    print(f"  reads   : {args.reads}")
    print()

    if not args.reuse:
        if os.path.exists(workdir):
            print(f"  wiping {workdir}...")
            shutil.rmtree(workdir)
        os.makedirs(workdir)
        print("Phase 1: populate")
        paths = populate(workdir, args.files, args.shards, args.seed)
    else:
        print("Phase 1: scanning existing files")
        paths = []
        for root, _, files in os.walk(workdir):
            for fname in files:
                p = os.path.join(root, fname)
                paths.append((p, os.stat(p).st_size))
        if not paths:
            print("ERROR: --reuse but no files found; run without --reuse first.", file=sys.stderr)
            sys.exit(1)
        print(f"  found {len(paths)} files")

    print()
    print("Phase 2: random whole-file reads (caches dropped)")
    result = random_read_test(paths, args.reads, args.seed)

    lats = result["lat_us"]
    print()
    print(f"  read {result['files']} files, {fmt_bytes(result['bytes'])} in {result['elapsed_s']:.2f}s")
    print(f"  throughput     : {result['mb_per_s']:8.2f} MB/s")
    print(f"  files per sec  : {result['files_per_s']:8.1f}")
    print(f"  mean file size : {fmt_bytes(result['bytes'] // result['files'])}")
    print()
    print(f"  open-to-close latency (microseconds):")
    print(f"    p50  : {pct(lats, 50):8.0f} us")
    print(f"    p90  : {pct(lats, 90):8.0f} us")
    print(f"    p95  : {pct(lats, 95):8.0f} us")
    print(f"    p99  : {pct(lats, 99):8.0f} us")
    print(f"    max  : {lats[-1]:8.0f} us")
    print()
    print("Done.")


if __name__ == "__main__":
    main()
