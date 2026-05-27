#!/usr/bin/env python3
"""Build a U-Boot script-image (`mkimage -A arm64 -O linux -T script` equivalent).

Usage:
  make-cfgload.py <input-script.txt> <output-uimage>

The script is wrapped in a 64-byte legacy uImage header with the right CRCs
so U-Boot's `source` / `autoscr` will accept it.

This is for the CE_FLASH `cfgload` file. The boot path:
  bootloader → `for p in 1..1F` fatload mmc 1:$p cfgload → source → run script.
"""
import struct
import sys
import time
import zlib

IH_MAGIC = 0x27051956
IH_OS_LINUX = 5
IH_ARCH_ARM64 = 22
IH_TYPE_SCRIPT = 6
IH_COMP_NONE = 0


def wrap_uimage_script(script_bytes: bytes, name: str = "ce-cfgload") -> bytes:
    # Verified empirically against a working CE cfgload:
    #   4B BE  : len(script_bytes)
    #   4B     : zeros
    #   N B    : script text (no trailing NUL added; trailing newline OK)
    if not script_bytes.endswith(b"\n"):
        script_bytes = script_bytes + b"\n"
    body = struct.pack(">II", len(script_bytes), 0) + script_bytes

    name_bytes = name.encode("ascii")[:32].ljust(32, b"\0")
    load_addr = 0
    entry_addr = 0
    timestamp = int(time.time())

    data_crc = zlib.crc32(body)
    header_no_crc = (
        struct.pack(
            ">IIIIIII",
            IH_MAGIC,
            0,  # header CRC placeholder
            timestamp,
            len(body),
            load_addr,
            entry_addr,
            data_crc,
        )
        + bytes([IH_OS_LINUX, IH_ARCH_ARM64, IH_TYPE_SCRIPT, IH_COMP_NONE])
        + name_bytes
    )
    assert len(header_no_crc) == 64
    header_crc = zlib.crc32(header_no_crc)
    header = (
        header_no_crc[:4]
        + struct.pack(">I", header_crc)
        + header_no_crc[8:]
    )
    return header + body


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 64
    src, dst = sys.argv[1], sys.argv[2]
    script = open(src, "rb").read()
    img = wrap_uimage_script(script)
    open(dst, "wb").write(img)
    print(f"wrote {dst}  ({len(img)} bytes, {len(script)} bytes of script)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
