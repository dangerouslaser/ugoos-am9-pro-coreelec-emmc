# CoreELEC eMMC installer — install and restore guide

`ce-emmc-install.sh` installs CoreELEC to the internal eMMC of a Ugoos AM9 Pro,
SK4 or SK4 Pro while CoreELEC's own `ceemmc` does not support these boards.
`ce-emmc-restore.sh` puts the Android partition layout back. Both run on the
box, from a CoreELEC booted off an SD card or USB stick.

Read the [warning in the README](README.md#read-this-first) before you start.
The design decisions behind these scripts (cfgload rebuild, `mount-storage.sh`,
the board check, the eMMC-mount check) are in
[`research/installer-design-notes.md`](research/installer-design-notes.md).

## Requirements

- A Ugoos AM9 Pro, SK4, or SK4 Pro booted into CoreELEC from removable media
  (SD card or USB stick — anywhere except the eMMC being repartitioned)
- A CoreELEC nightly build (AM9 Pro tested on `22.0-Piers_nightly_20260514`
  through `20260527`; SK4 / SK4 Pro on the nightlies current at their installs)
- SSH access or a terminal on the device

## Install

```bash
# From your computer
scp ce-emmc-install.sh root@<device-ip>:/storage/

# On the device
ssh root@<device-ip>
bash /storage/ce-emmc-install.sh --info       # read-only: what the installer sees
bash /storage/ce-emmc-install.sh --dry-run    # every step, no writes
bash /storage/ce-emmc-install.sh              # the real thing
```

The script walks you through confirmation prompts before changing anything.
With `whiptail` and a large enough terminal it uses a simple TUI; otherwise
plain text. Either way the destructive step requires typing `YES`.

When it finishes, remove the SD card / USB stick and reboot — the device boots
CoreELEC from the eMMC.

### Options

| Flag | Effect |
|------|--------|
| `--info` | Read-only diagnostic: partition layout, eMMC chip details, U-Boot env summary, keystore contents, bootloader version, current install state, identity check. Safe any time. |
| `--dry-run` | Show every step without executing the destructive ones. |
| `--no-cfgload-rebuild` | Skip the cfgload rebuild (step 10 below) if a future cfgload format breaks the rebuilder. `mount-storage.sh` + `nofsck` are still installed and keep the device booting. |
| `--restore-logo PATH` | Write a custom boot logo to p10 during the install. PATH is a packed `AML_RES` `.bin` or a directory of `NN_name.bmp` files from `aml-logo-tool.py unpack`. |

### After the first eMMC boot

SSH host keys are regenerated on a fresh CoreELEC install. Clear the old
entry before reconnecting:

```bash
ssh-keygen -R <device-ip>
```

## What the installer does

1. **Checks the board and the boot source.** Reads `coreelec-dt-id` from the
   active `/flash/dtb.img` and matches it against `SUPPORTED_BOARDS`; refuses
   to run unless CoreELEC is booted from removable media; confirms the eMMC
   still has the expected Android layout by partition **name**
   (`super`/`rsv`/`userdata`), so a half-finished previous run is caught here
   instead of poisoning the backups.
2. Reads the real partition sizes from the live GPT for the confirmation screen.
3. Checks whether `rsv` (p28) contains data and says so in the confirmation.
4. **Cross-checks device identity** — `androidboot.serialno` and `mac=` on the
   kernel cmdline must agree with the AMLNORMAL keystore in `reserved` (p1).
   A mismatch (tampering, partial flash) aborts.
5. **Backs up** to `/storage/emmc-backup` on the boot media. It refuses to run
   if that directory already exists non-empty, so a rerun can never clobber a
   previous backup set. Every `dd` is size-verified against its partition and
   the set is `sync`'d before the first destructive step.

   | File | What |
   |------|------|
   | `partition_layout.txt` | Full partition table — the restore script rebuilds p28/p29 from this |
   | `gpt_primary.bin`, `gpt_secondary.bin` | Raw GPT tables (type GUIDs, unique GUIDs, attribute flags, for an exact restore) |
   | `rsv_backup.bin` | `rsv` (p28, 64 MB) |
   | `env_backup.bin` | U-Boot environment (p2) |
   | `bootloader_a_backup.bin` (+ `_b` if present) | Bootloader partition (p7, 8 MB) |
   | `reserved_backup.bin` | `reserved` (p1, 64 MB) — the keystore with the ETH MAC and serial. Never written by the installer, but the factory image does not contain p1 either, so a USB Burning Tool restore would not bring it back. **Verified after backup**: if `aml-keystore-tool.py info` does not see a valid AMLNORMAL header with populated slots, the install aborts. |
   | `frp_backup.bin` | `frp` (p3, 2 MB) — 36 bytes of unit-unique anti-rollback / FRP material |
   | `param_backup.bin` | `param` (p15, 16 MB) — ext4 with the TV picture-quality DB, likely factory-tuned per device |

6. **Keeps `super` (p27)** — the Android system images stay on the eMMC.
7. Deletes `rsv` (p28, ~64 MB, backed up) and `userdata` (p29, ~54.4 GB,
   encrypted with hardware-bound keys and therefore unrecoverable).
8. Creates `CE_FLASH` (p28, 512 MB, FAT32) and `CE_STORAGE` (p29, the rest,
   ext4) in their place.
9. Copies all boot files from the boot media's `/flash` to `CE_FLASH`.
10. Rebuilds `cfgload` to use `disk=LABEL=CE_STORAGE` instead of the dual-boot
    `disk=FOLDER=/dev/CE_STORAGE`, with correct mkimage CRCs.
11. **Always installs `/flash/mount-storage.sh` and adds `nofsck` to
    `config.ini`.** CoreELEC's nightly updater overwrites `cfgload` on every
    update, reverting step 10. These two user files are never touched by the
    updater: `mount-storage.sh` is a first-class CE init hook that mounts
    `CE_STORAGE` by label, bypassing the broken `FOLDER=` path, and `nofsck`
    suppresses the retry loop the missing `/dev/CE_STORAGE` node would cause.
    They are what keeps the box booting through every nightly.
12. With `--restore-logo`, writes the custom logo to p10.
13. Optionally migrates your existing `/storage` (settings, addons, media) to
    `CE_STORAGE`: free-space check first, Kodi stopped during the copy so its
    SQLite databases are not copied hot, size and entry-count verification
    after (a failed verification prints manual recovery steps).

Not touched: p1–p26 (except p10 with `--restore-logo`), `super` (p27), and
the eMMC hardware boot partitions `boot0`/`boot1`. The installer is a
self-contained bash script; it picks up `aml-keystore-tool.py`,
`aml-bootloader-tool.py` and `aml-logo-tool.py` from the same directory if
they are there (identity check, `--info`, `--restore-logo`).

## Restoring Android

Two paths, depending on whether the installer's backup files are available.

### Option 1 — restore script (no Windows PC needed)

If the backup set from the install is on the boot media at
`/storage/emmc-backup` (older installer versions wrote the files flat into
`/storage`; the restore script finds either layout):

```bash
# Boot CoreELEC from the SD card / USB stick, then:
bash /storage/ce-emmc-restore.sh --dry-run   # preview
bash /storage/ce-emmc-restore.sh
```

The restore script:

1. Reads `partition_layout.txt` to reconstruct the exact original p28/p29 boundaries
2. Deletes `CE_FLASH` and `CE_STORAGE`
3. Recreates `rsv` and `userdata` at their original positions
4. Restores `rsv` from `rsv_backup.bin`
5. Restores `env` and `bootloader_a` (plus `frp`, `param`, `bootloader_b` if those backups exist)
6. Leaves `super` (p27) alone — it was never modified

`userdata` comes back empty; Android reinitialises it on first boot from the
system images in `super`, so the box boots as if factory-reset with the OS
intact. **All CoreELEC data on `CE_STORAGE` is lost.**

### If a script stops during repartitioning

Both scripts verify the on-disk partition table after every `parted` step and
abort if a step did not take effect. The usual cause is the kernel refusing
the table update because something still had an eMMC partition in use
([issue #1](https://github.com/dangerouslaser/ugoos-am9-pro-coreelec-emmc/issues/1)).
Both scripts unmount auto-mounted eMMC partitions first, but other holders (a
shell sitting in a mounted path, an unfinished device scan) can still trigger it.

Recovery, in order of preference:

1. **Reboot and re-run `ce-emmc-restore.sh`.** It recognises a half-repartitioned
   table, skips what is already done, and returns the device to the Android
   layout. Then move `/storage/emmc-backup` aside and re-run the installer.
2. **Restore the raw GPT from the backup set.** Partition-table edits never
   touch partition contents, so before any formatting has happened this returns
   the eMMC to exactly its pre-install state:

   ```bash
   SECTORS=$(cat /sys/block/mmcblk0/size)
   dd if=/storage/emmc-backup/gpt_primary.bin of=/dev/mmcblk0 bs=512 count=34
   dd if=/storage/emmc-backup/gpt_secondary.bin of=/dev/mmcblk0 bs=512 seek=$((SECTORS - 33))
   sync && reboot
   ```

   Then move `/storage/emmc-backup` aside and re-run the installer.

### Option 2 — Amlogic USB Burning Tool (no backup files)

Any of these boxes can be fully restored to stock Android with the Amlogic
USB Burning Tool. USB burn mode is implemented in the SoC's mask-ROM BootROM,
which nothing on the eMMC can affect, so the device can always enter it.

You need a Windows PC (or `burn/aml-dnl-burn.py` on Linux/macOS), USB Burning
Tool v3 from Ugoos, the factory image **for your model** (`AM9PRO_2.0.9.img`
or newer for the AM9 Pro; the SK4 / SK4 Pro image for those — they are not
interchangeable), and a USB-C to USB-A cable.

1. Power off the device.
2. Hold the recessed reset/ADB button while connecting the **USB-C OTG port**
   to the PC. On the AM9 Pro the USB-A ports are host-only and will not work;
   check your model's port layout.
3. The device appears in USB Burning Tool in burn mode.
4. Load the factory `.img` and click Start. Every partition is wiped and
   rewritten, including the GPT, restoring the original 29-partition layout.

The AM9 Pro factory image was fully parsed and contains `super`,
`bootloader_a`, `boot_a`, `vendor_boot_a`, `dtbo_a`, `init_boot_a`, `logo`,
`odm_ext_a`, the SoC DTB and the GPT. The SK4 / SK4 Pro images were not
parsed but use the same `AML_PACK_v2` container — `img-tools/aml-img-tool.py`
will inspect them.

**What you get back:** Android as Ugoos shipped it. For the AM9 Pro that means
**Magisk pre-installed** (root), an unlocked bootloader, and Widevine L3 only.
Whether the SK4 / SK4 Pro stock images ship the same way was not checked.
Per-device identity survives the burn: the factory image does not include
`reserved` (p1), and the WLAN/BT MAC lives in the Wi-Fi chip's OTP. See
[`research/factory-investigation.md`](research/factory-investigation.md).

## Brick risk

Low but non-zero. The installer never writes `boot0`/`boot1`, and the SoC's
USB burn mode is in mask ROM. U-Boot tries the SD card first, so a working SD
card is always a recovery path; the worst case (a corrupted GPT) is recovered
with the USB Burning Tool. Broken media playback or boot failures on future
firmware remain a real possibility with no known fix — see
[`FIRMWARE-UPDATE.md`](FIRMWARE-UPDATE.md) for keeping the Ugoos firmware in
step with CoreELEC.
