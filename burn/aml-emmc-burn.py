#!/usr/bin/env python3
"""In-device Amlogic .img flasher — writes partition blobs to /dev/mmcblk0pN
directly, bypassing the USB DNL path used by aml-dnl-burn.py.

Use case: "install a Ugoos firmware update from CoreELEC without leaving
CE."  Flashes everything a factory `.img` carries for the eMMC, directly
from the running system:

  1. Android-side GPT partitions (bootloader_a, boot_a, init_boot_a,
     vendor_boot_a, dtbo_a, logo, odm_ext_a, super) → /dev/mmcblk0pN
  2. The Android DTB (`_aml_dtb`) → both 256 KB slots in `reserved` (p1),
     in U-Boot's checksummed `aml_dtb_rsv` format
  3. The bootloader → eMMC hardware boot partitions boot0 and boot1, which
     is where the S905X5 BootROM actually loads it from (flashing
     bootloader_a alone changes nothing at boot)

CE_FLASH/CE_STORAGE are left untouched; the device reboots straight back
into CoreELEC with the new bootloader/Android underneath.

What this tool deliberately does NOT do:
  * Write the GPT (would shift partition boundaries and immediately corrupt
    the running CE_FLASH/CE_STORAGE we depend on).
  * Touch CE_FLASH (p28) or CE_STORAGE (p29).
  * Run any USB-DNL-only `.img` items (DDR/UBOOT loads, mem-stage writes).

Modes:
  --list                List the plan (partitions, DTB slots, boot area).
  --verify-only         Compare current eMMC contents against the .img
                        VERIFY hashes (read-only). Use this first.
  --dry-run             Print the flash plan; no writes.
  --ota                 Flash the full plan + reboot. Equivalent to
                        `--yes-i-mean-it --reboot` with the default plan.
                        Single-flag convenience for "install a Ugoos
                        firmware from CE".
  --yes-i-mean-it       Commit. Required for any writes.
  --reboot              `reboot` after a successful flash.
  --only NAME           Restrict the partition plan to NAME (repeatable).
  --skip NAME           Exclude partition NAME (repeatable).
  --no-dtb              Leave the `reserved` DTB slots alone.
  --no-boot-area        Leave boot0/boot1 alone (bootloader_a still written).
  --skip-boot1          Write boot0 only, keeping boot1 as the previous
                        bootloader for a manual two-phase update.

Order of writes: partitions, DTB slots, boot0, boot1 — the boot area goes
last so an interrupted run leaves the old, working bootloader in place.
Refuses to write to any partition that is currently mounted.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import struct
import subprocess
import sys

# Import aml_img from ../lib (repo layout) or from the script's own directory
# (flat layout on a device, where ugoos-fw-update.sh drops both files).
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(os.path.dirname(_HERE), "lib"), _HERE]
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


def mounted_block_devices() -> set[int]:
    """st_rdev of every mounted block device.

    Compared by device number rather than path: CoreELEC mounts its
    partitions as /dev/CE_FLASH and /dev/CE_STORAGE (symlinks), so a
    string compare against /dev/mmcblk0pN would miss them.
    """
    devs: set[int] = set()
    with open("/proc/mounts") as f:
        for line in f:
            src = line.split()[0]
            if not src.startswith("/dev/"):
                continue
            try:
                st = os.stat(src)
            except OSError:
                continue
            if stat.S_ISBLK(st.st_mode):
                devs.add(st.st_rdev)
    return devs


def _drop_caches() -> None:
    """Force the read-back verifies below to come from the media."""
    try:
        with open("/proc/sys/vm/drop_caches", "w") as f:
            f.write("3")
    except OSError:
        pass


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


# ── boot area (eMMC boot0 / boot1) ───────────────────────────────────────────
#
# On the S905X5 family the BootROM loads the bootloader from the eMMC
# hardware boot partitions (boot0, then boot1 as fallback) — NOT from the
# user-area `bootloader_a` GPT partition. Each boot partition holds:
#
#   0x000  struct storage_emmc_boot_info (512 B): U-Boot's "info sector"
#          (version=1, reserved-partition base sector, ddr-parameter
#          location). Derived from the partition layout, identical across
#          firmware versions on the same board — so we re-use the one that
#          is already on the device instead of synthesising it.
#   0x200  the .img `bootloader` blob, verbatim
#   …      zero padding to the end of the boot partition
#
# Verified on AM9 Pro: info sector + AM9PRO_2.1.0 blob reproduces the
# factory boot0 byte-for-byte, and info sector + 2.2.0 blob booted first
# time (androidboot.bootloader went 260518 → 260903).
#
# The kernel exposes boot0/boot1 read-only by default (`force_ro=1`). That is
# a software default, not eMMC write protection: BOOT_WP / BOOT_WP_STATUS
# read 0x00 on the AM9 Pro. Root can clear force_ro and write.

BOOT_INFO_SIZE = 512
BOOT_INFO_VERSION = 1


def _sysfs_block_dir(dev: str) -> str:
    return "/sys/block/" + os.path.basename(dev)


def boot_area_devices(emmc: str) -> list[str]:
    """['/dev/mmcblk0boot0', '/dev/mmcblk0boot1'] — mknod'd if missing."""
    devs = []
    for i in (0, 1):
        node = f"{emmc}boot{i}"
        sysd = _sysfs_block_dir(node)
        if not os.path.isdir(sysd):
            continue
        if not os.path.exists(node):
            with open(os.path.join(sysd, "dev")) as f:
                maj, min_ = f.read().strip().split(":")
            os.mknod(node, stat.S_IFBLK | 0o600,
                     os.makedev(int(maj), int(min_)))
        devs.append(node)
    return devs


