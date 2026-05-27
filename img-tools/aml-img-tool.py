#!/usr/bin/env python3
"""
aml-img-tool.py — parse and pack the Amlogic USB Burning Tool image
format (the `.img` archive shipped by OEMs like Ugoos for full factory
restores). Verified against `AM9PRO_2.0.9.img` (Ugoos AM9 Pro, S6 SoC).

This is a *file-format* tool only — it does NOT flash the device. For
that you would need an Amlogic USB-Boot-Protocol implementation like
pyamlboot, plus the device in burn mode. This tool just lets you
inspect, extract, and rebuild the archive without needing the Windows
USB Burning Tool.

Container format (verified empirically + cross-checked against
`aml_image_v2_packer` community sources):

  HEADER (64 bytes, little-endian):
    [0x00] crc      (u32)  CRC32 of bytes [4:end], XORed with 0xFFFFFFFF
                            (same "raw" CRC32 variant as AML_RES)
    [0x04] version  (u32)  observed = 2
    [0x08] magic    (4)    4 bytes — the actual ID; for v2 this reads as
                            0x27b51956 little-endian
    [0x0c] imageSz  (u64)  total file size in bytes (unaligned!)
    [0x14] reserved (u32)  observed = 0
    [0x18] alignSz  (u32)  item alignment; observed = 8
    [0x1c] itemNum  (u32)  number of items
    [0x20] reserved (32)   zero padding to 64

  ITEM DESCRIPTORS (0x240 = 576 bytes each, starting at 0x40):
    [0x000] reserved (16)  zeros (or itemId/fileType fields the tool
                            does not use)
    [0x010] offset   (u64)  byte offset of the item's data within the .img
    [0x018] size     (u64)  item data size
    [0x020] type     (32)   ASCII type string, null-padded
                            (e.g. "USB", "PARTITION", "VERIFY", "bin",
                             "dtb", "ini", "conf", "aml")
    [0x040] reserved (224)  zeros
    [0x120] name     (32)   ASCII name string, null-padded
                            (e.g. "DDR", "boot_a", "_aml_dtb", "super")
    [0x140] reserved (256)  zeros

  ITEM DATA: concatenated after the descriptor table, aligned to
  `alignSz` bytes. Multiple items can reference the same byte range
  (aliases — e.g., "USB/DDR" and "USB/UBOOT" both point at the same
  offset). For each `PARTITION` item there is typically a paired
  `VERIFY` item carrying a 48-byte hash of the partition payload.

Usage:
  aml-img-tool.py info <image.img>
      Parse header + items and print a summary table.

  aml-img-tool.py unpack <image.img> <output_dir>
      Extract every item to <output_dir>/NN__type__name.bin (sanitized).
      Aliases (items sharing offset+size with a prior item) are written
      as symlinks pointing at the original file. A manifest.json is
      written describing each item; the manifest is what `pack` reads.

  aml-img-tool.py pack <input_dir> <output.img>
      Read manifest.json from <input_dir> and the per-item .bin files;
      rebuild a fresh .img with the same items and a recomputed CRC.

Verified by round-trip: unpack + pack of AM9PRO_2.0.9.img produces a
file byte-identical to the original (modulo the contents of any item
files you edit between the two steps).
"""

import argparse
import json
import os
import re
import struct
import sys
import zlib

HEADER_SIZE = 64
ITEM_DESC_SIZE = 0x240
ITEM_TABLE_OFFSET = HEADER_SIZE  # = 0x40

# crc(4) + version(4) + magic(4) + imageSz(8) + alignSz(4) + itemNum(4) + reserved(36) = 64
HEADER_FMT = "<I I 4s Q I I 36s"
assert struct.calcsize(HEADER_FMT) == HEADER_SIZE

ITEM_OFFSET_OFF = 0x10
ITEM_SIZE_OFF   = 0x18
ITEM_TYPE_OFF   = 0x20
ITEM_TYPE_LEN   = 32
ITEM_NAME_OFF   = 0x120
ITEM_NAME_LEN   = 32


