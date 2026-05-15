# Ugoos AM9 Pro — CoreELEC eMMC Research Notes

**Date:** May 14, 2026  
**Device:** Ugoos AM9 Pro  
**SoC:** Amlogic A311D2 (identified as `s6_s905x5_ugoos_am9_pro`)  
**CoreELEC:** 22.0-Piers_nightly_20260514  
**Kernel:** 5.15.196  

---

## Final Setup

CoreELEC boots from internal eMMC (`mmcblk0`, 58.2 GB Samsung A31M8C). `CE_FLASH` (p27, 512 MB FAT32) holds the boot files; `CE_STORAGE` (p28, 57.9 GB ext4) holds settings and addons. SD card is no longer needed and can be removed.

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
| p1 | reserved | 0x2400000 | 64 MB | unknown |
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
| p16 | odm_ext_a | 0x17800000 | 16 MB | unknown |
| p17 | odm_ext_b | 0x18900000 | 16 MB | unknown |
| p18 | boot_a | 0x19a00000 | 64 MB | zeroed |
| p19 | boot_b | 0x1db00000 | 64 MB | zeroed |
| p20 | init_boot_a | 0x21c00000 | 8 MB | zeroed |
| p21 | init_boot_b | 0x22500000 | 8 MB | zeroed |
| p22 | metadata | 0x22e00000 | 64 MB | **zeroed** |
| p23 | vbmeta_a | 0x26f00000 | 2 MB | **zeroed** |
| p24 | vbmeta_b | 0x27200000 | 2 MB | **zeroed** |
| p25 | vbmeta_system_a | 0x27500000 | 2 MB | **zeroed** |
| p26 | vbmeta_system_b | 0x27800000 | 2 MB | **zeroed** |
| p27 | CE_FLASH | 0x27b00000 | 512 MB | **active** — FAT32, CoreELEC boot files |
| p28 | CE_STORAGE | — | 57.9 GB | **active** — ext4, CoreELEC storage |

The original Android layout had 29 partitions. Most were zeroed or unused — `super`, `boot_a/b`, `metadata`, `vbmeta_a/b`, and `TEE` were all empty. Only `bootloader_a` and `env` had live data. `userdata` was encrypted (Android FBE remnant, not recoverable).

For the CoreELEC install, `super` (p27), `rsv` (p28), and `userdata` (p29) were deleted to free GPT slots, and `CE_FLASH` and `CE_STORAGE` were created in their place. The GPT was originally allocated for exactly 29 entries — adding partitions beyond that limit requires freeing existing slots first.

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

RPMB is provisioned (a key has been burned in). RPMB is used by Android's Keymaster/TEE to store hardware-bound key material. Since the TEE partition is also zeroed, Keymaster was never actually run on this device. The RPMB key is there but nothing ever used it.

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

This design was built for ceemmc's dual-boot mode where CoreELEC's storage lives as a folder inside Android's userdata partition. It is **not suitable for a clean dedicated install** for two reasons:

1. `/dev/CE_STORAGE` needs to exist as a device node in `/dev/`. The initrd has no udev rules and no `platform_init` script — nothing creates this symlink. The mount would fail.
2. Even if the mount succeeded, it would expect a `coreelec_storage/` subdirectory inside the partition, not the partition root as storage.

### The correct approach for a dedicated install

Look at how SD card boot works today:

```
boot=LABEL=COREELEC   → mounts the FAT partition by label as /flash
disk=LABEL=STORAGE    → mounts the ext4 partition by label as /storage
```

This uses standard `LABEL=` resolution via blkid — no device nodes required. The initrd handles `LABEL=*` paths natively.

For a dedicated eMMC install, the same mechanism works with different labels. The cfgload on the eMMC `CE_FLASH` partition just needs one line changed from the default:

```
# change this:
disk=FOLDER=/dev/CE_STORAGE
# to this:
disk=LABEL=CE_STORAGE
```

That's the only modification needed. Everything else — the kernel, the DTB, the boot sequence — works as-is.

---

## Manual Install — What Actually Worked

The GPT partition table was created by Ugoos with exactly 29 entries — no room for a 30th. Adding two new partitions required freeing slots first by deleting unused Android partitions.

### Partition changes made

Deleted (all were zeroed/unused):
- `super` (p27, 3.1 GB) — Android system images, never written
- `rsv` (p28, 64 MB) — unknown reserved partition, empty
- `userdata` (p29, 54.4 GB) — encrypted remnant, not recoverable

Created:
- `CE_FLASH` (p27, 512 MB, FAT32) — boot partition, holds kernel/DTB/cfgload
- `CE_STORAGE` (p28, 57.9 GB, ext4) — CoreELEC storage

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

### Final working file layout on CE_FLASH

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
├── dovi.ko
├── recovery.img
└── device_trees/
    └── s6_s905x5_ugoos_am9_pro.dtb  (and all other DTBs)
```

### Result

Device boots CoreELEC successfully from eMMC with SD card removed. First boot initializes the empty CE_STORAGE partition. SSH host key changes on first eMMC boot (fresh install generates new keys) — clear the old entry with `ssh-keygen -R 192.168.1.139` before reconnecting.

---

## Android Restore Path

Ugoos distributes a full factory firmware image (`AM9PRO_2.0.9.img`) which can be flashed via the Amlogic USB Burning Tool v3. The image was inspected and confirmed to contain all partitions: `super`, `bootloader_a`, `boot_a`, `vendor_boot_a`, `dtbo_a`, `init_boot_a`, `logo`, `odm_ext_a`, and the DTB.

Because `boot0` is hardware write-protected, the BL2 is always intact and the device can always be put into USB burn mode by holding the reset/ADB button during power-on. A full USB Burning Tool flash wipes and rewrites every partition, fully restoring the original 29-partition Android layout regardless of what was done to the partition table.

This means the "no restore path" concern is not accurate — Android can be restored, it just requires a Windows PC, a USB-A to USB-A cable, and the factory image.

---

## Brick Risk Assessment

Low. Specifically:

- **boot0/boot1 are hardware write-protected** — the lowest-level bootloader that wakes the SoC cannot be overwritten by anything running in Linux. The device can always enter USB burn mode.
- **SD card boots first** — as long as the SD card is in, U-Boot tries it before anything else. A broken eMMC state is irrelevant.
- **Full factory restore is possible** — using the official Ugoos firmware image and USB Burning Tool v3, the entire eMMC can be wiped and rewritten to stock Android. See the restore section above.

---

## Migrating Settings from SD Card

After confirming eMMC boot works, the SD card is reinserted (device boots from SD). CE_STORAGE is mounted manually and `/storage` is rsynced across.

Since CoreELEC's udev doesn't create device nodes for eMMC partitions, they have to be created manually each time. The correct minor numbers come from `/proc/partitions` — mmcblk0p27 = 179:27, mmcblk0p28 = 179:28 (sequential, no offset):

```sh
mknod /dev/mmcblk0p27 b 179 27
mknod /dev/mmcblk0p28 b 179 28
mkdir -p /var/ce_storage
mount -t ext4 -o rw,noatime /dev/mmcblk0p28 /var/ce_storage
rsync -ax /storage/ /var/ce_storage/
umount /var/ce_storage
```

5.1 GB of settings, addons, and media copied in under a minute at ~65 MB/s (eMMC write speed over the internal bus).

---

## Status

**Complete.** eMMC boot working, all settings migrated. Remove the SD card and the device boots CoreELEC fully from internal eMMC.