def boot_area_size(dev: str) -> int:
    with open(os.path.join(_sysfs_block_dir(dev), "size")) as f:
        return int(f.read().strip()) * 512


def read_boot_info(dev: str) -> bytes:
    """The 512-byte info sector at the start of a boot partition."""
    with open(dev, "rb") as f:
        hdr = f.read(BOOT_INFO_SIZE)
    if len(hdr) != BOOT_INFO_SIZE:
        raise SystemExit(f"{dev}: short read of info sector")
    version, rsv_base = struct.unpack_from("<II", hdr, 0)
    if version != BOOT_INFO_VERSION or rsv_base == 0:
        raise SystemExit(
            f"{dev}: unexpected info sector (version={version}, "
            f"rsv_base_addr={rsv_base:#x}) — refusing to guess the boot "
            f"area layout")
    return hdr


def build_boot_area_image(info: bytes, blob: bytes, size: int) -> bytes:
    if BOOT_INFO_SIZE + len(blob) > size:
        raise SystemExit(f"bootloader blob ({len(blob)} B) does not fit in "
                         f"the {size} B boot partition")
    return info + blob + b"\x00" * (size - BOOT_INFO_SIZE - len(blob))


def _set_force_ro(dev: str, value: int) -> None:
    with open(os.path.join(_sysfs_block_dir(dev), "force_ro"), "w") as f:
        f.write(str(value))


def sha1_of_boot_blob(dev: str, blob_size: int) -> str:
    """SHA1 of the bootloader blob as stored (info sector skipped)."""
    h = hashlib.sha1()
    with open(dev, "rb") as f:
        f.seek(BOOT_INFO_SIZE)
        remaining = blob_size
        while remaining > 0:
            buf = f.read(min(IO_CHUNK, remaining))
            if not buf:
                break
            h.update(buf)
            remaining -= len(buf)
    return h.hexdigest()


def write_boot_area(dev: str, image: bytes) -> None:
    _set_force_ro(dev, 0)
    try:
        with open(dev, "r+b") as f:
            f.write(image)
            f.flush()
            os.fsync(f.fileno())
    finally:
        _set_force_ro(dev, 1)
    _drop_caches()
    with open(dev, "rb") as f:
        got = f.read(len(image))
    if got != image:
        raise SystemExit(f"{dev}: read-back after write does not match — "
                         f"do NOT reboot; the other boot copy is still the "
                         f"previous bootloader")


# ── Android DTB slots in `reserved` ──────────────────────────────────────────
#
# U-Boot keeps the Android DTB (the .img's `_aml_dtb` item) in two 256 KB
# slots at reserved + 4 MiB and + 4.25 MiB, each in `struct aml_dtb_rsv`
# form (same definition in U-Boot's cmd/amlogic/aml_mmc.c and the kernel's
# drivers/mmc/host/mmc_dtb.c):
#
#   u8  data[256K - 16]   the FDT at offset 0, zero fill after it
#   u32 magic             0x00447e41  ("A~D\0")
#   u32 version           1
#   u32 timestamp         monotonic; readers pick the newest valid slot
#   u32 checksum          u32 sum over the first 256K-4 bytes
#
# Readers validate magic + checksum per slot. Writers bump the timestamp
# above the newest valid one and rewrite both slots — which is exactly what
# we do. (A factory-burned slot also carries a stray word after the FDT:
# the DNL download handler's transfer checksum, left in the buffer that
# U-Boot then wrote out. It is not part of the format and we don't add it.)

