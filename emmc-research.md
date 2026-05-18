# Ugoos AM9 Pro — CoreELEC eMMC Research Notes

**Date:** May 14, 2026  
**Device:** Ugoos AM9 Pro  
**SoC:** Amlogic S905X5-J (S6 family per CoreELEC board ID `s6_s905x5_ugoos_am9_pro`; serial `0x3e` in Amlogic's internal numbering, referred to as "S5" in tee-loader.sh; the -J suffix denotes Dolby Vision licensing)  
**CPU:** Quad-core ARMv9.0 Cortex-A510  
**GPU:** Mali G310 V5  
**RAM:** 4 GB LPDDR5  
**CoreELEC:** 22.0-Piers_nightly_20260514  
**Kernel:** 5.15.196  

---

## Final Setup

CoreELEC boots from internal eMMC (`mmcblk0`, 58.2 GB Samsung A31M8C). `CE_FLASH` (p28, 512 MB FAT32) holds the boot files; `CE_STORAGE` (p29, ~53.9 GB ext4) holds settings and addons. `super` (p27) is preserved untouched — Android system images remain on the eMMC. SD card is no longer needed and can be removed.

---

## eMMC Hardware

- **Chip:** Samsung A31M8C, 58.2 GiB
- **CID:** `ec29004133314d3843309ea834a5bc00`
- **Firmware rev:** `0x0303000200000000`
- **Health:** `life_time=0x01 0x01`, `pre_eol_info=0x01` — essentially brand new, no wear
- **RPMB:** Provisioned (`rpmb_state=0x1`), 4 MB, enhanced RPMB supported

---

## Partition Layout

Offsets from dmesg, all 29 partitions confirmed:

| # | Name | Offset | Size | Status |
|---|------|--------|------|--------|
| p1 | reserved | 0x2400000 | 64 MB | zeroed — confirmed all-null; not in factory image |
| p2 | env | 0x6c00000 | 8 MB | **active** — U-Boot env |
| p3 | frp | 0x8400000 | 2 MB | unknown |
| p4 | factory | 0x8e00000 | 8 MB | unknown |
| p5 | vendor_boot_a | 0x9700000 | 64 MB | zeroed |
| p6 | vendor_boot_b | 0xd800000 | 64 MB | zeroed |
| p7 | bootloader_a | 0x11900000 | 8 MB | **active** — Amlogic `@ML` header |
| p8 | bootloader_b | 0x12200000 | 8 MB | unknown |
| p9 | tee | 0x12b00000 | 32 MB | **zeroed** |
| p10 | logo | 0x14c00000 | 8 MB | unknown |
| p11 | misc | 0x15500000 | 2 MB | unknown |
| p12 | dtbo_a | 0x15800000 | 2 MB | unknown |
| p13 | dtbo_b | 0x15b00000 | 2 MB | unknown |
| p14 | cri_data | 0x15e00000 | 8 MB | unknown |
| p15 | param | 0x16700000 | 16 MB | unknown |
| p16 | odm_ext_a | 0x17800000 | 16 MB | empty (also zeroed in factory image) |
| p17 | odm_ext_b | 0x18900000 | 16 MB | unknown |
| p18 | boot_a | 0x19a00000 | 64 MB | zeroed |
| p19 | boot_b | 0x1db00000 | 64 MB | zeroed |
| p20 | init_boot_a | 0x21c00000 | 8 MB | zeroed |
| p21 | init_boot_b | 0x22500000 | 8 MB | zeroed |
| p22 | metadata | 0x22e00000 | 64 MB | unknown |
| p23 | vbmeta_a | 0x26f00000 | 2 MB | **zeroed** |
| p24 | vbmeta_b | 0x27200000 | 2 MB | **zeroed** |
| p25 | vbmeta_system_a | 0x27500000 | 2 MB | **zeroed** |
| p26 | vbmeta_system_b | 0x27800000 | 2 MB | **zeroed** |
| p27 | super | 0x27b00000 | ~3.1 GB | **preserved** — Android dynamic partition (LP metadata + system/vendor images) |
| p28 | CE_FLASH | — | 512 MB | **active** — FAT32, CoreELEC boot files |
| p29 | CE_STORAGE | — | ~53.9 GB | **active** — ext4, CoreELEC storage |

The original Android layout had 29 partitions. Of those inspected: `boot_a/b`, `vbmeta_a/b`, `vendor_boot_a/b`, `init_boot_a/b`, `tee`, and `super` appeared empty on this unit. `bootloader_a` and `env` had live data. `userdata` was encrypted. The `metadata` partition holds FBE-related data and was not fully characterized.

For the CoreELEC install, `rsv` (p28) and `userdata` (p29) were deleted to free GPT slots, and `CE_FLASH` and `CE_STORAGE` were created in their place. `super` (p27) was preserved. The GPT was originally allocated for exactly 29 entries — deleting 2 and creating 2 keeps the total at 29.

---

## Encryption and Security

### Hardware eMMC Scrambling

The kernel command line includes `scramble_reg=0xfe02e030`. Reading that register returns `0x1`, meaning Amlogic's hardware eMMC scrambler is enabled.

This is a hardware-level data whitening mechanism built into the Amlogic eMMC controller — not AES encryption. It scrambles data patterns to improve wear leveling and reduce stuck bits. The key is fused into the SoC hardware and the scrambling is completely transparent to the OS. You read and write normally and the controller handles it underneath. This is NOT what's protecting the userdata.

### Android FBE (File-Based Encryption)

The kernel has fscrypt registered:
```
Key type .fscrypt registered
Key type fscrypt-provisioning registered
```

The `userdata` partition contains clearly encrypted data (high entropy, no discernible structure). This is Android's file-based encryption.

The `metadata` partition holds encryption-related data as part of Android's FBE implementation — it is not simply empty. The earlier characterization of it as "completely zeroed" was based on a surface-level read and the conclusion that FBE was never initialized was incorrect. The `androidboot.firstboot=1` flag in the kernel cmdline indicates the device hadn't completed its first Android boot, but the metadata partition may still have had content written during factory provisioning.

### RPMB

RPMB is provisioned (a key has been burned in). RPMB is used by Android's Keymaster/TEE to store hardware-bound key material. The device is Widevine L3 (software-only), so RPMB is not required for DRM. The `tee` eMMC partition (p9) is zeroed — TEE firmware (BL32) is embedded in `bootloader_a`, not stored in the `tee` partition on this device. The RPMB key was burned at the factory but Keymaster may never have run given `androidboot.firstboot=1`.

### Verified Boot

- `verifiedbootstate=orange` — bootloader is **unlocked**
- `avb2=0` — Android Verified Boot 2 is disabled
- vbmeta partitions are zeroed — no signatures to verify against

### Bootloader Write Protection

`mmcblk0boot0` and `mmcblk0boot1` both report `force_ro=1` — they are hardware write-protected. This is the first-stage bootloader (BL2). Nothing running in Linux can overwrite it. The U-Boot environment also has `bootloader_wp=1` and `write_boot=0` confirming this is intentional.

---

## U-Boot Environment (Key Variables)

Extracted from the `env` partition (p2):

```
active_slot=_a
avb2=0
board=umx5jyks
board_name=s6_umx5jyks
bootloader_version=01.01.260115.144049
bootloader_wp=1
write_boot=0
ce_on_emmc=no
vendor_boot_mode=true
upgrade_step=2
EnableSelinux=permissive
```

### Boot Order

```
bootcmd=run bootfromsd; run bootfromusb; run bootfromemmc; run storeboot
```

SD card → USB → internal eMMC → Android storeboot (in that order).

The `cfgloademmc` command scans eMMC partitions looking for a `cfgload` file — this is how CoreELEC would boot from eMMC if installed there. U-Boot iterates through partitions 1–31 on the internal eMMC looking for a FAT filesystem with a `cfgload` script.

The `ce_on_emmc=no` variable is notable — it's the U-Boot side of the "CoreELEC is not installed on eMMC" state.

---

## eMMC Writability

Confirmed via live write tests from CoreELEC:

- All data partitions are **writable** — including `bootloader_a` (p7), despite `bootloader_wp=1` in the U-Boot env. That flag is a U-Boot convention, not hardware enforcement.
- `boot0` and `boot1` correctly reject writes (`Operation not permitted`) — hardware enforced.
- The partition table is **standard GPT**, readable and modifiable with `parted`.
- Available tools on the running system: `parted`, `mkfs.fat`, `mkfs.ext4`, `e2fsck`, `resize2fs`.
- No free space at the end of the disk — `userdata` (p29) runs all the way to 62.5 GB. Any new partitions require shrinking or replacing it.

---

## CoreELEC eMMC Install — `ceemmc` Tool

`ceemmc` is present at `/usr/sbin/ceemmc`. Running it produces:

```
System is not supported: s6_s905x5_ugoos_am9_pro!
```

The AM9 Pro isn't in `ceemmc`'s supported device list as of the May 14 2026 nightly. The tool works by carving new `CE_FLASH` and `CE_STORAGE` partitions out of the `userdata` space and writing a `cfgload` file that U-Boot will find. It doesn't touch `boot0`/`boot1` or `bootloader_a` — but it's simply not implemented for this board yet.

---

## What's Already on the SD Card (the good news)

Everything needed for eMMC boot is already sitting in `/flash` on the SD card:

- `s6_s905x5_ugoos_am9_pro.dtb` exists in `device_trees/` — the AM9 Pro DTB is there and actively in use (it matches the 82.7KB `dtb.img` currently loaded at boot)
- `kernel.img` — the kernel, packaged as an Android boot image containing both the kernel and the initrd (ramdisk)
- `cfgload` — the U-Boot boot script, which already has the eMMC boot path written into it
- `config.ini`, `resolution.ini` — config files

The cfgload already handles eMMC boot:
```
setenv rootopt "BOOT_IMAGE=kernel.img boot=LABEL=COREELEC disk=LABEL=STORAGE"
if test "${ce_on_emmc}" = "yes"; then
  setenv rootopt "BOOT_IMAGE=kernel.img boot=LABEL=CE_FLASH disk=FOLDER=/dev/CE_STORAGE"
fi
```

And the U-Boot `cfgloademmc` script automatically sets `ce_on_emmc=yes` when it finds `cfgload` on the eMMC — no manual env editing needed.

---

## Initrd Analysis — How Storage Mounting Actually Works

The kernel image is an Android boot image (`ANDROID!` magic header) containing the initrd as a zstd-compressed cpio archive. Extracting and reading the init script reveals exactly how CoreELEC finds and mounts its partitions at boot.

### The `FOLDER=` mechanism (ceemmc's original design)

The cfgload's eMMC path uses `disk=FOLDER=/dev/CE_STORAGE`. This maps to a `mount_folder` function in the initrd:

```sh
mount_folder() {
  local target="${1#*=}"         # strips "FOLDER=" → /dev/CE_STORAGE
  mkdir -p /dev/bind_tmp
  mount_common "$target" "/dev/bind_tmp" "rw,noatime"
  mount_common "/dev/bind_tmp/coreelec_storage" "/storage" "bind"
  umount /dev/bind_tmp
}
```

What this does: mount the block device at `/dev/CE_STORAGE`, then bind-mount a **subfolder called `coreelec_storage`** from inside it as `/storage`. The factory-reset script even comments this directly: *"storage is just subfolder on Android data partition"*.

This design was built for ceemmc's dual-boot mode where CoreELEC's storage lives as a folder inside Android's userdata partition. In our environment (standalone install, no ceemmc), this path did not work cleanly for two reasons:

1. `/dev/CE_STORAGE` needs to exist as a device node. In our environment no device node was created for it during boot, so the mount failed. Whether ceemmc-managed installs handle this differently was not verified.
2. The mechanism bind-mounts a `coreelec_storage/` subdirectory from inside the mounted partition as `/storage` — not the partition root — which is not what a standalone install wants.

### The correct approach for a dedicated install

Look at how SD card boot works today:

```
boot=LABEL=COREELEC   → mounts the FAT partition by label as /flash
disk=LABEL=STORAGE    → mounts the ext4 partition by label as /storage
```

This uses standard `LABEL=` resolution via blkid — no device nodes required. The initrd handles `LABEL=*` paths natively.

For a dedicated eMMC install, the same mechanism works with different labels. The cfgload on the eMMC `CE_FLASH` partition needs the FOLDER= path replaced with a LABEL= path:

```
# change this:
disk=FOLDER=/dev/CE_STORAGE
# to this:
disk=LABEL=CE_STORAGE
```

However, cfgload is a compiled U-Boot script in mkimage format — it cannot be edited with a text editor. The change requires decompiling, editing, and recompiling with `mkimage`. See the CRC trap section below. This is what ceemmc would do correctly if it supported this board.

---

## Manual Install — What Actually Worked

The GPT partition table was created by Ugoos with exactly 29 entries — no room for a 30th. Adding two new partitions required freeing slots first by deleting unused Android partitions.

### Partition changes made

Preserved:
- `super` (p27, ~3.1 GB) — kept intact; preserves the Android restore path and avoids disrupting `tee-loader.sh` on SoCs where the Android TEE path is still used. On the AM9 Pro (S905X5-J, S5 serial), `tee-loader.sh` unconditionally uses CoreELEC's own TEE implementation and never reads from `super` — so `super` is not required for CoreELEC operation on this device and could be deleted in a CoreELEC-only install.

Deleted:
- `rsv` (p28, 64 MB) — unknown reserved partition; not present in the Ugoos factory restore image. Backed up to `/storage/rsv_backup.bin` before deletion. Content on this unit was not verified prior to deletion — the script now checks and reports non-zero content before proceeding.
- `userdata` (p29, 54.4 GB) — encrypted with hardware-bound FBE keys, not recoverable

Created:
- `CE_FLASH` (p28, 512 MB, FAT32) — boot partition, holds kernel/DTB/cfgload. U-Boot's `cfgloademmc` scans partitions 1–31 for a FAT filesystem containing cfgload — it finds CE_FLASH at p28 by content, not by partition number.
- `CE_STORAGE` (p29, ~53.9 GB, ext4) — CoreELEC storage

### cfgload CRC trap

The cfgload file is a compiled U-Boot script in mkimage format — it has a binary header containing a CRC of the script data. Editing the file with `sed` changes the content but leaves the old CRC in the header. U-Boot verifies the CRC on load and silently rejects a mismatched script, causing eMMC boot to fail without any obvious error.

**Do not edit cfgload with sed or any text editor.** Either recompile it with `mkimage` after editing, or use the hooks described below.

### The mount-storage.sh hook

The initrd sources `/flash/mount-storage.sh` if it exists, instead of running the normal `mount_part "$disk"` logic. This was used to sidestep the `FOLDER=/dev/CE_STORAGE` path entirely.

`/flash/mount-storage.sh` on CE_FLASH:
```sh
mount -t ext4 -o rw,noatime LABEL=CE_STORAGE /storage
```

**Note:** This is a workaround, not a proper solution. The right approach is to recompile cfgload with `mkimage` so it uses `disk=LABEL=CE_STORAGE` directly — which is exactly what ceemmc would do if it supported this board. The FOLDER= mechanism isn't "broken"; it's the dual-boot path designed for when CoreELEC storage lives as a subfolder inside Android's userdata. For a standalone CoreELEC install, cfgload should simply be rebuilt with the correct `LABEL=` argument. The mount-storage.sh hook achieves the same end result but bypasses the intended boot mechanism in a way that the CoreELEC team would not consider correct.

### nofsck in config.ini

With `disk=FOLDER=/dev/CE_STORAGE` still in the kernel cmdline (from the unmodified cfgload), the initrd adds `/dev/CE_STORAGE` to its fsck disk list. Since that device node never gets created, fsck retries 20 times at 0.5s each — a 10-second boot penalty.

Fix: add `nofsck` to the `coreelec` variable in `config.ini` on CE_FLASH:
```
coreelec='quiet nofsck'
```

This passes `nofsck` as a kernel argument, which the initrd parses to skip fsck entirely.

### Final working file layout on CE_FLASH (p28)

```
CE_FLASH/
├── SYSTEM           (346 MB squashfs — CoreELEC root)
├── SYSTEM.md5
├── kernel.img       (23.5 MB — kernel + initrd)
├── kernel.img.md5
├── dtb.img          (82.7 KB — s6_s905x5_ugoos_am9_pro.dtb)
├── cfgload          (original unmodified mkimage binary from SD)
├── config.ini       (with coreelec='quiet nofsck')
├── mount-storage.sh (mounts LABEL=CE_STORAGE as /storage)
├── resolution.ini
├── aml_autoscript
├── cfgload_env
├── dovi.ko          (Dolby Vision kernel module — present because S905X5-J is DV-licensed)
├── recovery.img
└── device_trees/
    └── s6_s905x5_ugoos_am9_pro.dtb  (and all other DTBs)
```

### Result

Device boots CoreELEC successfully from eMMC with SD card removed. First boot initializes the empty CE_STORAGE partition. SSH host key changes on first eMMC boot (fresh install generates new keys) — clear the old entry with `ssh-keygen -R 192.168.1.139` before reconnecting.

---

## Android Restore Path

There are two restore paths.

### Restore script (primary path)

`ce-emmc-restore.sh` restores the original Android partition layout from the backups created by `ce-emmc-install.sh`. It reads `partition_layout.txt` to reconstruct the exact original p28/p29 boundaries, then:
- Deletes CE_FLASH (p28) and CE_STORAGE (p29)
- Recreates `rsv` (p28) and `userdata` (p29) at their original byte offsets
- Restores rsv content from `rsv_backup.bin`
- Restores env and bootloader_a from their backups
- Leaves super (p27) untouched

Android's userdata is recreated empty. Android reinitializes it on first boot from the system images already in `super`. No Windows PC or USB cable required.

### USB Burning Tool (fallback)

Ugoos distributes a full factory firmware image (`AM9PRO_2.0.9.img`) which can be flashed via the Amlogic USB Burning Tool v3.

### Image format

The image uses the Amlogic USB Burning Tool format: magic `0x6cf66ed9`, version 2, 30 items. Item table starts at offset `0x40`; each item is `0x240` bytes with the type string at `+0x20`, name at `+0x120`, offset at `+0x10`, and size at `+0x18`.

The 30 items break down as 14 `PARTITION` entries, 14 matching `VERIFY` (hash) entries, plus USB-mode firmware (`DDR`, `UBOOT`), a GPT table entry (`bin/gpt`), a DTB (`dtb/meson1`), platform config, and a USB flow blob.

### Verified partition contents

`AM9PRO_2.0.9.img` was fully parsed and each partition payload inspected:

| Partition | Size in image | Content |
|-----------|--------------|---------|
| `bootloader` / `bootloader_a` | 3.91 MB each | Same binary, Amlogic `@ML` header — identical to what's on the device |
| `boot_a` | 51.0 MB | `ANDROID!` magic — real Android boot image |
| `dtbo_a` | 438 bytes | FDT magic `d7b7ab1e` — tiny DTB overlay, real content |
| `init_boot_a` | 2.62 MB | `ANDROID!` magic |
| `logo` | 1.65 MB | Amlogic logo partition |
| `odm_ext_a` | 16 MB | **All zeros** — empty in the factory image |
| `super` | 1507 MB | LP metadata magic `0x3aff26ed` at byte 0 — Android Logical Partition metadata + system/vendor images |
| `vendor_boot_a` | 50.86 MB | `VNDRBOOT` magic |
| GPT (`bin/gpt`) | 0.03 MB | Full GPT table — restores the original 29-partition layout |
| DTB (`dtb/meson1`) | 0.08 MB | Amlogic SoC DTB |

The `super` partition contains real Android system data (LP metadata at offset 0 followed by compressed system and vendor images). On SoCs where `tee-loader.sh` uses the Android TEE path (older SC2-era devices without CoreELEC-native TAs), deleting `super` would break TEE loading and video playback. On the AM9 Pro (S905X5-J), `tee-loader.sh` always uses CoreELEC's own TEE — `super` is not required for CoreELEC operation.

`odm_ext_a` being zeroed in the factory image is consistent with what was observed on the device — this partition appears to be unused in this firmware version.

### Provisioning state of the inspected device

The device examined in this research had `boot_a`, `vendor_boot_a`, `init_boot_a`, and `super` all appearing empty or zeroed, despite the factory image having real content for those partitions. The U-Boot environment contained `androidboot.firstboot=1`, suggesting the device had not completed its first Android boot. Ugoos may ship units in a partially provisioned state where the Android userspace images are not yet written to the eMMC.

### Factory image findings

The factory image was fully parsed. Notable findings:

- **`super` (1507 MB)** — present in the image; contains LP metadata and Android system/vendor images. Confirmed real content at offset 0.
- **`tee` partition** — not present in the factory image. TEE firmware is not distributed via USB Burning Tool; it is provisioned at the factory separately. The `tee` partition was zeroed on the examined unit, and `androidboot.firstboot=1` indicates Android had never completed first boot on this device.
- **`rsv` partition** — not present in the factory image. Ugoos does not write anything to this partition during a factory restore.
- **Magisk** — confirmed present in `init_boot_a` via the magic markers `.magisk`, `KEEPVERITY=true`, `FORCEENCRYPT`, `RECOVERYMODE=false`. The factory image ships with Magisk pre-installed. The bootloader is unlocked (`verifiedbootstate=orange`, `avb2=0`).
- **Widevine** — the device is certified at **Widevine L3** (confirmed by Ugoos official specs). L3 is software-only key handling and requires no TEE involvement for DRM — this is the expected level for a device with an unlocked bootloader, since L1 requires an intact verified boot trust chain. The Widevine device certificate (keybox) is stored in the `factory` partition (p4), which is not included in the factory restore image and is not touched by the CoreELEC install scripts. Whether L3 certification survives a USB Burning Tool restore is uncertain since the keybox in p4 is not rewritten by the factory image.
- **USB-C OTG port** — burn mode connects via the dedicated USB-C OTG port (labelled OTG on the device). The three USB-A ports are host-only and cannot be used for burn mode. Cable required: USB-C to USB-A.

### USB Burning Tool restore procedure

Because `boot0` is hardware write-protected, the BL2 is always intact and the device can always be put into USB burn mode by holding the reset/ADB button during power-on. A full USB Burning Tool flash wipes and rewrites every partition (including the GPT itself), fully restoring the original 29-partition Android layout regardless of what was done to the partition table.

This means a full restore is always possible, but it requires a Windows PC, a USB-C to USB-A cable, and the factory image. The restored Android will be in the Ugoos-shipped state — pre-rooted via Magisk with an unlocked bootloader.

---

## Brick Risk Assessment

Low. Specifically:

- **boot0/boot1 are hardware write-protected** — the lowest-level bootloader that wakes the SoC cannot be overwritten by anything running in Linux. The device can always enter USB burn mode.
- **SD card boots first** — as long as the SD card is in, U-Boot tries it before anything else. A broken eMMC state is irrelevant.
- **Full factory restore is possible** — using the official Ugoos firmware image and USB Burning Tool v3, the entire eMMC can be wiped and rewritten to stock Android. See the restore section above.

---

## Migrating Settings from SD Card

After confirming eMMC boot works, the SD card is reinserted (device boots from SD). CE_STORAGE is mounted manually and `/storage` is rsynced across.

Since CoreELEC's udev doesn't create device nodes for eMMC partitions, they have to be created manually. Read the major:minor numbers from sysfs rather than assuming them — the sequential numbering assumption is not reliable:

```sh
# Read actual major:minor from kernel
for part in 27 28; do
    read -r devnum < /sys/block/mmcblk0/mmcblk0p${part}/dev
    mknod /dev/mmcblk0p${part} b "${devnum%%:*}" "${devnum##*:}"
done
mkdir -p /var/ce_storage
mount -t ext4 -o rw,noatime /dev/mmcblk0p28 /var/ce_storage
rsync -ax /storage/ /var/ce_storage/
umount /var/ce_storage
```

5.1 GB of settings, addons, and media copied in under a minute at ~65 MB/s (eMMC write speed over the internal bus).

---

## Status

**Complete.** eMMC boot working, all settings migrated. Remove the SD card and the device boots CoreELEC fully from internal eMMC.
