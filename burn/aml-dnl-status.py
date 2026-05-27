#!/usr/bin/env python3
"""Read-only status probe — what we can learn about a connected device
without making any changes.

This exercises the new Layer-1 API end-to-end (identify, getvar_str,
getchipinfo, oem, optionally get_cbw). Useful as a sanity check that
the device is reachable and to identify which stage it's in before
doing anything destructive.
"""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'lib'))
from aml_dnl_proto import AmlogicDevice, AmlogicError, STAGE_BY_MODE
from aml_dnl_ops import dump_chipinfo_pages


def section(title): print(f"\n--- {title} ---")


def main() -> int:
    try:
        dev = AmlogicDevice.find()
    except AmlogicError as e:
        print(f"FAIL: {e}", file=sys.stderr); return 1

    print(dev.describe())
    print()

    section("identify")
    try:
        ident = dev.identify()
        print(f"  protocol      : {ident.protocol}  (3=Optimus  5=ADNL  6=S6-ADNL)")
        print(f"  minor version : {ident.minor_ver}")
        print(f"  mode          : {ident.mode:#04x} = {ident.stage}")
        print(f"  pages map     : {ident.pages_map:#04x}")
        print(f"  raw           : {ident.bytes_.hex()}")
    except AmlogicError as e:
        print(f"  ERROR: {e}")
        return 2

    section("chipinfo pages (per the populated pageMap)")
    try:
        pages = dump_chipinfo_pages(dev)
        for i, blob in sorted(pages.items()):
            magic_ascii = blob[:4]
            try: magic_ascii = magic_ascii.decode("ascii")
            except UnicodeDecodeError: magic_ascii = blob[:4].hex()
            print(f"  page {i}: magic={magic_ascii!r}  first16={blob[:16].hex()}")
    except AmlogicError as e:
        print(f"  ERROR: {e}")

    # Things that only work in certain stages — try them gracefully.
    section("U-Boot env (only in tpl stage)")
    if ident.stage == "tpl":
        for key in ("model", "serial#", "ethaddr", "bootloader_version", "active_slot",
                    "ce_on_emmc", "board", "lock"):
            try:
                v = dev.oem(f"env_get {key}")
                print(f"  {key:<22}  {v.rstrip(chr(0).encode()).decode('ascii', errors='replace')!r}")
            except AmlogicError:
                pass
    else:
        print(f"  skipped: stage is {ident.stage!r}, not tpl")

    section("oem queries (best effort)")
    for q in ("get_bootloaderversion", "sheader_need"):
        try:
            v = dev.oem(q, timeout_ms=2000)
            txt = v.rstrip(b"\x00").decode("ascii", errors="replace")
            print(f"  oem {q:<22}  {txt!r}")
        except AmlogicError as e:
            print(f"  oem {q:<22}  ERROR  {e}")

    section("get_cbw (only in spl/bl2e — will fail in tpl/romboot)")
    try:
        cbw = dev.get_cbw(timeout_ms=2000)
        if cbw:
            print(f"  sequence={cbw.sequence}  transferSize={cbw.transfer_size:#x}  "
                  f"startOffset={cbw.start_offset:#x}  needCheckSum={cbw.need_checksum}  "
                  f"isUpload={cbw.is_upload}  theEnd={cbw.the_end}  "
                  f"waitContinue={cbw.wait_continue}")
    except AmlogicError as e:
        print(f"  ERROR: {e}")

    dev.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
