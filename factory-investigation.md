# Factory Provisioning Investigation — AM9 Pro

**Date:** 2026-05-25 (initial), 2026-05-26 (revised after p1 keystore discovery)
**Device:** 192.168.1.139, CoreELEC 22.0-Piers_nightly_20260525
**Question:** On a pre-first-boot unit with empty `factory` (p4), where do the working MACs, serial, and other per-unit data actually live?

> **Revision history:**
>
> - **2026-05-25:** Initial investigation. Concluded RPMB held the identity values directly (this turned out to be wrong).
> - **2026-05-26:** A direct read of p1 `reserved` found the **Amlogic UKS keystore in plaintext on the eMMC** at offset 0x4000, with the serial and ETH MAC stored as named slots. RPMB is provisioned but is not on the keyman read path.
> - **2026-05-26 (later):** Public BSP correlation against [`CoreELEC/u-boot`](https://github.com/CoreELEC/u-boot) `bl33/v2023/drivers/amlogic/storagekey/normal_key.c` confirmed the on-disk struct: keystore integrity is **plain SHA-256** (covering the header and the data region separately), not HMAC. The earlier "RPMB holds the HMAC key" hypothesis was wrong — no secret key is involved in the integrity check at all. RPMB is provisioned for Android's Keymaster/TEE post-boot, not for U-Boot keyman.
>
> Net effect: p1 IS load-bearing for eMMC-stored identity, and the original "no eMMC operation can lose identity" claim was wrong. The conclusions below have been updated.

---

## TL;DR

| Identity | Value on this unit | Source (verified) |
|---|---|---|
| Ethernet MAC | `90:0E:B3:FD:F8:55` | Plaintext in the Amlogic UKS keystore on **p1 `reserved`** at offset 0x4000 (redundant copy at 0x44000). U-Boot's `cmdline_keys` script reads via `keyman read mac` and sets the kernel cmdline `mac=`. The env partition also stores `ethaddr=` as a redundant copy. Integrity is plain SHA-256 (no HMAC; no secret key) — verified against [`CoreELEC/u-boot`](https://github.com/CoreELEC/u-boot/blob/master/bl33/v2023/drivers/amlogic/storagekey/normal_key.c) source. |
| WLAN MAC | `40:D9:5A:FC:E2:88` | **BCM4389 chip OTP.** dmesg: `[dhd] use firmware generated mac_address 40:d9:5a:fc:e2:88` |
| BT MAC | `40:D9:5A:FC:E2:89` | Same BCM4389 OTP (WLAN MAC + 1) |
| Serial | `AM9PRO26010005693` | Plaintext in the p1 UKS keystore (slot `usid`); read at boot via `keyman read usid`. |

**Conclusions:**

1. The empty `factory` partition (p4 KEYBOX PART, FAT12, content-empty) is **not** the source of any working identity on this device. It is a placeholder.
2. The Amlogic SoC eFuses for MAC/USID are **all zero** — also not the source.
3. The working ETH MAC and serial are stored in plaintext in the **p1 `reserved` partition** under the Amlogic UKS / `AMLNORMAL` keystore format. U-Boot reads them via `keyman init 0x1234` + `keyman read mac/usid`.
4. The working WLAN/BT MACs are read by the Broadcom firmware from the **BCM4389 chip's own OTP**, completely independent of the eMMC.
5. `cri_data` (p14) is fully zeroed — no per-device data there.
6. **eMMC-stored identity IS at risk if p1 is wiped.** WLAN/BT MACs from BCM OTP survive any eMMC operation. ETH MAC and serial would be lost if p1 is wiped; the factory image does not include p1, so USB Burning Tool restore would NOT bring them back. None of this is a concern for the CoreELEC install — it does not touch p1 — but it changes how aggressive an eMMC-wipe experiment can be without permanent identity loss.

---

## Backups Saved

On device:
```
/storage/factory_backup.bin    8 MB   md5 7ee270f66bdc9db6a60166b8e6c09978
/storage/cri_data_backup.bin   8 MB   md5 96995b58d4cbf6aaa9041b4f00c7f6ae  (= 8MB of zero)
```

Also pulled to Mac at `~/Projects/ugoos-am9-pro-research/device-backups/` with matching md5s.

---

## What's in p4 (factory / KEYBOX PART)

```
DOS/MBR boot sector, OEM-ID "mkfs.fat", sectors 16384 (8 MB),
serial 0xd0a662de, label "KEYBOX PART", FAT12
```

- A fresh FAT12 boot sector at offset 0
- Empty FAT and root directory (just `f8 ff ff` cluster-zero markers)
- Everything else: zeros

The partition was preformatted at the factory but **never populated with a keybox**. This is consistent with `androidboot.firstboot=1` and with the Widevine L3-only certification (no L1 keybox blob to store here).

## What's in p14 (cri_data)

Entirely zeros across all 8 MB. md5 matches that of /dev/zero of the same size. No per-device data, no headers, no structure.

`cri_data` on Amlogic platforms is typically used for **critical data backups** (DRM keys, calibration) when the keybox/factory path is in use. On this unit it has never been written. Backing it up amounts to backing up a zero-fill.

---

## Where the IDs really come from

### Ethernet MAC and serial: U-Boot `keyman` → Amlogic UKS keystore in p1 reserved

The `env` partition (p2) contains a U-Boot script variable `cmdline_keys` that runs late in `storeargs`:

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

`keyman` is Amlogic's Unified Key Store (UKS) interface. The magic `0x1234` selects the secure key device. The UKS keystore on this SoC lives on the eMMC in the `reserved` partition (p1):

```
$ python3 -c 'import re; d=open("/dev/reserved","rb").read(); \
  print([hex(m.start()) for m in re.finditer(b"AMLNORMAL", d)])'
['0x4000', '0x44000']

$ python3 -c 'import re; d=open("/dev/reserved","rb").read(); \
  print([hex(m.start()) for m in re.finditer(b"AM9PRO26010005693", d)])'
['0x4434', '0x44434']

$ python3 -c 'import re; d=open("/dev/reserved","rb").read(); \
  print([hex(m.start()) for m in re.finditer(b"90:0e:b3:fd:f8:55", d)])'
['0x44bc', '0x444bc']
```

The keystore is duplicated (offsets 0x4000 and 0x44000 — a 256 KB stride for redundancy). Each bank has an `AMLNORMAL` magic header, a version word, and two 32-byte SHA-256 hashes (one over the header, one over the data region) — verified against the public CoreELEC/u-boot `storage_block_raw_head` struct. Then a slot table containing both the **slot names** (e.g. `usid`, `mac`, `$widevinekeybox`, `$attestationkeybox`, `$netflix_mgkid`, `$PlayReadykeybox25`, `$hdcp22_fw_private`, `$mac_wifi`, `$deviceid`, `$region_code`, `$secure_boot_set`) and their values. On this unit only `usid` and `mac` are populated; the other slot names exist but the values are empty — consistent with `androidboot.firstboot=1` (factory provisioning ran far enough to set identity, not far enough to run `factory_provision init` and populate the DRM keyboxes).

The other candidates were ruled out by direct observation:

- **eFuses** — `/sys/class/efuse/{mac,mac_wifi,mac_bt,usid}` all read as zero bytes.
- **factory partition (p4)** — empty FAT12.
- **mmcblk0boot0 / mmcblk0boot1** — nearly all zero (one-byte header each, identical md5).

**RPMB** is provisioned on this unit (`rpmb_state=0x1`) but is **not on the keyman read path**. The CoreELEC/u-boot source reads the keystore from the `"key"` sub-region of the reserved partition via `store_rsv_read("key", ...)` and verifies integrity with plain SHA-256 over the data — no HMAC, no secret key. RPMB exists for Android Keymaster / TEE hardware-backed key storage post-boot, not for U-Boot keyman.

The U-Boot env partition (p2) also stores `ethaddr=90:0e:b3:fd:f8:55` as a legacy variable, providing a secondary path to the ETH MAC if the keystore read fails. WLAN/BT MACs come from the BCM4389 chip OTP entirely independently.

### WLAN / BT MACs: BCM4389 chip OTP

The Wi-Fi/BT subsystem is a **BCM4389** (PCIe vendor `0x14E4`, device `0x449D`, chip `0xaae8` rev 2). The driver is `bcmdhd` (Cypress/Broadcom DHD), firmware path `/usr/lib/firmware/brcm/pcie/`.

The NVRAM file `nvram_ap6275p.txt` contains the **placeholder** Broadcom-default MAC `00:90:4c:12:d0:01` — clearly not the actual value in use.

The Linux-side `/sys/wifi/mac_addr` reads as `00:00:00:00:00:00` — the driver receives no MAC from the platform.

dmesg confirms the source:
```
[dhd] dhd_legacy_preinit_ioctls: use firmware generated mac_address 40:d9:5a:fc:e2:88
[dhd] Firmware up: op_mode=0x0005, MAC=40:d9:5a:fc:e2:88
[dhd] Register interface [wlan0]  MAC: 40:d9:5a:fc:e2:88
```

The BCM4389 firmware reads the MAC from the chip's internal OTP and presents it to the host. The BT MAC is the WLAN MAC + 1 by Broadcom convention (`40:d9:5a:fc:e2:89`). The Broadcom OUI `40:D9:5A` is registered to Broadcom — consistent with chip-burned identity.

These MACs are **independent of the eMMC entirely**. Wiping the eMMC, replacing the partition table, or running USB Burning Tool does not affect them.

---

## Paths and sysfs interfaces checked

| Source | Path | Result on this unit |
|---|---|---|
| Kernel cmdline | `/proc/cmdline` | `mac=90:0e:b3:fd:f8:55` present |
| dmesg | various | ETH MAC from `uboot setup mac-addr`; WLAN from `dhd firmware generated` |
| Amlogic eFuse driver | `/sys/class/efuse/mac` | all zero |
| Amlogic eFuse driver | `/sys/class/efuse/mac_wifi` | all zero |
| Amlogic eFuse driver | `/sys/class/efuse/mac_bt` | all zero |
| Amlogic eFuse driver | `/sys/class/efuse/usid` | all zero (16 bytes) |
| Amlogic eFuse driver | `/sys/class/efuse/checklist` | `dgpk1`, `dgpk2`, `aud_id` (nothing about MAC) |
| Amlogic eFuse driver | `/sys/class/efuse/checkburn` | `unknown` |
| Amlogic WLAN platform | `/sys/wifi/mac_addr` | `00:00:00:00:00:00` (no platform-supplied MAC) |
| Amlogic WLAN platform | `/sys/wifi/firmware_path` | `/usr/lib/firmware/brcm/pcie/` |
| Amlogic WLAN platform | `/sys/wifi/nvram_path` | `/usr/lib/firmware/brcm/pcie/` |
| Kernel param | `/sys/module/kernel/parameters/wifimac` | `(null)` |
| BCM NVRAM file | `/usr/lib/firmware/brcm/pcie/nvram_ap6275p.txt` | `macaddr=00:90:4c:12:d0:01` (placeholder, not used) |
| BCM firmware | runtime via dhd | reports `40:d9:5a:fc:e2:88` from chip OTP |
| U-Boot env | `mmcblk0p2` strings | `ethaddr=90:0e:b3:fd:f8:55`, `cmdline_keys` script with keyman calls |
| Factory partition | `mmcblk0p4` | empty FAT12 "KEYBOX PART", no content |
| cri_data | `mmcblk0p14` | all zeros |
| boot0/boot1 | `mmcblk0boot0/1` | nearly all zeros (1-byte header each, identical md5 `a2b832f9...`) — no clear-text keys |

`fw_printenv` was not usable because the on-disk config points at `/dev/env` / `/dev/nand_env`, neither of which exist as nodes; raw `dd` of p2 + string extraction was the working path.

---

## Survey of other per-device-unique partitions

For completeness, I ran a quick scan across all small partitions to identify what else has unit-specific data worth preserving. (Full md5 vs. matching-size all-zero md5 reference; sample of head bytes.)

| Part | Size | Status | Notes |
|---|---|---|---|
| p1 reserved | 64 MB | **Amlogic UKS keystore** (`AMLNORMAL` magic at 0x4000, redundant copy at 0x44000) | **Load-bearing for per-device identity.** Holds the ETH MAC and serial as plaintext slots, plus a catalog of empty slot names for DRM/attestation. Confirmed via direct read + CoreELEC/u-boot source. |
| p3 frp | 2 MB | **non-zero**: 36 bytes of random-looking data at offset 0, then zeros | Likely a per-device FRP/anti-rollback signing nonce. **Worth backing up.** |
| p4 factory | 8 MB | empty FAT12 (this report) | Empty — no risk |
| p9 tee | 32 MB | mostly zero, data starts ~256 KB in | TEE firmware lives inside `bootloader_a`, not this partition. The non-zero region appears to be uninitialized state, not unit-specific data |
| p10 logo | 8 MB | `AML_RES!` magic — Amlogic logo pack | Generic per firmware build, not per-device. Decoded — see `aml-logo-tool.py` |
| p11 misc | 2 MB | Android A/B slot metadata (`BCAB` magic at offset 0x800) | Currently selecting slot `_a`. Not unit-specific |
| p12 dtbo_a | 2 MB | FDT magic `d7 b7 ab 1e` — DTB overlay (438-byte payload) | Per-board not per-unit |
| p13 dtbo_b | 2 MB | all zero | empty B slot |
| p14 cri_data | 8 MB | all zero (this report) | Empty — no risk |
| p15 param | 16 MB | ext4 filesystem (`/mnt/vendor/param` in Android) | TV picture-quality DB (`pq.db`, `pq_ext.db`). May contain per-device display calibration — worth backing up |
| p16 odm_ext_a | 16 MB | data deep in partition | ODM extensions; not characterized |
| p17 odm_ext_b | 16 MB | all zero | empty B slot |
| p22 metadata | 64 MB | ext4 filesystem for Android FBE | vold encryption keys, password_slots, bootstat, OTA snapshots — Android-runtime state, not unit identity |
| p23–p26 vbmeta* | 2 MB each | all zero | AVB disabled (avb2=0) |

Candidates that carry unit-unique data and aren't fully captured by the standard CoreELEC install backup:
- **p1 reserved** (64 MB) — **critical** for ETH MAC + serial. The install script now backs this up by default to `/storage/reserved_backup.bin`.
- **p3 frp** (2 MB) — has 36 bytes of device-unique bytes at offset 0. Not in current backup.
- **p15 param** (16 MB) — likely TV calibration data. Not in current backup.

The CoreELEC install does not touch any of p1, p3, or p15. The first is backed up defensively; the others would only be at risk during a manual partition rebuild.

---

## What to add to `emmc-research.md`

Suggested additions / corrections to the existing notes file:

1. **Per-device identity provenance** — the existing notes say `factory` partition stores the Widevine keybox, but on this unit p4 is empty FAT12 and the Widevine state is L3-software anyway. Working ETH MAC and serial actually live as **plaintext slots in the Amlogic UKS keystore on `reserved` (p1)** at offset 0x4000, with a redundant copy at 0x44000. Wi-Fi/BT MACs live in the **BCM4389 chip OTP** and are not stored on the eMMC at all. This means:
   - The CoreELEC install scripts do not need to back up p4 to preserve identity (it is already empty on at least some Ugoos shipping units).
   - **p1 `reserved` IS load-bearing for ETH MAC and serial** — wiping or rewriting it would lose them, and USB Burning Tool restore would NOT bring them back because the factory image does not include p1.
   - USB Burning Tool restore IS safe for identity as long as p1 itself is left untouched, which the burn tool does (the `reserved` partition is not in the image manifest).

2. **eFuses are all zero on this unit.** `/sys/class/efuse/{mac,mac_wifi,mac_bt,usid}` all read as zero bytes. The Amlogic eFuse path is NOT used for per-device identity on the AM9 Pro S905X5-J — the on-eMMC UKS keystore in p1 (with plain SHA-256 integrity, no HMAC) is the storage backend.

3. **`cri_data` (p14) is unused.** Currently listed as "unknown" in the partition table — now confirmed all-zero. Not worth special-casing in install/restore scripts.

4. **`frp` (p3) has unit-specific data** in the first 36 bytes. Listed as "unknown" in the partition table — worth a note that this may be device-bound and should be backed up before destructive ops.

5. **U-Boot `cmdline_keys` flow.** The kernel cmdline `mac=`, `androidboot.serialno=`, `androidboot.wificountrycode=`, etc. are all assembled at boot from `keyman read` calls plus env vars. The U-Boot env partition has the script source for this — useful reference for understanding how identity propagates from secure storage to userspace.

6. **`/dev/env` device node missing under CoreELEC.** `fw_printenv` is shipped but unusable out of the box because `/etc/fw_env.config` points at `/dev/env` which doesn't get created. Workaround: `dd if=/dev/mmcblk0p2 ...` and grep for variables, or `mknod /dev/env b 179 2` (or write a passthrough config to `/dev/mmcblk0p2 0x0 0x10000 0x10000`).
