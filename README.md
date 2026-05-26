# CoreELEC eMMC Installer — Ugoos AM9 Pro

A manual installer for CoreELEC to the internal eMMC of the Ugoos AM9 Pro, documented here for reference while `ceemmc` does not yet support this board.

---

## ⚠️ READ BEFORE PROCEEDING ⚠️

**This installation method is not supported by CoreELEC. Any support request, bug report, or forum post related to a CoreELEC install performed this way will be rejected, closed, or removed by the CoreELEC team. Do not ask for help on the CoreELEC forums if something goes wrong.**

**Tested on one physical Ugoos AM9 Pro across CoreELEC nightlies `22.0-Piers_nightly_20260514` and `22.0-Piers_nightly_20260525`. A different hardware revision or a future firmware build may behave differently — the installer could fail or produce a non-booting eMMC, particularly if CoreELEC changes the cfgload script format the rebuild step depends on. The device cannot be permanently bricked from a software install: `boot0`/`boot1` are hardware write-protected, per-device identity (MAC, serial) lives in RPMB and the Wi-Fi chip's OTP rather than the eMMC, and the Amlogic USB Burning Tool can always restore stock Android over the USB-C OTG port. A bad install means an SD-card recovery cycle, not a dead device. See [`factory-investigation.md`](factory-investigation.md) for the underlying analysis.**

