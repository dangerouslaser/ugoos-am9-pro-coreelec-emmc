# Ugoos AM9 Pro — CoreELEC eMMC Research Notes

**Date:** May 14, 2026  
**Device:** Ugoos AM9 Pro  
**SoC:** Amlogic S905X5-J (S6 family per CoreELEC board ID `s6_s905x5_ugoos_am9_pro`; serial `0x3e` in Amlogic's internal numbering, referred to as "S5" in tee-loader.sh; the -J suffix denotes Dolby Vision licensing)  
**CPU:** Quad-core **Cortex-A510** (ARMv9.0a) — confirmed by `MIDR_EL1 = 0x411fd463` (part `0xd46` = A510) and the CPU feature set advertised in `/proc/cpuinfo`, which includes SVE2, MTE, BF16, and i8mm — all ARMv9 extensions absent on A55. Note that both the CoreELEC and factory Ugoos DTBs declare these cores as `compatible = "arm,cortex-a55"`; that's a kernel-compatibility shim (so existing A55 drivers in the older Linux 5.15 base bind correctly) and not what the hardware actually is. The PMU is correctly declared as `cortex-a510-pmu` — that gives away the real core identity.  
**GPU:** Mali Valhall G310 (CSF variant) — `compatible = "arm,mali-valhall-csf"` in CoreELEC's DTB. The factory Ugoos DTB misidentifies it as Mali Midgard (`"arm,malit60x", "arm,malit6xx", "arm,mali-midgard"`) — CoreELEC corrected this.  
**RAM:** 4 GB LPDDR5  
**CoreELEC:** 22.0-Piers_nightly_20260514  
**Kernel:** 5.15.196  

---

## Claims Provenance — Device Truth vs CoreELEC Observation

This document was assembled by observing the device while running CoreELEC, which means it's easy to accidentally describe "what CoreELEC happens to see" as if it were "what the hardware is." The DTB section is the obvious example — the CoreELEC DTB declares the CPU as A55 (a kernel-compatibility shim), but the hardware register `MIDR_EL1` says A510. Two different answers; the hardware register wins.

To make the audit explicit:

**Device-truth claims** — verifiable independently of the OS, by reading hardware registers, raw eMMC bytes, or chip IDs:

| Claim | How to verify | OS-independent? |
|---|---|---|
| CPU is Cortex-A510 (ARMv9.0a) | `cat /sys/devices/system/cpu/cpu0/regs/identification/midr_el1` → `0x411fd463` → part `0xd46` = A510. Also `/proc/cpuinfo` advertises SVE2/MTE/BF16/i8mm (ARMv9). | Yes — hardware register read |
| GPU is Mali Valhall | dmesg from the Mali driver: `mali fd000000.valhall: ... GPU identified as 0x4 arch 10.12.7`. Driver probes the GPU's own ID register. | Yes — driver-reported, would be identical on any kernel with Mali support |
| eMMC chip is Samsung A31M8C | `/sys/class/mmc_host/mmc0/mmc0:0001/{cid,name,manfid,oemid,serial}` — manfid 0xec = Samsung. | Yes — eMMC CID is a chip property |
| eMMC is ~58.2 GiB | `cat /sys/block/mmcblk0/size` × 512 | Yes |
| boot0/boot1 are HW write-protected | `cat /sys/block/mmcblk0boot{0,1}/force_ro` → `1` | Yes — reflects eMMC hardware WP state |
| RAM is ~4 GB | `/proc/meminfo` shows ~3.62 GiB usable | Yes |
| Wi-Fi chip is BCM4389 family | PCI enum: `/sys/bus/pci/devices/0000:01:00.0/{vendor,device,subsystem_device}` → `0x14E4 / 0x449D / 0xAAE8` | Yes — PCIe enumeration is bus-level |
| Partition layout (29 entries, sizes, offsets) | `parted -sm /dev/mmcblk0 unit B print` reads the GPT | Yes — GPT is a structure on the eMMC |
| Per-partition raw bytes (`AMLNORMAL` at p1 offset 0x4000; empty FAT12 at p4; etc.) | `dd if=/dev/<partition> ...` | Yes — raw bytes are the same regardless of OS |
| eFuses are zero | `/sys/class/efuse/{mac,mac_wifi,mac_bt,usid}` via Amlogic's driver | Yes — eFuse values are hardware-burned |
| Wi-Fi/BT MAC is from BCM chip OTP | dmesg from bcmdhd reports `firmware generated mac_address` — the BCM firmware reads its own OTP and reports the MAC | Yes — would be reported the same by any bcmdhd or brcmfmac driver |
| MAC `40:D9:5A:FC:E2:88` | Same path. The OUI `40:D9:5A` is registered to Broadcom. | Yes |
| ETH MAC `90:0E:B3:FD:F8:55` is in the p1 keystore | Direct read: `python3 -c 'print(open("/dev/mmcblk0p1","rb").read().find(b"90:0e:b3:fd:f8:55"))'` returns 17596 = 0x44bc | Yes |
| Bootloader is encrypted | Entropy of every section ≥7.6 bits/byte; no plaintext U-Boot/BL31/BL32 strings | Yes — pure byte-level |
| Factory image (`AM9PRO_2.0.9.img`) content | Parse the Amlogic packer format directly | Yes — file format |

**CoreELEC observations** — what CoreELEC happens to do with the device. May differ on Android, LibreELEC, or a custom build:

| Observation | Note |
|---|---|
| Active DTB `/flash/dtb.img` declares CPU as A55, GPU as Valhall | This is the **CoreELEC variant** of the DTB (has `coreelec;` and `coreelec-dt-id` properties). The factory Ugoos DTB (extracted from `AM9PRO_2.0.9.img` as `dtb/meson1`) also declares A55 — for kernel-binding compatibility, not because the silicon is A55 — but misidentifies the GPU as Mali Midgard. See [Active DTB](#active-dtb--flashdtbimg-coreelec-variant). |
| `/sys/class/aml_wifi/`, `/sys/class/efuse/` exist | Amlogic-vendor kernel modules. Present in CoreELEC because they bundle Amlogic's drivers; would be missing on a mainline Linux kernel. Underlying values (eFuse contents, Wi-Fi platform state) are still hardware truth — just exposed differently. |
| `wlan0`/`eth0` interface names | systemd/udev choice; could differ |
| Kernel cmdline ends with `disk=LABEL=CE_STORAGE quiet` | Set by our rebuilt `cfgload`; on a fresh Android boot the cmdline tail would be different (no `disk=`, no `quiet`) |
| `/dev/mmcblk0p*` device nodes only exist for actively-mounted partitions | CoreELEC's udev policy; other Linux distros may create all of them automatically |
| `fw_printenv` doesn't work out of the box | CoreELEC's `/etc/fw_env.config` references a `/dev/env` node that udev never creates |

**Joint claims** — true of the device but with state that the OS or U-Boot may have written:

| Claim | Note |
|---|---|
| U-Boot env content (`bootcmd`, `ce_on_emmc=no`, `firstboot=1`, etc.) | The env partition (p2) is writable. Most of what we see appears to be factory state on this unit (`firstboot=1`, `ce_on_emmc=no`), but the partition has been written to at least once (env A/B slots show different `firstboot` values). Specific values like `ethaddr` and `cmdline_keys` are Amlogic-factory U-Boot defaults; the `bootcmd` chain is also factory. |
| RPMB `rpmb_state=0x1` | Set at factory; this is "provisioned" state. Hardware fact. |
| AMLNORMAL keystore content | Written at factory by Ugoos's provisioning tool. The `keycnt=2` field and the populated slots are factory state; further slots may be added if Android first-boot ever runs. |

If you're auditing this document or extending the install scripts, **the rule is: claims about silicon or partition contents should be backed by a hardware register or raw byte read, not by what CoreELEC's userspace says.** When in doubt, the verification commands in the table above are the source of truth.

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
| p1 | reserved | 0x2400000 | 64 MB | **Amlogic UKS keystore** — `AMLNORMAL` magic at 0x4000 with redundant copy at 0x44000; holds plaintext `usid` and `mac` values plus catalog of slot names (widevinekeybox, attestationkeybox, netflix_mgkid, PlayReadykeybox25, hdcp22_fw_private, etc., most empty on this unit). Not in factory image — written by factory tooling. **Critical: do not wipe.** |
| p2 | env | 0x6c00000 | 8 MB | **active** — U-Boot env |
| p3 | frp | 0x8400000 | 2 MB | 36 bytes of high-entropy unit-unique data at offset 0 — Android FRP / anti-rollback nonce; rest zero |
| p4 | factory | 0x8e00000 | 8 MB | empty FAT12 placeholder labeled "KEYBOX PART" — no keybox content |
| p5 | vendor_boot_a | 0x9700000 | 64 MB | zeroed |
| p6 | vendor_boot_b | 0xd800000 | 64 MB | zeroed |
| p7 | bootloader_a | 0x11900000 | 8 MB | **active** — Amlogic `@ML` header |
| p8 | bootloader_b | 0x12200000 | 8 MB | unknown |
| p9 | tee | 0x12b00000 | 32 MB | **zeroed** |
| p10 | logo | 0x14c00000 | 8 MB | Amlogic logo resource pack (`AML_RES!` magic) — not unit-specific |
| p11 | misc | 0x15500000 | 2 MB | Android A/B slot metadata at offset 0x800 (`_a\0\0` + `BCAB` magic + version 1, 2 slots); BCB region empty |
| p12 | dtbo_a | 0x15800000 | 2 MB | FDT magic `d7b7ab1e` — DTB overlay (438 bytes payload per factory image) |
| p13 | dtbo_b | 0x15b00000 | 2 MB | all zero — empty B slot |
| p14 | cri_data | 0x15e00000 | 8 MB | all zero — unused on this unit |
| p15 | param | 0x16700000 | 16 MB | ext4 filesystem mounted at `/mnt/vendor/param` in Android — TV picture-quality DB (`pq.db`, `pq_ext.db`, `TV_PICTURE`), Amlogic per-device display calibration |
| p16 | odm_ext_a | 0x17800000 | 16 MB | empty (also zeroed in factory image) |
| p17 | odm_ext_b | 0x18900000 | 16 MB | all zero — empty B slot |
| p18 | boot_a | 0x19a00000 | 64 MB | zeroed |
| p19 | boot_b | 0x1db00000 | 64 MB | zeroed |
| p20 | init_boot_a | 0x21c00000 | 8 MB | zeroed |
| p21 | init_boot_b | 0x22500000 | 8 MB | zeroed |
| p22 | metadata | 0x22e00000 | 64 MB | ext4 filesystem for Android FBE metadata — vold encryption keys, password_slots, bootstat, OTA snapshots (Android 14 kernel string visible: `5.15.192-android14-11-…`) |
| p23 | vbmeta_a | 0x26f00000 | 2 MB | **zeroed** |
| p24 | vbmeta_b | 0x27200000 | 2 MB | **zeroed** |
| p25 | vbmeta_system_a | 0x27500000 | 2 MB | **zeroed** |
| p26 | vbmeta_system_b | 0x27800000 | 2 MB | **zeroed** |
| p27 | super | 0x27b00000 | ~3.1 GB | **preserved** — Android dynamic partition (LP metadata + system/vendor images) |
| p28 | CE_FLASH | — | 512 MB | **active** — FAT32, CoreELEC boot files |
| p29 | CE_STORAGE | — | ~53.9 GB | **active** — ext4, CoreELEC storage |

The original Android layout had 29 partitions. Of those inspected: `boot_a/b`, `vbmeta_a/b`, `vendor_boot_a/b`, `init_boot_a/b`, `tee`, and `super` appeared empty on this unit. `bootloader_a`, `env`, `param`, `metadata`, `frp`, `misc`, `logo`, `dtbo_a`, and **`reserved` (p1, the AMLNORMAL keystore)** had live data. `userdata` was encrypted. `factory` (p4) is an empty FAT12 placeholder; `cri_data` (p14) is all zero; eFuses for MAC/USID are all zero — per-device identity (ETH MAC + serial) is stored as plaintext in p1's AMLNORMAL keystore; WLAN/BT MAC comes from the Wi-Fi chip's OTP. See [Per-Device Identity Provenance](#per-device-identity-provenance) below.

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

## Per-Device Identity Provenance

Where the working ETH MAC, WLAN/BT MACs, and serial actually come from — verified on a pre-first-boot unit where the `factory` (p4) partition is empty:

| Identity | Value (example) | Source |
|---|---|---|
| ETH MAC | `90:0E:B3:FD:F8:55` | Plaintext in the **Amlogic UKS keystore on `reserved` (p1)** at offset 0x4000 (redundant copy at 0x44000). U-Boot's `cmdline_keys` script reads it via `keyman read mac` and sets the kernel cmdline `mac=`. The env partition also stores `ethaddr=` as a redundant copy. Integrity is SHA-256 (not HMAC) — see [Bootloader Partition](#bootloader-partition-p7--amlogic-amlboot-container) for the public-source confirmation. |
| WLAN MAC | `40:D9:5A:FC:E2:88` | BCM4389 chip OTP. dmesg: `[dhd] use firmware generated mac_address`. Not stored on the eMMC at all. |
| BT MAC | `40:D9:5A:FC:E2:89` | BCM4389 OTP (WLAN MAC + 1, Broadcom convention). |
| Serial | `AM9PRO26010005693` | Plaintext in the p1 UKS keystore (slot `usid`); read at boot via `keyman read usid`. |

### What is NOT used for identity on this unit

- **Amlogic eFuses are all zero.** `/sys/class/efuse/{mac,mac_wifi,mac_bt,usid}` all read as zero bytes. The eFuse path is not the storage backend on the AM9 Pro S905X5-J.
- **The `factory` (p4) partition is empty.** It is preformatted as FAT12 with label "KEYBOX PART" but contains no keybox data. The Widevine L3 state does not depend on any on-eMMC blob.
- **The `cri_data` (p14) partition is empty.** All-zero across the full 8 MB.
- **`mmcblk0boot0` / `mmcblk0boot1` are nearly all zero.** No clear-text keys live in the boot partitions.
- **RPMB does not hold the identity values directly, and is not part of the read path.** The integrity check on the AMLNORMAL keystore is plain SHA-256 — see [Bootloader Partition](#bootloader-partition-p7--amlogic-amlboot-container) for the public-source struct. RPMB is provisioned on this unit (`rpmb_state=0x1`) but is used by Android Keymaster / TEE for hardware-backed key storage, not by U-Boot's `keyman` flow.

### The `cmdline_keys` flow

The U-Boot env partition (p2) defines a script variable that runs late in `storeargs`:

```sh
if keyman init 0x1234; then
  if keyman read usid ${loadaddr} str; then
    setenv bootconfig ${bootconfig} androidboot.serialno=${usid}
    setenv serial ${usid}
  ...
  fi
  if keyman read mac ${loadaddr} str; then
    setenv bootargs ${bootargs} mac=${mac}
    setenv bootconfig ${bootconfig} androidboot.mac=${mac}
  fi
  if keyman read deviceid ...
  if keyman read factory_flag ...
fi
... factory_provision init;
```

`keyman` is Amlogic's Unified Key Store interface; `0x1234` selects the secure key device. The keystore itself sits in p1 reserved (verified — `AMLNORMAL` magic at offset 0x4000, with the `usid` slot containing the literal serial `AM9PRO26010005693` and the `mac` slot containing the literal MAC `90:0e:b3:fd:f8:55`). The on-disk format is verified against the public CoreELEC/u-boot source — see [Bootloader Partition](#bootloader-partition-p7--amlogic-amlboot-container) for the full struct definition and reader API. The trailing `factory_provision init` initializes the factory partition (p4) for AVB persistent storage; on this unit `avb2=0` so the AVB code path is inactive and `init` is never invoked, explaining why p4 stays as an empty FAT12 placeholder.

### Keystore structure observed in p1

The `reserved` partition (p1) contains a duplicated Amlogic UKS bank starting at offset 0x4000 and again at offset 0x44000 (a 256 KB stride). Each bank starts with an `AMLNORMAL` magic, a version word, a count word, and **two 32-byte SHA-256 hashes** (one for the header, one for the data region — verified against the [public `storage_block_raw_head` struct](https://github.com/CoreELEC/u-boot/blob/master/bl33/v2023/drivers/amlogic/storagekey/normal_key.c)). Then a slot catalog and the slot key/value pairs. Confirmed-populated slots on this unit: `usid`, `mac`. Slot names present but values empty (or not yet provisioned): `$widevinekeybox`, `$PlayReadykeybox25`, `$netflix_mgkid`, `$attestationkeybox`, `$prprivkeybox`, `$prpubkeybox`, `$hdcp22_fw_private`, `$hdcp2_rx`, `$hdcp2_tx`, `$mac_wifi`, `$deviceid`, `$region_code`, `$secure_boot_set`.

### Implications for backup, install, and restore

- **`reserved` (p1) is the eMMC-resident copy of per-device identity** and IS at risk if you wipe it. The ETH MAC and serial are stored as plaintext slots in p1's UKS keystore. Integrity is plain SHA-256 (no HMAC, no secret key — verified against the [CoreELEC/u-boot `storage_block_raw_head`](https://github.com/CoreELEC/u-boot/blob/master/bl33/v2023/drivers/amlogic/storagekey/normal_key.c) struct). RPMB is provisioned but is **not** on the keyman read path — see [Bootloader Partition](#bootloader-partition-p7--amlogic-amlboot-container). **Wiping or rewriting p1 would lose the eMMC keystore — and the factory image does not include p1, so USB Burning Tool restore would NOT bring it back.** WLAN/BT MACs from the BCM4389 OTP would survive, but ETH MAC, serial, and the (empty-on-this-unit) keybox slots would be gone.
- **The CoreELEC install scripts do not touch p1.** Confirmed safe — the install only operates on p28 and p29. Still, a backup of p1 is a cheap precaution before any partition-table edits.
- **The CoreELEC install scripts do not need to back up `factory` (p4)** — there is nothing in it to preserve on this unit.
- **The Widevine L3 certification does not depend on the eMMC.** L3 is software-only key handling; no L1 keybox blob is provisioned in p1 either on this unit (slot exists, value empty).
- **USB Burning Tool restore is safe for identity.** The factory image does not write to p1, so a full burn leaves the existing UKS keystore intact and identity is preserved across the restore.
- **The `frp` (p3) partition contains 36 bytes of unit-unique data** at offset 0 (anti-rollback / FRP signing material, most likely). If you ever wipe p3 destructively, you may want to back it up first; the CoreELEC install does not touch this partition.

### `fw_printenv` quirk

`fw_printenv` is shipped on CoreELEC but the bundled `/etc/fw_env.config` points at `/dev/env` and `/dev/nand_env`, neither of which exist as device nodes by default. Reading the env partition with `dd if=/dev/mmcblk0p2` and grepping for variable names is the working path on this system. Alternatively, `mknod /dev/env b 179 2` or rewriting `fw_env.config` to point at `/dev/mmcblk0p2` would make `fw_printenv` work.

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

cfgload is a compiled U-Boot script in mkimage format and cannot be edited with a text editor. The change requires decompiling, editing, and recompiling so the CRC fields in the mkimage header are correct. See the "cfgload rebuild" section below. This is what ceemmc would do if it supported this board, and what the install script in this repository now does.

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

### cfgload rebuild

The cfgload file is a compiled U-Boot script in mkimage legacy-image format. The header carries two CRC32 fields: `ih_hcrc` (over the 64-byte header with the hcrc field zeroed) and `ih_dcrc` (over the entire payload). Editing the file with `sed` changes the inner script but leaves the old CRCs in place — U-Boot verifies both on load and silently rejects a mismatched image, causing eMMC boot to fail with no obvious error.

The install script reads the stock cfgload from the SD card's `/flash`, performs the `FOLDER=/dev/CE_STORAGE` → `LABEL=CE_STORAGE` substitution in the inner script, then rebuilds the mkimage container with correct CRCs. This is done in Python because `mkimage` is not installed on CoreELEC. The script-image payload format is a null-terminated list of big-endian uint32 script sizes followed by the script bodies concatenated; for a single-script image the payload is `[size_be32][0_terminator][script]`, an 8-byte overhead.

The Python rebuilder is idempotent — running it on an already-patched cfgload prints "already patched" and exits 0. The output is byte-identical (modulo timestamp and header CRC) to what `mkimage -A arm64 -T script -O linux -C none -d <script> <out>` produces.

With cfgload using `LABEL=CE_STORAGE`, the kernel cmdline gets `disk=LABEL=CE_STORAGE`, the initrd resolves the label via `blkid` to the actual partition device, and `mount_part` + `fsck` work the way they do for SD-card boots.

**However**, this patch alone is not durable across CoreELEC's nightly auto-updates — see the [CE auto-update vs cfgload section](#ce-auto-update-vs-cfgload) below. The installer also installs `/flash/mount-storage.sh` and adds `nofsck` to `config.ini`'s `coreelec=` line, which together survive the updater and keep the device booting even when cfgload reverts to its stock form.

### Final working file layout on CE_FLASH (p28)

```
CE_FLASH/
├── SYSTEM           (346 MB squashfs — CoreELEC root)
├── SYSTEM.md5
├── kernel.img       (23.5 MB — kernel + initrd)
├── kernel.img.md5
├── dtb.img          (82.7 KB — s6_s905x5_ugoos_am9_pro.dtb)
├── cfgload          (rebuilt — disk=LABEL=CE_STORAGE, CRCs recomputed)
├── config.ini       (stock from SD — coreelec='quiet')
├── resolution.ini
├── aml_autoscript
├── cfgload_env
├── dovi.ko          (Dolby Vision kernel module — present because S905X5-J is DV-licensed)
├── recovery.img
└── device_trees/
    └── s6_s905x5_ugoos_am9_pro.dtb  (and all other DTBs)
```

### CE auto-update vs cfgload

**Findings 2026-05-27:** CoreELEC's nightly self-updater unconditionally overwrites `/flash/cfgload` from its stock source (`/usr/share/bootloader/${DEVICE_CFGLOAD}`) on every update. The rebuild from the install step is reverted; the next eMMC boot fails because the stock cfgload sets `disk=FOLDER=/dev/CE_STORAGE` for `ce_on_emmc=yes` (a path intended for ceemmc dual-boot, which we don't have).

An attempt at a `/flash/user-update.sh` post-update hook (also a CE-supported customization point) was tried and abandoned — the hook runs in the initramfs context where only `busybox`, `sh`, and `splash-image` are available, so a Python-based cfgload re-patcher crashed and bootlooped the device.

The durable fix: the installer always installs `/flash/mount-storage.sh` (sourced by CE's `/init` `mount_storage()` in place of `mount_part`, so the broken `FOLDER=` cmdline never gets used) **and** adds `nofsck` to `coreelec=` in `config.ini` (suppresses the fsck retry loop against the bogus `/dev/CE_STORAGE` node). Both files are user-added and never touched by the updater. The cfgload rebuild becomes a clean-state nicety; the mount-storage.sh hook is what actually keeps the device booting across updates.

### Result

Device boots CoreELEC successfully from eMMC with SD card removed. First boot initializes the empty CE_STORAGE partition. fsck runs cleanly on both CE_FLASH and CE_STORAGE during initrd. SSH host key changes on first eMMC boot (fresh install generates new keys) — clear the old entry with `ssh-keygen -R 192.168.1.139` before reconnecting.

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

Per-device identity (MAC, serial) is independent of this state — see [Per-Device Identity Provenance](#per-device-identity-provenance) above. The empty `factory` (p4) partition is consistent with the device never having run the `factory_provision init` step that runs on Android first boot; it is not a sign of broken provisioning. The unit boots and operates normally with `factory` empty because identity comes from the AMLNORMAL keystore in `reserved` (p1) — populated at factory time — and the BCM4389 chip's OTP, not from `factory` (p4).

### Factory image findings

The factory image was fully parsed. Notable findings:

- **`super` (1507 MB)** — present in the image; contains LP metadata and Android system/vendor images. Confirmed real content at offset 0.
- **`tee` partition** — not present in the factory image. TEE firmware is not distributed via USB Burning Tool; it is provisioned at the factory separately. The `tee` partition was zeroed on the examined unit, and `androidboot.firstboot=1` indicates Android had never completed first boot on this device.
- **`rsv` partition** — not present in the factory image. Ugoos does not write anything to this partition during a factory restore.
- **Magisk** — confirmed present in `init_boot_a` via the magic markers `.magisk`, `KEEPVERITY=true`, `FORCEENCRYPT`, `RECOVERYMODE=false`. The factory image ships with Magisk pre-installed. The bootloader is unlocked (`verifiedbootstate=orange`, `avb2=0`).
- **Widevine** — the device is certified at **Widevine L3** (confirmed by Ugoos official specs). L3 is software-only key handling and requires no TEE involvement for DRM — this is the expected level for a device with an unlocked bootloader, since L1 requires an intact verified boot trust chain. Earlier notes assumed the Widevine keybox lived in `factory` (p4); on inspection p4 is an empty FAT12 placeholder labeled "KEYBOX PART" with no keybox content. L3 does not require an on-eMMC keybox blob, and no per-device DRM material is at risk from a USB Burning Tool restore or CoreELEC install.
- **USB-C OTG port** — burn mode connects via the dedicated USB-C OTG port (labelled OTG on the device). The three USB-A ports are host-only and cannot be used for burn mode. Cable required: USB-C to USB-A.

### USB Burning Tool restore procedure

Because `boot0` is hardware write-protected, the BL2 is always intact and the device can always be put into USB burn mode by holding the reset/ADB button during power-on. A full USB Burning Tool flash wipes and rewrites every partition (including the GPT itself), fully restoring the original 29-partition Android layout regardless of what was done to the partition table.

This means a full restore is always possible, but it requires a Windows PC, a USB-C to USB-A cable, and the factory image. The restored Android will be in the Ugoos-shipped state — pre-rooted via Magisk with an unlocked bootloader.

---

## USB Burn Mode (Amlogic DNL) — Protocol Findings

Investigated whether the device's USB DNL (mask-ROM + BL2 + BL33 burn) protocol can be driven from Linux/macOS using public open-source tooling, as an alternative to the Windows-only USB Burning Tool. Findings below — all verified against a live device in burn mode.

### Burn-mode entry, USB enumeration

Hold the recessed reset button while plugging in USB-C OTG + power. Device enumerates as:

```
USB Vendor Name: "Amlogic Inc"
idVendor:        0x1b8e
idProduct:       0xc004           ← newer ADNL PID (older devices use 0xc003)
USB Product:     "DNL"
Serial:          801df76cc500321500000000
```

The serial here is the **USB DNL session serial** — not the same as `usid` (the device's per-unit serial stored in the AMLNORMAL keystore, which is `AM9PRO26010005693`).

### Protocol type 6 — the S6 generation marker

Both [Khadas's `adnl` v2.7.5](https://github.com/khadas/utils/tree/master/aml-flash-tool/tools/adnl) and [pyamlboot's `adnl.py`](https://github.com/superna9999/pyamlboot/blob/master/adnl.py) **reject the device** with two different error messages that turn out to be the same root cause:

- Khadas adnl: `ERR[DNL]illegle device mode:06-00-00-16  FAILED`
- pyamlboot:   `RuntimeError: Unexpected data in reply to "identify"` (from the `if msg[4] != 0x5` check in `send_cmd_identify`)

The device's reply to `getvar:identify`:

```
OKAY........
4f 4b 41 59 06 00 00 10 00 00 00 00
^^^^^^^^^^^ "OKAY" header (standard ADNL ACK)
            ^^ protocol type = 0x06 (S6 generation; pyamlboot/khadas expect 0x05 = S5)
               ^^ minor version = 0
                  ^^ reserved = 0
                     ^^ stage = 0x10 = 16 = Stage.TPL (U-Boot already running!)
```

Both clients hardcode "ADNL protocol type 5" and refuse anything else. Our S6 device speaks **protocol type 6** — almost certainly "ADNL v2" or the next-generation variant. The reply layout is otherwise identical: same `OKAY` header, same byte positions, same `Stage` semantics.

This is a **one-line patch** to pyamlboot:

```python
# pyamlboot adnl.py send_cmd_identify():
- if msg[4] != 0x5:
+ if msg[4] not in (0x5, 0x6):
      raise RuntimeError('Unexpected data in reply to "identify"')
```

### What the BL33/U-Boot fastboot surface actually exposes

The device boots through BL1 → BL2 → BL33 (U-Boot) automatically when entering burn mode, and **U-Boot is what's running and answering the USB protocol**. Stage = TPL (16) from the identify reply confirms this.

U-Boot's fastboot implementation in burn mode is heavily locked down:

| Query | Result | Notes |
|---|---|---|
| `getvar:identify` | OK | Returns protocol+stage as above |
| `getvar:serialno` | OK | `801df76cc500321500000000` |
| `getvar:product` | OK | `amlogic` |
| `getvar:slot-count` | OK | `2` (A/B slots) |
| `getvar:version` | OK | `0.1` (fastboot protocol version) |
| `getvar:chipinfo` / `chipinfo-1` | FAIL | "Variable not implemented" |
| `getvar:soc_family`, `getvar:feat`, `getvar:cbw`, `getvar:downloadsize`, `getvar:current-slot`, `getvar:max-download-size`, `getvar:variant`, `getvar:secure`, `getvar:unlocked` | FAIL | All "Variable not implemented" |
| `getvar:partition-type:*`, `getvar:partition-size:*` | FAIL | All "Variable not implemented" (partition table not enumerable via fastboot!) |
| `getvar:all` | INFO | Returns only `version:0.1` |

The `oem` command channel is gated by a **secure-boot whitelist**, even though the bootloader is "unlocked" (`verifiedbootstate=orange`):

| `oem` command | Result |
|---|---|
| `oem help` | OKAY (empty body) |
| `oem version`, `oem ?`, `oem keyman ...`, `oem env ...`, `oem mac`, `oem serial`, `oem chipinfo` | `FAIL: 'cmd <name> not in secure boot white list'` |

So **burn-mode fastboot is NOT a UART substitute**. The hope of running `keyman read mac` or `env print bootcmd` over USB to verify the AMLNORMAL keystore values from outside CoreELEC is blocked by the whitelist. UART or JTAG remain the only paths for live runtime introspection of the encrypted U-Boot's internal state.

### Implications for Phase 2 (actual restore flow)

A Linux/macOS USB-restore would need:

1. **A one-line patch to pyamlboot** (or a fork) to accept protocol type 6.
2. Probably patches to handle whatever S6 BURNSTEPS sequence the Windows USB Burning Tool sends (we have not characterized this — it's distinct from the protocol-type byte question).
3. Acceptance that the flash is flying blind — the partition table is not enumerable via fastboot getvar.
4. Recovery path on failure: Windows USB Burning Tool with the same `.img`.

Whether this is worth doing depends on whether you want a fully Linux/macOS-native restore flow. For the common case ("go back to Android from CoreELEC") the existing `ce-emmc-restore.sh` already handles it without USB Burning Tool. USB Burning Tool is only needed for catastrophic recovery, and at that point the question is just "is it OK to boot Windows once" — the answer for most users is yes.

### Deeper investigation — actual DNL protocol on this S6 chip

After the Khadas-adnl-rejects-mode-06 finding, a deeper dive using pyamlboot's source as a reference (and bypassing its hardcoded checks) was done against a live device. The full picture:

#### USB descriptor

```
idVendor:        0x1b8e
idProduct:       0xc004  (newer "ADNL" PID; older Amlogic chips used 0xc003)
bInterfaceClass:    0xff (vendor-specific)
bInterfaceSubClass: 0x42
bInterfaceProtocol: 0x3
endpoints:       0x01 BULK OUT, 0x81 BULK IN, 512-byte max packets
USB serial:      first 8 bytes of the chip's unique CID, hex-encoded
```

#### Two stages reachable from burn mode

Initial entry (reset button + power) lands the device in **TPL stage** — U-Boot is already running, loaded automatically from boot0+bootloader_a. From there, `reboot-romusb` (a fastboot top-level command, OKAY-accepted) transitions back to **ROM/BootROM stage**.

```
power-cycle + reset → TPL (U-Boot)  ──reboot-romusb──→ ROM (mask-ROM)
                       ^                                   ^
                       │                                   │
                       │            ← power-cycle ←        │
                       └───────────────────────────────────┘
```

Stages confirmed via `getvar:identify` reply byte 7 (`Stage` enum from pyamlboot: ROM=0, SPL=8, TPL=16).

#### Identify replies decoded

**TPL stage** (12-byte reply):
```
4f 4b 41 59 06 00 00 10 00 00 00 00
^^^^^^^^^^^ OKAY
            ^^ protocol type = 0x06 (S6 generation)
               ^^ minor version
                  ^^ reserved
                     ^^ stage = 0x10 = Stage.TPL
```

**ROM stage** (69-byte reply):
```
4f 4b 41 59 08 00 00 00 00 00 00 0f ... 57 more zero bytes
^^^^^^^^^^^ OKAY
            ^^ different field meaning in ROM stage
               ^^^^^ ...                ^^ stage = 0x00 = Stage.ROM
                                ^^ msg[11] = 0x0f = bitmap (pages 0-3 available)
```

Both `adnl` v2.7.5 (Khadas) and pyamlboot reject msg[4] != 0x05. The fix is a one-line patch:

```python
# pyamlboot adnl.py send_cmd_identify:
- if msg[4] != 0x5:
+ if msg[4] not in (0x5, 0x6):
      raise RuntimeError('Unexpected data in reply to "identify"')
```

But the patch is **necessary but not sufficient** for S6 flashing — see below.

#### Chip identification from BootROM stage

`getvar:getchipinfo-{0..7}` returns 65-byte pages. Pages 0-3 populated on this device:

| Page | Magic | Content |
|---|---|---|
| 0 | `INDX` | Index page; byte 4 = 0x0f = pages bitmap (pages 0-3 valid) |
| 1 | `CHIP` | **SoC family ID at offset 4 = `0x48`**; FEAT at offset 0x24 = `0x00000000`; chip unique ID at offset 0x14 = `80 1d f7 6c c5 00 32 15` |
| 2 | `CID_` | Chip ID, 16 bytes (8-byte unique ID + 8-byte repeat): `80 1d f7 6c c5 00 32 15 80 1d f7 6c c5 00 32 15` |
| 3 | `SGVR` | Signature/secure-version page — all zeros (no secureboot version provisioned, consistent with the unlocked bootloader) |

**SoC family ID 0x48 is NOT in pyamlboot's `SocFamily` enum** (which has values up to S4 = 0x37). This is the S6 family ID — undocumented in public Amlogic tools.

```python
class SocFamily(IntEnum):  # pyamlboot's current enum
    A1 = 0x2c
    C1 = 0x30
    SC2 = 0x32
    C2 = 0x33
    T5 = 0x34
    T5D = 0x35
    T7 = 0x36
    S4 = 0x37
    # Missing — discovered from chipinfo-1 on Ugoos AM9 Pro:
    # S6 = 0x48
```

The chip's unique ID (`80 1d f7 6c c5 00 32 15`) is the same value that appears as the USB DNL serial (`801df76cc500321500000000` with zero-padding). Different from `usid` in the AMLNORMAL keystore (the OEM-assigned `AM9PRO26010005693`).

#### TPL (U-Boot) fastboot dispatch table on S6

Tested every standard fastboot command from TPL stage. Implemented:

| Command | Result |
|---|---|
| `getvar:identify` | OKAY, returns identify struct (protocol=6, stage=TPL) |
| `getvar:serialno` | OKAY, returns USB DNL serial |
| `getvar:product` | OKAY: `amlogic` |
| `getvar:version`, `getvar:version-baseband`, `getvar:version-bootloader` | OKAY: `0.1` |
| `getvar:slot-count` | OKAY: `2` |
| `getvar:erase-block-size` | OKAY: `2000` |
| `getvar:all` | INFO returns `version:0.1` (everything else hidden) |
| `getvar:<anything else>` | FAIL: `Variable not implemented` |
| `flash:<part>` | **FAIL: `unknown command`** |
| `erase:<part>` | FAIL: `unknown command` |
| `boot`, `continue`, `set_active:*`, `download:*` | FAIL: `unknown command` |
| `reboot` | OKAY (device reboots) |
| `reboot-romusb` | OKAY (transitions to ROM stage) |
| `oem <anything>` | FAIL: `'cmd <X> not in secure boot white list'` |
| `oem help` | OKAY (empty body) — the only `oem` command not denied |

So U-Boot in burn mode exposes essentially nothing useful for writes. **The Windows USB Burning Tool cannot be using the TPL/U-Boot fastboot interface** — there's nothing there to use.

#### BootROM stage primitives

Tested from ROM stage:

| Command | Result |
|---|---|
| `getvar:identify` | OKAY, returns 69-byte reply (different format than TPL) |
| `getvar:serialno` | OKAY, returns USB DNL serial |
| `getvar:getchipinfo-0` through `-7` | OKAY, returns 65-byte pages (see above) |
| **`setvar:burnsteps`** | **FAIL: `unknow command`** (typo in firmware — sic) |
| `download:N`, `getvar:downloadsize` | **FAIL: `'1sect not send'`** |

#### The smoking gun: `'1sect not send'`

`'1sect not send'` is the most diagnostic message in the whole investigation. It tells us:

1. S6's BootROM has a **state machine** that gates data-plane commands behind an undocumented "first section" prerequisite — some specific control transfer or special opening packet must be sent before `download:` / `getvar:downloadsize` / probably all other write-path commands are accepted.
2. **pyamlboot's flow never sends this packet.** It assumes the BURNSTEPS primitive (`setvar:burnsteps`) is available immediately — and on S4-era chips it is. On S6 the primitive itself is `unknow command`, and the alternative path requires the "1sect" preamble we don't have.
3. **Windows USB Burning Tool must be sending the "1sect" packet** before any data plane work. Amlogic's proprietary tool knows the protocol; the public open-source tools don't.

#### Why this means Phase 2 is blocked

Combining all the findings, the obstacles to a Linux/macOS USB restore on the AM9 Pro:

| Layer | Problem |
|---|---|
| pyamlboot `send_cmd_identify` | Rejects protocol type 6 (one-line patch fixes) |
| pyamlboot `SocFamily` enum | Doesn't include 0x48 (one-line patch fixes) |
| pyamlboot `FeatSecurebootMask` | No entry for S6 (would need entry; FEAT=0 on this chip so trivially could default to 0x0) |
| pyamlboot `run_bootrom_stage` | Sends `setvar:burnsteps` which S6 BootROM doesn't implement — fundamental incompatibility |
| pyamlboot `run_tpl_stage` | Uses `oem mwrite ...` which the current bootloader denies via whitelist |
| **S6 BootROM** | Requires undocumented "1sect" preamble before any data-plane commands work |

The minor patches fix problems 1-3 in isolation. Problems 4-5 are protocol incompatibilities — the S6 BURNSTEPS / `oem mwrite` either has different syntax or is gated by the "1sect" preamble (#6). **Reverse-engineering the "1sect" preamble and S6's actual write protocol is the unblock — and that requires capturing USB traffic from the Windows USB Burning Tool against a real device.**

#### What about `aml_boot` (Rust)?

[`platform-system-interface/aml_boot`](https://github.com/platform-system-interface/aml_boot) is a Rust-based reverse-engineered Amlogic boot tool. It uses **USB control transfers** (vendor request type `0xc0` IN / `0x40` OUT on endpoint 0), with request codes documented in their `src/protocol.rs`:

```
REQ_CHIP_GEN       = 0x12
REQ_READ_MEM       = 0x02
REQ_WRITE_MEM      = 0x01
REQ_RUN            = 0x05
REQ_ADNL_WRITE_MEM = 0x07
REQ_ADNL_READ_MEM  = 0x09
REQ_IDENTIFY_HOST  = 0x20
REQ_CHIPINFO       = 0x40
REQ_TPL_CMD        = 0x30
REQ_BULK           = 0x34
REQ_PASSWORD       = 0x35
REQ_NOP            = 0x36
```

Tested every one against the AM9 Pro in burn mode — both TPL and ROM stages. **All eight returned EPIPE (USB STALL)**:

```
REQ_NOP            (0x36) → ERROR: USBError: [Errno 32] Pipe error
REQ_IDENTIFY_HOST  (0x20) → ERROR: USBError: [Errno 32] Pipe error
REQ_CHIP_GEN       (0x12) → ERROR: USBError: [Errno 32] Pipe error
REQ_CHIPINFO       (0x40) → ERROR: USBError: [Errno 32] Pipe error
REQ_READ_MEM       (0x02) → ERROR: USBError: [Errno 32] Pipe error
REQ_ADNL_READ_MEM  (0x09) → ERROR: USBError: [Errno 32] Pipe error
REQ_PASSWORD       (0x35) → ERROR: USBError: [Errno 32] Pipe error
REQ_BULK           (0x34) → ERROR: USBError: [Errno 32] Pipe error
```

`aml_boot` targets the older "GX-CHIP" protocol (USB PID **`0xc003`**, product string `"GX-CHIP"`) used on S905X / S905X2 / S905X3 / S905D3. The protocol primitives are at the USB-control-transfer layer. **Our AM9 Pro device uses PID `0xc004` and reports product string `"DNL"`** — the newer ADNL protocol family, which lives entirely on bulk endpoints. The vendor-specific control-transfer requests aren't implemented on this chip's BootROM.

`aml_boot`'s `proto-rev.md` does have well-documented chipinfo page structures for S905D3 — they match ours format-wise (CHIP / OPS_ / ROMV / INDX magics, family ID at offset 4, chip ID at offset 0x14). Only protocol primitives diverged across generations.

#### State of the art across public tools

| Tool | Protocol layer | SoCs covered | Works on AM9 Pro? |
|---|---|---|---|
| `aml_boot` (Rust) | Vendor control transfers (EP0, type 0xc0/0x40) | GX-CHIP (PID `0xc003`): S905X/X2/X3/D3 | ✗ All STALL |
| Khadas `adnl` v2.7.5 | ADNL bulk endpoints | ADNL (PID `0xc004`): S4, T7 | ✗ Rejects mode 06 |
| pyamlboot `adnl.py` | ADNL bulk endpoints | ADNL: S4 family (`0x37`) | ✗ Rejects mode 06 |
| pyamlboot with patch | ADNL bulk endpoints | (Adds S6 ID `0x48` to enum) | ✗ `setvar:burnsteps` "unknow command" |
| Windows USB Burning Tool | ADNL bulk endpoints (private) | Everything Amlogic ships | ✓ |

So we have full source for the older protocol (`aml_boot`) and partial source for the older ADNL (`pyamlboot`), but **the S6 ADNL variant is a closed protocol not yet reverse-engineered publicly**.

#### Concrete recommendation for anyone picking this up

The path forward to make this work is **NOT** "keep poking at pyamlboot." It's:

1. Boot Windows on a real PC or in a VM with USB passthrough.
2. Install the Amlogic USB Burning Tool v3 (or newer) — same one Ugoos ships.
3. Install [USBPcap](https://desowin.org/usbpcap/) (Windows) or use Wireshark with USB capture.
4. Capture an entire burn cycle of `AM9PRO_2.1.0.img` against a real AM9 Pro.
5. The "1sect" preamble will be visible in the capture as the first non-enumeration packet from host → device after USB configuration.
6. The S6 BURNSTEPS / `oem mwrite` commands will be visible with their actual payload formats.
7. Replicate in Python (extending pyamlboot or starting fresh), specifically:
   - Add SoC family ID `0x48` to pyamlboot's `SocFamily` enum
   - Patch `send_cmd_identify` to accept `msg[4]==0x06` for S6
   - Replace the `setvar:burnsteps`-based opening with whatever the Windows tool actually sends (the "1sect" packet)
   - Verify `oem mwrite` works after the fresh BL33 is loaded via the new opening sequence

Until that capture exists, **Linux/macOS USB restore on AM9 Pro is not achievable** with public tooling. The fastboot path is closed; the BURNSTEPS path is closed; the GX-CHIP vendor-control-transfer path is closed; there's no fourth option.

#### What stayed working

For the common "go back to Android from CoreELEC" case, [`ce-emmc-restore.sh`](ce-emmc-restore.sh) in this repo handles it without USB Burning Tool — it recreates the original partition table from the backup the install made, and Android reinitializes its own state from the still-intact `super` partition. **No Windows needed for the routine case.**

USB Burning Tool only matters for catastrophic recovery (lost backups, broken partition table, manual bootloader writes). For that, booting Windows once is the working path; everything we've built lets you inspect/modify the firmware image you'll flash from there.

### Where this stands

- `am9pro-usb-restore.sh` Phase 1 + 1.5 — **working, complete, useful** as a diagnostic.
- Phase 2 (actual flash) — **blocked at the protocol level**, not just at the tooling level. Requires Windows-side USB capture to make further progress.
- Tools shipped: `aml-img-tool.py` (image parser), `aml-bootloader-tool.py` (encrypted bootloader), `aml-keystore-tool.py` (AMLNORMAL), `aml-logo-tool.py` (AML_RES). The complete file-format toolset on the Linux/macOS side, ready for the day someone reverse-engineers the S6 USB DNL write protocol.

---

## Boot-Time Scripts on CE_FLASH

Three U-Boot-related files sit alongside `cfgload` on CE_FLASH. Documented here for completeness — only `cfgload` is on the live boot path; the other two are inactive on this install.

### `aml_autoscript` — env "installer"

A mkimage-format U-Boot script (1.3 KB; same `[size_be32][0_terminator][script]` layout as `cfgload`). Auto-loaded by some U-Boot configurations when a `aml_autoscript` file is found on the boot device. Contents:

```sh
defenv                                # reset U-Boot env to defaults
setenv bootfromnand 0
setenv upgrade_step 2
setenv ce_on_emmc "no"

# overwrite the boot-flow env variables with CoreELEC's eMMC-scan logic
setenv cfgload_env 'if fatload ${device} 0:1 ${loadaddr} cfgload_env; then env import -t ${loadaddr} ${filesize}; run ceboot; fi'
setenv cfgloadsd   'if fatload mmc 0:1 ${loadaddr} cfgload; then setenv device mmc; setenv devnr 0; setenv partnr 1; source ${loadaddr}; ...; run cfgload_env; fi'
setenv cfgloadusb  '...'
setenv cfgloademmc 'for p in 1 .. 1F; do if fatload mmc 1:${p} ${loadaddr} cfgload; then setenv device mmc; setenv devnr 1; setenv partnr ${p}; setenv ce_on_emmc "yes"; source ${loadaddr}; ...; fi; done'
setenv bootfromsd 'if mmcinfo; then run cfgloadsd; fi'
setenv bootfromusb '...'
setenv bootfromemmc 'run cfgloademmc'
setenv bootcmd '... run bootfromsd; run bootfromusb; run bootfromemmc; ...'

saveenv
run storeargs
run bootfromsd
run bootfromusb
run bootfromemmc
```

This is the **boot-env installer**: when U-Boot's `autoscr` is invoked on this file, it rewrites the persistent env to enable CoreELEC's "scan SD → USB → eMMC for `cfgload`" boot flow. On our system the env has already been provisioned (via the original CoreELEC SD-card boot), so `aml_autoscript` is not re-run; it stays on CE_FLASH as a recovery aid.

### `cfgload_env` — plaintext alternative to `cfgload`

A plain U-Boot env-import text file (2.2 KB). Defines a single env variable `ceboot=...` whose value is the same boot-logic script as `cfgload`'s inner script, but as one long line with `\` continuations. Intended use:

```sh
if fatload ${device} 0:1 ${loadaddr} cfgload_env; then
    env import -t ${loadaddr} ${filesize}   # import as env vars
    run ceboot                              # execute the variable
fi
```

Notable: this file contains the same `disk=FOLDER=/dev/CE_STORAGE` dual-boot path that the original `cfgload` had. Our install rebuilds `cfgload` to use `LABEL=` but does not touch `cfgload_env`, because the boot flow goes through `cfgload`, never `cfgload_env`. If you ever switch CoreELEC's boot mechanism to the env-import path, you'd need to fix `cfgload_env` similarly (or it would re-introduce the FOLDER= bug).

---

## AMLNORMAL Keystore — TLV Slot Format (in p1)

The header struct from CoreELEC/u-boot covers the AMLNORMAL block start, but the **slot value layout** is the file's own TLV encoding — reverse-engineered from a p1 dump on this device.

### Slot table layout (offset 0x200 from the AMLNORMAL header)

```
0x4200: T=1  L=24   value=<index/metadata>      (one per bank — purpose TBD)
0x4400: T=3  L=128  value=<slot 1: usid>
0x4488: T=3  L=128  value=<slot 2: mac>
0x4510+ zeros (no more populated slots on this unit)
```

Each top-level record is `(u32 type LE, u32 length LE, u8 value[length])`. T=3 records are slot containers.

### Slot inner records (within a T=3 container's value)

```
T=4   L=4   attribute     (4-byte flags — see below)
T=5   L=4+  name          ("usid", "mac", "$widevinekeybox", etc., variable length)
T=6   L=4   datasize      (4-byte actual byte count of the value)
T=7   L=N+  data buffer   (variable, often padded to a 4-byte boundary)
T=8   L=4   object type   (0xA00000BF = OBJ_TYPE_GENERIC; matches normal_key.h)
T=9   L=4   reserved      (observed 0; purpose unclear)
T=10  L=32  hash          (SHA-256 over the slot's value)
```

Attribute flag values (from `normal_key.h`):

| Bit | Constant | Meaning |
|---|---|---|
| 0x1 | `OBJ_ATTR_SECURE` | Slot is marked as "secure" (TEE-only on supported SoCs) |
| 0x2 | `OBJ_ATTR_OTP` | Slot is write-once after first programming |
| 0x4 | (undocumented) | Observed on `usid`; possibly "OTP locked" or "key was burned in" |
| 0x100 | `OBJ_ATTR_ENC` | Slot value is AES-encrypted (with a key from eFuse) |

### Decoded slots on this unit

```
slot #1: usid
  attribute: 0x4 (the undocumented bit; see above)
  type:      GENERIC
  datasize:  17
  value:     'AM9PRO26010005693'
  hash:      30b031bf2136977905b8826ffb049d60d61bfe31653cd2f99affb9f2c0c2fd64

slot #2: mac
  attribute: 0x3 (SECURE | OTP)
  type:      GENERIC
  datasize:  17
  value:     '90:0e:b3:fd:f8:55'
  hash:      524d46490670ee6963cdb90617f14c9572ef58274db0a4bc15863b725cf123d6
```

The `mac` slot's `OBJ_ATTR_OTP` flag means it's nominally write-once. The slot's SHA-256 hash would detect tampering on read but doesn't actually prevent rewrites at the eMMC level — see the security caveat in [Per-Device Identity Provenance](#per-device-identity-provenance).

### Inspector tool

`aml-keystore-tool.py` in this repo decodes any AMLNORMAL dump:

```bash
python3 aml-keystore-tool.py info reserved.bin     # header summary
python3 aml-keystore-tool.py list reserved.bin     # walk all populated slots
python3 aml-keystore-tool.py extract reserved.bin out/   # write each slot's value to a file
```

Verified against the dump of `/dev/mmcblk0p1` on this unit. Read-only; never writes to the input file.

---

## Active DTB — `/flash/dtb.img` (CoreELEC variant)

The active device tree (`/flash/dtb.img`, 84 KB) is **CoreELEC's customized DTB for the AM9 Pro**, not the stock Ugoos one. The CoreELEC variant has CoreELEC-specific marker properties:

```dts
/ {
    coreelec;                                       // CoreELEC marker
    coreelec-dt-id = "s6_s905x5_ugoos_am9_pro";    // CoreELEC board name
    ...
};
```

The factory Ugoos DTB lives inside `bootloader_a` (encrypted) and is also shipped in `AM9PRO_2.0.9.img` as the `dtb/meson1` item (86004 bytes, plaintext). The factory and CoreELEC DTBs both compile to ~85 KB and describe the same hardware but differ in a few important places:

| Subsystem | Factory Ugoos DTB | CoreELEC DTB |
|---|---|---|
| Root `compatible` | `"s6_s905x5_umx5jyks"` | `"amlogic, s6"` |
| Root `model` | not set | `"Ugoos AM9 Pro"` |
| CoreELEC markers | none | `coreelec;` + `coreelec-dt-id` |
| GPU `compatible` | **`"arm,malit60x", "arm,malit6xx", "arm,mali-midgard"`** (WRONG — Midgard is the older generation) | `"arm,mali-valhall-csf"` (correct) |
| CPU `compatible` | `"arm,cortex-a55", "arm,armv8"` | same as factory |

Both DTBs declare the CPU as A55 even though the hardware is A510 (see CPU note above). Both DTBs correctly identify the PMU as A510-family. The GPU is the one place CoreELEC's DTB is actually more accurate than the factory's — `Mali Valhall G310 CSF` is what the silicon actually is, not Midgard.

Decompilable with standard `dtc`:

```bash
dtc -I dtb -O dts -o ugoos-am9-pro.dts dtb.img
```

Resulting DTS (~4900 lines) confirms the hardware identification used elsewhere in this document:

```dts
/ {
    amlogic-dt-id = "s6_s905x5_umx5jyks";
    coreelec-dt-id = "s6_s905x5_ugoos_am9_pro";
    compatible = "amlogic, s6";
    model = "Ugoos AM9 Pro";
    ...
};
```

Notable findings from the DTBs:

- **CPU declaration is a software shim, not ground truth.** Both DTBs declare the CPU as `cortex-a55`, but `MIDR_EL1 = 0x411fd463` and the SVE2/MTE/BF16 feature flags confirm the hardware is Cortex-A510 (ARMv9.0a). The DTB uses A55 for kernel-driver binding compatibility on the 5.15 kernel base; the silicon is A510.
- **GPU**: CoreELEC's DTB correctly declares `arm,mali-valhall-csf` (Mali Valhall G310 CSF). The factory Ugoos DTB declares the GPU as Mali Midgard — an older generation entirely. CoreELEC fixed this in their patch set.
- **Board name**: `umx5jyks` (matches the U-Boot env `board=umx5jyks`). Ugoos's internal codename.
- **Bootloader build timestamp**: from the U-Boot env `bootloader_version=01.01.260115.144049` and the `@AMLBOOT` board ID `S6-s905x5-2601151440`, the encoding is `YYMMDD.HHMMSS` → built **2026-01-15 at 14:40:49**.

---

## Bootloader Partition (p7) — Amlogic `@AMLBOOT` Container

The `bootloader_a` partition (p7, 8 MB allocated, 3.91 MB active content) is the Amlogic signed bootloader image — a single binary that bundles BL2, BL30, BL31 (ARM Trusted Firmware), BL32 (TEE), BL33 (U-Boot), and the DDR firmware. The SoC's mask-ROM BL1 reads this partition at boot and chain-loads each stage.

### Container structure (verified on `s6_s905x5_ugoos_am9_pro`)

```
+--------+--------+----------------------------------------------+
| 0x000  |        | encrypted boot-stage chunks, framed by       |
|        |        | @ML  16-byte markers, with 32-byte tail      |
|        | BBST   | hashes and DBLK_PAYLOAD_END sentinels         |
| 0x4382 | ◀━━━━━ | @AMLBOOT manifest (plaintext, decodable)     |
| 0x44000|--------+----------------------------------------------+
|        | BL2E   | BL2 entry image (encrypted)                  |
| 0x61000|--------+----------------------------------------------+
|        | BL2X   | BL2 extension (encrypted; DDR init logic)    |
| 0x7c000|--------+----------------------------------------------+
|        | DDRF   | DDR firmware slot (256 KB, all zero — DDR     |
|        |        | firmware is bundled into the BL2 stages on    |
|        |        | this SoC family)                              |
| 0xbc000|--------+----------------------------------------------+
|        | DEVF   | device firmware: BL31 + BL32 + BL33 + DTBs   |
|        |        | (encrypted as a single 2.5 MB blob)          |
| 0x339000|------------------------------------------------------|
|        |        | zero padding to 8 MB partition end           |
+--------+--------+----------------------------------------------+
```

### `@AMLBOOT` manifest (32 bytes + N×16-byte entries)

The manifest at file offset `0x43820` is the only **plaintext** part of the container. It is what `aml-bootloader-tool.py info` decodes:

```
@AMLBOOT manifest at file offset 0x43820
  flags: 02059000
  board: 'S6-s905x5-2601151440'

name       offset       size  size (KB)  flags
BBST          0x0    0x44000        272  0x0
BL2E      0x44000    0x1d000        116  0x0
BL2X      0x61000    0x1b000        108  0x0
DDRF      0x7c000    0x40000        256  0x0
DEVF      0xbc000   0x27d000       2548  0x0
```

Manifest header:
- `[0:8]`   ASCII magic `"@AMLBOOT"`
- `[8:12]`  flags/version (observed `02 05 90 00`)
- `[12:32]` 20-byte board identifier (`S6-s905x5-2601151440` on this unit — matches the U-Boot env `bootloader_version=01.01.260115.144049`)

Each entry (16 bytes):
- `[0:4]`   4-character ASCII section name
- `[4:8]`   absolute offset within the bootloader image (uint32 LE)
- `[8:12]`  section size in bytes (uint32 LE)
- `[12:16]` flags (zero on this unit)

The entry list terminates on a `\\0\\0\\0\\0` name.

### Encryption — and why we can't disassemble it

All four content sections (BBST, BL2E, BL2X, DEVF) measure as near-maximum byte entropy:

| Section | Entropy (bits/byte) |
|---|---|
| BBST | 7.723 |
| BL2E | 7.606 |
| BL2X | 7.897 |
| DDRF | 0.000 (all zeros) |
| DEVF | 7.988 |

For comparison, plaintext code and data on ARM64 typically measures 4–6 bits/byte. Anything above ~7.8 is essentially indistinguishable from random output — the SoC encrypts the stages at rest and decrypts them in the BootROM at load time using a per-SoC-family hardware key. No `U-Boot` string, no `OP-TEE` magic, no FDT magic (`0xd00dfeed`) appears anywhere in the file.

The encryption key for the S6 family (`s5_s4` / `s5_s6` Amlogic naming) is not publicly available. Without it:

- The U-Boot binary, BL31 (ARM-TF), and BL32 (TEE) cannot be disassembled from the on-disk image.
- The implementation of `keyman read mac/usid` (the U-Boot command that reads the UKS keystore in p1) cannot be inspected statically.
- `factory_provision init` (the command that runs at the end of `cmdline_keys` and is expected to write the UKS keystore on first Android boot) is opaque.

### Paths forward if you actually want the decrypted binary

1. **Runtime memory capture via UART.** Solder/clip to the debug pads, interrupt U-Boot's autoboot (the env has `bootdelay=0`, so this requires sending characters before BL2 hands off), and use `md` to dump the U-Boot text segment from RAM. Same approach works for BL31/BL32 if you can read secure memory (you generally can't from U-Boot non-secure, but can from a custom BL31 patch — circular dependency).

2. **JTAG / SWD.** Most Amlogic boards expose JTAG pads. Halting before BL2 decrypts would let you watch the decryption happen. Significant hardware project.

3. **Public Amlogic BSP releases.** Khadas, Hardkernel (Odroid), and others have released U-Boot source for some Amlogic SoC families. The Khadas VIM4 (S6 family) sources may correlate. The vendor binary would still differ from Ugoos's, but the surface API (`keyman`, `factory_provision`) would be identical or near-identical.

4. **`aml_image_v2_packer` / `aml_encrypt_*` tools.** Amlogic ships these for SDK customers. They are sometimes leaked. The packer side teaches you the container format (already partially documented above); the encrypt side requires the family key, which is not in the public versions.

### What you can still see without decryption

- The `@AMLBOOT` manifest reveals which stages are present, their sizes, and their offsets — confirmed via this tool.
- The 5 `@ML`-framed chunks in BBST (at offsets `0x1000, 0x1fc0, 0x4320, 0x6680, 0x7640`) plus two more (`0x99a0, 0x9a20`) are likely BL2 boot stages (each followed by a `DBLK_PAYLOAD_END` sentinel). They are encrypted independently — possibly different stages run with different keys.
- All DTBs referenced at boot are also shipped unencrypted in CoreELEC's `/flash/device_trees/` directory, so device-tree inspection doesn't require decrypting the bootloader.

### Public BSP correlation — what's actually inside

The encrypted U-Boot binary on the device is a Ugoos build of Amlogic's reference U-Boot. CoreELEC publishes a near-identical fork at [`CoreELEC/u-boot`](https://github.com/CoreELEC/u-boot) — same SoC family (S5/S6), same `bl33/v2023` tree, same command set. Reading the public source gives us the exact same behavior at the API level even though the on-device binary stays encrypted.

**`keyman` command** — [`bl33/v2023/drivers/amlogic/keymanage/key_manage.c`](https://github.com/CoreELEC/u-boot/blob/master/bl33/v2023/drivers/amlogic/keymanage/key_manage.c):

```
U_BOOT_CMD(keyman, 5, 0, do_keymanage, "Unify key ops interfaces based dts cfg",
    "    init seedNum <dtbAddr>\n"
    "    read keyname addr <hex/str>\n"
    "    write keyname size addr \n"
    "    write keyname hex/str value\n"
    "    query exist/secure/size keyname\n"
    "    exit \n");
```

`keyman init 0x1234` passes the seed number to `key_unify_init(seednum, dtbaddr)` which selects the storage backend. `keyman read <name> <addr> str` calls `key_manage_read(keyname, dataBuf, keyLen)`, null-terminates the result, and calls `env_set(keyname, dataBuf)` — that's how the U-Boot env variable `mac=...` gets populated from the keystore.

**Storage backend** — [`bl33/v2023/drivers/amlogic/storagekey/storagekey.c`](https://github.com/CoreELEC/u-boot/blob/master/bl33/v2023/drivers/amlogic/storagekey/storagekey.c) and [`normal_key.c`](https://github.com/CoreELEC/u-boot/blob/master/bl33/v2023/drivers/amlogic/storagekey/normal_key.c):

The keystore reads from the eMMC via `store_rsv_read("key", ...)`. The `"key"` sub-region is a named area within Amlogic's reserved partition (p1). Default block size is `DEF_NORMAL_BLOCK_SIZE = 256 KB`, and total storage area `SECUESTORAGE_WHOLE_SIZE = 0x40000` (256 KB) — exactly matching the stride we observed between the two `AMLNORMAL` copies at offsets `0x4000` and `0x44000` in p1.

**AMLNORMAL on-disk header (from the public source):**

```c
struct storage_block_raw_head {
    u8  mark[16];      /* "AMLNORMAL" magic */
    u32 version;
    u32 enctype;       /* 0 = plaintext, nonzero = AES with eFuse-bound key */
    u32 keycnt;        /* number of populated slots */
    u32 initcnt;
    u32 wrtcnt;
    u32 errcnt;
    u32 flags;
    u8  headhash[32];  /* SHA-256 of the header */
    u8  hash[32];      /* SHA-256 of the data */
};
```

Integrity is via SHA-256 (NOT HMAC — anyone with the on-disk image can compute and verify these hashes). This means the integrity protection is weaker than initially assumed: there's no secret involved, only an integrity check. The protection against eMMC-level tampering would have to come from the `enctype` field (AES-encrypted slots), but on this unit `enctype = 0` (plaintext, which is why `usid` and `mac` show up directly as readable strings).

Slot lookup uses `normalkey_get((u8*)"usid")` — an in-memory linked-list search after the on-disk block is loaded and decoded. Slot definitions live in [`normal_key.h`](https://github.com/CoreELEC/u-boot/blob/master/bl33/v2023/drivers/amlogic/storagekey/normal_key.h):

```c
struct storage_object {
    char name[MAX_OBJ_NAME_LEN];  /* 80 bytes */
    u32 namesize;
    u32 attribute;                 /* OBJ_ATTR_SECURE / OTP / ENC */
    u32 type;                      /* AES / RSA / GENERIC (0xA00000BF) */
    u32 datasize;
    u8 *dataptr;
    u8 hashptr[32];
};
```

**`factory_provision init`** — [`bl33/v2023/cmd/amlogic/cmd_avb.c`](https://github.com/CoreELEC/u-boot/blob/master/bl33/v2023/cmd/amlogic/cmd_avb.c) in `persistent_store()`:

```c
/* initialize factory partition */
rc = run_command("factory_provision init", 0);
if (rc) {
    printf("init factory partition failed\n");
    return NULL;
}
```

The `init` subcommand prepares the factory partition (p4) for use before AVB persistent data can be written. The `factory_provision` command itself is in [`cmd/amlogic/factory_provision/cmd_factory_provision.c`](https://github.com/CoreELEC/u-boot/blob/master/bl33/v2023/cmd/amlogic/factory_provision/cmd_factory_provision.c) and supports `write`, `query`, `list`, `remove`, `clear`, `version`. The `init` form is invoked when Android's AVB code needs to materialize the partition's filesystem; on this unit `avb2=0` is set in the U-Boot env, so the AVB code path is inactive — explaining why p4 has been left as an empty FAT12 placeholder.

### Key takeaways from the BSP correlation

1. **Verified that the `cmdline_keys` env script in p2 maps 1:1 to public CoreELEC/u-boot source.** Nothing in the boot path is Ugoos-proprietary at the command level — it's the standard Amlogic SDK U-Boot.
2. **`keyman init 0x1234` is the standard secure-key-device selector.** The seed value picks which backend (eFuse vs. normal-key-storage). On this device it resolves to the AMLNORMAL keystore in p1.
3. **The keystore's integrity guarantee is SHA-256, not HMAC.** That's enough to detect bit-rot, not to prevent forgery. The "anti-tamper" property comes from `enctype` (encryption) which is OFF on this unit — anyone with eMMC-level write access could rewrite the ETH MAC and serial. (Practically: only useful if you have the device opened up and can wire to the eMMC bus, since boot/storage signing prevents post-boot rewrites of p1.)
4. **`factory_provision init` is only triggered by the AVB code path.** With `avb2=0`, it never runs — confirming why p4 stays empty on this unit.
5. **Slot value format is structured, not raw.** The header is followed by TLV-encoded `storage_object` entries (name, attribute, type, data, hash). Our observation of `05 00 00 00 04 00 00 00 75 73 69 64 …` followed by the literal `"AM9PRO26010005693"` matches the expected TLV layout (type 5, name-length 4, name `"usid"`, …).
6. **Decrypting the actual on-device U-Boot binary is no longer necessary** to understand its behavior — the public source has it line-for-line. Decryption would still be needed to verify the on-device binary matches the public source byte-for-byte (i.e., Ugoos didn't add custom commands beyond the standard set), but functional understanding is complete.

### Unpack tool

`aml-bootloader-tool.py` in this repo handles inspection and extraction:

```bash
python3 aml-bootloader-tool.py info bootloader_a.bin
python3 aml-bootloader-tool.py unpack bootloader_a.bin extracted/
```

The unpacker writes each section to its own file (`BBST.bin`, `BL2E.bin`, etc.) plus a `manifest.txt`. The output is preserved in encrypted form — useful as a starting point if the S6 family key ever surfaces, and useful for cross-comparison against bootloader updates.

---

## Logo Partition (p10) — AML_RES v2 Format

The `logo` partition holds Amlogic's resource container for the bootup logo and various upgrade-state graphics. It uses the **AML_RES v2** format (`AML_RES!` magic). On this unit the partition is 8 MB allocated; the active content is 1,679,328 bytes (1.6 MB).

### Container layout

```
+--------+------------------------------------------+
| 0x000  | header (64 bytes)                        |
| 0x040  | item descriptors (N × 64 bytes)          |
| 0x???  | BMP data, each aligned to alignSz (16)   |
+--------+------------------------------------------+
```

**Header (64 bytes):**

| Offset | Size | Field |
|---|---|---|
| 0 | 4 | CRC32 of bytes `[4:imgSz]` — Amlogic uses the *raw* CRC32 (`zlib.crc32() XOR 0xFFFFFFFF`), i.e., the intermediate state without the standard final XOR-out. |
| 4 | 4 | version (`= 2`) |
| 8 | 8 | magic `"AML_RES!"` |
| 16 | 4 | `imgSz` — total bytes including header |
| 20 | 4 | `imgItemNum` — number of items |
| 24 | 4 | `alignSz` — alignment for item data (16) |
| 28 | 36 | reserved (zeros) |

**Each item descriptor (64 bytes):**

| Offset | Size | Field |
|---|---|---|
| 0 | 4 | item magic `0x27051956` (the mkimage magic, used here as a marker) |
| 4 | 4 | nameId = 0 |
| 8 | 4 | BMP size in bytes |
| 12 | 4 | BMP start offset within the file |
| 16 | 4 | 0 |
| 20 | 4 | file offset of the **next** item descriptor (`0` for the last item) |
| 24 | 4 | 0 |
| 28 | 4 | `index | (totalItems << 8)` |
| 32 | 32 | null-terminated item name |

### Items on this unit

| Idx | Name | Size | Dimensions | bpp | Content |
|---|---|---|---|---|---|
| 0 | `upgrade_upgrading` | 180072 | 300 × 300 | 16 (BI_BITFIELDS) | Android bugdroid with "Upgrading…" label |
| 1 | `upgrade_logo` | 180072 | 300 × 300 | 16 | Android bugdroid |
| 2 | `upgrade_error` | 180072 | 300 × 300 | 16 | Android + orange warning triangle |
| 3 | `upgrade_bar` | 184 | 4 × 14 | 16 | Tiny progress-bar fill |
| 4 | `upgrade_success` | 180072 | 300 × 300 | 16 | Android + green check |
| 5 | `bootup_lowcurrent` | 259272 | 360 × 360 | 16 | "INSUFFICIENT POWER SUPPLY" warning |
| 6 | `upgrade_fail` | 180072 | 300 × 300 | 16 | Android + red X |
| 7 | `bootup` | 259270 | 360 × 360 | 16 | **Main boot logo** — Amlogic + AV1 + S905X5 jaguar |
| 8 | `low_voltage` | 259270 | 360 × 360 | 16 | "USB PD 9V 12V" warning |
| 9 | `upgrade_unfocus` | 184 | 4 × 14 | 16 | Progress-bar background |

All BMPs use 16bpp with `BI_BITFIELDS` compression and Adobe Photoshop's alpha-channel mask variant (the `file` utility reports them as "Adobe Photoshop with alpha channel mask").

### Unpack and pack

`aml-logo-tool.py` in this repo handles both directions:

```bash
# Extract BMPs from a logo partition dump
python3 aml-logo-tool.py unpack logo.bin extracted/

# Repack a directory of BMPs into an AML_RES container
python3 aml-logo-tool.py pack edited/ new-logo.bin
```

The unpacker names files as `NN_name.bmp` so the packer can recover the ordering and slot names from the filenames alone. Verified by round-trip: `unpack` + `pack` of the stock AM9 Pro logo produces a byte-identical container with the same CRC.

### Customizing the boot logo

To replace, e.g., the main boot logo with a custom image:

1. Pull p10 to a working machine: `dd if=/dev/logo of=logo.bin bs=1M count=2` (only the active 1.6 MB is meaningful).
2. Unpack: `python3 aml-logo-tool.py unpack logo.bin extracted/`.
3. Edit `07_bootup.bmp` in any image editor. Match the original's dimensions (360 × 360), color depth (16bpp / BI_BITFIELDS), and BMP variant if you want the exact look. Different sizes work as long as the total fits in the 8 MB partition; the packer adjusts offsets.
4. Repack: `python3 aml-logo-tool.py pack extracted/ new-logo.bin`.
5. Write back: `dd if=new-logo.bin of=/dev/logo bs=1M conv=fsync` on the device.

Write-back has not been performed or tested on this device. The format is well-understood and the unpack→repack round-trip is verified byte-identical, but customizing the live logo is a destructive operation against p10 that should be done after backing up the original.

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
