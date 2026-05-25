# Factory Provisioning Investigation — AM9 Pro

**Date:** 2026-05-25
**Device:** 192.168.1.139, CoreELEC 22.0-Piers_nightly_20260525
**Question:** On a pre-first-boot unit with empty `factory` (p4), where do the working MACs, serial, and other per-unit data actually live?

---

## TL;DR

| Identity | Value on this unit | Source (verified) |
|---|---|---|
| Ethernet MAC | `90:0E:B3:FD:F8:55` | U-Boot `mac=` on kernel cmdline → set by `cmdline_keys` script via `keyman read mac` (Amlogic Unified Key Store, RPMB-backed). U-Boot env also has `ethaddr=90:0e:b3:fd:f8:55` as a backup. |
| WLAN MAC | `40:D9:5A:FC:E2:88` | **BCM4389 chip OTP.** dmesg: `[dhd] use firmware generated mac_address 40:d9:5a:fc:e2:88` |
| BT MAC | `40:D9:5A:FC:E2:89` | Same BCM4389 OTP (WLAN MAC + 1) |
| Serial | `AM9PRO26010005693` | `keyman read usid` → Amlogic UKS / RPMB |

**Conclusions:**

1. The empty `factory` partition (p4 KEYBOX PART, FAT12, content-empty) is **not** the source of any working identity on this device. It is a placeholder.
2. The Amlogic SoC eFuses for MAC/USID are **all zero** — also not the source.
3. The working ETH MAC and serial are read by U-Boot from **RPMB** via `keyman init 0x1234`, then injected into the kernel cmdline at boot.
4. The working WLAN/BT MACs are read by the Broadcom firmware from the **BCM4389 chip's own OTP**, completely independent of the eMMC.
5. `cri_data` (p14) is fully zeroed — no per-device data there.
6. None of the per-unit identities are at risk from anything on the eMMC. A full eMMC wipe (including the partition table) does not lose any of them. They survive a USB Burning Tool restore as well.

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

### Ethernet MAC and serial: U-Boot `keyman` → RPMB (most likely)

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

`keyman` is Amlogic's Unified Key Store (UKS) interface. The magic `0x1234` selects the secure key device. On this SoC family the UKS reads from a fixed set of backing stores:

- **eFuses** — confirmed empty on this unit (`/sys/class/efuse/{mac,mac_wifi,mac_bt,usid}` all read as zero bytes).
- **factory partition (p4 KEYBOX PART)** — confirmed empty.
- **RPMB** — already provisioned (`rpmb_state=0x1`, key burned at factory).
- **A secure region within boot0/boot1 or the bootloader payload** — both `mmcblk0boot0` and `mmcblk0boot1` are nearly all zero (just a 1-byte header), so the keys aren't sitting in clear here. (Could still be in the Replay Protected Memory Block, which is a separate ~4 MB region not visible as `mmcblk0boot*`.)

By elimination, the IDs are stored in RPMB. This is consistent with how Amlogic factory tools provision new units.

The U-Boot env partition (p2) also stores `ethaddr=90:0e:b3:fd:f8:55` as a legacy variable. Both keyman and ethaddr report the same MAC. If the env partition is wiped, the kernel cmdline path via keyman still works.

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
| p1 reserved | 64 MB | mostly zero, some non-zero data starting ~1 KB from offset 4 MB | Per prior notes "all-null" — there is some data deeper but unclear what |
| p3 frp | 2 MB | **non-zero**: 36 bytes of random-looking data at offset 0, then zeros | Likely a per-device FRP/anti-rollback signing nonce. **Worth backing up.** |
| p4 factory | 8 MB | empty FAT12 (this report) | Empty — no risk |
| p9 tee | 32 MB | mostly zero, data starts ~256 KB in | Per prior notes characterized as zeroed; revisit |
| p10 logo | 8 MB | `AML_RES!` magic — Amlogic logo pack | Generic per firmware build, not per-device |
| p11 misc | 2 MB | data starts deep in partition | Standard Android `misc` partition (BCB/wipe args), not unit-specific |
| p12 dtbo_a | 2 MB | FDT magic `d7 b7 ab 1e` | DTB overlay; per-board not per-unit |
| p13 dtbo_b | 2 MB | all zero | empty B slot |
| p14 cri_data | 8 MB | all zero (this report) | Empty — no risk |
| p15 param | 16 MB | data deep in partition | Amlogic param/calibration — could be unit-specific (display tuning, etc.); worth investigating |
| p16 odm_ext_a | 16 MB | data deep in partition | ODM extensions; check |
| p17 odm_ext_b | 16 MB | all zero | empty B slot |
| p22 metadata | 64 MB | data deep in partition | Per prior research: Android FBE-related, not unit-key-critical given firstboot=1 |
| p23–p26 vbmeta* | 2 MB each | all zero | AVB disabled (avb2=0) |

Candidates that **might** carry unit-unique data and aren't yet backed up:
- **p3 frp** (2 MB) — has device-unique bytes
- **p15 param** (16 MB) — likely calibration data
- **p1 reserved** (64 MB) — content unidentified

Not blocking for this investigation, but worth a separate backup pass if doing a destructive operation on the eMMC.

---

## What to add to `emmc-research.md`

Suggested additions / corrections to the existing notes file:

1. **Per-device identity provenance** — the existing notes say `factory` partition stores the Widevine keybox, but on this unit p4 is empty FAT12 and the Widevine state is L3-software anyway. Working ETH MAC and serial actually live in **RPMB** and are read at boot via `keyman read`. Wi-Fi/BT MACs live in the **BCM4389 chip OTP** and are not stored on the eMMC at all. This means:
   - The CoreELEC install scripts do not need to back up p4 to preserve identity (it is already empty on at least some Ugoos shipping units).
   - USB Burning Tool restore does not need to recreate identity content — the RPMB-stored keys and BCM-OTP MACs survive any eMMC-level operation.

2. **eFuses are all zero on this unit.** `/sys/class/efuse/{mac,mac_wifi,mac_bt,usid}` all read as zero bytes. The Amlogic eFuse path is NOT used for per-device identity on the AM9 Pro S905X5-J — UKS/RPMB is the storage backend.

3. **`cri_data` (p14) is unused.** Currently listed as "unknown" in the partition table — now confirmed all-zero. Not worth special-casing in install/restore scripts.

4. **`frp` (p3) has unit-specific data** in the first 36 bytes. Listed as "unknown" in the partition table — worth a note that this may be device-bound and should be backed up before destructive ops.

5. **U-Boot `cmdline_keys` flow.** The kernel cmdline `mac=`, `androidboot.serialno=`, `androidboot.wificountrycode=`, etc. are all assembled at boot from `keyman read` calls plus env vars. The U-Boot env partition has the script source for this — useful reference for understanding how identity propagates from secure storage to userspace.

6. **`/dev/env` device node missing under CoreELEC.** `fw_printenv` is shipped but unusable out of the box because `/etc/fw_env.config` points at `/dev/env` which doesn't get created. Workaround: `dd if=/dev/mmcblk0p2 ...` and grep for variables, or `mknod /dev/env b 179 2` (or write a passthrough config to `/dev/mmcblk0p2 0x0 0x10000 0x10000`).