DTB_RESERVE_OFFSET = 4 * 1024 * 1024
DTB_SLOT_SIZE = 256 * 1024
DTB_COPIES = 2
DTB_MAGIC = 0x00447E41
DTB_VERSION = 1
FDT_MAGIC = 0xD00DFEED


def _sum32(b: bytes) -> int:
    if len(b) % 4:
        b = b + b"\x00" * (4 - len(b) % 4)
    return sum(struct.unpack(f"<{len(b) // 4}I", b)) & 0xFFFFFFFF


def dtb_slot_offsets() -> list[int]:
    return [DTB_RESERVE_OFFSET + i * DTB_SLOT_SIZE for i in range(DTB_COPIES)]


def parse_dtb_slot(region: bytes) -> tuple[bool, int]:
    """(valid, timestamp) for a raw 256 KB slot."""
    magic, _version, stamp, csum = struct.unpack_from(
        "<IIII", region, DTB_SLOT_SIZE - 16)
    valid = magic == DTB_MAGIC and csum == _sum32(region[:DTB_SLOT_SIZE - 4])
    return valid, stamp


def build_dtb_slot(fdt: bytes, timestamp: int) -> bytes:
    if len(fdt) > DTB_SLOT_SIZE - 16:
        raise SystemExit("DTB does not fit in a 256 KB slot")
    region = bytearray(DTB_SLOT_SIZE)
    region[:len(fdt)] = fdt
    struct.pack_into("<III", region, DTB_SLOT_SIZE - 16,
                     DTB_MAGIC, DTB_VERSION, timestamp)
    struct.pack_into("<I", region, DTB_SLOT_SIZE - 4,
                     _sum32(bytes(region[:DTB_SLOT_SIZE - 4])))
    return bytes(region)


def read_dtb_slots(reserved: str) -> list[bytes]:
    out = []
    with open(reserved, "rb") as f:
        for off in dtb_slot_offsets():
            f.seek(off)
            out.append(f.read(DTB_SLOT_SIZE))
    return out


def img_fdt(img: AmlogicImage) -> bytes | None:
    """The `_aml_dtb` PARTITION item as a sanity-checked flat FDT."""
    try:
        it = img.find("_aml_dtb", "PARTITION")
    except KeyError:
        return None
    fdt = bytes(img.read_at(it, 0, it.size))
    magic, total = struct.unpack_from(">II", fdt, 0)
    if magic != FDT_MAGIC or total != len(fdt):
        raise SystemExit(f"_aml_dtb item is not a flat FDT (magic={magic:#x}, "
                         f"totalsize={total}, item={len(fdt)})")
    return fdt


def write_dtb_slots(reserved: str, fdt: bytes) -> int:
    """Rewrite both slots with `fdt`; returns the timestamp used."""
    stamps = [stamp for valid, stamp in map(parse_dtb_slot,
                                            read_dtb_slots(reserved)) if valid]
    stamp = (max(stamps) + 1) & 0xFFFFFFFF if stamps else 0
    region = build_dtb_slot(fdt, stamp)
    with open(reserved, "r+b") as f:
        for off in dtb_slot_offsets():
            f.seek(off)
            f.write(region)
        f.flush()
        os.fsync(f.fileno())
    _drop_caches()
    for i, got in enumerate(read_dtb_slots(reserved)):
        if got != region:
            raise SystemExit(f"DTB slot {i}: read-back after write does not "
                             f"match")
    return stamp


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

class Extras:
    """The non-GPT parts of a firmware update: DTB slots + boot area."""
    __slots__ = ("reserved", "fdt", "fdt_sha1", "boot_devs", "boot_blob",
                 "boot_sha1")

    def __init__(self) -> None:
        self.reserved: str | None = None      # /dev/mmcblk0p1
        self.fdt: bytes | None = None         # _aml_dtb from the .img
        self.fdt_sha1: str | None = None
        self.boot_devs: list[str] = []        # /dev/mmcblk0boot0, boot1
        self.boot_blob: bytes | None = None   # `bootloader` item bytes
        self.boot_sha1: str | None = None


