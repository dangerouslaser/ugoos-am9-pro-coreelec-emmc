#!/usr/bin/env python3
"""
aml-logo-tool.py — unpack and pack the Amlogic AML_RES v2 logo container
used by the AM9 Pro (and other Amlogic devices) on the `logo` partition.

Usage:
  aml-logo-tool.py unpack <logo.bin> <output_dir>
      Extracts each BMP into <output_dir> as NN_name.bmp, where NN is the
      zero-padded slot index and `name` is the slot label from the container.

  aml-logo-tool.py pack <input_dir> <output.bin>
      Reads NN_name.bmp files from <input_dir>, sorts them by NN, and writes
      a valid AML_RES v2 container to <output.bin>. The CRC is recomputed.

The container format:
  Header (64 bytes):
    [0:4]   CRC32 of [4:imgSz], using zlib.crc32() XORed with 0xFFFFFFFF
            (i.e., the intermediate CRC state without the standard final XOR)
    [4:8]   version (uint32 LE) = 2
    [8:16]  magic "AML_RES!"
    [16:20] imgSz — total bytes including header
    [20:24] imgItemNum — number of items
    [24:28] alignSz = 16
    [28:64] reserved (zeros)
  Items (64 bytes each, starting at offset 0x40):
    [0:4]   0x27051956 (mkimage magic, LE — Amlogic uses this as a marker)
    [4:8]   nameId = 0
    [8:12]  size of the BMP
    [12:16] file offset where this BMP starts
    [16:20] 0
    [20:24] file offset of the *next* item descriptor (0 for the last item)
    [24:28] 0
    [28:32] index | (totalItems << 8)
    [32:64] 32-byte null-terminated name
  Then BMP data, each aligned to alignSz (16 bytes).

Verified by round-trip: unpack + pack of the stock AM9 Pro logo produces a
file byte-identical to the original.
"""

import argparse
import os
import re
import struct
import sys
import zlib

MAGIC = b"AML_RES!"
VERSION = 2
ITEM_MAGIC = 0x27051956
HDR_SIZE = 64
ITEM_SIZE = 64
ALIGN_SZ = 16


def amlogic_crc(buf: bytes) -> int:
    """Amlogic's CRC32 variant — zlib.crc32 without the final XOR (raw)."""
    return zlib.crc32(buf) ^ 0xFFFFFFFF


def align_up(n: int, a: int) -> int:
    return (n + a - 1) & ~(a - 1)


def unpack(input_path: str, out_dir: str) -> None:
    with open(input_path, "rb") as f:
        data = f.read()

    if data[8:16] != MAGIC:
        sys.exit(f"{input_path}: bad magic, not an AML_RES container")
    ver = struct.unpack_from("<I", data, 4)[0]
    if ver != VERSION:
        sys.exit(f"{input_path}: unsupported version {ver}")

    img_sz, n_items, align = struct.unpack_from("<III", data, 16)
    if align != ALIGN_SZ:
        print(f"warning: non-default alignSz={align}", file=sys.stderr)

    stored_crc = struct.unpack_from("<I", data, 0)[0]
    computed_crc = amlogic_crc(data[4:img_sz])
    if stored_crc != computed_crc:
        print(f"warning: CRC mismatch (stored 0x{stored_crc:08x}, "
              f"computed 0x{computed_crc:08x})", file=sys.stderr)

    os.makedirs(out_dir, exist_ok=True)
    for i in range(n_items):
        off = HDR_SIZE + i * ITEM_SIZE
        magic, _name_id, size, start, _, _, _, _ = struct.unpack_from(
            "<IIIIIIII", data, off,
        )
        name = data[off+32:off+64].split(b"\x00")[0].decode("utf-8", "replace")
        if magic != ITEM_MAGIC:
            print(f"warning: item {i} ({name}) has unexpected magic "
                  f"0x{magic:08x}", file=sys.stderr)
        bmp = data[start:start+size]
        if len(bmp) != size:
            sys.exit(f"item {i} ({name}): truncated payload")
        out_path = os.path.join(out_dir, f"{i:02d}_{name}.bmp")
        with open(out_path, "wb") as f:
            f.write(bmp)
        print(f"  {out_path}  ({size} bytes)")
    print(f"unpacked {n_items} items to {out_dir}/")


_NAME_RE = re.compile(r"^(\d+)_(.+)\.bmp$", re.IGNORECASE)


def pack(in_dir: str, out_path: str) -> None:
    entries = []
    for fn in os.listdir(in_dir):
        m = _NAME_RE.match(fn)
        if not m:
            continue
        idx = int(m.group(1))
        name = m.group(2)
        with open(os.path.join(in_dir, fn), "rb") as f:
            bmp = f.read()
        if bmp[:2] != b"BM":
            sys.exit(f"{fn}: not a BMP file")
        if len(name.encode("utf-8")) > 31:
            sys.exit(f"{fn}: name too long (max 31 bytes)")
        entries.append((idx, name, bmp))
    if not entries:
        sys.exit(f"{in_dir}: no NN_name.bmp files found")
    entries.sort(key=lambda e: e[0])
    n_items = len(entries)

    # Layout
    data_start = HDR_SIZE + n_items * ITEM_SIZE
    offsets = []
    cur = data_start
    for _idx, _name, bmp in entries:
        offsets.append(cur)
        cur = align_up(cur + len(bmp), ALIGN_SZ)
    img_sz = cur  # total size including trailing alignment of last item

    # Build header (CRC placeholder; we'll patch it at the end)
    header = bytearray(HDR_SIZE)
    struct.pack_into("<I", header, 4, VERSION)
    header[8:16] = MAGIC
    struct.pack_into("<III", header, 16, img_sz, n_items, ALIGN_SZ)

    # Build item descriptors
    items = bytearray(n_items * ITEM_SIZE)
    for i, ((_idx, name, bmp), start) in enumerate(zip(entries, offsets)):
        off = i * ITEM_SIZE
        next_off = HDR_SIZE + (i + 1) * ITEM_SIZE if i < n_items - 1 else 0
        index_total = i | (n_items << 8)
        struct.pack_into(
            "<IIIIIIII", items, off,
            ITEM_MAGIC, 0, len(bmp), start, 0, next_off, 0, index_total,
        )
        name_bytes = name.encode("utf-8").ljust(32, b"\x00")
        items[off+32:off+64] = name_bytes

    # Build payload area
    payload = bytearray(img_sz - data_start)
    for (_idx, _name, bmp), start in zip(entries, offsets):
        local = start - data_start
        payload[local:local+len(bmp)] = bmp

    body = bytes(header) + bytes(items) + bytes(payload)
    assert len(body) == img_sz, (len(body), img_sz)
    crc = amlogic_crc(body[4:])
    final = struct.pack("<I", crc) + body[4:]

    with open(out_path, "wb") as f:
        f.write(final)
    print(f"packed {n_items} items into {out_path} ({img_sz} bytes, "
          f"CRC=0x{crc:08x})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_un = sub.add_parser("unpack", help="extract BMPs from an AML_RES file")
    p_un.add_argument("input")
    p_un.add_argument("output_dir")
    p_pk = sub.add_parser("pack", help="build an AML_RES file from BMPs")
    p_pk.add_argument("input_dir")
    p_pk.add_argument("output")
    args = ap.parse_args()
    if args.cmd == "unpack":
        unpack(args.input, args.output_dir)
    else:
        pack(args.input_dir, args.output)


if __name__ == "__main__":
    main()
