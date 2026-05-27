#!/usr/bin/env python3
"""Top-level CLI for the Linux/Mac S6 Amlogic burner.

Currently supports:
  dry-run            Show what would be written from a .img, no device.
  ota-keep-ce        Update Android slot _a from a .img while leaving
                     CoreELEC's CE_FLASH/CE_STORAGE on eMMC alone.
                     Requires --yes-i-mean-it.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'lib'))
from aml_dnl_proto import AmlogicDevice, AmlogicError
from aml_dnl_flows import (
    plan_android_slot_a_update, plan_full_restore, execute_plan,
    execute_full_restore, dry_run, AmlogicImage,
)


def cmd_dry_run(args):
    dry_run(args.img,
            include_super=args.include_super,
            include_gpt=args.include_gpt,
            include_bootloader=not args.no_bootloader)
    return 0


def cmd_ota_keep_ce(args):
    if not args.yes_i_mean_it:
        print("refusing to flash without --yes-i-mean-it", file=sys.stderr)
        print("run with `dry-run` first to see the plan", file=sys.stderr)
        return 2
    img = AmlogicImage(args.img)
    plan = plan_android_slot_a_update(
        img,
        include_super=args.include_super,
        include_gpt=args.include_gpt,
        include_bootloader=not args.no_bootloader,
        # We never call disk_initial in OTA, so we don't need DTB/GPT
        # in mem — which is good because mem writes are blocked under
        # secure boot when we're entered via TPL.
        include_mem_loads=False,
    )
    print(f"prepared {len(plan)} writes from {args.img}")
    try:
        dev = AmlogicDevice.find()
    except AmlogicError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    ident = dev.identify()
    if ident.stage != "tpl":
        print(f"ABORT: device in {ident.stage!r}, need tpl", file=sys.stderr)
        dev.close()
        return 3
    print(f"connected: {dev.describe()}")

    last_pct = {"name": "", "pct": -1}
    def progress(name, sent, total):
        pct = sent * 100 // total
        if name != last_pct["name"] or pct - last_pct["pct"] >= 10 or sent == total:
            print(f"  {name:<18}  {sent:>10}/{total} ({pct:>3}%)")
            last_pct["name"], last_pct["pct"] = name, pct

    try:
        execute_plan(dev, img, plan, on_progress=progress)
    except AmlogicError as e:
        print(f"\nFAIL: {e}", file=sys.stderr)
        dev.close()
        return 4
    dev.close()
    print("\n✓ all writes completed; remember to run `oem save_setting` if "
          "you also flashed the bootloader (TODO: integrate into flow)")
    return 0


def cmd_full_restore(args):
    img = AmlogicImage(args.img)
    plan = plan_full_restore(img)
    total = sum(s.size for s in plan)
    print(f"FULL RESTORE plan from {args.img}")
    print(f"  {len(plan)} writes, total payload: {total:,} bytes "
          f"({total / 1024 / 1024:.1f} MiB)")
    print(f"  + oem disk_initial {args.disk_initial} (erases CE_FLASH/CE_STORAGE)")
    print(f"  + oem save_setting at end")
    print()
    for step in plan:
        print(step)
    print()
    if not args.yes_i_mean_it:
        print("refusing to flash without --yes-i-mean-it")
        print("re-run with --yes-i-mean-it to actually execute")
        return 0

    try:
        dev = AmlogicDevice.find()
    except AmlogicError as e:
        print(f"FAIL: {e}", file=sys.stderr); return 1
    ident = dev.identify()
    print(f"\nconnected: {dev.describe()}")
    if ident.stage != "tpl":
        print(f"ABORT: stage={ident.stage!r}", file=sys.stderr); dev.close(); return 3

    last = {"name": "", "pct": -1}
    def progress(name, sent, total):
        pct = sent * 100 // total if total else 100
        if name != last["name"] or pct - last["pct"] >= 5 or sent == total:
            print(f"  {name:<18}  {sent:>11}/{total} ({pct:>3}%)")
            last["name"], last["pct"] = name, pct

    try:
        execute_full_restore(dev, img, plan, on_progress=progress,
                              disk_initial=args.disk_initial)
    except AmlogicError as e:
        print(f"\nFAIL during restore: {e}", file=sys.stderr); dev.close(); return 4

    dev.close()
    print("\n✓ FULL RESTORE complete.")
    print("  Power-cycle the device to boot stock Android.")
    print("  To return to CoreELEC: insert your CE SD card, hold reset, "
          "plug in power, release after ~3s; then run ce-emmc-install.sh.")
    return 0


def main():
    p = argparse.ArgumentParser(description="Amlogic S6 burner (Linux/Mac).")
    sub = p.add_subparsers(dest="cmd", required=True)

    pd = sub.add_parser("dry-run", help="Show write plan for a .img")
    pd.add_argument("img", help="path to AML_PACK .img file")
    pd.add_argument("--include-super", action="store_true",
                    help="include super partition (not yet supported — sparse)")
    pd.add_argument("--include-gpt", action="store_true",
                    help="include gpt (DANGEROUS — may erase CE partitions)")
    pd.add_argument("--no-bootloader", action="store_true",
                    help="skip bootloader")
    pd.set_defaults(func=cmd_dry_run)

    po = sub.add_parser("ota-keep-ce",
                        help="Flash Android slot _a from a .img, leaving CE alone")
    po.add_argument("img", help="path to AML_PACK .img file")
    po.add_argument("--include-super", action="store_true")
    po.add_argument("--include-gpt", action="store_true")
    po.add_argument("--no-bootloader", action="store_true")
    po.add_argument("--yes-i-mean-it", action="store_true",
                    help="required — actually run the writes")
    po.set_defaults(func=cmd_ota_keep_ce)

    pf = sub.add_parser("full-restore",
                        help="Full USB-Burning-Tool-equivalent restore "
                             "(includes super + disk_initial — erases CE!)")
    pf.add_argument("img", help="path to AML_PACK .img file")
    pf.add_argument("--disk-initial", type=int, default=1,
                    dest="disk_initial",
                    help="0=keep, 1=erase user (default), 2=erase incl keys")
    pf.add_argument("--yes-i-mean-it", action="store_true",
                    help="required — actually run the writes")
    pf.set_defaults(func=cmd_full_restore)

    args = p.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
