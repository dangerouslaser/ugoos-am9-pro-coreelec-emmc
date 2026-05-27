#!/usr/bin/env python3
"""Stage 14 → 15 → 16 transition test.

Drives the device from a fresh burn-mode plug-in all the way through DDR
init firmware load, ending in the U-Boot-load stage (16). Safe — no eMMC
writes, no Android partitions touched. Just brings the device "alive enough"
to verify the protocol stack.

Stages exercised:
  14 (fresh DNL)
  → reboot-romusb →
  15 (DDR-firmware-load)
  → firstsect + download:0x42800 + boot →
  16 (U-Boot-load — chipinfo page 4/5 unlocked, cbw works, download accepts
       the next 16 KB chunks of the U-Boot binary)

After this test the device is sitting in stage 16 waiting for the U-Boot
binary download. Power-cycle to clear.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aml_dnl_proto import AmlogicDevice, AmlogicError
from aml_dnl_ops import (
    AmlogicImage, load_ddr_firmware, reboot_to_romusb,
)

IMG_PATH = "AM9PRO_2.0.9/AM9PRO_2.0.9.img"


def probe_stage(dev: AmlogicDevice) -> str:
    """Best-effort label for which burn stage the device is currently in."""
    # chipinfo only works in stages ≥15
    chipinfo_ok = False
    try:
        s, _ = dev.cmd("getvar:getchipinfo-0", timeout_ms=2000)
        chipinfo_ok = (s == "OKAY")
    except AmlogicError:
        pass
    # env_get only works in stage 14
    env_get_ok = False
    try:
        s, _ = dev.cmd("oem env_get model", timeout_ms=2000)
        env_get_ok = (s == "OKAY")
    except AmlogicError:
        pass
    # cbw works in stage 16 (post-DDR-firmware-boot)
    cbw_ok = False
    try:
        s, _ = dev.cmd("getvar:cbw", timeout_ms=2000)
        cbw_ok = (s == "OKAY")
    except AmlogicError:
        pass
    if env_get_ok and not chipinfo_ok:
        return "14 (fresh DNL)"
    if chipinfo_ok and cbw_ok:
        return "16 (U-Boot-load) — cbw unlocked"
    if chipinfo_ok and not cbw_ok:
        return "15 (DDR-firmware-load)"
    return "unknown (chipinfo=?  env_get=?  cbw=?)"


def main() -> int:
    print(f"loading DDR blob from {IMG_PATH} ...")
    img = AmlogicImage(IMG_PATH)
    ddr = img.blob("DDR", item_type="USB")
    print(f"  DDR blob: {len(ddr)} bytes; first 16: {ddr[:16].hex()}")
    print()

    print("connecting ...")
    try:
        dev = AmlogicDevice.find()
    except AmlogicError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    print(f"  {dev.describe()}")
    print(f"  stage: {probe_stage(dev)}")
    print()

    print("stage 14 → 15: reboot-romusb")
    dev = reboot_to_romusb(dev, timeout_s=30)
    print(f"  {dev.describe()}")
    print(f"  stage: {probe_stage(dev)}")
    print()

    print(f"stage 15 → 16: firstsect + download:{0x42800:#x} + boot")
    print("  uploading DDR init portion (0x400 + 0x42800 = 0x42c00 bytes) ...")
    t0 = time.monotonic()
    load_ddr_firmware(dev, ddr)
    dt = time.monotonic() - t0
    print(f"  done in {dt*1000:.0f} ms")

    # Capture shows ~2s gap between download OKAY and the `boot` command —
    # the device may need time to internally process before boot.
    print("  waiting 2s before boot (per capture timing) ...")
    time.sleep(2)
    print("  sending boot ...")
    try:
        s, b = dev.cmd("boot", timeout_ms=5000)
        print(f"  boot reply: {s} {b!r}")
    except AmlogicError as e:
        # Acceptable — device may disconnect mid-reply.
        print(f"  boot (disconnect expected): {e}")
    dev.close()

    print()
    print("  waiting for re-enumeration into stage 16 ...")
    time.sleep(2)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            dev = AmlogicDevice.find()
            # Probe to confirm.
            stage = probe_stage(dev)
            if stage.startswith("16"):
                print(f"  {dev.describe()}")
                print(f"  stage: {stage}")
                print()
                print("✓ reached stage 16 — DDR init succeeded, device booted U-Boot-load shim")
                dev.close()
                return 0
            else:
                print(f"  re-enumerated but stage={stage}; will keep watching")
                dev.close()
        except AmlogicError as e:
            pass
        time.sleep(1)

    print("FAIL: did not reach stage 16 within 30s")
    return 2


if __name__ == "__main__":
    sys.exit(main())