def build_extras(img: AmlogicImage, partmap: dict[str, str], emmc: str,
                 want_dtb: bool, want_boot: bool, skip_boot1: bool) -> Extras:
    x = Extras()
    if want_dtb:
        x.fdt = img_fdt(img)
        if x.fdt is None:
            print("  (skip dtb: .img has no _aml_dtb item)", file=sys.stderr)
        elif "reserved" not in partmap:
            print("  (skip dtb: no `reserved` partition in GPT)", file=sys.stderr)
            x.fdt = None
        else:
            x.reserved = partmap["reserved"]
            x.fdt_sha1 = lookup_verify_sha1(img, "_aml_dtb")
    if want_boot:
        item = None
        for name in ("bootloader", "bootloader_a"):
            try:
                item = img.find(name, "PARTITION")
                break
            except KeyError:
                continue
        if item is None:
            print("  (skip boot area: .img has no bootloader item)", file=sys.stderr)
        else:
            devs = boot_area_devices(emmc)
            if skip_boot1:
                devs = devs[:1]
            if not devs:
                print("  (skip boot area: no eMMC boot partitions found)",
                      file=sys.stderr)
            else:
                x.boot_devs = devs
                x.boot_blob = bytes(img.read_at(item, 0, item.size))
                x.boot_sha1 = lookup_verify_sha1(img, item.name)
    return x


def print_plan(plan: list[PlanStep], extras: Extras) -> None:
    print(f"  {'NAME':<20}  {'TARGET':<20}  {'ON-DISK':>14}  {'FMT':<8}  {'SHA1':<40}")
    print("  " + "-" * 110)
    total = 0
    for s in plan:
        fmt = "sparse" if s.is_sparse else "raw"
        sha = s.expected_sha1 or "(no verify item)"
        print(f"  {s.name:<20}  {s.target:<20}  {s.on_disk_size:>14,}  {fmt:<8}  {sha}")
        total += s.on_disk_size
    if extras.fdt is not None:
        for i, off in enumerate(dtb_slot_offsets()):
            tgt = f"{extras.reserved}+{off:#x}"
            print(f"  {'dtb slot ' + str(i):<20}  {tgt:<20}  {DTB_SLOT_SIZE:>14,}  "
                  f"{'dtbslot':<8}  {extras.fdt_sha1 or '(no verify item)'}")
            total += DTB_SLOT_SIZE
    if extras.boot_blob is not None:
        for dev in extras.boot_devs:
            print(f"  {os.path.basename(dev):<20}  {dev:<20}  "
                  f"{BOOT_INFO_SIZE + len(extras.boot_blob):>14,}  {'bootarea':<8}  "
                  f"{extras.boot_sha1 or '(no verify item)'}")
            total += BOOT_INFO_SIZE + len(extras.boot_blob)
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


def verify_extras(extras: Extras) -> tuple[int, int, int]:
    """(match, differ, unknown) for the DTB slots and boot area."""
    n_match = n_diff = n_unknown = 0
    if extras.fdt is not None:
        for i, region in enumerate(read_dtb_slots(extras.reserved)):
            name = f"dtb slot {i}"
            valid, stamp = parse_dtb_slot(region)
            got = hashlib.sha1(region[:len(extras.fdt)]).hexdigest()
            state = f"valid, stamp {stamp}" if valid else "INVALID checksum"
            if not extras.fdt_sha1:
                print(f"  {name:<20}  ?  ({state}; .img has no SHA1 for _aml_dtb)")
                n_unknown += 1
            elif got == extras.fdt_sha1 and valid:
                print(f"  {name:<20}  ✓  fdt sha1 matches .img ({got}); {state}")
                n_match += 1
            else:
                print(f"  {name:<20}  ✗  fdt sha1 {got}; {state}")
                print(f"  {'':<20}     .img     {extras.fdt_sha1}")
                n_diff += 1
    if extras.boot_blob is not None:
        for dev in extras.boot_devs:
            name = os.path.basename(dev)
            got = sha1_of_boot_blob(dev, len(extras.boot_blob))
            if not extras.boot_sha1:
                print(f"  {name:<20}  ?  (.img has no SHA1 for bootloader)")
                n_unknown += 1
            elif got == extras.boot_sha1:
                print(f"  {name:<20}  ✓  blob sha1 matches .img ({got})")
                n_match += 1
            else:
                print(f"  {name:<20}  ✗  blob sha1 {got}")
                print(f"  {'':<20}     .img      {extras.boot_sha1}")
                n_diff += 1
    return n_match, n_diff, n_unknown