**This process removes Android userdata and the rsv partition. Android can be restored — see [Restoring Android](#restoring-android) below.**

Specifically:

- **Android userdata is unrecoverable.** The userdata partition is encrypted with hardware-bound keys tied to the device's TEE. Even with a raw backup, the data cannot be decrypted. The partition itself is recreated empty on Android restore, and Android reinitializes it on first boot.
- **`super` is preserved.** Unlike older approaches, this script does not delete the `super` partition (p27), which contains Android system images and is used by CoreELEC's `tee-loader.sh` for TEE firmware on some devices. The Android system remains intact on the eMMC.
- **Future firmware may prevent booting entirely.** This method bypasses normal eMMC install tooling. There is no guarantee it will work with any build other than the one it was tested on.
- **No CoreELEC support.** This is explicitly unsupported. Do not file issues or ask for help on CoreELEC forums or Discord.

If you are not comfortable with all of the above, run CoreELEC from the SD card instead.

---

## Background

The Ugoos AM9 Pro runs an Amlogic S905X5 (S6) SoC. CoreELEC supports the hardware and includes the correct DTB (`s6_s905x5_ugoos_am9_pro.dtb`), but as of the May 2026 nightly the automated `ceemmc` install tool does not list this board as supported.

These scripts document what was done to get a working eMMC install on one specific unit. They are published as a technical reference, not a recommendation.

See [`emmc-research.md`](emmc-research.md) for the full research notes.

---

## Requirements

- Ugoos AM9 Pro booted into CoreELEC from an SD card
- CoreELEC nightly build (tested on `22.0-Piers_nightly_20260514` and `22.0-Piers_nightly_20260525`)
- SSH access or direct terminal access to the device

---

## What the installer does

1. Verifies you're on the right board and booting from SD
2. Reads actual partition sizes from the live GPT for the confirmation screen
3. Checks whether `rsv` (p28) contains data and notes it in the confirmation
4. Backs up to `/storage` (on the SD card):
   - `partition_layout.txt` — the full partition table, needed by the restore script
   - `rsv_backup.bin` — the rsv partition (64 MB)
   - `env_backup.bin` — U-Boot environment (p2)
   - `bootloader_a_backup.bin` — bootloader (p7)
5. **Keeps `super` (p27) untouched** — Android system images remain on the eMMC
6. Deletes two Android partitions:
   - `rsv` (p28, ~64 MB) — reserved partition, unknown purpose, backed up first
   - `userdata` (p29, ~54.4 GB) — encrypted, unrecoverable
7. Creates two new partitions in their place:
   - `CE_FLASH` (p28, 512 MB, FAT32) — CoreELEC boot partition
   - `CE_STORAGE` (p29, ~53.9 GB, ext4) — CoreELEC storage
8. Copies all boot files from the SD card's `/flash` to `CE_FLASH`
9. Rebuilds `cfgload` to use `disk=LABEL=CE_STORAGE` instead of the dual-boot `disk=FOLDER=/dev/CE_STORAGE` path, with correct mkimage CRCs (see technical notes)
10. Optionally migrates your existing `/storage` (settings, addons, media) to `CE_STORAGE`, with a free-space check before proceeding

Partitions p1–p26, `super` (p27), `boot0`, and `boot1` are not touched. `boot0`/`boot1` are hardware write-protected and cannot be modified by anything running in Linux.

---

## Running the installer

Copy the script to the device and run it as root:

```bash
# From your computer
scp ce-emmc-install.sh root@<device-ip>:/storage/

# SSH into the device
ssh root@<device-ip>

# Preview what will happen without making changes
bash /storage/ce-emmc-install.sh --dry-run

# Run it
bash /storage/ce-emmc-install.sh
```

The script will walk you through confirmation prompts before making any changes. If `whiptail` is available and the terminal is large enough, it will use a simple TUI for the confirmation dialogs; otherwise it falls back to plain text with a typed `YES` confirmation.

When it's done, remove the SD card and reboot — the device will boot CoreELEC from eMMC automatically.

### After first eMMC boot

SSH host keys are regenerated on a fresh CoreELEC install. Clear your old entry before reconnecting:

```bash
ssh-keygen -R <device-ip>
```

---

## Restoring Android

There are two restore paths depending on whether the backup files from the installer are available.

### Option 1 — Restore script (recommended, no Windows PC required)

If you have the backup files that `ce-emmc-install.sh` saved to the SD card's `/storage`, you can restore the original Android partition layout directly:

```bash
# Boot CoreELEC from the SD card, then:
bash /storage/ce-emmc-restore.sh
```

The restore script:
1. Reads `partition_layout.txt` to reconstruct the exact original p28/p29 boundaries
2. Deletes CE_FLASH and CE_STORAGE
3. Recreates the original `rsv` and `userdata` partitions at their exact original positions
4. Restores `rsv` content from `rsv_backup.bin`
5. Restores `env` and `bootloader_a` from their backups
6. Leaves `super` (p27) untouched — it was never modified

Android's `userdata` partition is recreated empty. Android will reinitialize it on first boot from the system images in `super`. The device will boot as if from a factory reset — you will go through Android setup again, but the OS is intact.

**Note:** All CoreELEC data on CE_STORAGE will be permanently lost when the restore runs.

Use `--dry-run` to preview what will happen before committing:

```bash
bash /storage/ce-emmc-restore.sh --dry-run
```

### Option 2 — Amlogic USB Burning Tool (backup files not available)

The AM9 Pro can be fully restored to stock Android using the Amlogic USB Burning Tool. This works because `boot0` (the BL2 first-stage bootloader) is hardware write-protected and cannot be touched by anything running in Linux — the device can always enter USB burn mode.

**What you need:**

- A Windows PC
- USB Burning Tool v3 (available from Ugoos)
- The official factory firmware image (`AM9PRO_2.0.9.img` or newer)
- A USB-C to USB-A cable

**Process:**

1. Power off the device
2. Hold the recessed reset/ADB button while connecting the **USB-C OTG port** to the PC via USB-C to USB-A cable (the three USB-A ports are host-only and will not work)
3. The device will appear in USB Burning Tool in burn mode
4. Load the factory `.img` file and click Start
5. USB Burning Tool will wipe and rewrite every partition, fully restoring Android

The official firmware image (`AM9PRO_2.0.9.img`) was fully parsed and confirmed to contain all required partitions: `super` (1507 MB, LP metadata + Android system images), `bootloader_a`, `boot_a`, `vendor_boot_a`, `dtbo_a`, `init_boot_a`, `logo`, `odm_ext_a`, and the SoC DTB. The image also includes the GPT table itself, so a full flash restores the original 29-partition Android layout exactly.

**Important:** The factory image restores Android to the state Ugoos shipped it — which includes **Magisk pre-installed** (root access). The device ships with an unlocked bootloader and Magisk patched into `init_boot_a`. The device is certified at Widevine L3 only (no L1 attestation path with an unlocked bootloader), and the per-device identity (MAC, serial) lives in RPMB and the Broadcom chip's OTP — not on the eMMC — so identity is preserved across any restore path. See [`factory-investigation.md`](factory-investigation.md) for the underlying analysis.

---

## Technical notes

### Why CE_FLASH is at p28, not p27

U-Boot's `cfgloademmc` command scans eMMC partitions 1–31 looking for a FAT filesystem containing a `cfgload` script. It finds CE_FLASH by content, not by partition number. CE_FLASH at p28 works identically to p27.

The original install approach placed CE_FLASH at p27 (replacing `super`). The current approach keeps `super` at p27 and places CE_FLASH at p28 (replacing `rsv`). The boot process is unchanged.

### How the cfgload rebuild works

`cfgload` is a compiled U-Boot script in mkimage format with two CRC32 fields in its header (one over the header itself, one over the data). Editing it with a text editor or `sed` changes the content but leaves the old CRCs in place — U-Boot verifies them on load and silently rejects the script, failing without any obvious error.

The installer reads the stock cfgload from the SD card, performs the `FOLDER=/dev/CE_STORAGE` → `LABEL=CE_STORAGE` substitution in the inner script body, then rebuilds the mkimage container with correct CRCs. It does this in Python because `mkimage` is not installed on CoreELEC — the legacy-script format is straightforward to pack with `struct` and `zlib.crc32`. The result is byte-identical (modulo timestamp and header CRC) to what `mkimage -A arm64 -T script -O linux -C none -d <script> <out>` produces.

The rebuild step is idempotent: running it on an already-patched cfgload is a no-op.

This is the same change `ceemmc` would make if it supported this board — `LABEL=`-based mounting goes through the initrd's standard `mount_part` path, no hook scripts or `nofsck` workarounds required.

### Brick risk

Low but non-zero. `boot0`/`boot1` are hardware write-protected — the SoC's first-stage bootloader cannot be overwritten from Linux. U-Boot tries the SD card first, so a working SD card always provides a recovery path. Worst case (corrupted GPT): Amlogic devices can be recovered via USB Burning Tool from a PC. However, broken media playback or boot failures on future firmware are a real possibility with no known fix.

---

## Files

| File | Description |
|------|-------------|
| `ce-emmc-install.sh` | CoreELEC eMMC installer |
| `ce-emmc-restore.sh` | Android partition restore script |
| `emmc-research.md` | Full technical research notes |
| `factory-investigation.md` | Pre-first-boot investigation into where MAC/serial actually live |
