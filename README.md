# Ugoos AM9 Pro research & tooling

This repo contains two related pieces of tooling for the Ugoos AM9 Pro (Amlogic S6 / S905X5):

1. **CoreELEC eMMC installer** (`ce-emmc-install.sh` / `ce-emmc-restore.sh`) — installs CoreELEC to internal eMMC while `ceemmc` does not yet support this board, with a parallel restore script back to stock Android
2. **A native Linux/macOS Amlogic burner** (`aml-dnl-burn.py` + the `aml_dnl_*` library trio) — a port of the relevant subset of the Windows-only Amlogic USB Burning Tool, sufficient to OTA-flash and full-restore the AM9 Pro from a `.img` file without booting Windows. Required only when restoring stock Android over the USB-C OTG port; the eMMC installer itself runs entirely on the device.

---

## ⚠️ READ BEFORE PROCEEDING ⚠️

**This installation method is not supported by CoreELEC. Any support request, bug report, or forum post related to a CoreELEC install performed this way will be rejected, closed, or removed by the CoreELEC team. Do not ask for help on the CoreELEC forums if something goes wrong.**

**Tested on one physical Ugoos AM9 Pro across CoreELEC nightlies from `22.0-Piers_nightly_20260514` through `22.0-Piers_nightly_20260527`, including survival across the auto-update path (see the cfgload-vs-mount-storage.sh discussion in the technical notes). A different hardware revision or a future firmware build may behave differently — the installer could fail or produce a non-booting eMMC, particularly if CoreELEC changes the cfgload script format the rebuild step depends on or restructures the `mount_storage` init function in a way that bypasses `/flash/mount-storage.sh`. The device cannot be permanently bricked from a software install: `boot0`/`boot1` are hardware write-protected, and the Amlogic USB Burning Tool (or the bundled `aml-dnl-burn.py`) can always restore stock Android over the USB-C OTG port. Per-device identity is preserved across the install too — the installer does not touch the `reserved` partition (p1) where the ETH MAC and serial are stored, and the WLAN/BT MAC lives in the Wi-Fi chip's OTP entirely independent of the eMMC. A bad install means an SD-card recovery cycle, not a dead device. See [`factory-investigation.md`](factory-investigation.md) and the [Per-Device Identity Provenance](emmc-research.md#per-device-identity-provenance) section in the research notes for the verified analysis.**

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
4. **Cross-checks device identity** — confirms the running `androidboot.serialno` and `mac=` on the kernel cmdline agree with the AMLNORMAL keystore values stored in `reserved` (p1). Aborts if they disagree (would indicate tampering or partial-flash state)
5. Backs up to `/storage` (on the SD card):
   - `partition_layout.txt` — the full partition table, needed by the restore script
   - `rsv_backup.bin` — the rsv partition (64 MB)
   - `env_backup.bin` — U-Boot environment (p2)
   - `bootloader_a_backup.bin` — bootloader (p7, 8 MB)
   - `reserved_backup.bin` — `reserved` partition (p1, 64 MB), which holds the Amlogic UKS keystore with the device's ETH MAC and serial. The installer doesn't write to p1, but the backup is cheap insurance because the factory image doesn't include p1 either — if it were ever wiped, USB Burning Tool restore would NOT bring it back. **The backup is verified after writing**: if `aml-keystore-tool.py info` doesn't see a valid AMLNORMAL header + populated slot count, the install aborts before any destructive operations.
   - `frp_backup.bin` — `frp` partition (p3, 2 MB) — contains 36 bytes of unit-unique anti-rollback / FRP signing material
   - `param_backup.bin` — `param` partition (p15, 16 MB) — ext4 filesystem with the TV picture-quality DB (`pq.db`, `TV_PICTURE`), likely tuned per-device at the factory
6. **Keeps `super` (p27) untouched** — Android system images remain on the eMMC
7. Deletes two Android partitions:
   - `rsv` (p28, ~64 MB) — reserved partition, unknown purpose, backed up first
   - `userdata` (p29, ~54.4 GB) — encrypted, unrecoverable
8. Creates two new partitions in their place:
   - `CE_FLASH` (p28, 512 MB, FAT32) — CoreELEC boot partition
   - `CE_STORAGE` (p29, ~53.9 GB, ext4) — CoreELEC storage
