# CoreELEC eMMC Installer — Ugoos AM9 Pro

A manual installer for CoreELEC to the internal eMMC of the Ugoos AM9 Pro, documented here for reference while `ceemmc` does not yet support this board.

---

## ⚠️ READ BEFORE PROCEEDING ⚠️

**This installation method is not supported by CoreELEC. Any support request, bug report, or forum post related to a CoreELEC install performed this way will be rejected, closed, or removed by the CoreELEC team. Do not ask for help on the CoreELEC forums if something goes wrong.**

**This was tested on one specific device running one specific firmware build (22.0-Piers_nightly_20260514). A different device revision or a newer firmware version may fail to boot or permanently break media playback. There is no way to know in advance.**

**This process permanently removes Android and destroys any path back to it. There is no restore procedure. The original eMMC partition layout cannot be recovered without a full factory image, which is not publicly available for this device.**

Specifically:

- **Android is gone permanently.** The userdata partition is encrypted and unrecoverable. You will not be able to boot Android again.
- **Media playback may break.** CoreELEC uses the `super` partition to load TEE firmware for DRM-protected content (Widevine, etc.). Removing `super` may break playback of DRM-protected streams depending on your firmware. The script checks whether `super` is empty before proceeding, but this behaviour may change across firmware versions.
- **Future firmware may prevent booting entirely.** This method bypasses normal eMMC install tooling. There is no guarantee it will work with any build other than the one it was tested on.
- **No CoreELEC support.** This is explicitly unsupported. Do not file issues or ask for help on CoreELEC forums or Discord.

If you are not comfortable with all of the above, run CoreELEC from the SD card instead.

---

## Background

The Ugoos AM9 Pro runs an Amlogic S905X5 (S6) SoC. CoreELEC supports the hardware and includes the correct DTB (`s6_s905x5_ugoos_am9_pro.dtb`), but as of the May 2026 nightly the automated `ceemmc` install tool does not list this board as supported.

This script documents what was done to get a working eMMC install on one specific unit. It is published as a technical reference, not a recommendation.

See [`emmc-research.md`](emmc-research.md) for the full research notes.

---

## Requirements

- Ugoos AM9 Pro booted into CoreELEC from an SD card
- CoreELEC nightly build (tested on 22.0-Piers_nightly_20260514 only)
- SSH access or direct terminal access to the device

---

## What the script does

1. Verifies you're on the right board and booting from SD
2. Backs up the U-Boot `env` and `bootloader_a` partitions to `/storage`
3. Checks whether `super` (p27) contains data and warns you before deleting it
4. Deletes three Android partitions from the end of the GPT:
   - `super` (p27, 3.1 GB) — was empty on the tested unit; may contain TEE firmware on others
   - `rsv` (p28, 64 MB) — reserved, was empty
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

Low but non-zero. `boot0`/`boot1` are hardware write-protected — the SoC's first-stage bootloader cannot be overwritten from Linux. U-Boot tries the SD card first, so a working SD card always provides a recovery path. Worst case (corrupted GPT): Amlogic devices can be recovered via USB Burning Tool from a PC. However, broken media playback or boot failures on future firmware are a real possibility with no known fix.

---

## Files

| File | Description |
|------|-------------|
| `ce-emmc-install.sh` | The installer script |
| `emmc-research.md` | Full technical research notes |
