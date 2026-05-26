#!/usr/bin/env python3
"""
aml-bootloader-tool.py — inspect and unpack the Amlogic @AMLBOOT signed
bootloader container used by S5/S6-family SoCs (verified on the Ugoos
AM9 Pro's bootloader_a partition).

Container layout (verified on s6_s905x5_ugoos_am9_pro):

  +-------------------+------------------------------------------+
  | offset 0          | encrypted boot-stage chunks, framed by   |
  |                   | @ML  16-byte markers + 32-byte SHA-256-  |
  |                   | -shaped tail hashes (DBLK_PAYLOAD_END)   |
  +-------------------+------------------------------------------+
  | offset 0x43820    | @AMLBOOT manifest (this tool decodes it) |
  +-------------------+------------------------------------------+
  | manifest sections | concatenated, each described by a 16-    |
  |  BBST BL2E BL2X   | byte entry: name(4) + offset(4) +        |
  |  DDRF DEVF        | size(4) + flags(4)                       |
  +-------------------+------------------------------------------+
  | after last section| zero padding to partition end            |
  +-------------------+------------------------------------------+

@AMLBOOT header (32 bytes):
  [0:8]   magic "@AMLBOOT"
  [8:12]  flags / version (observed 0x02 0x05 0x90 0x00)
  [12:32] 20-byte board identifier (e.g. "S6-s905x5-2601151440")

Each manifest entry (16 bytes):
  [0:4]   name (4 ASCII chars; entries terminate on a name of \\0\\0\\0\\0)
  [4:8]   absolute offset within the bootloader image (uint32 LE)
  [8:12]  size in bytes (uint32 LE)
  [12:16] flags (observed 0 across all entries)

Sections:
  BBST  bootloader boot stub — contains the @ML-framed boot-stage
        chunks AND the @AMLBOOT manifest itself near its tail
  BL2E  BL2 entry image (encrypted)
  BL2X  BL2 extension / DDR init (encrypted)
  DDRF  DDR firmware slot (all zero on AM9 Pro — DDR firmware is
        bundled into the BL2 stages on this SoC)
  DEVF  device firmware — BL31 (ATF) + BL32 (TEE) + BL33 (U-Boot)
        + embedded DTBs, all encrypted as a single blob

All four content sections (BBST excluded, since it carries the
plaintext manifest) measure as near-maximum entropy (~7.9 bits/byte).
The Amlogic BootROM/BL1 decrypts them at load using a per-SoC-family
hardware key. The S6 family key is not publicly known, so direct
disassembly of U-Boot or BL31/BL32 is not possible from the on-eMMC
binary alone — capturing the decrypted versions from RAM (UART
console + md command, or JTAG) is the practical path if you want
to inspect them.

Usage:
  aml-bootloader-tool.py info <bootloader.bin>
      Parse the manifest and print a summary.

  aml-bootloader-tool.py unpack <bootloader.bin> <output_dir>
      Write each section (BBST, BL2E, BL2X, DDRF, DEVF) to its own
      file in <output_dir>. The manifest is also extracted as
      manifest.txt for reference.
"""

import argparse
import os
import struct
import sys


MAGIC = b"@AMLBOOT"
HEADER_SIZE = 32
ENTRY_SIZE = 16


def find_manifest(data: bytes) -> int:
    """Return the file offset of the @AMLBOOT magic, or raise."""
    pos = data.find(MAGIC)
    if pos < 0:
        raise SystemExit("error: @AMLBOOT magic not found")
    return pos


def parse_manifest(data: bytes) -> tuple[dict, list[dict]]:
    manifest_off = find_manifest(data)
    flags = data[manifest_off + 8 : manifest_off + 12]
    board_raw = data[manifest_off + 12 : manifest_off + 32]
    board = board_raw.rstrip(b"\x00").decode("latin1", "replace")
    header = {
        "offset": manifest_off,
        "flags": flags.hex(),
        "board": board,
    }

    entries = []
    off = manifest_off + HEADER_SIZE
    while off + ENTRY_SIZE <= len(data):
        name = data[off : off + 4]
        if name == b"\x00\x00\x00\x00":
            break
        if not all(0x20 <= b < 0x7f for b in name):
            break
        section_off, size, f3 = struct.unpack_from("<III", data, off + 4)
        entries.append({
            "name": name.decode("latin1"),
            "offset": section_off,
            "size": size,
            "flags": f3,
        })
        off += ENTRY_SIZE

    return header, entries


def info(path: str) -> None:
    with open(path, "rb") as f:
        data = f.read()

    header, entries = parse_manifest(data)
    print(f"@AMLBOOT manifest at file offset 0x{header['offset']:x}")
    print(f"  flags: {header['flags']}")
    print(f"  board: {header['board']!r}")
    print()
    total = 0
    print(f"{'name':<6} {'offset':>10} {'size':>10} {'size (KB)':>10}  flags")
    for e in entries:
        total += e["size"]
        print(f"{e['name']:<6} {e['offset']:>#10x} {e['size']:>#10x} {e['size']//1024:>10}  0x{e['flags']:x}")
    print(f"{'total':<6} {'':>10} {total:>#10x} {total//1024:>10}")
    print(f"file size: {len(data)} bytes ({len(data)//1024} KB)")


def unpack(path: str, out_dir: str) -> None:
    with open(path, "rb") as f:
        data = f.read()

    header, entries = parse_manifest(data)
    os.makedirs(out_dir, exist_ok=True)

    with open(os.path.join(out_dir, "manifest.txt"), "w") as f:
        f.write(f"@AMLBOOT manifest from {os.path.basename(path)}\n")
        f.write(f"  manifest offset: 0x{header['offset']:x}\n")
        f.write(f"  flags: {header['flags']}\n")
        f.write(f"  board: {header['board']!r}\n\n")
        for e in entries:
            f.write(f"  {e['name']}: offset=0x{e['offset']:x} size=0x{e['size']:x} ({e['size']} bytes) flags=0x{e['flags']:x}\n")

    for e in entries:
        out_path = os.path.join(out_dir, f"{e['name']}.bin")
        chunk = data[e["offset"] : e["offset"] + e["size"]]
        if len(chunk) != e["size"]:
            print(f"warning: {e['name']} truncated ({len(chunk)} of {e['size']} bytes)", file=sys.stderr)
        with open(out_path, "wb") as f:
            f.write(chunk)
        print(f"  {out_path}  ({len(chunk)} bytes)")

    print(f"unpacked {len(entries)} sections to {out_dir}/")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_info = sub.add_parser("info", help="print manifest summary")
    p_info.add_argument("input")
    p_unpack = sub.add_parser("unpack", help="extract each section")
    p_unpack.add_argument("input")
    p_unpack.add_argument("output_dir")
    args = ap.parse_args()
    if args.cmd == "info":
        info(args.input)
    else:
        unpack(args.input, args.output_dir)


if __name__ == "__main__":
    main()