def verify_only(img: AmlogicImage, plan: list[PlanStep], extras: Extras) -> int:
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
    m, d, u = verify_extras(extras)
    n_match += m
    n_diff += d
    n_unknown += u
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


def execute_extras(extras: Extras) -> None:
    """DTB slots first, then boot0, then boot1 — bootloader last, so an
    interrupted run leaves the previous, working bootloader in place."""
    if extras.fdt is not None:
        if extras.fdt_sha1:
            got = hashlib.sha1(extras.fdt).hexdigest()
            if got != extras.fdt_sha1:
                print(f"  ✗ _aml_dtb in .img fails its own integrity check "
                      f"({got} != {extras.fdt_sha1})", file=sys.stderr)
                sys.exit(3)
        print(f"  → dtb ({len(extras.fdt):,} bytes) → {extras.reserved} "
              f"slots {[hex(o) for o in dtb_slot_offsets()]}")
        stamp = write_dtb_slots(extras.reserved, extras.fdt)
        print(f"  ✓ dtb: both slots written and read back (timestamp {stamp})")
    if extras.boot_blob is not None:
        if extras.boot_sha1:
            got = hashlib.sha1(extras.boot_blob).hexdigest()
            if got != extras.boot_sha1:
                print(f"  ✗ bootloader in .img fails its own integrity check "
                      f"({got} != {extras.boot_sha1})", file=sys.stderr)
                sys.exit(3)
        # Re-use the info sector already on boot0 (layout-derived, not
        # firmware-derived); insist that boot0 and boot1 agree on it.
        infos = {dev: read_boot_info(dev) for dev in boot_area_devices(
            extras.boot_devs[0][:-len("boot0")])}
        if len(set(infos.values())) != 1:
            print("  ✗ boot0 and boot1 carry different info sectors — "
                  "refusing to pick one", file=sys.stderr)
            sys.exit(3)
        info = next(iter(infos.values()))
        for dev in extras.boot_devs:
            image = build_boot_area_image(info, extras.boot_blob,
                                          boot_area_size(dev))
            print(f"  → bootloader ({len(extras.boot_blob):,} bytes) → {dev}")
            write_boot_area(dev, image)
            print(f"  ✓ {os.path.basename(dev)}: written and read back")


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    # Line-buffer stdout so progress survives `nohup … > log` and the
    # os.execvp("reboot") at the end (exec replaces the process image
    # without flushing Python's userspace buffer — without this, a
    # non-interactive --ota run logs nothing but stderr).
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
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
    p.add_argument("--no-dtb", action="store_true",
                   help="Do not touch the Android DTB slots in `reserved`")
    p.add_argument("--no-boot-area", action="store_true",
                   help="Do not touch eMMC boot0/boot1 (the running bootloader "
                        "then stays at the previous version)")
    p.add_argument("--skip-boot1", action="store_true",
                   help="Write boot0 only; keep boot1 as the previous "
                        "bootloader (manual two-phase update)")
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
    # --only/--skip are about GPT partitions; a restricted plan means the
    # user is being surgical, so leave the DTB/boot area out unless the
    # plan is the full default one.
    restricted = bool(args.only or args.skip)
    extras = build_extras(img, partmap, args.emmc,
                          want_dtb=not args.no_dtb and not restricted,
                          want_boot=not args.no_boot_area and not restricted,
                          skip_boot1=args.skip_boot1)

    if not plan and extras.fdt is None and extras.boot_blob is None:
        print("Plan is empty — nothing to do.", file=sys.stderr)
        return 0

    print(f"Plan for {args.img} → {args.emmc}:\n")
    print_plan(plan, extras)
    print()

    if args.list:
        return 0

    if args.verify_only:
        print("Verifying on-disk SHA1 against .img …\n")
        return verify_only(img, plan, extras)

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
    execute_extras(extras)
    print("\n✓ all writes completed")

    if args.reboot:
        subprocess.run(["sync"])
        print("rebooting …")
        sys.stdout.flush()
        sys.stderr.flush()
        os.execvp("reboot", ["reboot"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
