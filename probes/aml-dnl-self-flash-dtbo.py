#!/usr/bin/env python3
"""Self-flash test — write dtbo_a back over itself with the bytes we
already have in factory-2.1.0/dumps. If the verify succeeds, the entire
oem-mwrite + sha1sum stack is validated end-to-end without changing
anything material on the device.

Why dtbo_a:
  - Small (2 MB) so the test runs in seconds.
  - The device is currently running CoreELEC, which doesn't use dtbo_a.
    Corruption here would only affect a future stock Android boot, and
    is recoverable by re-running USB Burning Tool.
  - We have the exact factory bytes captured before any modification.

Aborts on any error and reports exactly what failed.
"""
import hashlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'lib'))
from aml_dnl_proto import AmlogicDevice, AmlogicError
from aml_dnl_ops import flash_partition


BACKUP = os.path.join(
    os.path.dirname(__file__),
    "device-backups/factory-2.1.0/dumps/p12-dtbo_a.bin",
)


def main() -> int:
    print("loading factory dtbo_a backup ...")
    with open(BACKUP, "rb") as f:
        blob = f.read()
    sha1 = hashlib.sha1(blob).hexdigest()
    print(f"  {len(blob)} bytes ({len(blob):#x}); SHA1 = {sha1}")
    print()

    print("connecting ...")
    try:
        dev = AmlogicDevice.find()
    except AmlogicError as e:
        print(f"FAIL: {e}", file=sys.stderr); return 1

    ident = dev.identify()
    print(f"  {dev.describe()}")
    print(f"  stage = {ident.stage}  protocol = {ident.protocol}")
    if ident.stage != "tpl":
        print(f"ABORT: device is in {ident.stage!r}, expected tpl", file=sys.stderr)
        dev.close()
        return 2
    print()

    print("FLASH dtbo_a (writing the same 2 MB back over itself) ...")
    bytes_sent = [0]
    def progress(sent, total):
        if sent - bytes_sent[0] >= 0x80000 or sent == total:
            pct = sent * 100 // total
            print(f"  ... {sent:>8}/{total} ({pct:>3}%)")
            bytes_sent[0] = sent
    try:
        flash_partition(dev, "dtbo_a", blob,
                        media="store", file_fmt="normal",
                        verify_cmd=f"verify sha1sum {sha1}",
                        on_progress=progress)
    except AmlogicError as e:
        print(f"\nFAIL: {e}", file=sys.stderr)
        dev.close()
        return 3
    print()
    print("✓ flash_partition + verify succeeded end-to-end")
    print("  device dtbo_a now contains exactly the bytes we wrote, ")
    print("  and the device computed SHA1 matches ours.")
    dev.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
