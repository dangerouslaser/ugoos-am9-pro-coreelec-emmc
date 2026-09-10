# In-place Ugoos firmware update from CoreELEC — how the eMMC boot area and DTB slots really work

Companion to [`../FIRMWARE-UPDATE.md`](../FIRMWARE-UPDATE.md) (the guide) and
[`../ugoos-fw-update.sh`](../ugoos-fw-update.sh) / [`../burn/aml-emmc-burn.py`](../burn/aml-emmc-burn.py)
(the tools). This file records what was established on 2026-09-10 while
taking an AM9 Pro from firmware 2.1.0 to 2.2.0 without USB, and corrects
two claims made earlier in this repo.

## Trigger

CoreELEC nightly `22.0-Piers_nightly_20260910`. vpeter, 2026-09-09, AM9 Pro
thread: *"With next nightly build 20260910 the Android firmware MUST be
updated to v. 2.2.0"*, with the advice "update to nightly to get new dtb,
then update android fw". Ugoos ships `AM9PRO_2.2.0.7z` on mega.nz
(`.img` SHA1 `a0b9adc543788205de03b1a04cfba88be1dc2300`, 1,723,430,088 bytes).

### What actually changed between 2.1.0 and 2.2.0

Item-level diff of the two `.img` files (`lib/aml_img.py`):

| Item | 2.1.0 → 2.2.0 |
|------|---------------|
| `gpt` | identical — partition layout unchanged, so the in-device flasher's "never write the GPT" rule costs nothing |
| `dtbo_a`, `logo` | identical |
| `bootloader` / `bootloader_a` | changed; `@AMLBOOT` board id `S6-s905x5-2605181448` → `S6-s905x5-2609031514`; section layout (BBST/BL2E/BL2X/DDRF/DEVF offsets and sizes) identical |
| `boot_a`, `init_boot_a`, `vendor_boot_a`, `odm_ext_a`, `super` | changed |
| `_aml_dtb` (and the `meson1` copy) | 86,004 → 86,042 bytes |
| `DDR`, `UBOOT` (USB-only loader stages) | changed |

`dtc` diff of `_aml_dtb`: nothing but interrupt trigger types —
`interrupts = <0 N 0x01>` (edge rising) → `<0 N 0x04>` (level high) for
IRQs 0x93, 0xa0–0xa5, 0xc5, 0xca/0xcb, 0xcc, 0xd2, 0xd4, 0xd7, 0xd9 — plus a
new `amlogic,level-high-to-gic;` on the `meson-s6-gpio-intc` node and the
`ddr_bandwidth` reg growing from 0x400 to 0x1000. The CoreELEC 20260910
`dtb.img` carries exactly the same values (`level-high-to-gic` present, same
`0x04` flags). So the requirement is: the DTB now describes the IRQ polarity
the 2.2.0 BL31/U-Boot configures. The box did boot the new nightly on the
old bootloader (Kodi ran), so the failure mode is subtle, not fatal — the
dmesg "error/fail" line count dropped from 99 to 32 after the update, and
the OP-TEE revision changed (`3.18 (b5e952e3)` → `3.18 (f64cf932)`),
confirming BL32 comes from the bootloader that actually runs.

## Step 1 — the user-area flash alone does nothing for the bootloader

`burn/aml-emmc-burn.py AM9PRO_2.2.0.img --ota` (the tool as it was) wrote the
eight GPT partitions and rebooted in ~20 s. All eight verified against the
image afterwards. But:

```
androidboot.bootloader=01.01.260518.144819     ← still 2.1.0
```

`bootloader_a` (p7) held the 2.2.0 blob; the box was still running the 2.1.0
bootloader. It loads from the eMMC hardware boot partitions. Both
`/dev/mmcblk0boot0` and `boot1` still hashed to the factory-2.1.0 value
recorded in `factory-2.1.0-snapshot.md` (`a1104a81…`), which also tells us
U-Boot env `forUpgrade_bootloaderIndex=1` means "booted from copy 1 =
boot0".

Found in passing: with `--ota`, Python's stdout buffer was never flushed
before `os.execvp("reboot")`, so a `nohup … > log` run recorded only stderr.
Fixed (line-buffered stdout + explicit flush).

## Step 2 — boot0/boot1 are not write-protected

The repo said (README, `emmc-research.md`) that boot0/boot1 are "hardware
write-protected". The evidence was `force_ro=1` in sysfs and `EPERM` on
write. That is the kernel's default for eMMC boot partitions, not the eMMC's
write-protect state. The actual registers:

```
$ mmc extcsd read /dev/mmcblk0          (mmc-utils, from the CE system-tools addon)
Boot write protection status registers [BOOT_WP_STATUS]: 0x00
Boot Area Write protection [BOOT_WP]: 0x00
User area write protection register [USER_WP]: 0x00
Boot configuration bytes [PARTITION_CONFIG]: 0x00
```

Neither power-on nor permanent boot WP is set, despite `bootloader_wp=1` /
`write_boot=0` in the U-Boot env (those are env conventions, nothing more).
`echo 0 > /sys/block/mmcblk0boot0/force_ro` followed by `dd` works from
CoreELEC. The "can't be permanently bricked" argument in the README still
holds, but for the right reason: the USB burn mode is in the SoC's mask-ROM
BootROM.

## Step 3 — what a boot partition contains

Aligning the factory boot0 dump (`device-backups/factory-2.1.0/dumps/mmcblk0boot0.bin`)
with the 2.1.0 image's `bootloader_a` item:

```
offset 0x000   512-byte header:  01000000 00200100 00000000 00000000 00400000 08000000 00…
offset 0x200   the bootloader blob, verbatim (4,097,024 bytes) — 1001/1001 4K pages match
…              zeros to 4 MiB
```