9. Copies all boot files from the SD card's `/flash` to `CE_FLASH`
10. Rebuilds `cfgload` to use `disk=LABEL=CE_STORAGE` instead of the dual-boot `disk=FOLDER=/dev/CE_STORAGE` path, with correct mkimage CRCs (see technical notes). Pass `--no-cfgload-rebuild` to skip this step.
11. **Always installs `/flash/mount-storage.sh` + adds `nofsck` to `config.ini`** — these are the durable rescue layer. CoreELEC's nightly updater unconditionally overwrites `cfgload` from its stock source, reverting the step-10 patch on every update. `mount-storage.sh` is a first-class CE init hook (line ~635 of `/init` sources it instead of `mount_part`) that bypasses the broken `FOLDER=` mount path entirely; `nofsck` suppresses the retry loop the missing `/dev/CE_STORAGE` node would otherwise cause. Both files are user-added and never touched by the updater, so the device boots cleanly through every nightly update.
12. With `--restore-logo PATH`: writes a custom boot logo to `p10` (the AML_RES container). PATH can be either a packed `.bin` (validated against the `AML_RES!` magic) or a directory of `NN_name.bmp` files produced by `aml-logo-tool.py unpack`.
13. Optionally migrates your existing `/storage` (settings, addons, media) to `CE_STORAGE`, with a free-space check before proceeding

The installer also supports **`--info`** — a read-only diagnostic mode that prints the partition layout, eMMC chip details, U-Boot env summary, AMLNORMAL keystore contents, bootloader build version, current install state (with warnings if legacy workarounds are present), and the cmdline-vs-keystore identity check result. No backups, no writes, safe to run any time. Useful before committing to an install.

Partitions p1–p26 (except p10 if `--restore-logo` is used), `super` (p27), `boot0`, and `boot1` are not touched. `boot0`/`boot1` are hardware write-protected and cannot be modified by anything running in Linux. The installer is a self-contained bash script; it picks up `aml-keystore-tool.py` / `aml-bootloader-tool.py` / `aml-logo-tool.py` from the same directory if they're there (for verification / `--info` / `--restore-logo` respectively).

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

**Important:** The factory image restores Android to the state Ugoos shipped it — which includes **Magisk pre-installed** (root access). The device ships with an unlocked bootloader and Magisk patched into `init_boot_a`. The device is certified at Widevine L3 only (no L1 attestation path with an unlocked bootloader). Per-device identity is preserved across the burn because the factory image does not include the `reserved` partition (p1) where the ETH MAC and serial are stored — USB Burning Tool leaves p1 alone. The WLAN/BT MAC lives in the Broadcom chip's OTP and is entirely independent of the eMMC. See [`factory-investigation.md`](factory-investigation.md) for the underlying analysis.

---

## Technical notes

### Why CE_FLASH is at p28, not p27

U-Boot's `cfgloademmc` command scans eMMC partitions 1–31 looking for a FAT filesystem containing a `cfgload` script. It finds CE_FLASH by content, not by partition number. CE_FLASH at p28 works identically to p27.

The original install approach placed CE_FLASH at p27 (replacing `super`). The current approach keeps `super` at p27 and places CE_FLASH at p28 (replacing `rsv`). The boot process is unchanged.

### How the cfgload rebuild works

`cfgload` is a compiled U-Boot script in mkimage format with two CRC32 fields in its header (one over the header itself, one over the data). Editing it with a text editor or `sed` changes the content but leaves the old CRCs in place — U-Boot verifies them on load and silently rejects the script, failing without any obvious error.

The installer reads the stock cfgload from the SD card, performs the `FOLDER=/dev/CE_STORAGE` → `LABEL=CE_STORAGE` substitution in the inner script body, then rebuilds the mkimage container with correct CRCs. It does this in Python because `mkimage` is not installed on CoreELEC — the legacy-script format is straightforward to pack with `struct` and `zlib.crc32`. The result is byte-identical (modulo timestamp and header CRC) to what `mkimage -A arm64 -T script -O linux -C none -d <script> <out>` produces. The same packing logic is exposed as a standalone utility in [`scripts/make-cfgload.py`](scripts/make-cfgload.py).

The rebuild step is idempotent: running it on an already-patched cfgload is a no-op.

### Why mount-storage.sh + nofsck is also installed

The cfgload patch above is one half of the story. The other half: **CoreELEC's nightly updater overwrites `cfgload` on every update** — `/usr/share/bootloader/update.sh` unconditionally does `cp -p /usr/share/bootloader/${DEVICE_CFGLOAD} /flash/cfgload`, reverting the rebuild. Without a second layer of defense, every nightly update would break eMMC boot until the user manually re-ran the patch.

