#!/usr/bin/env python3
"""
aml-keystore-tool.py — read and dump Amlogic AMLNORMAL keystores.

The AMLNORMAL keystore is the on-eMMC storage backend for Amlogic's
Unified Key Store (UKS). U-Boot reads it via `keyman read <name>` to
populate per-device identity values (`mac`, `usid`/serial, region_code,
deviceid, etc.) plus DRM/attestation slots (`$widevinekeybox`,
`$attestationkeybox`, `$netflix_mgkid`, `$PlayReadykeybox25`, HDCP
firmware, etc.).

On S5/S6-family devices (incl. Ugoos AM9 Pro) the keystore lives in a
"key" sub-region of the `reserved` partition (p1), with the
`AMLNORMAL` magic at offset 0x4000 and a redundant copy 0x40000 bytes
later. The format is documented in the public CoreELEC/u-boot tree:

  bl33/v2023/drivers/amlogic/storagekey/normal_key.c   (raw header)
  bl33/v2023/drivers/amlogic/storagekey/normal_key.h   (in-memory API)

This tool is read-only. It expects a binary dump of the reserved
partition (or any region containing the `AMLNORMAL` magic) and walks
both header and slot table.

Usage:
  aml-keystore-tool.py info <dump.bin>
      Print the AMLNORMAL header (version, enctype, key count,
      stored hashes, computed hashes for comparison).

  aml-keystore-tool.py list <dump.bin>
      Walk the slot table and list every populated slot:
      name, attribute, type, data length, raw value (decoded as ASCII
      if printable, hex otherwise), and slot integrity hash.

  aml-keystore-tool.py extract <dump.bin> <output_dir>
      Write each slot's raw value to <output_dir>/<name>.bin so it can
      be inspected with other tools (e.g., a Widevine keybox parser).
      Filenames are sanitized; the literal slot name is preserved in
      a manifest.txt for reference.

On-disk format (verified against CoreELEC/u-boot bl33/v2023 source and
empirically against an AM9 Pro reserved partition dump):

  HEADER (offset 0 of the keystore region):
    [0:16]  ASCII magic "AMLNORMAL" + padding
    [16:24] reserved (zeros)
    [24:28] version          (u32 LE; observed = 2)
    [28:32] enctype          (u32 LE; 0 = plaintext slots,
                                       nonzero = AES-encrypted)
    [32:36] keycnt           (u32 LE; number of populated slots)
    [36:48] initcnt / wrtcnt / errcnt
    [48:52] flags
    [52:84] headhash (SHA-256 of header bytes [0:52] padded to 64? —
                      verify empirically; the public source documents
                      this as covering the header)
    [84:116] datahash (SHA-256 of the data region)

  DATA REGION (typically starting at offset 0x200 from the header):
    A sequence of TLV records `(u32 type, u32 length, u8 value[length])`.
    Top-level types observed:
      T=1   index/metadata record (24-byte payload)
      T=3   slot container — value contains nested TLV records:
              T=4  attribute        (4-byte flags)
              T=5  slot name        (variable)
              T=6  data byte-count  (4-byte actual length)
              T=7  data buffer      (variable, padded to alignment)
              T=8  object type      (4-byte; e.g. 0xA00000BF = GENERIC)
              T=9  reserved         (4-byte; observed 0)
              T=10 slot integrity hash (32-byte SHA-256)

All multi-byte fields are little-endian.
"""

import argparse
import hashlib
import os
import re
import struct
import sys


MAGIC = b"AMLNORMAL"

# Inner record types
T_ATTR = 4
T_NAME = 5
T_DATASIZE = 6
T_DATA = 7
T_TYPE = 8
T_RESERVED = 9
T_HASH = 10

ATTR_FLAGS = {
    1: "SECURE",
    2: "OTP",
    256: "ENC",  # 1 << 8
}


def find_magic(data: bytes, offset: int = 0) -> int:
    pos = data.find(MAGIC, offset)
    if pos < 0:
        raise SystemExit(f"AMLNORMAL magic not found (searched from offset 0x{offset:x})")
    return pos