`header + blob + zero pad` reproduces the factory boot0 SHA256 exactly. The
header is U-Boot's `struct storage_emmc_boot_info`
(`bl33/v2023/include/amlogic/aml_mmc.h`, written by
`amlmmc_write_info_sector()` in `drivers/amlogic/mmc/storage_emmc.c`):

```c
struct storage_emmc_boot_info {
    u32 version;              // 1
    u32 rsv_base_addr;        // 0x12000 sectors = 36 MiB = where `reserved` (p1) starts
    struct vpart_property dtb;   // {addr, size} — 0,0 in this firmware's U-Boot
    struct vpart_property ddr;   // {0x4000, 8}  — ddr-parameter at sector 0x4000, 8 sectors
    struct part_property parts[4];
    uint8_t reserved[…];
    u32 checksum;             // 0 here — this U-Boot build doesn't fill it
};
```

Everything in it derives from the partition layout, not from the firmware
version, so the updater re-uses the sector that is already on the device
(and insists boot0 and boot1 agree on it) rather than synthesising one.
2.1.0 and 2.2.0 have identical `@AMLBOOT` section layouts, so nothing else
about the blob's placement changes.

## Step 4 — the Android DTB slots in `reserved`

The DNL burn flow writes `_aml_dtb` with `oem mwrite 0x14ff4 normal store
_aml_dtb`; U-Boot's storage layer routes a write to the rsv name `dtb` into
`dtb_write()` (`bl33/v2023/cmd/amlogic/aml_mmc.c`). The kernel's `/dev/dtb`
(`common_drivers/drivers/mmc/host/mmc_dtb.c`) uses the same format:

```c
#define DTB_RESERVE_OFFSET (4 * SZ_1M)      // from the start of `reserved`
#define DTB_SIZE           (512 * 0x200)    // 256 KB per slot
#define DTB_COPIES         2

struct aml_dtb_rsv {
    u8  data[DTB_SIZE - 16];   // the FDT at offset 0, zero fill after it
    u32 magic;                 // 0x00447e41  "A~D\0"
    u32 version;               // 1
    u32 timestamp;             // bumped on every write; readers pick the newest valid slot
    u32 checksum;              // u32 sum over the first DTB_SIZE-4 bytes
};
```

On the AM9 Pro that is absolute eMMC offset 40 MiB (36 MiB + 4 MiB, the
kernel's `MMC_DTB_PART_OFFSET`) and 40.25 MiB. Reading both factory slots
back and recomputing: magic `A~D`, version 1, timestamp 1, checksum
`0xc96bc6b5` — matches. One quirk: right after the FDT (8-byte aligned)
the factory slot holds a stray word `0x6493a439`, which is the u32 sum of
the FDT itself. It is the DNL download handler's transfer checksum left in
the buffer U-Boot then wrote out; U-Boot's validation covers only the
trailing struct, so the updater does not reproduce it. A slot builder that
does reproduce it matches the factory slot byte-for-byte, which is how the
format was confirmed.

## Step 5 — the update, and the test log

All on `living.ce` (AM9 Pro, CE on eMMC, 22.0-Piers nightly 20260910).

| Time | Action | Result |
|------|--------|--------|
| 18:46 | `aml-emmc-burn.py --ota` (user-area only, old tool) | 8/8 partitions verified; reboot; bootloader still 260518 |
| 19:01 | boot0 ← info sector + 2.2.0 blob, boot1 untouched; reboot | `androidboot.bootloader=01.01.260903.151400`; CE + Kodi fine |
| 19:07 | boot1 ← same image | both boot partitions SHA256 `636f6a47…` |
| 19:14 | both DTB slots ← 2.2.0 FDT, timestamp 2 | `/dev/dtb` returns the FDT with the image's VERIFY SHA1 `66d1b1b3…`; reboot OK |
| 19:20 | new tool, `--ota` end to end (idempotent 2.2.0 rewrite) | 8 partitions + 2 DTB slots (timestamp 3) + boot0 + boot1 written and read back in 21 s; reboot OK; `--verify-only` 12/12 |

Backups taken before any of it: `/storage/emmc-backup-boot/mmcblk0boot{0,1}-fw2.1.0.bin`
and `dtb-slot{0,1}-fw2.1.0.bin` on the box (copies on the workstation).

Rollback = the same tool with `AM9PRO_2.1.0.img`.

## Corrections to earlier notes

- README / `emmc-research.md`: "boot0/boot1 are hardware write-protected;
  nothing in Linux can overwrite them" — **wrong**, see Step 2. The
  "unbrickable via USB" conclusion stands because the BootROM's USB mode is
  mask ROM, not because of eMMC WP.
- `emmc-research.md` "U-Boot … loaded automatically from boot0+bootloader_a"
  — it loads from boot0 (fallback boot1) only; `bootloader_a` is the copy
  Android's OTA machinery stages, not what the ROM reads.
- `burn/aml-emmc-burn.py` docstring: "the device reboots straight back into
  CoreELEC with the new Android/bootloader underneath" — was only true for
  Android; now true for the bootloader too.

## Open items

- SK4 / SK4 Pro: same partition layout and the same U-Boot code paths, but
  the info sector, boot-partition size and DTB slot offsets have not been
  read on one. `ugoos-fw-update.sh` gates on `--allow-untested-board`.
- Whether U-Boot 2.2.0 *needs* the 2.2.0 `_aml_dtb` for anything beyond
  Android boot: unknown (it booted CoreELEC fine between the boot-area
  update and the DTB update). Writing it costs nothing and matches the
  USB-tool end state, so the tool does it by default.
