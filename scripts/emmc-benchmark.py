#!/usr/bin/env python3
# emmc-benchmark.py — sequential + random read/write benchmark for the AM9 Pro eMMC.
#
# Run on-device (CoreELEC or Android shell):
#   python3 emmc-benchmark.py [--device /dev/mmcblk0] [--workdir /storage]
#
# Read tests run against the raw block device (read-only). Write tests run against
# a temp file on --workdir (default /storage), which is on the eMMC once CE is
# installed there. All tests use O_DIRECT to bypass the page cache; mmap-backed
# buffers satisfy the O_DIRECT alignment requirement.

import argparse
import mmap
import os
import random
import sys
import time


KB = 1024
MB = 1024 * 1024
GB = 1024 * 1024 * 1024


def aligned_buf(size, fill=b"\x5a"):
    """Return a page-aligned mutable buffer of `size` bytes."""
    mm = mmap.mmap(-1, size)
    mm.write(fill * size)
    mm.seek(0)
    return mm


def drop_caches():
    try:
        with open("/proc/sys/vm/drop_caches", "w") as f:
            f.write("3\n")
    except (PermissionError, FileNotFoundError):
        pass


def open_direct(path, flags):
    o_direct = getattr(os, "O_DIRECT", 0o40000)  # Linux value
    try:
        return os.open(path, flags | o_direct)
    except OSError as e:
        if e.errno in (22, 95):  # EINVAL / ENOTSUP — fallback without O_DIRECT
            print(f"  (O_DIRECT not supported on {path}, falling back to buffered I/O)", file=sys.stderr)
            return os.open(path, flags)
        raise


def fmt_throughput(bytes_, seconds):
    if seconds <= 0:
        return "—"
    return f"{bytes_/seconds/MB:8.2f} MB/s"


def fmt_iops(ops, seconds):
    if seconds <= 0:
        return "—"
    return f"{ops/seconds:10.1f} IOPS"


# --- Sequential tests ---------------------------------------------------------

def seq_read(device, block_size, total_bytes):
    drop_caches()
    fd = open_direct(device, os.O_RDONLY)
    try:
        buf = aligned_buf(block_size)
        n = 0
        t0 = time.monotonic()
        while n < total_bytes:
            r = os.readv(fd, [buf])
            if r <= 0:
                break
            n += r
        t1 = time.monotonic()
    finally:
        os.close(fd)
    return n, t1 - t0


