#!/usr/bin/env python3
# multi-angle-benchmark.py — probe scenarios where USB might out-perform eMMC.
#
# Tests:
#   1. Sustained sequential read (4 GB, single stream, BUFFERED — uses readahead).
#   2. Sustained sequential write (4 GB, single stream, buffered + fsync at end).
#   3. SLC-cache stress (8 GB sequential write to detect cache exhaustion).
#   4. Concurrent random reads (8 threads × 256 random whole-file reads).
#   5. Single-large-file streaming read (2 GB file, sequential, like movie playback).
#   6. Mixed read+write (concurrent writer + reader, simulates Kodi background scan).
#
# All tests report MB/s and (where useful) timing variance over chunks.

import argparse
import os
import random
import shutil
import sys
import threading
import time

KB = 1024
MB = 1024 * 1024
GB = 1024 * 1024 * 1024


def drop_caches():
    try:
        with open("/proc/sys/vm/drop_caches", "w") as f:
            f.write("3\n")
    except (PermissionError, FileNotFoundError):
        pass


def fmt_mbs(bytes_, seconds):
    if seconds <= 0:
        return "—"
    return f"{bytes_/seconds/MB:8.2f} MB/s"


def fmt_size(n):
    for u, s in [("GB", GB), ("MB", MB), ("KB", KB)]:
        if n >= s:
            return f"{n/s:.2f} {u}"
    return f"{n} B"


# ---------------------------------------------------------------------------
# 1 + 2: sustained sequential read / write, BUFFERED (kernel readahead in play)
# ---------------------------------------------------------------------------

def make_large_file(path, size, chunk=4 * MB):
    """Create a `size`-byte file containing pseudo-random data."""
    buf = os.urandom(chunk)
    written = 0
    with open(path, "wb") as f:
        while written < size:
            n = f.write(buf[: min(chunk, size - written)])
            written += n
        f.flush()
        os.fsync(f.fileno())


def sustained_seq_read(path, total, chunk=1 * MB):
    """Buffered sequential read — exercises kernel readahead."""
    drop_caches()
    n = 0
    t0 = time.perf_counter()
    with open(path, "rb") as f:
        while n < total:
            d = f.read(chunk)
            if not d:
                break
            n += len(d)
    t1 = time.perf_counter()
    return n, t1 - t0


def sustained_seq_write(path, total, chunk=1 * MB):
    """Buffered sequential write — measures sustained throughput including fsync."""
    drop_caches()
    buf = os.urandom(chunk)
    n = 0
    t0 = time.perf_counter()
    with open(path, "wb") as f:
        while n < total:
            w = f.write(buf[: min(chunk, total - n)])
            n += w
        f.flush()
        os.fsync(f.fileno())
    t1 = time.perf_counter()
    return n, t1 - t0


# ---------------------------------------------------------------------------
# 3: SLC-cache stress — write enough to overflow any front-cache and observe
# whether MB/s drops dramatically partway through.
# ---------------------------------------------------------------------------

def slc_stress_write(path, total, chunk=64 * MB):
    """Write `total` bytes in `chunk`-sized pieces, time each piece, report curve."""
    drop_caches()
    buf = os.urandom(chunk)
    n = 0
    per_chunk = []
    with open(path, "wb") as f:
        while n < total:
            this = min(chunk, total - n)
            t0 = time.perf_counter()
            f.write(buf[:this])
            f.flush()
            os.fsync(f.fileno())  # force each chunk to disk
            t1 = time.perf_counter()
            per_chunk.append((this, t1 - t0))
            n += this
    return n, per_chunk


# ---------------------------------------------------------------------------
# 4: Concurrent random reads — simulate multiple skin widgets loading at once.
# ---------------------------------------------------------------------------

