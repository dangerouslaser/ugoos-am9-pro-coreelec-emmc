#!/usr/bin/env python3
"""Test the stage 14 → 15 transition (reboot-romusb + re-enumeration).

After this runs, the device is sitting in the DDR-firmware-load stage
waiting for a `firstsect` + `download:` of the DDR init blob. We don't
upload anything — that's the next test. The device just sits there until
power-cycled, no harm done.

Safe: no writes to eMMC.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aml_dnl_proto import AmlogicDevice, AmlogicError
from aml_dnl_ops import reboot_to_romusb


def main() -> int:
    try:
        dev = AmlogicDevice.find()
    except AmlogicError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1

    print("BEFORE:", dev.describe())
    identify = dev.cmd("getvar:identify")[1]
    print(f"  identify: {identify.hex()}")
    print()
    print("sending reboot-romusb...")
    try:
        new_dev = reboot_to_romusb(dev, timeout_s=15)
    except AmlogicError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 2

    print()
    print("AFTER:  ", new_dev.describe())
    try:
        identify = new_dev.cmd("getvar:identify")[1]
        print(f"  identify: {identify.hex()}")
    except AmlogicError as e:
        print(f"  identify failed (may be in a stage that needs other commands first): {e}")

    # In stage 15 these should start working (per the burn capture).
    print()
    print("checking stage-15 vocabulary:")
    for q in ("getvar:cbw", "getvar:downloadsize",
              "getvar:getchipinfo-0", "getvar:getchipinfo-5"):
        try:
            status, body = new_dev.cmd(q)
            print(f"  {q:<28}  {status} {body.hex()[:48]}")
        except AmlogicError as e:
            print(f"  {q:<28}  ERROR  {e}")

    new_dev.close()
    print()
    print("Device is now in stage 15 — power-cycle it (unplug power) before next test.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