def seq_write(path, block_size, total_bytes):
    drop_caches()
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    fd = open_direct(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    try:
        buf = aligned_buf(block_size)
        n = 0
        t0 = time.monotonic()
        while n < total_bytes:
            w = os.writev(fd, [buf])
            if w <= 0:
                break
            n += w
        os.fsync(fd)
        t1 = time.monotonic()
    finally:
        os.close(fd)
    return n, t1 - t0


# --- Random tests -------------------------------------------------------------

def random_read(device, block_size, op_count, span_bytes):
    drop_caches()
    fd = open_direct(device, os.O_RDONLY)
    try:
        buf = aligned_buf(block_size)
        max_off = (span_bytes // block_size) - 1
        rng = random.Random(0xA1B2C3D4)
        offsets = [rng.randint(0, max_off) * block_size for _ in range(op_count)]
        ok = 0
        t0 = time.monotonic()
        for off in offsets:
            os.lseek(fd, off, os.SEEK_SET)
            r = os.readv(fd, [buf])
            if r == block_size:
                ok += 1
        t1 = time.monotonic()
    finally:
        os.close(fd)
    return ok, t1 - t0


def random_write(path, block_size, op_count, file_size):
    drop_caches()
    # Pre-allocate the test file (fully written, so writes hit real eMMC blocks,
    # not sparse holes).
    fd = open_direct(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    chunk = aligned_buf(MB)
    written = 0
    while written < file_size:
        w = os.writev(fd, [chunk])
        if w <= 0:
            break
        written += w
    os.fsync(fd)
    os.close(fd)
    drop_caches()

    fd = open_direct(path, os.O_WRONLY)
    try:
        buf = aligned_buf(block_size)
        max_off = (file_size // block_size) - 1
        rng = random.Random(0xA1B2C3D4)
        offsets = [rng.randint(0, max_off) * block_size for _ in range(op_count)]
        ok = 0
        t0 = time.monotonic()
        for off in offsets:
            os.lseek(fd, off, os.SEEK_SET)
            w = os.writev(fd, [buf])
            if w == block_size:
                ok += 1
        os.fsync(fd)
        t1 = time.monotonic()
    finally:
        os.close(fd)
    return ok, t1 - t0


# --- Identification helpers ---------------------------------------------------

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


def block_device_size(device):
    try:
        fd = os.open(device, os.O_RDONLY)
        size = os.lseek(fd, 0, os.SEEK_END)
        os.close(fd)
        return size
    except OSError:
        return 0


def board_info():
    info = {}
    for f in ("/proc/device-tree/model", "/proc/device-tree/coreelec-dt-id"):
        try:
            with open(f, "rb") as fh:
                info[os.path.basename(f)] = fh.read().rstrip(b"\x00").decode("ascii", "replace")
        except OSError:
            pass
    return info


# ------------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="eMMC sequential + random R/W benchmark.")
    ap.add_argument("--device", default="/dev/mmcblk0",
                    help="Raw block device to read from (default: /dev/mmcblk0).")
    ap.add_argument("--workdir", default="/storage",
                    help="Directory for the write-test file (default: /storage).")
    ap.add_argument("--seq-size", type=int, default=512,
                    help="Sequential test transfer size in MB (default: 512).")
    ap.add_argument("--rand-ops", type=int, default=4096,
                    help="Number of operations per random test (default: 4096).")
    ap.add_argument("--rand-span", type=int, default=2048,
                    help="Random-test span in MB across the device/file (default: 2048).")
    ap.add_argument("--skip-write", action="store_true",
                    help="Skip write tests (read-only run, safe on any partition).")
    args = ap.parse_args()

    seq_bytes = args.seq_size * MB
    span_bytes = args.rand_span * MB
    write_file = os.path.join(args.workdir, ".emmc-bench.tmp")

    print("=== AM9 Pro eMMC benchmark ===")
    for k, v in board_info().items():
        print(f"  {k}: {v}")
    print(f"  read device : {args.device}  (size: {block_device_size(args.device)/GB:.2f} GiB)")
    if not args.skip_write:
        dev, mnt = device_for_path(args.workdir)
        print(f"  write file  : {write_file}")
        print(f"  backed by   : {dev} mounted at {mnt}")
        if "mmcblk1" in dev or "mmcblk2" in dev:
            print(f"  WARNING     : {dev} looks like SD, not eMMC. Writes will benchmark the SD card.")
    print(f"  seq xfer    : {args.seq_size} MB")
    print(f"  rand ops    : {args.rand_ops} ops over {args.rand_span} MB span")
    print()

    print(f"{'Test':<32} {'Throughput':>14}  {'IOPS':>14}  {'Time':>8}")
    print("-" * 78)

    def report(name, bytes_done, ops_done, dt, show_iops=False):
        thr = fmt_throughput(bytes_done, dt)
        iops = fmt_iops(ops_done, dt) if show_iops else " " * 14
        print(f"{name:<32} {thr:>14}  {iops:>14}  {dt:>6.2f} s")

    # --- Sequential reads ---
    for bs in (1 * MB, 4 * KB):
        n, dt = seq_read(args.device, bs, seq_bytes)
        report(f"seq read   bs={bs//KB:>4}K", n, n // bs, dt, show_iops=(bs <= 64 * KB))

    if not args.skip_write:
        for bs in (1 * MB, 4 * KB):
            n, dt = seq_write(write_file, bs, seq_bytes)
            report(f"seq write  bs={bs//KB:>4}K", n, n // bs, dt, show_iops=(bs <= 64 * KB))

    # --- Random reads ---
    for bs in (4 * KB, 16 * KB):
        ok, dt = random_read(args.device, bs, args.rand_ops, span_bytes)
        report(f"rand read  bs={bs//KB:>4}K", ok * bs, ok, dt, show_iops=True)

    if not args.skip_write:
        for bs in (4 * KB, 16 * KB):
            ok, dt = random_write(write_file, bs, args.rand_ops, span_bytes)
            report(f"rand write bs={bs//KB:>4}K", ok * bs, ok, dt, show_iops=True)

        try:
            os.unlink(write_file)
        except FileNotFoundError:
            pass

    print()
    print("Done.")


if __name__ == "__main__":
    main()