def populate_files(workdir, count, size_min, size_max, seed):
    rng = random.Random(seed)
    blob = os.urandom(MB)
    os.makedirs(workdir, exist_ok=True)
    paths = []
    for i in range(count):
        sz = rng.randint(size_min, size_max)
        p = os.path.join(workdir, f"f{i:05d}.bin")
        with open(p, "wb") as f:
            full, rem = divmod(sz, MB)
            for _ in range(full):
                f.write(blob)
            if rem:
                f.write(blob[:rem])
        paths.append((p, sz))
    os.sync()
    return paths


def concurrent_reads(paths, n_threads, reads_per_thread, seed):
    drop_caches()
    rng = random.Random(seed)
    plans = []
    for t in range(n_threads):
        trng = random.Random(seed + t * 1009)
        plans.append([trng.choice(paths) for _ in range(reads_per_thread)])

    totals = [0] * n_threads

    def worker(idx, plan):
        n = 0
        buf = bytearray(64 * KB)
        for path, _ in plan:
            with open(path, "rb") as f:
                while True:
                    r = f.readinto(buf)
                    if not r:
                        break
                    n += r
        totals[idx] = n

    threads = [threading.Thread(target=worker, args=(i, plans[i]))
               for i in range(n_threads)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    t1 = time.perf_counter()
    return sum(totals), t1 - t0


# ---------------------------------------------------------------------------
# 5: Single large-file streaming read — movie playback simulation
# ---------------------------------------------------------------------------

def streaming_read(path, chunk=4 * MB):
    """Read whole file end-to-end, time each chunk to see if throughput stays stable."""
    drop_caches()
    per_chunk = []
    n = 0
    with open(path, "rb") as f:
        while True:
            t0 = time.perf_counter()
            d = f.read(chunk)
            t1 = time.perf_counter()
            if not d:
                break
            per_chunk.append((len(d), t1 - t0))
            n += len(d)
    return n, per_chunk


# ---------------------------------------------------------------------------
# 6: Mixed read+write
# ---------------------------------------------------------------------------

def mixed_rw(read_path, write_path, duration_s):
    """One thread reads `read_path` in a loop; another writes to `write_path`. Run for duration_s."""
    drop_caches()
    stop = threading.Event()
    counts = {"read": 0, "write": 0}

    def reader():
        buf = bytearray(1 * MB)
        while not stop.is_set():
            try:
                with open(read_path, "rb") as f:
                    while not stop.is_set():
                        r = f.readinto(buf)
                        if not r:
                            break
                        counts["read"] += r
            except OSError:
                break

    def writer():
        buf = os.urandom(1 * MB)
        with open(write_path, "wb") as f:
            while not stop.is_set():
                f.write(buf)
                counts["write"] += len(buf)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass

    tr = threading.Thread(target=reader)
    tw = threading.Thread(target=writer)
    t0 = time.perf_counter()
    tr.start()
    tw.start()
    time.sleep(duration_s)
    stop.set()
    tr.join(timeout=5)
    tw.join(timeout=5)
    t1 = time.perf_counter()
    return counts["read"], counts["write"], t1 - t0


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--big-file-gb", type=float, default=4.0)
    ap.add_argument("--slc-stress-gb", type=float, default=8.0)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--reads-per-thread", type=int, default=256)
    ap.add_argument("--small-files", type=int, default=2000)
    ap.add_argument("--mixed-duration", type=int, default=10)
    args = ap.parse_args()

    workdir = os.path.abspath(args.workdir)
    if os.path.exists(workdir):
        shutil.rmtree(workdir)
    os.makedirs(workdir)

    big = os.path.join(workdir, "big.bin")
    big_w = os.path.join(workdir, "big_w.bin")
    slc = os.path.join(workdir, "slc.bin")
    small_dir = os.path.join(workdir, "small")
    mixed_r = os.path.join(workdir, "mixed_r.bin")
    mixed_w = os.path.join(workdir, "mixed_w.bin")

    big_size = int(args.big_file_gb * GB)
    slc_size = int(args.slc_stress_gb * GB)

    print(f"=== multi-angle benchmark @ {workdir} ===\n")

    print(f"prep: creating {fmt_size(big_size)} test file...")
    t0 = time.perf_counter()
    make_large_file(big, big_size)
    print(f"  done in {time.perf_counter()-t0:.1f}s\n")

    print("[1] sustained sequential read (buffered, with kernel readahead)")
    n, dt = sustained_seq_read(big, big_size)
    print(f"    {fmt_size(n)} in {dt:.2f}s  →  {fmt_mbs(n, dt)}\n")

    print("[2] sustained sequential write (buffered, fsync at end)")
    n, dt = sustained_seq_write(big_w, big_size)
    print(f"    {fmt_size(n)} in {dt:.2f}s  →  {fmt_mbs(n, dt)}\n")
    os.unlink(big_w)

    print(f"[3] SLC-cache stress: {fmt_size(slc_size)} write, 64 MB chunks, fsync each")
    n, per_chunk = slc_stress_write(slc, slc_size)
    bucket = max(1, len(per_chunk) // 8)
    print("    chunk-window throughput (MB/s):")
    for i in range(0, len(per_chunk), bucket):
        window = per_chunk[i:i + bucket]
        wb = sum(c for c, _ in window)
        wt = sum(t for _, t in window)
        start_gb = (i * 64 * MB) / GB
        end_gb = ((i + len(window)) * 64 * MB) / GB
        print(f"      {start_gb:5.2f}-{end_gb:5.2f} GB: {fmt_mbs(wb, wt)}")
    total_b = sum(c for c, _ in per_chunk)
    total_t = sum(t for _, t in per_chunk)
    print(f"    overall: {fmt_mbs(total_b, total_t)}\n")
    os.unlink(slc)

    print(f"[4] concurrent reads: {args.threads} threads × {args.reads_per_thread} random whole files")
    print(f"    populating {args.small_files} small/medium files...")
    paths = populate_files(small_dir, args.small_files, 50 * KB, 1 * MB, seed=42)
    n, dt = concurrent_reads(paths, args.threads, args.reads_per_thread, seed=42)
    total_reads = args.threads * args.reads_per_thread
    print(f"    {total_reads} reads, {fmt_size(n)} in {dt:.2f}s")
    print(f"    aggregate: {fmt_mbs(n, dt)}  ({total_reads/dt:.0f} files/sec)\n")

    print("[5] single-large-file streaming (sequential 4 MB chunks)")
    n, per_chunk = streaming_read(big)
    bucket = max(1, len(per_chunk) // 8)
    print("    chunk-window throughput (MB/s):")
    for i in range(0, len(per_chunk), bucket):
        window = per_chunk[i:i + bucket]
        wb = sum(c for c, _ in window)
        wt = sum(t for _, t in window)
        start = (sum(c for c, _ in per_chunk[:i])) / GB
        end = start + wb / GB
        print(f"      {start:5.2f}-{end:5.2f} GB: {fmt_mbs(wb, wt)}")
    total_b = sum(c for c, _ in per_chunk)
    total_t = sum(t for _, t in per_chunk)
    print(f"    overall: {fmt_mbs(total_b, total_t)}\n")

    print(f"[6] mixed read+write (reader streams {os.path.basename(big)}, writer to new file, {args.mixed_duration}s)")
    rb, wb, dt = mixed_rw(big, mixed_w, args.mixed_duration)
    print(f"    read:  {fmt_size(rb)} → {fmt_mbs(rb, dt)}")
    print(f"    write: {fmt_size(wb)} → {fmt_mbs(wb, dt)}")
    print(f"    combined: {fmt_mbs(rb+wb, dt)}\n")
    try:
        os.unlink(mixed_w)
    except FileNotFoundError:
        pass

    # cleanup
    shutil.rmtree(workdir)
    print("Done.")


if __name__ == "__main__":
    main()
