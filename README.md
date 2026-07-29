# Ugoos S905X5 research & tooling — AM9 Pro / SK4 / SK4 Pro

This repo contains two related pieces of tooling for Ugoos boxes built on the Amlogic S905X5 family:

| Device | SoC | CoreELEC board ID |
|--------|-----|-------------------|
| Ugoos AM9 Pro | S905X5-J (S6) | `s6_s905x5_ugoos_am9_pro` |
| Ugoos SK4 | S905X5M-J (S7D) | `s7d_s905x5m_ugoos_sk4` |
| Ugoos SK4 Pro | S905X5M-J (S7D) | `s7d_s905x5m_ugoos_sk4` (same DTB as the SK4) |

1. **CoreELEC eMMC installer** (`ce-emmc-install.sh` / `ce-emmc-restore.sh`) — installs CoreELEC to internal eMMC while `ceemmc` does not yet support these boards, with a parallel restore script back to stock Android. **Tested on all three devices above.**
2. **A native Linux/macOS Amlogic burner** (`burn/aml-dnl-burn.py` + the `lib/aml_dnl_*` library trio) — a port of the relevant subset of the Windows-only Amlogic USB Burning Tool, sufficient to OTA-flash and full-restore from a factory `.img` without booting Windows. Required only when restoring stock Android over the USB-C OTG port; the eMMC installer itself runs entirely on the device. **Exercised on AM9 Pro only** — see [Scope of testing](#scope-of-testing).

The installer gates on the active DTB, so it accepts exactly the boards listed above. Adding another Ugoos S905X5 board with the same Amlogic partition layout is a one-line change to `SUPPORTED_BOARDS` in `ce-emmc-install.sh`.

---

## ⚠️ READ BEFORE PROCEEDING ⚠️

**This installation method is not supported by CoreELEC. Any support request, bug report, or forum post related to a CoreELEC install performed this way will be rejected, closed, or removed by the CoreELEC team. Do not ask for help on the CoreELEC forums if something goes wrong.**

**Tested on physical Ugoos AM9 Pro, SK4, and SK4 Pro hardware (see [Scope of testing](#scope-of-testing) for exactly what was verified where). A different hardware revision or a future firmware build may behave differently — the installer could fail or produce a non-booting eMMC, particularly if CoreELEC changes the cfgload script format the rebuild step depends on or restructures the `mount_storage` init function in a way that bypasses `/flash/mount-storage.sh`. The device cannot be permanently bricked from a software install: `boot0`/`boot1` are hardware write-protected, and the Amlogic USB Burning Tool can always restore stock Android over the USB-C OTG port. Per-device identity is preserved across the install too — the installer does not touch the `reserved` partition (p1) where the ETH MAC and serial are stored, and the WLAN/BT MAC lives in the Wi-Fi chip's OTP entirely independent of the eMMC. A bad install means an SD-card recovery cycle, not a dead device. See [`factory-investigation.md`](research/factory-investigation.md) and the [Per-Device Identity Provenance](research/emmc-research.md#per-device-identity-provenance) section in the research notes for the verified analysis.**

**This process removes Android userdata and the rsv partition. Android can be restored — see [Restoring Android](#restoring-android) below.**

Specifically:

- **Android userdata is unrecoverable.** The userdata partition is encrypted with hardware-bound keys tied to the device's TEE. Even with a raw backup, the data cannot be decrypted. The partition itself is recreated empty on Android restore, and Android reinitializes it on first boot.
- **`super` is preserved.** Unlike older approaches, this script does not delete the `super` partition (p27), which contains Android system images and is used by CoreELEC's `tee-loader.sh` for TEE firmware on some devices. The Android system remains intact on the eMMC.
- **Future firmware may prevent booting entirely.** This method bypasses normal eMMC install tooling. There is no guarantee it will work with any build other than the one it was tested on.
- **No CoreELEC support.** This is explicitly unsupported. Do not file issues or ask for help on CoreELEC forums or Discord.

If you are not comfortable with all of the above, run CoreELEC from the SD card / USB stick instead.

---

## Background

The Ugoos AM9 Pro runs an Amlogic S905X5 (S6); the SK4 and SK4 Pro run the S905X5M (S7D). CoreELEC supports all three and ships the correct DTBs (`s6_s905x5_ugoos_am9_pro.dtb`, and `s7d_s905x5m_ugoos_sk4.dtb` for both SK4 variants), but the automated `ceemmc` install tool does not list any of these boards as supported.

They share the same Amlogic 29-partition Android layout, which is why one installer covers all three: `super` at p27, `rsv` at p28, `userdata` at p29, with the keystore in `reserved` (p1) and the U-Boot env at p2.

These scripts document what was done to get a working eMMC install on specific units. They are published as a technical reference, not a recommendation.

See [`emmc-research.md`](research/emmc-research.md) for the full research notes.

---

## Scope of testing

Not every part of this repo has been exercised on every device. What was verified where:

| Component | AM9 Pro | SK4 | SK4 Pro |
|-----------|---------|-----|---------|
| `ce-emmc-install.sh` — install to eMMC | ✅ | ✅ | ✅ |
| `ce-emmc-restore.sh` — restore Android layout | ✅ | ✅ | ✅ |
| Survival across CoreELEC nightly auto-updates | ✅ | ✅ | ✅ |
| `burn/` — USB DNL burner + in-device eMMC flasher | ✅ | ❌ untested | ❌ untested |
| `img-tools/` — keystore / bootloader / logo / img parsing | ✅ | partial¹ | partial¹ |
| `research/` — protocol captures, factory image analysis | ✅ | ❌ not repeated | ❌ not repeated |

¹ `aml-keystore-tool.py` is used by the installer's identity cross-check on every board, so it is exercised on all three. The logo, bootloader, and `AML_PACK_v2` image tooling was only ever pointed at AM9 Pro artifacts.

AM9 Pro coverage spans CoreELEC nightlies `22.0-Piers_nightly_20260514` through `22.0-Piers_nightly_20260527`, including survival across the auto-update path (see the cfgload-vs-`mount-storage.sh` discussion in the technical notes). SK4 and SK4 Pro were verified against the nightlies current at the time of their installs.

The `burn/` and `research/` work is AM9 Pro-specific because it was built from a USB capture of the Windows burner flashing `AM9PRO_2.1.0.img` and from that image's contents. The DNL protocol itself is SoC-generic and the S905X5M should speak it identically, but nothing in `burn/` has been pointed at an SK4 or a Ugoos SK4 factory image. **Do not assume a full-restore flow that works on AM9 Pro will work on an SK4** — verify with `dry-run` first.

---

## Requirements

- A Ugoos AM9 Pro, SK4, or SK4 Pro booted into CoreELEC from removable media (SD card or USB stick — anywhere except the eMMC being repartitioned)
- CoreELEC nightly build (AM9 Pro tested on `22.0-Piers_nightly_20260514` through `22.0-Piers_nightly_20260527`)
- SSH access or direct terminal access to the device

---

## What the installer does

1. Verifies you're on a supported board (md5-matching the active `/flash/dtb.img` against the DTB list in `SUPPORTED_BOARDS`) and booting from removable media, and that the eMMC still has the expected Android layout — partition **names** (`super`/`rsv`/`userdata`) are checked, not just numbers, so a partially-completed previous run is caught here instead of poisoning the backups below
2. Reads actual partition sizes from the live GPT for the confirmation screen
3. Checks whether `rsv` (p28) contains data and notes it in the confirmation
4. **Cross-checks device identity** — confirms the running `androidboot.serialno` and `mac=` on the kernel cmdline agree with the AMLNORMAL keystore values stored in `reserved` (p1). Aborts if they disagree (would indicate tampering or partial-flash state)
5. Backs up to `/storage/emmc-backup` (on the boot media — the installer refuses to run if that directory already exists non-empty, so a rerun can never clobber a previous backup set):
   - `partition_layout.txt` — the full partition table, needed by the restore script
   - `gpt_primary.bin` / `gpt_secondary.bin` — the raw GPT tables (the parted text dump captures geometry and names; the raw tables also preserve type GUIDs, unique GUIDs, and attribute flags for an exact restore if ever needed)
   - `rsv_backup.bin` — the rsv partition (64 MB)
   - `env_backup.bin` — U-Boot environment (p2)
   - `bootloader_a_backup.bin` — bootloader (p7, 8 MB), plus `bootloader_b_backup.bin` if the layout has one
   - `reserved_backup.bin` — `reserved` partition (p1, 64 MB), which holds the Amlogic UKS keystore with the device's ETH MAC and serial. The installer doesn't write to p1, but the backup is cheap insurance because the factory image doesn't include p1 either — if it were ever wiped, USB Burning Tool restore would NOT bring it back. **The backup is verified after writing**: if `aml-keystore-tool.py info` doesn't see a valid AMLNORMAL header + populated slot count, the install aborts before any destructive operations.
   - `frp_backup.bin` — `frp` partition (p3, 2 MB) — contains 36 bytes of unit-unique anti-rollback / FRP signing material
   - `param_backup.bin` — `param` partition (p15, 16 MB) — ext4 filesystem with the TV picture-quality DB (`pq.db`, `TV_PICTURE`), likely tuned per-device at the factory

   Every `dd` backup is size-verified against its partition (catches silent short reads), and the whole set is `sync`'d to the boot media before the first destructive operation.
6. **Keeps `super` (p27) untouched** — Android system images remain on the eMMC
7. Deletes two Android partitions:
   - `rsv` (p28, ~64 MB) — reserved partition, unknown purpose, backed up first
   - `userdata` (p29, ~54.4 GB) — encrypted, unrecoverable
8. Creates two new partitions in their place:
   - `CE_FLASH` (p28, 512 MB, FAT32) — CoreELEC boot partition
   - `CE_STORAGE` (p29, ~53.9 GB, ext4) — CoreELEC storage
9. Copies all boot files from the boot media's `/flash` to `CE_FLASH`
10. Rebuilds `cfgload` to use `disk=LABEL=CE_STORAGE` instead of the dual-boot `disk=FOLDER=/dev/CE_STORAGE` path, with correct mkimage CRCs (see technical notes). Pass `--no-cfgload-rebuild` to skip this step.
11. **Always installs `/flash/mount-storage.sh` + adds `nofsck` to `config.ini`** — these are the durable rescue layer. CoreELEC's nightly updater unconditionally overwrites `cfgload` from its stock source, reverting the step-10 patch on every update. `mount-storage.sh` is a first-class CE init hook (line ~635 of `/init` sources it instead of `mount_part`) that bypasses the broken `FOLDER=` mount path entirely; `nofsck` suppresses the retry loop the missing `/dev/CE_STORAGE` node would otherwise cause. Both files are user-added and never touched by the updater, so the device boots cleanly through every nightly update.
12. With `--restore-logo PATH`: writes a custom boot logo to `p10` (the AML_RES container). PATH can be either a packed `.bin` (validated against the `AML_RES!` magic) or a directory of `NN_name.bmp` files produced by `aml-logo-tool.py unpack`.
13. Optionally migrates your existing `/storage` (settings, addons, media) to `CE_STORAGE` — with a free-space check before proceeding, Kodi stopped during the copy so its local SQLite databases aren't copied hot, and a size/entry-count verification of the result afterwards (a failed verification prints manual recovery steps instead of silently proceeding)

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

The script will walk you through confirmation prompts before making any changes. If `whiptail` is available and the terminal is large enough, it will use a simple TUI for the confirmation dialogs; otherwise it falls back to plain text. Either way, the destructive step requires typing `YES` — a single keypress is deliberately not enough to delete partitions.

When it's done, remove the SD card / USB stick and reboot — the device will boot CoreELEC from eMMC automatically.

### After first eMMC boot

SSH host keys are regenerated on a fresh CoreELEC install. Clear your old entry before reconnecting:

```bash
ssh-keygen -R <device-ip>
```

---

## Restoring Android

There are two restore paths depending on whether the backup files from the installer are available.

### Option 1 — Restore script (recommended, no Windows PC required)

If you have the backup files that `ce-emmc-install.sh` saved to the boot media's `/storage/emmc-backup` (older installer versions wrote them flat into `/storage` — the restore script finds either layout automatically), you can restore the original Android partition layout directly:

```bash
# Boot CoreELEC from the SD card / USB stick, then:
bash /storage/ce-emmc-restore.sh
```

The restore script:
1. Reads `partition_layout.txt` to reconstruct the exact original p28/p29 boundaries
2. Deletes CE_FLASH and CE_STORAGE
3. Recreates the original `rsv` and `userdata` partitions at their exact original positions
4. Restores `rsv` content from `rsv_backup.bin`
5. Restores `env` and `bootloader_a` from their backups (plus `frp`, `param`, and `bootloader_b` if those backups exist)
6. Leaves `super` (p27) untouched — it was never modified

Android's `userdata` partition is recreated empty. Android will reinitialize it on first boot from the system images in `super`. The device will boot as if from a factory reset — you will go through Android setup again, but the OS is intact.

**Note:** All CoreELEC data on CE_STORAGE will be permanently lost when the restore runs.

Use `--dry-run` to preview what will happen before committing:

```bash
bash /storage/ce-emmc-restore.sh --dry-run
```

### If a script stops during repartitioning

Both scripts verify the on-disk partition table after every `parted` step and abort immediately if a step did not take effect. The usual cause is the kernel refusing the table update because something still had an eMMC partition in use (see [issue #1](https://github.com/dangerouslaser/ugoos-am9-pro-coreelec-emmc/issues/1)); both scripts now unmount any auto-mounted eMMC partitions before repartitioning, but other holders (a shell sitting in a mounted path, an unfinished device scan) can still trigger it.

Recovery options, in order of preference:

1. **Reboot and re-run `ce-emmc-restore.sh`.** The restore script recognizes a half-repartitioned table and skips whatever is already done, returning the device to the original Android layout. From there you can re-run the installer (move `/storage/emmc-backup` aside first).
2. **Restore the raw GPT from the backup set.** Partition-table edits never touch partition contents, so before any formatting has happened this returns the eMMC to exactly its pre-install state:

   ```bash
   SECTORS=$(cat /sys/block/mmcblk0/size)
   dd if=/storage/emmc-backup/gpt_primary.bin of=/dev/mmcblk0 bs=512 count=34
   dd if=/storage/emmc-backup/gpt_secondary.bin of=/dev/mmcblk0 bs=512 seek=$((SECTORS - 33))
   sync && reboot
   ```

   Then move `/storage/emmc-backup` aside and re-run the installer.

### Option 2 — Amlogic USB Burning Tool (backup files not available)

Any of these boxes can be fully restored to stock Android using the Amlogic USB Burning Tool. This works because `boot0` (the BL2 first-stage bootloader) is hardware write-protected and cannot be touched by anything running in Linux — the device can always enter USB burn mode.

**What you need:**

- A Windows PC
- USB Burning Tool v3 (available from Ugoos)
- The official factory firmware image **for your specific model** — `AM9PRO_2.0.9.img` or newer for the AM9 Pro, the corresponding SK4 / SK4 Pro image for those. These are not interchangeable; flashing the wrong model's image will produce a non-booting device.
- A USB-C to USB-A cable

**Process:**

1. Power off the device
2. Hold the recessed reset/ADB button while connecting the **USB-C OTG port** to the PC via USB-C to USB-A cable (on the AM9 Pro, the three USB-A ports are host-only and will not work; check your model's port layout — the SK4 and SK4 Pro differ)
3. The device will appear in USB Burning Tool in burn mode
4. Load the factory `.img` file and click Start
5. USB Burning Tool will wipe and rewrite every partition, fully restoring Android

The AM9 Pro factory image (`AM9PRO_2.0.9.img`) was fully parsed and confirmed to contain all required partitions: `super` (1507 MB, LP metadata + Android system images), `bootloader_a`, `boot_a`, `vendor_boot_a`, `dtbo_a`, `init_boot_a`, `logo`, `odm_ext_a`, and the SoC DTB. The image also includes the GPT table itself, so a full flash restores the original 29-partition Android layout exactly. The SK4 / SK4 Pro images were not parsed as part of this work, but they use the same `AML_PACK_v2` container — `img-tools/aml-img-tool.py` will inspect them.

**Important:** The factory image restores Android to the state Ugoos shipped it — which for the AM9 Pro includes **Magisk pre-installed** (root access). That unit ships with an unlocked bootloader and Magisk patched into `init_boot_a`, and is certified at Widevine L3 only (no L1 attestation path with an unlocked bootloader). Whether the SK4 / SK4 Pro stock images ship the same way was not checked. Per-device identity is preserved across the burn because the factory image does not include the `reserved` partition (p1) where the ETH MAC and serial are stored — USB Burning Tool leaves p1 alone. The WLAN/BT MAC lives in the Wi-Fi chip's OTP and is entirely independent of the eMMC. See [`factory-investigation.md`](research/factory-investigation.md) for the underlying analysis.

---

## Technical notes

### Why CE_FLASH is at p28, not p27

U-Boot's `cfgloademmc` command scans eMMC partitions 1–31 looking for a FAT filesystem containing a `cfgload` script. It finds CE_FLASH by content, not by partition number. CE_FLASH at p28 works identically to p27.

The original install approach placed CE_FLASH at p27 (replacing `super`). The current approach keeps `super` at p27 and places CE_FLASH at p28 (replacing `rsv`). The boot process is unchanged.

### How the cfgload rebuild works

`cfgload` is a compiled U-Boot script in mkimage format with two CRC32 fields in its header (one over the header itself, one over the data). Editing it with a text editor or `sed` changes the content but leaves the old CRCs in place — U-Boot verifies them on load and silently rejects the script, failing without any obvious error.

The installer reads the stock cfgload from the boot media, performs the `FOLDER=/dev/CE_STORAGE` → `LABEL=CE_STORAGE` substitution in the inner script body, then rebuilds the mkimage container with correct CRCs. It does this in Python because `mkimage` is not installed on CoreELEC — the legacy-script format is straightforward to pack with `struct` and `zlib.crc32`. The result is byte-identical (modulo timestamp and header CRC) to what `mkimage -A arm64 -T script -O linux -C none -d <script> <out>` produces. The same packing logic is exposed as a standalone utility in [`scripts/make-cfgload.py`](scripts/make-cfgload.py).

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
| `ce-emmc-install.sh` | CoreELEC eMMC installer. Board-gated on `SUPPORTED_BOARDS` — AM9 Pro, SK4, SK4 Pro. |
| `ce-emmc-restore.sh` | Android partition restore script. Board-agnostic — it reconstructs the layout from the installer's backup set, so it works on any board the installer ran on. |

### Root files

| File | Description |
|------|-------------|
| `am9pro-usb-restore.sh` | Bash wrapper that verifies a `.img`, sanity-checks the closed-source `adnl` binary if present, and (when device is in burn mode) confirms it identifies cleanly. Predates the native burner — kept for the closed-source verification path. Named for the AM9 Pro because that is the only model it has been run against. **Read-only.** |

### Subdirectories

| Path | What's in it |
|------|--------------|
| [`burn/`](burn/README.md) | The active flashers — `aml-dnl-burn.py` (USB DNL from a host), `aml-dnl-status.py` (read-only USB probe), and `aml-emmc-burn.py` (direct eMMC writes from inside CE). |
| [`img-tools/`](img-tools/README.md) | Standalone file-format CLIs that operate on local files only: `aml-img-tool.py`, `aml-bootloader-tool.py`, `aml-keystore-tool.py`, `aml-logo-tool.py`. Used by `ce-emmc-install.sh` for identity / logo / bootloader inspection. |
| [`lib/`](lib/) | Shared Python libraries imported by the burners and probes: `aml_dnl_proto.py` (Layer 1 USB transport + ADNL/DNL wire protocol), `aml_dnl_ops.py` (Layer 2 partition flash + addsum + DDR load + CBW uboot upload), `aml_dnl_flows.py` (Layer 3 composable plans), `aml_img.py` (USB-free AML_PACK_v2 parser + Android sparse decoder, mmap-backed). |
| [`probes/`](probes/README.md) | One-off diagnostic & RE bring-up scripts used to map the ADNL protocol. Not user-facing; kept as reference. |
| [`scripts/`](scripts/README.md) | Small standalone utilities. Currently: `make-cfgload.py` (pure-Python `mkimage -T script` equivalent). |
| [`research/`](research/README.md) | Technical research notes and frozen evidence snapshots — `emmc-research.md`, `factory-investigation.md`, `dnl-protocol-from-capture.md`, and the AM9PRO_2.1.0 factory snapshot data. |
| [`aml-analysis/`](aml-analysis/README.md) | Reverse-engineering artifacts for the Windows `Aml_Burn_Tool.exe` + decryption of the embedded `usb_flow.aml` Lua scripts. |