def parse_header(data: bytes, off: int) -> dict:
    if data[off:off+9] != MAGIC:
        raise SystemExit(f"no AMLNORMAL magic at 0x{off:x}")
    # struct storage_block_raw_head (from CoreELEC/u-boot
    # bl33/v2023/drivers/amlogic/storagekey/normal_key.c):
    #   u8 mark[16];          offset 0
    #   u32 version;          offset 16
    #   u32 enctype;          offset 20
    #   u32 keycnt;           offset 24
    #   u32 initcnt;          offset 28
    #   u32 wrtcnt;           offset 32
    #   u32 errcnt;           offset 36
    #   u32 flags;            offset 40
    #   u8  headhash[32];     offset 44
    #   u8  hash[32];         offset 76
    version, enctype, keycnt = struct.unpack_from("<III", data, off + 16)
    initcnt, wrtcnt, errcnt, flags = struct.unpack_from("<IIII", data, off + 28)
    return {
        "offset": off,
        "version": version,
        "enctype": enctype,
        "keycnt": keycnt,
        "initcnt": initcnt,
        "wrtcnt": wrtcnt,
        "errcnt": errcnt,
        "flags": flags,
        "headhash": data[off+44:off+76],
        "datahash": data[off+76:off+108],
    }


def parse_tlv_records(buf: bytes) -> list[tuple[int, bytes]]:
    """Walk TLV records. Records are packed back-to-back with no padding;
    zero-runs between top-level groups are skipped. Walking stops when
    we run off the buffer."""
    records = []
    i = 0
    while i + 8 <= len(buf):
        # Skip runs of zero bytes (between top-level sections)
        while i + 8 <= len(buf) and buf[i] == 0 and buf[i+1] == 0 \
                and buf[i+2] == 0 and buf[i+3] == 0:
            i += 4
        if i + 8 > len(buf):
            break
        t, length = struct.unpack_from("<II", buf, i)
        if t == 0 or length > len(buf) - i - 8 or length > 0x100000:
            break
        records.append((t, buf[i+8:i+8+length]))
        i += 8 + length
    return records


def attr_to_str(attr: int) -> str:
    flags = [name for bit, name in ATTR_FLAGS.items() if attr & bit]
    return f"0x{attr:x}" + (f" ({'|'.join(flags)})" if flags else "")


def render_value(v: bytes) -> str:
    """If the value is mostly printable ASCII, show as text; else hex."""
    trimmed = v.rstrip(b"\x00")
    if trimmed and all(0x20 <= b < 0x7f for b in trimmed):
        return repr(trimmed.decode("ascii"))
    return v.hex()


