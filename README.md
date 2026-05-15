# CoreELEC eMMC Installer — Ugoos AM9 Pro

A manual installer for CoreELEC to the internal eMMC of the Ugoos AM9 Pro, documented here for reference while `ceemmc` does not yet support this board.

---

## ⚠️ READ BEFORE PROCEEDING ⚠️

**This installation method is not supported by CoreELEC. Any support request, bug report, or forum post related to a CoreELEC install performed this way will be rejected, closed, or removed by the CoreELEC team. Do not ask for help on the CoreELEC forums if something goes wrong.**

**This was tested on one specific device running one specific firmware build (22.0-Piers_nightly_20260514). A different device revision or a newer firmware version may fail to boot or permanently break media playback. There is no way to know in advance.**

**This process removes Android. It can be restored, but only via the Amlogic USB Burning Tool on a Windows PC using the official factory image — it is not a simple undo. See [Restoring Android](#restoring-android) below.**

Specifically:

- **Android is removed.** The userdata partition is encrypted and unrecoverable on its own, but a full factory restore via USB Burning Tool will wipe and rewrite everything including userdata.
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
7. Installs a `mount-storage.sh` hook as a workaround for the cfgload FOLDER= path (see technical notes)
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

`cfgload` is a compiled U-Boot script in mkimage format with a CRC in the binary header. Editing it with a text editor or `sed` changes the content but not the CRC — U-Boot verifies the CRC on load and silently rejects a mismatched script, failing silently with no obvious error.

The correct approach is to decompile, edit, and recompile cfgload with `mkimage` so it uses `disk=LABEL=CE_STORAGE` directly — which is what ceemmc would do if it supported this board. The `mount-storage.sh` hook was used here as a workaround to avoid that recompilation step, but it is not the intended mechanism.

### Why nofsck?

Because cfgload was left unmodified, the kernel cmdline still contains `disk=FOLDER=/dev/CE_STORAGE`. The CoreELEC initrd adds `/dev/CE_STORAGE` to its fsck checklist, then retries 20 times at 0.5 seconds each when the device node is not found. `nofsck` skips this check. If cfgload were properly recompiled to use `LABEL=`, this workaround would not be needed.

### Brick risk

Low but non-zero. `boot0`/`boot1` are hardware write-protected — the SoC's first-stage bootloader cannot be overwritten from Linux. U-Boot tries the SD card first, so a working SD card always provides a recovery path. Worst case (corrupted GPT): Amlogic devices can be recovered via USB Burning Tool from a PC. However, broken media playback or boot failures on future firmware are a real possibility with no known fix.

---

## Restoring Android

The AM9 Pro can be fully restored to stock Android using the Amlogic USB Burning Tool, even after this script has run. This works because `boot0` (the BL2 first-stage bootloader) is hardware write-protected and cannot be touched by anything running in Linux — the device can always enter USB burn mode.

**What you need:**

- A Windows PC
- USB Burning Tool v3 (available from Ugoos)
- The official factory firmware image (`AM9PRO_2.0.9.img` or newer)
- A USB-A to USB-A cable

**Process:**

1. Power off the device
2. Hold the recessed reset/ADB button while connecting the USB-A cable to the PC
3. The device will appear in USB Burning Tool in burn mode
4. Load the factory `.img` file and click Start
5. USB Burning Tool will wipe and rewrite every partition, fully restoring Android

The official firmware image (`AM9PRO_2.0.9.img`) was inspected and confirmed to contain all required partitions: `super`, `bootloader_a`, `boot_a`, `vendor_boot_a`, `dtbo_a`, `init_boot_a`, `logo`, `odm_ext_a`, and the DTB. A full flash will restore the original 29-partition Android layout.

---

## Files

| File | Description |
|------|-------------|
| `ce-emmc-install.sh` | The installer script |
| `emmc-research.md` | Full technical research notes |
