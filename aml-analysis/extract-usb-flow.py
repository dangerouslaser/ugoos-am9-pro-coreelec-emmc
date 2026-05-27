#!/usr/bin/env python3
"""Decrypt usb_flow.aml and dump each Lua item.

Container format (reverse-engineered from aml_usb_flow.dll
lua_item_decrypt_and_load):

  outer header (64 bytes, plaintext):
    [0x00] crc       (u32)   container CRC32
    [0x04] version   (u32)   = 2
    [0x08] magic     (8s)    "AML_RES!"
    [0x10] size      (u32)   total file size in bytes
    [0x14] item_num  (u32)   = 12
    [0x18] align_sz  (u32)   = 16
    [0x1c]                    zero padding to 0x40

  encrypted payload begins at 0x40, AES-128-CBC,
  KEY = 0x1000b478 in DLL = 2c7d161728aed2a6abf7158809cf503c
  IV  = 0x1000b468 in DLL = 000102030505060708090a0b0c0d0e0f

  decrypted payload layout:
    [0x000..0x300]   12 × 64-byte item descriptors
        per descriptor:
          [0x00] magic    (u32)   = 0x27051956 (DLL-internal magic)
          [0x04] reserved (u32)   = 0
          [0x08] size     (u32)   decrypted content size in bytes
          [0x0c] offset   (u32)   offset (relative to start of decrypted
                                  payload) where the item content begins
          [0x10] reserved
          [0x14] padded   (u32)   item size aligned to 16-byte AES block
          [0x18] reserved
          [0x1c] index    (u32)   item index (low byte) + 0x0c00 marker
          [0x20] name     (32s)   NUL-padded ASCII filename (e.g. "json.lua")
    [0x300+]         item payloads (each item's bytes; total size matches header)
"""
import os, struct
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

KEY = bytes.fromhex("2c7d161728aed2a6abf7158809cf503c")
IV  = bytes.fromhex("000102030505060708090a0b0c0d0e0f")

HERE = os.path.dirname(os.path.abspath(__file__))


def decrypt(path: str) -> bytes:
    """Read an .aml container and return the decrypted payload (no header)."""
    with open(path, "rb") as f:
        data = f.read()
    assert data[8:16] == b"AML_RES!", f"{path}: not an AML_RES container"
    enc = data[0x40:]
    if len(enc) % 16:
        enc = enc[:len(enc) - (len(enc) % 16)]
    c = Cipher(algorithms.AES(KEY), modes.CBC(IV), backend=default_backend()).decryptor()
    return c.update(enc) + c.finalize()


def parse_items(pt: bytes):
    """Yield (index, name, content_bytes) tuples."""
    # First read item count from descriptors area: each is 64 bytes,
    # we have 12 of them per the outer header.
    item_count = 12  # also extractable from the container's header
    for i in range(item_count):
        d = pt[i * 64: (i + 1) * 64]
        magic, _r1, size, offset, _r2, padded, _r3, idx = struct.unpack_from("<8I", d, 0)
        if magic != 0x27051956:
            continue
        name = d[0x20:0x40].rstrip(b"\x00").decode("ascii", errors="replace")
        content = pt[offset: offset + size]
        yield i, name, content


def main():
    in_path = os.path.join(HERE, "usb_flow.aml")
    out_dir = os.path.join(HERE, "usb_flow_decrypted")
    os.makedirs(out_dir, exist_ok=True)

    pt = decrypt(in_path)
    print(f"decrypted {len(pt)} bytes from {os.path.basename(in_path)}")
    print()
    print(f"{'idx':>3}  {'size':>7}  {'offset':>7}  name")
    print("-" * 60)
    for idx, name, content in parse_items(pt):
        out = os.path.join(out_dir, name or f"item{idx}.bin")
        with open(out, "wb") as f:
            f.write(content)
        head = content[:4].hex()
        is_lua = content[:4] == b"\x1bLua"
        tag = "  <Lua bytecode>" if is_lua else ""
        print(f"{idx:>3}  {len(content):>7}  {'-':>7}  {name}  (head={head}){tag}")
    print()
    print(f"items written to: {out_dir}/")


if __name__ == "__main__":
    main()