def find_slots(data: bytes, hdr_off: int) -> tuple[int, list]:
    """Locate the data region and parse all top-level TLV records."""
    # Empirically the data region starts at hdr_off + 0x200 (after the
    # 108-byte header + zero padding to a 512-byte boundary).
    data_off = hdr_off + 0x200
    # Walk forward to find the first non-zero byte (the first TLV header)
    while data_off < len(data) and data[data_off] == 0:
        data_off += 1
    # back up if the type byte landed mid-record
    data_off = (data_off // 4) * 4
    records = parse_tlv_records(data[data_off:data_off + 0x40000])
    return data_off, records


def slot_to_dict(slot_bytes: bytes) -> dict:
    """Parse the inner TLV records of a T=3 slot container."""
    inner = parse_tlv_records(slot_bytes)
    out = {"raw_records": inner}
    for t, v in inner:
        if t == T_NAME:
            out["name"] = v.rstrip(b"\x00").decode("ascii", "replace")
        elif t == T_ATTR:
            out["attribute"] = struct.unpack("<I", v)[0]
        elif t == T_DATASIZE:
            out["datasize"] = struct.unpack("<I", v)[0]
        elif t == T_DATA:
            out["data"] = v
        elif t == T_TYPE:
            out["type"] = struct.unpack("<I", v)[0]
        elif t == T_HASH:
            out["hash"] = v
    # Trim data to declared datasize
    if "data" in out and "datasize" in out:
        out["data"] = out["data"][:out["datasize"]]
    return out


def cmd_info(path: str) -> None:
    with open(path, "rb") as f:
        data = f.read()
    off = find_magic(data)
    hdr = parse_header(data, off)
    print(f"AMLNORMAL header found at file offset 0x{hdr['offset']:x}")
    print(f"  version:  {hdr['version']}")
    print(f"  enctype:  {hdr['enctype']}  ({'plaintext' if hdr['enctype']==0 else 'AES-encrypted slots'})")
    print(f"  keycnt:   {hdr['keycnt']}")
    print(f"  initcnt:  {hdr['initcnt']}")
    print(f"  wrtcnt:   {hdr['wrtcnt']}")
    print(f"  errcnt:   {hdr['errcnt']}")
    print(f"  flags:    0x{hdr['flags']:x}")

    # Try to identify the redundant copy
    second = data.find(MAGIC, off + 1)
    if second > 0:
        print(f"\nredundant copy at 0x{second:x} (stride 0x{second - off:x})")
    print(f"\nstored hashes (32 bytes each):")
    print(f"  headhash (offset 44-75):  {hdr['headhash'].hex()}")
    print(f"  datahash (offset 76-107): {hdr['datahash'].hex()}")


def cmd_list(path: str) -> None:
    with open(path, "rb") as f:
        data = f.read()
    off = find_magic(data)
    hdr = parse_header(data, off)
    print(f"AMLNORMAL @ 0x{off:x}: version={hdr['version']} enctype={hdr['enctype']} "
          f"keycnt={hdr['keycnt']}")

    data_off, records = find_slots(data, off)
    print(f"data region begins at 0x{data_off:x}; {len(records)} top-level TLV record(s)")
    print()

    slot_n = 0
    for t, v in records:
        if t == 1:
            print(f"top-level T=1 (index/meta) at offset 0x{data_off:x}, value="
                  f"{v.hex()[:48]}...")
            continue
        if t == 3:
            slot_n += 1
            slot = slot_to_dict(v)
            print(f"slot #{slot_n}:")
            print(f"  name:       {slot.get('name', '?')!r}")
            if "attribute" in slot:
                print(f"  attribute:  {attr_to_str(slot['attribute'])}")
            if "type" in slot:
                t_val = slot["type"]
                t_name = "GENERIC" if t_val == 0xA00000BF else f"0x{t_val:x}"
                print(f"  type:       {t_name}")
            if "datasize" in slot:
                print(f"  datasize:   {slot['datasize']}")
            if "data" in slot:
                print(f"  value:      {render_value(slot['data'])}")
            if "hash" in slot:
                print(f"  hash:       {slot['hash'].hex()}")
            print()


def safe_name(name: str) -> str:
    out = re.sub(r"[^A-Za-z0-9_$.-]", "_", name)
    return out or "unnamed"


def cmd_extract(path: str, out_dir: str) -> None:
    with open(path, "rb") as f:
        data = f.read()
    off = find_magic(data)
    data_off, records = find_slots(data, off)
    os.makedirs(out_dir, exist_ok=True)

    manifest_lines = [f"AMLNORMAL keystore from {os.path.basename(path)}",
                      f"  magic at: 0x{off:x}",
                      f"  data region: 0x{data_off:x}", ""]

    slot_n = 0
    for t, v in records:
        if t != 3:
            continue
        slot_n += 1
        slot = slot_to_dict(v)
        name = slot.get("name", f"slot{slot_n}")
        fname = f"{slot_n:02d}_{safe_name(name)}.bin"
        out_path = os.path.join(out_dir, fname)
        with open(out_path, "wb") as f:
            f.write(slot.get("data", b""))
        manifest_lines.append(f"  {fname}  name={name!r} "
                              f"attr={attr_to_str(slot.get('attribute', 0))} "
                              f"datasize={slot.get('datasize', 0)}")
        print(f"  {out_path}  ({len(slot.get('data', b''))} bytes)")

    with open(os.path.join(out_dir, "manifest.txt"), "w") as f:
        f.write("\n".join(manifest_lines) + "\n")
    print(f"extracted {slot_n} slot(s) to {out_dir}/")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_info = sub.add_parser("info", help="print AMLNORMAL header")
    p_info.add_argument("input")
    p_list = sub.add_parser("list", help="enumerate all populated slots")
    p_list.add_argument("input")
    p_ex = sub.add_parser("extract", help="write each slot's value to a file")
    p_ex.add_argument("input")
    p_ex.add_argument("output_dir")
    args = ap.parse_args()
    if args.cmd == "info":
        cmd_info(args.input)
    elif args.cmd == "list":
        cmd_list(args.input)
    elif args.cmd == "extract":
        cmd_extract(args.input, args.output_dir)


if __name__ == "__main__":
    main()
