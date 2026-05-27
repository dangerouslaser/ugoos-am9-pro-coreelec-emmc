#!/usr/bin/env python3
"""Test the firstsect handshake at stage 15.

Plan:
  1. From stage 14, send reboot-romusb to reach stage 15.
  2. Send `firstsect`. Per the capture this returns `DATA00000400` then `OKAY`.
  3. Hypothesis A: between DATA and OKAY the device expects us to upload 1024
     bytes (the @AML header of the DDR blob).
     Hypothesis B: DATA00000400 is informational; OKAY follows immediately
     with no data exchange. We test A by uploading; if OKAY follows that's
     a confirmation. If we get an error, we'll know to try B.
  4. STOPS HERE. We do NOT `download:00042800` or `boot` yet — we just want
     to confirm the firstsect framing.

If the device gets stuck after this test, power-cycle into stage 14 again.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aml_dnl_proto import AmlogicDevice, AmlogicError, STATUS_OKAY, STATUS_DATA
from aml_dnl_ops import reboot_to_romusb, AmlogicImage


def main() -> int:
    print("loading DDR blob from AM9PRO_2.0.9.img ...")
    img = AmlogicImage("AM9PRO_2.0.9/AM9PRO_2.0.9.img")
    ddr = img.blob("DDR", item_type="USB")
    print(f"  DDR blob: {len(ddr)} bytes; first 16: {ddr[:16].hex()}")
    print()

    print("connecting (stage 14) ...")
    try:
        dev = AmlogicDevice.find()
    except AmlogicError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    print(f"  {dev.describe()}")
    print()

    print("transition: reboot-romusb (stage 14 → 15) ...")
    try:
        dev = reboot_to_romusb(dev, timeout_s=30)
    except AmlogicError as e:
        print(f"FAIL transitioning: {e}", file=sys.stderr)
        return 2
    print(f"  {dev.describe()}")
    # Stage check
    try:
        status, body = dev.cmd("getvar:getchipinfo-0", timeout_ms=2000)
        print(f"  chipinfo-0 OK ({len(body)} bytes) → confirmed stage 15")
    except AmlogicError as e:
        print(f"  chipinfo-0 FAIL: {e} — not in stage 15?", file=sys.stderr)
        return 3
    print()

    print("firstsect handshake ...")
    # Send firstsect; expect DATA<size> in reply.
    try:
        status, body = dev.cmd("firstsect", timeout_ms=2000)
    except AmlogicError as e:
        print(f"  firstsect FAILED: {e}")
        return 4
    print(f"  firstsect reply: status={status} body={body!r}")
    if status != STATUS_DATA:
        print(f"  expected DATA, got {status}")
        return 5
    size_str = body[:8].decode("ascii", errors="replace")
    try:
        size = int(size_str, 16)
    except ValueError:
        print(f"  DATA payload not hex: {body!r}")
        return 6
    print(f"  device wants {size} bytes ({size:#x})")
    print()

    # Hypothesis A: upload that many bytes from DDR blob's @AML header.
    print(f"hypothesis A: uploading first {size} bytes of DDR blob ...")
    payload = ddr[:size]
    print(f"  payload first 32: {payload[:32].hex()}")
    try:
        dev._write(payload, timeout_ms=5000)
    except Exception as e:
        print(f"  WRITE FAILED: {e}")
        return 7

    # Read trailing OKAY (or whatever).
    try:
        reply = dev._read(64, timeout_ms=3000)
        print(f"  device reply after upload: {reply!r}")
        if reply[:4] == b"OKAY":
            print()
            print("✓ HYPOTHESIS A CONFIRMED — firstsect expects 1024 bytes followed by OKAY.")
            print("  Stage 15 firstsect framing is now decoded.")
        else:
            print(f"  unexpected reply: {reply!r}")
    except Exception as e:
        print(f"  no OKAY received after upload: {e}")
        print()
        print("Hypothesis A appears WRONG. Power-cycle the device and we'll try B.")
        return 8

    dev.close()
    print()
    print("(Device is sitting between firstsect and download — power-cycle"
          " to clear state before next test.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
