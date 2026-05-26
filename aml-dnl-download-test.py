#!/usr/bin/env python3
"""Tiny round-trip test of the download() flow.

Stages 16 bytes of zeros via `download:00000010`. The device acks with
DATA00000010, accepts the bytes, replies OKAY — and nothing else happens.
We never issue `boot`, so the staged buffer just sits in DRAM until the
next power cycle.

If this prints OK, Layer 1 download is functional end-to-end.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aml_dnl_proto import AmlogicDevice, AmlogicError


def main() -> int:
    try:
        dev = AmlogicDevice.find()
    except AmlogicError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    print(dev.describe())
    print()

    payload = b"\x00" * 16
    print(f"staging {len(payload)} zero bytes via download: ...")
    try:
        dev.download(payload)
    except AmlogicError as e:
        print(f"download FAILED: {e}", file=sys.stderr)
        dev.close()
        return 2
    print("download OK — device acked DATA + accepted bytes + replied OKAY")

    # Confirm we can still talk to the device afterwards (no stuck state).
    status, body = dev.cmd("getvar:serialno")
    print(f"post-download getvar:serialno → {status} {body.rstrip(b'\\x00')!r}")

    dev.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