CoreELEC's `/init` already supports a post-update hook at `/flash/user-update.sh`, which runs after `update_bootloader` returns and before `do_reboot`. An earlier version of this installer used that hook to re-apply the cfgload patch. **It does not work**: the hook runs in the initramfs context where only `busybox`, `sh`, and `splash-image` are present — Python isn't available, so the hook crashes immediately and the device bootloops.

The working solution: install two CE-supported customization files that live on `/flash` as user files (never touched by the updater):

- **`/flash/mount-storage.sh`** — sourced by `/init`'s `mount_storage()` function instead of the default `mount_part "$disk" "/storage"`. It runs `mount -t ext4 -o rw,noatime LABEL=CE_STORAGE /storage`, completely bypassing the broken `disk=FOLDER=/dev/CE_STORAGE` value in `bootargs`.
- **`nofsck` in `coreelec=` config.ini** — appended via `setenv bootargs`, suppresses CE's fsck retry loop on the bogus `/dev/CE_STORAGE` node.

The result: even if (when) a future CE nightly reverts cfgload back to its stock `FOLDER=/dev/CE_STORAGE` form, `mount-storage.sh` rescues the mount and the device keeps booting. The cfgload patch becomes a nice-to-have rather than survival-critical.

This is the same outcome `ceemmc` would produce natively if it supported this board — `LABEL=`-based mounting through the standard `mount_part` path with no hook scripts needed. We can't get there without upstream support, so we ship the hook + nofsck as a stable workaround.

### Brick risk

Low but non-zero. `boot0`/`boot1` are hardware write-protected — the SoC's first-stage bootloader cannot be overwritten from Linux. U-Boot tries the SD card first, so a working SD card always provides a recovery path. Worst case (corrupted GPT): Amlogic devices can be recovered via USB Burning Tool from a PC. However, broken media playback or boot failures on future firmware are a real possibility with no known fix.

---

## Files

### CoreELEC eMMC installer

| File | Description |
|------|-------------|
| `ce-emmc-install.sh` | CoreELEC eMMC installer |
| `ce-emmc-restore.sh` | Android partition restore script |

### Shared libraries (at project root)

| File | Description |
|------|-------------|
| `aml_dnl_proto.py` | Layer 1 — USB transport + ADNL/DNL wire protocol (CBW, OUT/IN data acks, identify, getvar, oem, etc.). |
| `aml_dnl_ops.py` | Layer 2 — partition flash, addsum verification, DDR firmware load, CBW-driven uboot upload. |
| `aml_dnl_flows.py` | Layer 3 — composable flows (`plan_android_slot_a_update`, `plan_full_restore`, `execute_*`) built on Layers 1+2. |
| `aml_img.py` | USB-free Amlogic `.img` parser (AML_PACK_v2 format) + Android sparse-image stream decoder. Shared by the host-side and in-device burners. Uses `mmap` so even a 1.6 GB OTA stays light on RAM. |
| `am9pro-usb-restore.sh` | Bash wrapper that verifies a `.img`, sanity-checks the closed-source `adnl` binary if present, and (when device is in burn mode) confirms it identifies cleanly. Predates the native burner — kept for the closed-source verification path. **Read-only.** |

### Subdirectories

| Path | What's in it |
|------|--------------|
| [`burn/`](burn/README.md) | The active flashers — `aml-dnl-burn.py` (USB DNL from a host), `aml-dnl-status.py` (read-only USB probe), and `aml-emmc-burn.py` (direct eMMC writes from inside CE). |
| [`img-tools/`](img-tools/README.md) | Standalone file-format CLIs that operate on local files only: `aml-img-tool.py`, `aml-bootloader-tool.py`, `aml-keystore-tool.py`, `aml-logo-tool.py`. Used by `ce-emmc-install.sh` for identity / logo / bootloader inspection. |
| [`probes/`](probes/README.md) | One-off diagnostic & RE bring-up scripts used to map the ADNL protocol. Not user-facing; kept as reference. |
| [`scripts/`](scripts/README.md) | Small standalone utilities. Currently: `make-cfgload.py` (pure-Python `mkimage -T script` equivalent). |
| [`docs/`](docs/) | Supporting reference documentation. |
| [`aml-analysis/`](aml-analysis/README.md) | Reverse-engineering artifacts for the Windows `Aml_Burn_Tool.exe` + decryption of the embedded `usb_flow.aml` Lua scripts. |

### Documentation

| File | Description |
|------|-------------|
| `emmc-research.md` | Full technical research notes |
| `factory-investigation.md` | Pre-first-boot investigation into where MAC/serial actually live |
| `dnl-protocol-from-capture.md` | Wire-protocol decoding notes for the ADNL/DNL protocol used by the native burner |
