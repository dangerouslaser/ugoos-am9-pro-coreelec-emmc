#!/usr/bin/env python3
"""Map the stage-14 (initial post-burn-mode) command surface.

Read-only — issues only fastboot getvars and harmless oem queries. Skips
anything that could trigger a stage transition (no `reboot-romusb`, `boot`,
`oem reboot`).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aml_dnl_proto import AmlogicDevice, AmlogicError, STATUS_OKAY

STD_FASTBOOT_VARS = [
    "unlocked", "secure", "off-mode-charge", "battery-voltage",
    "battery-soc-ok", "variant", "version-bootloader", "version-baseband",
    "slot-suffixes", "active-slot",
    # slot-specific (we know slot-count = 2 so _a/_b exist)
    "has-slot:boot", "has-slot:system", "has-slot:bootloader",
    "slot-successful:_a", "slot-successful:_b",
    "slot-unbootable:_a", "slot-unbootable:_b",
    "slot-retry-count:_a", "slot-retry-count:_b",
    # partition info (worth knowing if u-boot can report partition layout)
    "partition-size:boot_a", "partition-size:super",
    "partition-type:super", "partition-type:userdata",
    # diagnostic
    "hw-revision", "platform", "secure-boot",
]

OEM_QUERIES = [
    "oem device-info",
    "oem help",
    "oem getvar burnsteps",
    "oem getvar bootloader_version",
    "oem env_get boot_count",
    "oem env_get console",
    "oem env_get mac_wifi",
    "oem env_get wifi_mac",
    "oem env_get usid",
    "oem env_get reboot_mode",
    "oem env_get systemmode",
    "oem env_get verifiedbootstate",
]


def try_cmd(dev, cmd):
    try:
        status, body = dev.cmd(cmd)
        return status, body, None
    except AmlogicError as e:
        return "FAIL", b"", str(e)


def show(label, status, body, err):
    if err and "Variable not implemented" in err:
        return  # silent
    if err and "unknown command" in err:
        return  # silent
    if status == STATUS_OKAY:
        if body:
            txt = body.rstrip(b"\0").decode("ascii", errors="replace")
            print(f"  ✓  {label:<32}  {txt!r}")
        else:
            print(f"  ✓  {label:<32}  (empty OKAY)")
    else:
        print(f"  !  {label:<32}  {err or status}")


def main() -> int:
    try:
        dev = AmlogicDevice.find()
    except AmlogicError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    print(dev.describe())
    print()
    print("--- standard fastboot getvars ---")
    for v in STD_FASTBOOT_VARS:
        show(v, *try_cmd(dev, f"getvar:{v}"))
    print()
    print("--- oem queries ---")
    for q in OEM_QUERIES:
        show(q, *try_cmd(dev, q))
    dev.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
