# CoreELEC eMMC Installer — Ugoos AM9 Pro

A manual installer for CoreELEC to the internal eMMC of the Ugoos AM9 Pro, until the official `ceemmc` tool adds support for the `s6_s905x5_ugoos_am9_pro` board.

---

> **⚠️ WARNING: THIS PROCESS IS DESTRUCTIVE AND PERMANENT ⚠️**
>
> **Once you run this script, Android is gone. There is currently no known way to restore the original Android installation on the Ugoos AM9 Pro after the eMMC partitions are modified. The `userdata` partition is encrypted and unrecoverable. You will not be able to boot back into Android.**
>
> **Only proceed if you have no need for Android on this device.**

---

## Background

The Ugoos AM9 Pro runs an Amlogic S905X5 (S6) SoC. CoreELEC supports the hardware and includes the correct DTB (`s6_s905x5_ugoos_am9_pro.dtb`), but as of the May 2026 nightly the automated `ceemmc` install tool does not yet list this board as supported.

The eMMC is fully writable from a CoreELEC SD card boot, and all the necessary boot files are already present. This script does what `ceemmc` would do once support is added.

See [`emmc-research.md`](emmc-research.md) for the full technical research and reasoning behind every step.

---

## Requirements

- Ugoos AM9 Pro booted into CoreELEC from an SD card
- CoreELEC nightly build (tested on 22.0-Piers_nightly_20260514)
- SSH access or direct terminal access to the device
- No requirement for Android — **this process is one-way**

---

## What the script does

1. Verifies you're on the right board and booting from SD
2. Backs up the U-Boot `env` and `bootloader_a` partitions to `/storage`
3. Checks whether `super` (p27) is safe to delete — CoreELEC's `tee-loader.sh` uses `/dev/super` to load TEE firmware on some devices; the script reads the first 512 bytes and warns you if the partition has content before proceeding
4. Deletes three Android partitions from the end of the GPT:
   - `super` (p27, 3.1 GB) — tested on a unit where this was empty; yours may differ (see above)
   - `rsv` (p28, 64 MB) — reserved, empty
   - `userdata` (p29, 54.4 GB) — encrypted, unrecoverable
5. Creates two new partitions in their place:
   - `CE_FLASH` (p27, 512 MB, FAT32) — boot partition
   - `CE_STORAGE` (p28, ~57.9 GB, ext4) — CoreELEC storage
6. Copies all boot files from the SD card's `/flash` to `CE_FLASH`
7. Installs a `mount-storage.sh` hook that fixes a device node issue in the CoreELEC initrd
8. Adds `nofsck` to `config.ini` to avoid a 10-second boot delay
9. Optionally migrates your existing `/storage` (settings, addons, media) to `CE_STORAGE`

Partitions p1–p26, `boot0`, and `boot1` are not touched. `boot0`/`boot1` are hardware write-protected and cannot be modified by anything running in Linux.

---

## Running the script

Copy the script to the device and run it as root:

```bash
# From your computer
scp ce-emmc-install.sh root@<device-ip>:/storage/

# SSH into the device
ssh root@<device-ip>

# Run it
bash /storage/ce-emmc-install.sh
```

The script will walk you through confirmation prompts before making any changes. When it's done, remove the SD card and reboot — the device will boot CoreELEC from eMMC automatically.

### After first eMMC boot

SSH host keys are regenerated on a fresh CoreELEC install. Clear your old entry before reconnecting:

```bash
ssh-keygen -R <device-ip>
```

---

## Technical notes

### Why not just edit cfgload?

`cfgload` is a compiled U-Boot script in mkimage format with a CRC in the binary header. Editing it with a text editor or `sed` changes the content but not the CRC — U-Boot verifies the CRC on load and silently rejects a mismatched script. The `mount-storage.sh` hook sidesteps this entirely.

### Why nofsck?

With `cfgload` unmodified, the kernel cmdline still contains `disk=FOLDER=/dev/CE_STORAGE`. The CoreELEC initrd adds `/dev/CE_STORAGE` to its fsck checklist, then retries 20 times at 0.5 seconds each when the device node never appears (the initrd has no udev rules). `nofsck` skips this check.

### Brick risk

Very low. `boot0`/`boot1` are hardware write-protected — the SoC's first-stage bootloader cannot be overwritten from Linux. U-Boot always tries the SD card first, so inserting an SD card always gives you a recovery path. Worst case (corrupted GPT): Amlogic devices can be recovered via USB Burning Tool from a PC.

---

## Files

| File | Description |
|------|-------------|
| `ce-emmc-install.sh` | The installer script |
| `emmc-research.md` | Full technical research notes |