def aml_crc(buf: bytes) -> int:
    """Amlogic's CRC32 variant — zlib.crc32 XORed with 0xFFFFFFFF."""
    return zlib.crc32(buf) ^ 0xFFFFFFFF


def align_up(n: int, a: int) -> int:
    if a <= 1:
        return n
    return (n + a - 1) & ~(a - 1)


# ─── Parsing ──────────────────────────────────────────────────────────────────

def parse_header(data: bytes) -> dict:
    if len(data) < HEADER_SIZE:
        raise SystemExit("file too small to be an Amlogic .img")
    (crc, version, magic, image_sz, align_sz,
     item_num, _reserved) = struct.unpack(HEADER_FMT, data[:HEADER_SIZE])
    return {
        "crc": crc,
        "version": version,
        "magic": magic,
        "image_size": image_sz,
        "align_size": align_sz,
        "item_num": item_num,
    }


def parse_item(data: bytes, idx: int) -> dict:
    base = ITEM_TABLE_OFFSET + idx * ITEM_DESC_SIZE
    if base + ITEM_DESC_SIZE > len(data):
        raise SystemExit(f"item {idx}: descriptor past end of file")
    offset = struct.unpack_from("<Q", data, base + ITEM_OFFSET_OFF)[0]
    size = struct.unpack_from("<Q", data, base + ITEM_SIZE_OFF)[0]
    type_b = data[base + ITEM_TYPE_OFF : base + ITEM_TYPE_OFF + ITEM_TYPE_LEN]
    name_b = data[base + ITEM_NAME_OFF : base + ITEM_NAME_OFF + ITEM_NAME_LEN]
    type_s = type_b.rstrip(b"\x00").decode("ascii", "replace")
    name_s = name_b.rstrip(b"\x00").decode("ascii", "replace")
    return {
        "index": idx,
        "type": type_s,
        "name": name_s,
        "offset": offset,
        "size": size,
    }


def parse_image(data: bytes) -> tuple[dict, list[dict]]:
    hdr = parse_header(data)
    items = [parse_item(data, i) for i in range(hdr["item_num"])]
    return hdr, items


# ─── Verification ────────────────────────────────────────────────────────────

def verify_crc(data: bytes, header: dict) -> bool:
    expected = aml_crc(data[4:])
    return expected == header["crc"]


# ─── Commands ────────────────────────────────────────────────────────────────

def cmd_info(path: str) -> None:
    with open(path, "rb") as f:
        data = f.read()
    hdr, items = parse_image(data)
    print(f"Amlogic image: {path}")
    print(f"  file size:  {len(data):,} bytes")
    print(f"  header crc: 0x{hdr['crc']:08x} (computed: 0x{aml_crc(data[4:]):08x}) "
          f"{'OK' if verify_crc(data, hdr) else 'MISMATCH'}")
    print(f"  version:    {hdr['version']}")
    print(f"  magic:      {hdr['magic'].hex()} ({hdr['magic']!r})")
    print(f"  imageSz:    0x{hdr['image_size']:x} ({hdr['image_size']:,})")
    print(f"  alignSz:    {hdr['align_size']}")
    print(f"  itemNum:    {hdr['item_num']}")
    print()
    print(f"{'idx':>3}  {'type':<12} {'name':<22} {'offset':>12} {'size':>14}")
    for it in items:
        print(f"{it['index']:>3}  {it['type']:<12} {it['name']:<22} "
              f"{it['offset']:>#12x} {it['size']:>14,}")


_SANITIZE = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_name(s: str) -> str:
    s = _SANITIZE.sub("_", s)
    return s or "x"


