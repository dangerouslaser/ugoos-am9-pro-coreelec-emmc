#!/usr/bin/env python3
"""Generalised self-flash test — write any small partition back over itself
from device-backups/factory-2.1.0/dumps/.

Usage:  aml-dnl-self-flash.py <partition-name>

Validates that the oem-mwrite + verify path works on a given partition by
writing its CURRENT exact bytes back. If verify succeeds, device state is
unchanged but the entire stack is exercised.
"""
import hashlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'lib'))
from aml_dnl_proto import AmlogicDevice, AmlogicError
from aml_dnl_ops import flash_partition


HERE = os.path.dirname(os.path.abspath(__file__))
DUMPS = os.path.join(HERE, "device-backups/factory-2.1.0/dumps")


def find_backup(part_name: str) -> str:
    """Locate the .bin dump for `part_name` in our factory backup."""
    for fn in os.listdir(DUMPS):
        if not fn.endswith(".bin"):
            continue
        # Naming: pN-<name>.bin
        if fn.endswith(f"-{part_name}.bin"):
            return os.path.join(DUMPS, fn)
    raise FileNotFoundError(
        f"no backup for partition {part_name!r} in {DUMPS}"
    )


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 64
    part_name = sys.argv[1]

    path = find_backup(part_name)
    with open(path, "rb") as f:
        blob = f.read()
    sha1 = hashlib.sha1(blob).hexdigest()
    print(f"loaded {os.path.basename(path)}: {len(blob)} bytes "
          f"({len(blob):#x}); SHA1 = {sha1}")
    print()

    try:
        dev = AmlogicDevice.find()
    except AmlogicError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1

    ident = dev.identify()
    print(f"  {dev.describe()}")
    if ident.stage != "tpl":
        print(f"ABORT: stage={ident.stage!r}, need tpl", file=sys.stderr)
        dev.close()
        return 2
    print()

    print(f"FLASH {part_name} (rewriting same {len(blob)} bytes over itself) ...")
    bytes_sent = [0]
    def progress(sent, total):
        if sent - bytes_sent[0] >= total // 8 or sent == total:
            pct = sent * 100 // total
            print(f"  ... {sent:>9}/{total} ({pct:>3}%)")
            bytes_sent[0] = sent
    try:
        flash_partition(dev, part_name, blob,
                        media="store", file_fmt="normal",
                        verify_cmd=f"verify sha1sum {sha1}",
                        on_progress=progress)
    except AmlogicError as e:
        print(f"\nFAIL: {e}", file=sys.stderr)
        dev.close()
        return 3
    print()
    print(f"✓ self-flash of {part_name} succeeded — verify SHA1 matched")
    dev.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
