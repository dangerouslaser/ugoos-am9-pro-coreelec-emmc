#!/usr/bin/env python3
"""In-device Amlogic .img flasher — writes partition blobs to /dev/mmcblk0pN
directly, bypassing the USB DNL path used by aml-dnl-burn.py.

Use case: "install a Ugoos OTA from CoreELEC without leaving CE."  Flashes
the Android-side partitions from an AML_PACK `.img` (bootloader_a, boot_a,
init_boot_a, vendor_boot_a, dtbo_a, logo, odm_ext_a, super) directly to the
running eMMC. CE_FLASH/CE_STORAGE are left untouched; the device reboots
straight back into CoreELEC with the new Android/bootloader underneath.

What this tool deliberately does NOT do:
  * Write the GPT (would shift partition boundaries and immediately corrupt
    the running CE_FLASH/CE_STORAGE we depend on).
  * Touch CE_FLASH (p28) or CE_STORAGE (p29).
  * Run any USB-DNL-only `.img` items (DDR/UBOOT loads, mem-stage writes).

Modes:
  --list                List partitions in the .img + the local mapping.
  --verify-only         Compare current eMMC partition SHA1 against .img
                        VERIFY items (read-only). Use this first.
  --dry-run             Print the flash plan; no writes.
  --ota                 Flash the full Android-side plan + reboot. Equivalent
                        to `--yes-i-mean-it --reboot` with the default plan.
                        Single-flag convenience for the common case of
                        "install a Ugoos OTA from CE".
  --yes-i-mean-it       Commit. Required for any writes.
  --reboot              `reboot` after a successful flash.
  --only NAME           Restrict plan to the given partition (repeatable).
  --skip NAME           Exclude the given partition (repeatable).

Refuses to write to any partition that is currently mounted.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aml_img import (
    AmlogicImage, ImgItem,
    is_sparse, unpack_sparse_size, iter_sparse_chunks,
)


# Partitions we will never touch from this tool (host runs on them).
CE_PARTITIONS = frozenset({"CE_FLASH", "CE_STORAGE"})

# `.img` item types that are not partition writes (USB protocol bits,
# GPT byte blob, config metadata, etc.). We list PARTITION items only.
WRITABLE_ITEM_TYPE = "PARTITION"

# Items skipped even if labelled PARTITION — they don't map 1:1 to a
# block-device partition (e.g. `_aml_dtb` is consumed by the bootloader
# from a hidden area, not a regular GPT entry).
SKIP_PARTITION_NAMES = frozenset({"_aml_dtb"})

# I/O chunk for streaming sparse + sha1.
IO_CHUNK = 4 * 1024 * 1024


# ── helpers ──────────────────────────────────────────────────────────────────

def make_emmc_nodes(emmc: str) -> None:
    """Create /dev/mmcblk0pN nodes for every sysfs-known partition.

    CoreELEC's kernel only auto-populates /dev nodes for partitions it
    actually mounts (CE_FLASH, CE_STORAGE). To read/write the rest we
    have to mknod them from the sysfs-reported major:minor — same pattern
    used by ce-emmc-install.sh's make_emmc_nodes().

    No-op on partitions that already have nodes. Silently ignores EPERM
    (we'll error in the actual open() if not running as root).
    """
    base = "/sys/block/" + os.path.basename(emmc)
    if not os.path.isdir(base):
        return
    for entry in sorted(os.listdir(base)):
        sub = os.path.join(base, entry)
        if not os.path.isfile(os.path.join(sub, "partition")):
            continue
        with open(os.path.join(sub, "partition")) as f:
            partnum = f.read().strip()
        if not partnum:
            continue
        with open(os.path.join(sub, "dev")) as f:
            maj, min_ = f.read().strip().split(":")
        node = f"{emmc}p{partnum}"
        if os.path.exists(node):
            continue
        try:
            os.mknod(node, stat.S_IFBLK | 0o600, os.makedev(int(maj), int(min_)))
        except (PermissionError, FileExistsError):
            pass


def parse_partition_map(emmc: str) -> dict[str, str]:
    """{partition_name: /dev/mmcblk0pN}, read from parted on the live disk."""
    out = subprocess.check_output(
        ["parted", "-sm", emmc, "unit", "B", "print"],
        text=True,
    )
    m: dict[str, str] = {}
    for line in out.splitlines():
        if ":" not in line or line.startswith("BYT"):
            continue
        fields = line.split(":")
        if len(fields) < 6 or not fields[0].isdigit():
            continue
        m[fields[5]] = f"{emmc}p{fields[0]}"
    return m


def mounted_block_devices() -> set[str]:
    with open("/proc/mounts") as f:
        return {line.split()[0] for line in f if line.startswith("/dev/")}


_SHA1_RE = re.compile(rb"(?:verify\s+)?sha1sum\s+([0-9a-fA-F]{40})")

def lookup_verify_sha1(img: AmlogicImage, partition_name: str) -> str | None:
    """Extract the SHA1 hex from the matching VERIFY item, if present."""
    for it in img.items:
        if it.type == "VERIFY" and it.name == partition_name:
            blob = img.read_at(it, 0, it.size).rstrip(b"\x00")
            m = _SHA1_RE.match(blob)
            return m.group(1).decode() if m else None
    return None


def sha1_of_block_device(path: str, length: int, chunk: int = IO_CHUNK) -> str:
    h = hashlib.sha1()
    remaining = length
    with open(path, "rb") as f:
        while remaining > 0:
            buf = f.read(min(chunk, remaining))
            if not buf:
                break
            h.update(buf)
            remaining -= len(buf)
    return h.hexdigest()


def write_raw_blob(img: AmlogicImage, item: ImgItem, target: str) -> None:
    """Stream item.size bytes from img → target, no transformation."""
    remaining = item.size
    with open(target, "wb") as out, open(img.path, "rb") as src:
        src.seek(item.offset)
        while remaining > 0:
            n = min(IO_CHUNK, remaining)
            buf = src.read(n)
            if len(buf) != n:
                raise IOError(
                    f"short read in .img at offset {src.tell():#x}: "
                    f"expected {n}, got {len(buf)}"
                )
            out.write(buf)
            remaining -= n
        out.flush()
        os.fsync(out.fileno())


# ── sparse-aware path ────────────────────────────────────────────────────────

def _sparse_blob_view(img: AmlogicImage, item: ImgItem) -> memoryview:
    """Get a memoryview of the sparse blob inside the .img (no copy)."""
    return memoryview(img._data)[item.offset: item.offset + item.size]


def write_sparse_blob(img: AmlogicImage, item: ImgItem, target: str) -> int:
    """Decode the Android sparse image at item → write to target block device.

    DONT_CARE chunks are SEEKed past, not zero-filled — preserves whatever
    was there. For a target that was previously flashed with the same .img
    this is byte-identical and avoids writing GB of zeros over eMMC. For
    a fresh target, DONT_CARE regions retain whatever the partition had
    (usually zeros after a factory format).

    Returns the unpacked size in bytes.
    """
    blob = _sparse_blob_view(img, item)
    total = unpack_sparse_size(blob)
    with open(target, "r+b") as out:
        pos = 0
        for kind, payload, blk_sz in iter_sparse_chunks(blob):
            if kind == "raw":
                out.write(bytes(payload))
                pos += len(payload)
            elif kind == "fill":
                fill, n_bytes = payload
                pattern = fill * (IO_CHUNK // 4)
                full = n_bytes // len(pattern)
                rem = n_bytes - full * len(pattern)
                for _ in range(full):
                    out.write(pattern)
                if rem:
                    out.write(pattern[:rem])
                pos += n_bytes
            elif kind == "skip":
                out.seek(payload, 1)
                pos += payload
        out.flush()
        os.fsync(out.fileno())
    return total


def sha1_of_sparse_unpacked(img: AmlogicImage, item: ImgItem) -> tuple[str, int]:
    """Return (sha1_hex, unpacked_size) for the sparse blob's logical content.

    DONT_CARE chunks are hashed as zero bytes, so the digest matches what
    a fresh-partition flash would leave on disk and what Amlogic's
    `verify sha1sum` computes after a sparse mwrite.
    """
    blob = _sparse_blob_view(img, item)
    total = unpack_sparse_size(blob)
    h = hashlib.sha1()
    zero_chunk = b"\x00" * IO_CHUNK
    pos = 0
    for kind, payload, blk_sz in iter_sparse_chunks(blob):
        if kind == "raw":
            h.update(bytes(payload))
            pos += len(payload)
        elif kind == "fill":
            fill, n_bytes = payload
            pattern = fill * (IO_CHUNK // 4)
            full = n_bytes // len(pattern)
            rem = n_bytes - full * len(pattern)
            for _ in range(full):
                h.update(pattern)
            if rem:
                h.update(pattern[:rem])
            pos += n_bytes
        elif kind == "skip":
            n = payload
            full = n // IO_CHUNK
            rem = n - full * IO_CHUNK
            for _ in range(full):
                h.update(zero_chunk)
            if rem:
                h.update(zero_chunk[:rem])
            pos += n
    return h.hexdigest(), total


# ── plan ─────────────────────────────────────────────────────────────────────

class PlanStep:
    __slots__ = ("name", "item", "target", "expected_sha1",
                 "is_sparse", "unpacked_size")

    def __init__(self, name: str, item: ImgItem, target: str,
                 expected_sha1: str | None, is_sparse: bool,
                 unpacked_size: int):
        self.name = name
        self.item = item
        self.target = target
        self.expected_sha1 = expected_sha1
        self.is_sparse = is_sparse
        self.unpacked_size = unpacked_size  # = item.size if not sparse

    @property
    def on_disk_size(self) -> int:
        """Bytes the partition occupies on the target eMMC after flash."""
        return self.unpacked_size


def build_plan(img: AmlogicImage, partmap: dict[str, str],
               only: list[str] | None, skip: set[str]) -> list[PlanStep]:
    """Build the ordered list of partitions we'd flash."""
    seen_blob_offsets: set[int] = set()
    plan: list[PlanStep] = []
    for it in img.items:
        if it.type != WRITABLE_ITEM_TYPE:
            continue
        if it.name in SKIP_PARTITION_NAMES:
            continue
        if it.name in CE_PARTITIONS:
            continue
        if only and it.name not in only:
            continue
        if it.name in skip:
            continue
        # Dedupe bootloader / bootloader_a (they point at identical bytes).
        if it.offset in seen_blob_offsets and it.name == "bootloader":
            continue
        seen_blob_offsets.add(it.offset)
        if it.name not in partmap:
            print(f"  (skip {it.name}: not in current GPT layout)",
                  file=sys.stderr)
            continue
        target = partmap[it.name]
        sha1 = lookup_verify_sha1(img, it.name)
        # Sniff sparse from the actual blob magic — not the partition name.
        blob_head = img.read_at(it, 0, min(28, it.size))
        sparse = is_sparse(blob_head)
        unpacked = unpack_sparse_size(_sparse_blob_view(img, it)) if sparse else it.size
        plan.append(PlanStep(it.name, it, target, sha1, sparse, unpacked))
    return plan


def safety_check(plan: list[PlanStep]) -> None:
    """Abort if any target block device is currently mounted."""
    mounted = mounted_block_devices()
    bad = [(s.name, s.target) for s in plan if s.target in mounted]
    if bad:
        print("ERROR: refusing to flash — these targets are currently mounted:",
              file=sys.stderr)
        for name, tgt in bad:
            print(f"  {tgt}  ({name})", file=sys.stderr)
        sys.exit(2)


# ── modes ────────────────────────────────────────────────────────────────────

def print_plan(plan: list[PlanStep]) -> None:
    print(f"  {'NAME':<20}  {'TARGET':<20}  {'ON-DISK':>14}  {'FMT':<8}  {'SHA1':<40}")
    print("  " + "-" * 110)
    total = 0
    for s in plan:
        fmt = "sparse" if s.is_sparse else "raw"
        sha = s.expected_sha1 or "(no verify item)"
        print(f"  {s.name:<20}  {s.target:<20}  {s.on_disk_size:>14,}  {fmt:<8}  {sha}")
        total += s.on_disk_size
    print()
    print(f"  total to write: {total:,} bytes ({total / 1024 / 1024:.1f} MiB)")


def _sparse_blob_sha1(img: AmlogicImage, item: ImgItem) -> str:
    """SHA1 of the raw sparse bytes in the .img — what Amlogic's
    `verify sha1sum` for a sparse mwrite is actually computed against
    (the transfer-integrity check, NOT the unpacked on-disk content).

    Verified empirically against AM9PRO_2.1.0.img's super VERIFY item.
    """
    h = hashlib.sha1()
    with open(img.path, "rb") as f:
        f.seek(item.offset)
        remaining = item.size
        while remaining > 0:
            buf = f.read(min(IO_CHUNK, remaining))
            if not buf:
                break
            h.update(buf)
            remaining -= len(buf)
    return h.hexdigest()


def verify_only(img: AmlogicImage, plan: list[PlanStep]) -> int:
    """Verify each step against the .img VERIFY hash.

    For raw partitions: SHA1 of on-disk bytes vs .img hash. End-to-end
    check that the partition currently holds what the .img claims.

    For sparse partitions: SHA1 of the .img sparse blob vs .img hash.
    This is an .img-integrity check (the same one Amlogic's USB tool
    does post-transfer), not a post-flash check — Amlogic's sparse hash
    convention is over the wire bytes, not the unpacked partition. We
    cannot easily SHA1 the partition itself because the unpacking
    convention for DONT_CARE chunks isn't deterministic across tools.
    """
    n_match = n_diff = n_unknown = 0
    for s in plan:
        if not s.expected_sha1:
            print(f"  {s.name:<20}  ?  (.img has no SHA1 for this partition)")
            n_unknown += 1
            continue
        if s.is_sparse:
            got = _sparse_blob_sha1(img, s.item)
            label = "img-blob-sha1"
        else:
            got = sha1_of_block_device(s.target, s.on_disk_size)
            label = "on-disk-sha1"
        if got == s.expected_sha1:
            print(f"  {s.name:<20}  ✓  {label} matches .img ({got})")
            n_match += 1
        else:
            print(f"  {s.name:<20}  ✗  {label} {got}")
            print(f"  {'':<20}     .img        {s.expected_sha1}")
            n_diff += 1
    print()
    print(f"  match: {n_match}   differ: {n_diff}   unknown: {n_unknown}")
    if n_diff:
        return 1
    if n_unknown:
        return 2
    return 0


def execute(img: AmlogicImage, plan: list[PlanStep]) -> None:
    for s in plan:
        fmt = "sparse" if s.is_sparse else "raw"
        print(f"  → {s.name} ({fmt}, {s.on_disk_size:,} bytes) → {s.target}")
        # Pre-write integrity check on sparse: cheap insurance that the .img
        # we're about to decode hasn't been corrupted on disk.
        if s.is_sparse and s.expected_sha1:
            img_hash = _sparse_blob_sha1(img, s.item)
            if img_hash != s.expected_sha1:
                print(f"  ✗ .img sparse blob for {s.name} fails integrity check:",
                      file=sys.stderr)
                print(f"      expected {s.expected_sha1}", file=sys.stderr)
                print(f"      got      {img_hash}", file=sys.stderr)
                sys.exit(3)
        # Write
        if s.is_sparse:
            write_sparse_blob(img, s.item, s.target)
        else:
            write_raw_blob(img, s.item, s.target)
        # Post-write check on raw: read back + hash.
        if not s.is_sparse and s.expected_sha1:
            got = sha1_of_block_device(s.target, s.on_disk_size)
            if got != s.expected_sha1:
                print(f"  ✗ sha1 MISMATCH for {s.name} after write:",
                      file=sys.stderr)
                print(f"      expected {s.expected_sha1}", file=sys.stderr)
                print(f"      got      {got}", file=sys.stderr)
                sys.exit(3)
            print(f"  ✓ {s.name}: on-disk sha1 verified")
        elif s.is_sparse and s.expected_sha1:
            print(f"  ✓ {s.name}: .img blob sha1 verified pre-write "
                  f"({s.expected_sha1})")
        else:
            print(f"  (no SHA1 in .img — wrote bytes, no verify)")


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(
        description="In-device Amlogic .img flasher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("img", help="AML_PACK .img file")
    p.add_argument("--emmc", default="/dev/mmcblk0",
                   help="eMMC block device (default: /dev/mmcblk0)")
    p.add_argument("--only", action="append", default=[],
                   help="Restrict plan to these partition names (repeatable)")
    p.add_argument("--skip", action="append", default=[],
                   help="Exclude these partition names (repeatable)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="List partition plan; exit")
    mode.add_argument("--verify-only", action="store_true",
                      help="SHA1 each target partition vs .img; no writes")
    mode.add_argument("--dry-run", action="store_true",
                      help="Print full plan; no writes")
    mode.add_argument("--ota", action="store_true",
                      help="Flash full Android-side plan + reboot (combines "
                           "--yes-i-mean-it and --reboot for the common case)")
    p.add_argument("--yes-i-mean-it", action="store_true",
                   help="Required to actually write to eMMC")
    p.add_argument("--reboot", action="store_true",
                   help="Reboot after successful flash")
    args = p.parse_args()

    # --ota is a convenience: implies --yes-i-mean-it + --reboot. It also
    # forbids --only/--skip — if the user wants to surgically restrict the
    # plan they should not be using the shorthand.
    if args.ota:
        if args.only or args.skip:
            print("--ota does not accept --only or --skip; use --yes-i-mean-it "
                  "--reboot --only/--skip explicitly for a custom plan.",
                  file=sys.stderr)
            return 1
        args.yes_i_mean_it = True
        args.reboot = True

    if not os.path.exists(args.emmc):
        print(f"FAIL: {args.emmc} does not exist", file=sys.stderr)
        return 1

    img = AmlogicImage(args.img)
    make_emmc_nodes(args.emmc)
    partmap = parse_partition_map(args.emmc)
    plan = build_plan(img, partmap, args.only or None, set(args.skip))

    if not plan:
        print("Plan is empty — nothing to do.", file=sys.stderr)
        return 0

    print(f"Plan for {args.img} → {args.emmc}:\n")
    print_plan(plan)
    print()

    if args.list:
        return 0

    if args.verify_only:
        print("Verifying on-disk SHA1 against .img …\n")
        return verify_only(img, plan)

    if args.dry_run:
        print("(--dry-run: no writes)")
        return 0

    if not args.yes_i_mean_it:
        print("Refusing to write without --yes-i-mean-it.", file=sys.stderr)
        print("Re-run with --yes-i-mean-it to commit.", file=sys.stderr)
        return 1

    safety_check(plan)

    print("Flashing …\n")
    execute(img, plan)
    print("\n✓ all writes completed")

    if args.reboot:
        subprocess.run(["sync"])
        print("rebooting …")
        os.execvp("reboot", ["reboot"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