def cmd_unpack(path: str, out_dir: str) -> None:
    with open(path, "rb") as f:
        data = f.read()
    hdr, items = parse_image(data)
    if not verify_crc(data, hdr):
        print(f"warning: header CRC mismatch (stored 0x{hdr['crc']:08x}, "
              f"computed 0x{aml_crc(data[4:]):08x})", file=sys.stderr)

    os.makedirs(out_dir, exist_ok=True)
    seen_ranges: dict[tuple[int, int], str] = {}
    manifest_items = []

    # Capture the full header bytes (after CRC) so pack can preserve any
    # fields we don't explicitly model.
    header_blob_hex = data[4:HEADER_SIZE].hex()

    for it in items:
        idx = it["index"]
        fname = f"{idx:02d}__{safe_name(it['type'])}__{safe_name(it['name'])}.bin"
        out_path = os.path.join(out_dir, fname)
        key = (it["offset"], it["size"])

        if key in seen_ranges:
            target = seen_ranges[key]
            try:
                if os.path.lexists(out_path):
                    os.unlink(out_path)
                os.symlink(target, out_path)
                alias_of = target
            except OSError:
                with open(os.path.join(out_dir, target), "rb") as src, \
                     open(out_path, "wb") as dst:
                    dst.write(src.read())
                alias_of = None
        else:
            blob = data[it["offset"] : it["offset"] + it["size"]]
            if len(blob) != it["size"]:
                raise SystemExit(f"item {idx} ({it['name']}): "
                                 f"truncated (expected {it['size']}, got {len(blob)})")
            with open(out_path, "wb") as f:
                f.write(blob)
            seen_ranges[key] = fname
            alias_of = None

        # Capture the full raw descriptor so pack can preserve "mystery"
        # fields (itemId, fileType, etc. — bytes outside the offset/size/
        # type/name slots we explicitly track).
        desc_base = ITEM_TABLE_OFFSET + idx * ITEM_DESC_SIZE
        raw_desc = data[desc_base : desc_base + ITEM_DESC_SIZE]
        manifest_items.append({
            "index": idx,
            "type": it["type"],
            "name": it["name"],
            "offset": it["offset"],
            "size": it["size"],
            "file": fname,
            "alias_of": alias_of,
            "raw_desc_hex": raw_desc.hex(),
        })
        suffix = f"  (alias → {alias_of})" if alias_of else ""
        print(f"  {fname}  ({it['size']:,} bytes){suffix}")

    manifest = {
        "header": {
            "version": hdr["version"],
            "magic_hex": hdr["magic"].hex(),
            "align_size": hdr["align_size"],
            "header_blob_hex": header_blob_hex,
        },
        "items": manifest_items,
    }
    with open(os.path.join(out_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"\nunpacked {len(items)} items to {out_dir}/")
    print(f"manifest: {os.path.join(out_dir, 'manifest.json')}")


def cmd_pack(in_dir: str, out_path: str) -> None:
    manifest_path = os.path.join(in_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        raise SystemExit(f"no manifest.json in {in_dir}/")
    with open(manifest_path) as f:
        manifest = json.load(f)

    hdr = manifest["header"]
    magic = bytes.fromhex(hdr["magic_hex"])
    if len(magic) != 4:
        raise SystemExit(f"manifest magic_hex must decode to 4 bytes, got {len(magic)}")
    align_size = int(hdr["align_size"]) or 8
    item_list = manifest["items"]
    item_num = len(item_list)

    # Read each item's payload up front; aliases share buffers
    payloads: dict[str, bytes] = {}
    for entry in item_list:
        if entry.get("alias_of"):
            target = entry["alias_of"]
        else:
            target = entry["file"]
        if target not in payloads:
            with open(os.path.join(in_dir, target), "rb") as f:
                payloads[target] = f.read()
        entry["_payload_key"] = target
        if entry.get("size") != len(payloads[target]) and entry.get("alias_of") is None:
            entry["size"] = len(payloads[target])

    # Use the offsets recorded in the manifest. This preserves the
    # original .img's spacing exactly (including any gaps the OEM tool
    # left between items) — needed for byte-perfect round-trip and also
    # the safest choice for an .img the device's burn tool may flash.
    # Aliased items inherit the offset of their target.
    payload_offsets: dict[str, int] = {}
    for entry in item_list:
        key = entry["_payload_key"]
        if key in payload_offsets:
            continue
        if "offset" not in entry:
            raise SystemExit(f"item {entry.get('name','?')}: manifest lacks "
                             f"'offset' field — re-run unpack with the "
                             f"current tool to regenerate")
        payload_offsets[key] = int(entry["offset"])

    # Total image size = the highest (offset + size) across all items
    image_sz = max(payload_offsets[entry["_payload_key"]]
                   + len(payloads[entry["_payload_key"]])
                   for entry in item_list)

    # Build item descriptors — start from the captured raw descriptor when
    # available, then patch offset/size/type/name. This preserves any
    # fields (itemId, fileType, etc.) that the original .img used but this
    # tool doesn't explicitly model.
    desc_table = bytearray(item_num * ITEM_DESC_SIZE)
    for i, entry in enumerate(item_list):
        base = i * ITEM_DESC_SIZE
        key = entry["_payload_key"]
        if "raw_desc_hex" in entry:
            raw = bytes.fromhex(entry["raw_desc_hex"])
            if len(raw) != ITEM_DESC_SIZE:
                raise SystemExit(f"item {i}: raw_desc_hex wrong length")
            desc_table[base:base + ITEM_DESC_SIZE] = raw
        struct.pack_into("<Q", desc_table, base + ITEM_OFFSET_OFF,
                         payload_offsets[key])
        struct.pack_into("<Q", desc_table, base + ITEM_SIZE_OFF,
                         len(payloads[key]))
        type_b = entry["type"].encode("ascii", "replace").ljust(ITEM_TYPE_LEN, b"\x00")[:ITEM_TYPE_LEN]
        name_b = entry["name"].encode("ascii", "replace").ljust(ITEM_NAME_LEN, b"\x00")[:ITEM_NAME_LEN]
        desc_table[base + ITEM_TYPE_OFF : base + ITEM_TYPE_OFF + ITEM_TYPE_LEN] = type_b
        desc_table[base + ITEM_NAME_OFF : base + ITEM_NAME_OFF + ITEM_NAME_LEN] = name_b

    # Build header — start from the captured header_blob_hex if available
    # so we preserve any unknown fields, then patch our known fields.
    header_buf = bytearray(HEADER_SIZE)
    if "header_blob_hex" in hdr:
        blob = bytes.fromhex(hdr["header_blob_hex"])
        if len(blob) != HEADER_SIZE - 4:
            raise SystemExit("manifest header_blob_hex wrong length")
        header_buf[4:HEADER_SIZE] = blob
    # Patch the documented fields
    struct.pack_into("<I", header_buf, 4, int(hdr["version"]))
    header_buf[8:12] = magic
    struct.pack_into("<Q", header_buf, 12, image_sz)
    struct.pack_into("<I", header_buf, 20, align_size)
    struct.pack_into("<I", header_buf, 24, item_num)

    # Build the full body (header + descriptors + payloads), then CRC bytes [4:end]
    body = bytearray(image_sz)
    body[:HEADER_SIZE] = header_buf
    body[ITEM_TABLE_OFFSET:ITEM_TABLE_OFFSET + len(desc_table)] = desc_table
    for key, off in payload_offsets.items():
        body[off:off + len(payloads[key])] = payloads[key]

    crc = aml_crc(bytes(body[4:]))
    struct.pack_into("<I", body, 0, crc)

    with open(out_path, "wb") as f:
        f.write(body)
    print(f"packed {item_num} items into {out_path} "
          f"({image_sz:,} bytes, crc=0x{crc:08x})")


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_info = sub.add_parser("info", help="parse header + list items")
    p_info.add_argument("image")
    p_un = sub.add_parser("unpack", help="extract every item to files")
    p_un.add_argument("image")
    p_un.add_argument("output_dir")
    p_pk = sub.add_parser("pack", help="rebuild an .img from an unpack/ directory")
    p_pk.add_argument("input_dir")
    p_pk.add_argument("output")
    args = ap.parse_args()
    if args.cmd == "info":
        cmd_info(args.image)
    elif args.cmd == "unpack":
        cmd_unpack(args.image, args.output_dir)
    elif args.cmd == "pack":
        cmd_pack(args.input_dir, args.output)


if __name__ == "__main__":
    main()
