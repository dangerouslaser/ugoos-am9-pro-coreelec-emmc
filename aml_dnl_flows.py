"""Layer 3 — top-level burn flows.

Sequences Layer 2 operations into the user-visible "do thing" commands:
full restore, single-partition write, OTA-keeping-CE, etc.

Reference: `usb_flow_decrypted/usb_flow_dnl.lua::tpl_flow` for the
canonical Amlogic ordering. We replicate the ordering (dtb → gpt →
sheader? → partitions → bootloader → save_setting) but never call
`oem disk_initial` (would erase the partition table — including
CE_FLASH and CE_STORAGE on a CoreELEC-on-eMMC device).

Status:
  - flash_one_partition_from_img: WORKING (validated against dtbo_a + frp)
  - update_android_a_slot:        in progress; dry-run only as of this commit
  - full_restore:                 not implemented (needs sparse support for super)
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable, Optional

from aml_dnl_proto import AmlogicDevice, AmlogicError
from aml_dnl_ops import (
    AmlogicImage, ImgItem, flash_partition,
    set_burnstep, TPL_STEP_DOWN_DTB, TPL_STEP_DOWN_PART,
)


# ── partition write planning ─────────────────────────────────────────────────

# These eMMC partition labels exist on the AM9 Pro after a CoreELEC eMMC
# install (created by ce-emmc-install.sh in this repo). Any flow that
# might touch them needs an explicit allow-flag.
CE_PARTITIONS = ("CE_FLASH", "CE_STORAGE", "rsv", "userdata")

# Per usb_flow_dnl.lua::tpl_flow, gpt + dtb + sheader are written to DRAM
# only (`mem` media). gpt is also written to `store` afterwards. We treat
# `store gpt` as protected by default because writing the GPT can ALSO
# erase the CE partitions if the source .img doesn't include CE.
GPT_GUARDED = "gpt"

# Items the .img typically contains that aren't "things to flash to a
# partition" — skip during partition enumeration.
_NOT_PARTITION_TYPES = {"USB", "VERIFY", "ini", "dtb", "aml", "conf",
                         "UBOOT", "bin"}


@dataclass
class WriteStep:
    """One scheduled write. Used by dry-run output and the executor."""
    item: ImgItem
    media: str
    file_fmt: str
    verify_cmd: Optional[str]
    # For "bootloader is really bootloader_a backup" the source bytes
    # come from a different item.
    source_item: Optional[ImgItem] = None

    @property
    def size(self) -> int:
        return (self.source_item or self.item).size

    def __str__(self) -> str:
        src = self.source_item.name if self.source_item else self.item.name
        srcnote = f"  (bytes from {src})" if self.source_item else ""
        v = "  + sha1 verify" if self.verify_cmd else ""
        return (f"  mwrite  {self.item.name:<18}  {self.size:>10} B  "
                f"{self.file_fmt:<6}  → {self.media:<5}{v}{srcnote}")


def _verify_cmd_for(img: AmlogicImage, partition_name: str,
                    main_type: str = "PARTITION") -> Optional[str]:
    """Look up the matching VERIFY item and pull out its verify command.

    The .img typically stores just `sha1sum HEX` (without the `verify`
    prefix the bootloader needs). We add `verify ` if it's missing —
    the bootloader's whitelist requires the full `verify sha1sum HEX`
    invocation; sending bare `sha1sum HEX` fails with "cmd sha1sum not
    in secure boot white list" on a secure-boot device.
    """
    try:
        v = img.find(partition_name, item_type="VERIFY")
    except KeyError:
        return None
    blob = img._data[v.offset: v.offset + v.size]  # noqa: SLF001
    text = blob.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()
    if not text:
        return None
    if not text.startswith("verify "):
        text = "verify " + text
    return text


def _resolve_source(img: AmlogicImage, item: ImgItem) -> ImgItem:
    """If `item` looks like a backup of another item, find the real one.

    The .img format has an `is_backup` flag we don't parse yet, but the
    naming convention is consistent: items with the same offset+size as
    another are duplicates (e.g. `bootloader` is a backup of `bootloader_a`).
    """
    for cand in img.items:
        if cand is item:
            continue
        if cand.offset == item.offset and cand.size == item.size:
            return cand
    return item


def plan_android_slot_a_update(
    img: AmlogicImage, *,
    include_super: bool = False,
    include_gpt: bool = False,
    include_bootloader: bool = True,
    include_mem_loads: bool = True,
) -> list[WriteStep]:
    """Build the list of writes for an OTA-style Android update that keeps
    CoreELEC on eMMC.

    Mirrors the partition selection from `tpl_flow` but:
      - never touches CE_FLASH / CE_STORAGE
      - skips `super` by default (no sparse support yet; pass include_super
        to opt in once supported)
      - skips `gpt` by default (rewriting could repartition and erase CE;
        pass include_gpt only for full-restore scenarios)
      - puts the bootloader LAST so a crash leaves a working previous
        bootloader on flash

    Returns a list of WriteStep — use `print()` on them for a clear
    dry-run report, then pass each to `flash_partition` via the executor.
    """
    plan: list[WriteStep] = []

    # Sub-pass 1: mem-loaded helpers (dtb to mem, optionally gpt to mem,
    # optionally sheader to mem). These don't touch eMMC but DO require
    # the device to allow `mem`-media writes. On a secure-boot device
    # entered via TPL (no BL1→BL2→TPL chain), the bootloader rejects
    # these with "partition memory not allowed if secure boot en". Pass
    # include_mem_loads=False to omit them — fine if you're not doing
    # disk_initial (which depends on the freshly-loaded DTB).
    if include_mem_loads:
        try:
            meson_dtb = img.find("meson1", item_type="dtb")
            plan.append(WriteStep(item=meson_dtb, media="mem",
                                   file_fmt="normal", verify_cmd=None))
        except KeyError:
            pass
        if include_gpt:
            try:
                gpt = img.find("gpt", item_type="bin")
                plan.append(WriteStep(item=gpt, media="mem",
                                       file_fmt="normal", verify_cmd=None))
            except KeyError:
                pass

    # Sub-pass 2: PARTITION items, in two halves — non-bootloader first.
    deferred_bootloader: list[WriteStep] = []
    seen_offsets: set[tuple[int, int]] = set()
    for item in img.items:
        if item.type != "PARTITION":
            continue
        if item.name in CE_PARTITIONS:
            continue
        if item.name == "super" and not include_super:
            continue
        if item.name == GPT_GUARDED and not include_gpt:
            continue
        # Skip the bare "bootloader" alias when "bootloader_a" exists in
        # the .img with the same bytes — the A/B-suffixed name maps to
        # the real eMMC partition on AM9 Pro (p7 = bootloader_a).
        if item.name == "bootloader":
            try:
                bla = img.find("bootloader_a", item_type="PARTITION")
                if (bla.offset, bla.size) == (item.offset, item.size):
                    continue
            except KeyError:
                pass
        # Skip exact-byte duplicates we already planned.
        key = (item.offset, item.size)
        if key in seen_offsets:
            continue
        seen_offsets.add(key)

        source = _resolve_source(img, item)
        verify = _verify_cmd_for(img, item.name)
        file_fmt = "sparse" if item.name == "super" else "normal"
        step = WriteStep(item=item, media="store", file_fmt=file_fmt,
                         verify_cmd=verify,
                         source_item=source if source is not item else None)
        if "bootloader" in item.name:
            if include_bootloader:
                deferred_bootloader.append(step)
            continue
        plan.append(step)

    plan.extend(deferred_bootloader)
    return plan


# ── executors ────────────────────────────────────────────────────────────────

def plan_full_restore(img: AmlogicImage) -> list[WriteStep]:
    """Build the write plan for a complete USB-Burning-Tool-equivalent
    restore — INCLUDING super (sparse) AND triggering disk_initial.

    THIS ERASES CE_FLASH/CE_STORAGE on a CoreELEC-on-eMMC device. The
    partition table is recreated from the .img's gpt item, so any
    user-created partitions disappear.

    The disk_initial command is not part of the plan list — it's an
    executor-level concern. The plan is just the writes themselves.

    Matches the order in `usb_flow_dnl.lua::tpl_flow`, including the
    gpt-to-store write that the slot_a-update plan deliberately skips.
    """
    plan = plan_android_slot_a_update(
        img,
        include_super=True,
        include_gpt=True,
        include_bootloader=True,
    )
    # The official flow ALSO writes gpt to eMMC (in addition to loading
    # to mem). plan_android_slot_a_update doesn't do this — it can't,
    # since for OTA-keep-CE we explicitly do NOT want to rewrite the
    # partition table. For full restore we do, so add gpt→store right
    # after the _aml_dtb→store entry, before the bulk partition writes.
    try:
        gpt = img.find("gpt", item_type="bin")
    except KeyError:
        return plan
    gpt_to_store = WriteStep(item=gpt, media="store", file_fmt="normal",
                             verify_cmd=None)
    # Find where _aml_dtb sits and insert just after it. If not present,
    # insert right after the last mem entry.
    insert_at = 0
    for i, step in enumerate(plan):
        if step.media == "mem":
            insert_at = i + 1
        elif step.item.name == "_aml_dtb":
            insert_at = i + 1
            break
    plan.insert(insert_at, gpt_to_store)
    return plan


def execute_full_restore(dev: AmlogicDevice, img: AmlogicImage, plan: list[WriteStep],
                         *, on_progress: Optional[Callable[[str, int, int], None]] = None,
                         disk_initial: int = 1) -> None:
    """Execute a full restore: dtb to mem → disk_initial → all partition
    writes → save_setting.

    `disk_initial` value (per usb_flow_dnl.lua):
        0 = keep existing partitions (so don't use here)
        1 = erase user partitions, keep keystore (typical OEM-restore default)
        2 = erase everything including keys (rarely needed)

    On exit the device is still in DNL mode; caller is responsible for
    triggering a reboot (oem reboot or power-cycle).
    """
    ident = dev.identify()
    if ident.stage != "tpl":
        raise AmlogicError(
            f"execute_full_restore: device in {ident.stage!r}, need tpl"
        )

    # First flash any "mem" entries (dtb, optionally gpt-to-mem and
    # sheader-to-mem). disk_initial wants dtb already loaded so the
    # bootloader knows the eMMC layout it's about to recreate.
    set_burnstep(dev, "tpl", TPL_STEP_DOWN_DTB)
    mem_steps = [s for s in plan if s.media == "mem"]
    store_steps = [s for s in plan if s.media != "mem"]
    for step in mem_steps:
        blob_item = step.source_item or step.item
        blob = img._data[blob_item.offset: blob_item.offset + blob_item.size]  # noqa: SLF001
        cb = (lambda n=step.item.name: (lambda s, t: on_progress and on_progress(n, s, t)))()
        flash_partition(dev, step.item.name, blob,
                        media=step.media, file_fmt=step.file_fmt,
                        verify_cmd=step.verify_cmd, on_progress=cb)

    # disk_initial — this is the destructive step that erases user
    # partitions and recreates the partition table from the loaded DTB.
    set_burnstep(dev, "tpl", 0x31)  # TPL_STEP_DISK_INIT
    dev.oem(f"disk_initial {disk_initial}", timeout_ms=300_000)

    # All the actual partition writes.
    set_burnstep(dev, "tpl", TPL_STEP_DOWN_PART)
    for step in store_steps:
        blob_item = step.source_item or step.item
        blob = img._data[blob_item.offset: blob_item.offset + blob_item.size]  # noqa: SLF001
        cb = (lambda n=step.item.name: (lambda s, t: on_progress and on_progress(n, s, t)))()
        flash_partition(dev, step.item.name, blob,
                        media=step.media, file_fmt=step.file_fmt,
                        verify_cmd=step.verify_cmd, on_progress=cb)

    # Commit env changes (matches `usbDev:OemCmd('save_setting')` at the
    # end of tpl_flow).
    try:
        dev.oem("save_setting", timeout_ms=10_000)
    except AmlogicError as e:
        # save_setting can fail on some firmware versions; don't kill
        # the restore over it.
        print(f"WARN: save_setting failed (continuing): {e}")


def execute_plan(dev: AmlogicDevice, img: AmlogicImage, plan: list[WriteStep],
                 *, set_steps: bool = True,
                 on_progress: Optional[Callable[[str, int, int], None]] = None,
                 ) -> None:
    """Run a previously-built plan against the device.

    `on_progress(partname, bytes_sent, partition_size)` is called between
    chunks if given. Aborts on the first failure — caller can re-run with
    the failing step if needed.
    """
    if set_steps:
        set_burnstep(dev, "tpl", TPL_STEP_DOWN_DTB)

    for step in plan:
        blob_item = step.source_item or step.item
        blob = img._data[blob_item.offset: blob_item.offset + blob_item.size]  # noqa: SLF001

        def _on(part_name: str = step.item.name):
            def cb(sent, total):
                if on_progress:
                    on_progress(part_name, sent, total)
            return cb

        # Tell the device which step we're on. tpl_flow uses DownPart for
        # eMMC partition writes; mem writes are part of the DownDtb phase.
        if step.media == "store" and set_steps:
            set_burnstep(dev, "tpl", TPL_STEP_DOWN_PART)

        flash_partition(dev, step.item.name, blob,
                        media=step.media, file_fmt=step.file_fmt,
                        verify_cmd=step.verify_cmd,
                        on_progress=_on())


def dry_run(img_path: str, **kwargs) -> None:
    """Print the planned writes for a .img file without touching a device.

    Pass `include_super=True`, `include_gpt=True`, `include_bootloader=False`
    etc. as keyword args.
    """
    img = AmlogicImage(img_path)
    plan = plan_android_slot_a_update(img, **kwargs)
    print(f"plan for {img_path}")
    print(f"  options: {kwargs}")
    total = sum(s.size for s in plan)
    print(f"  {len(plan)} writes, total payload: {total:,} bytes "
          f"({total / 1024 / 1024:.1f} MiB)")
    print()
    for step in plan:
        print(step)
    print()
    skipped = []
    if not kwargs.get("include_super"):
        skipped.append("super (sparse — pass --include-super to flash)")
    if not kwargs.get("include_gpt"):
        skipped.append("gpt (would repartition eMMC, may erase CE)")
    skipped.append("CE_FLASH / CE_STORAGE (CoreELEC on eMMC)")
    print(f"  intentionally skipped: {', '.join(skipped)}")
