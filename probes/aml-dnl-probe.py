#!/usr/bin/env python3
"""Read-only probe of an Amlogic device in DNL mode.

Replays the opening commands the Windows USB Burning Tool sends and prints
their replies. Safe — issues no writes, no boots, no stage transitions.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aml_dnl_proto import AmlogicDevice, AmlogicError


def hexdump(b: bytes, n: int = 64) -> str:
    sample = b[:n]
    h = " ".join(f"{x:02x}" for x in sample)
    a = "".join(chr(x) if 32 <= x < 127 else "." for x in sample)
    tail = f" …(+{len(b)-n} more)" if len(b) > n else ""
    return f"{h}  |{a}|{tail}"


def show(dev: AmlogicDevice, label: str, cmd: str) -> bytes:
    try:
        status, body = dev.cmd(cmd)
        print(f"  {label:<24}  {status}  len={len(body)}")
        if body:
            print(f"      {hexdump(body)}")
            # If the body looks like printable ASCII, show that too.
            if body and all(32 <= b < 127 or b in (0,) for b in body):
                txt = body.rstrip(b"\0").decode("ascii", errors="replace")
                if txt:
                    print(f"      ascii: {txt!r}")
        return body
    except AmlogicError as e:
        print(f"  {label:<24}  ERROR  {e}")
        return b""


# U-Boot env vars worth querying — taken from the capture-decoded transcript,
# the live `printenv` we ran via the install script's --info, and the
# standard Amlogic/Android boot env. All are read-only (`oem env_get`).
ENV_VARS_TO_PROBE = [
    # capture / --info observed
    "active_slot", "avb2", "board", "bootcmd", "bootloader_version",
    "ce_on_emmc", "ethaddr", "firstboot", "EnableSelinux",
    # standard u-boot
    "baudrate", "bootdelay", "bootargs", "preboot", "stdout", "stderr",
    "stdin", "ver", "model",
    # amlogic-specific likely candidates
    "machid", "serial#", "serialno", "chipid", "factory_mac",
    "mac", "wifi_mac", "bt_mac", "hdmi_cec", "vout",
    "store", "device_param", "lock", "lock_vendor", "upgrade_step",
    "fb_width", "fb_height", "outputmode", "logo_addr", "dtb_mem_addr",
]


def main() -> int:
    try:
        dev = AmlogicDevice.find()
    except AmlogicError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    print(dev.describe())
    print()

    print("--- identity ---")
    show(dev, "getvar:identify", "getvar:identify")
    show(dev, "getvar:serialno", "getvar:serialno")

    print()
    print("--- chip info pages (S6 supports 0..5, only valid in BootROM/DDR-fw stages) ---")
    for page in range(6):
        show(dev, f"getchipinfo-{page}", f"getvar:getchipinfo-{page}")

    print()
    print("--- other getvars seen in capture ---")
    show(dev, "getvar:cbw", "getvar:cbw")
    show(dev, "getvar:downloadsize", "getvar:downloadsize")
    show(dev, "getvar:max-download-size", "getvar:max-download-size")
    show(dev, "getvar:version", "getvar:version")
    show(dev, "getvar:version-baseband", "getvar:version-baseband")
    show(dev, "getvar:product", "getvar:product")
    show(dev, "getvar:slot-count", "getvar:slot-count")
    show(dev, "getvar:current-slot", "getvar:current-slot")

    print()
    print("--- bootloader info ---")
    show(dev, "get_bootloaderversion", "oem get_bootloaderversion")
    show(dev, "sheader_need", "oem sheader_need")

    print()
    print("--- U-Boot env (read-only via `oem env_get NAME`) ---")
    found = []
    for name in ENV_VARS_TO_PROBE:
        try:
            status, body = dev.cmd(f"oem env_get {name}")
            if status == "OKAY" and body:
                val = body.rstrip(b"\0").decode("ascii", errors="replace")
                if val:
                    print(f"  {name:<24}  {val!r}")
                    found.append(name)
            elif status == "OKAY":
                # Empty body — variable exists but unset.
                print(f"  {name:<24}  (empty)")
        except AmlogicError:
            # FAIL — variable doesn't exist or command not supported.
            pass
    print(f"  ({len(found)}/{len(ENV_VARS_TO_PROBE)} env vars with non-empty values)")

    dev.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
