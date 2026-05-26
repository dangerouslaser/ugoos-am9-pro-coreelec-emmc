# Factory snapshot — AM9 Pro after USB Burning Tool flashed AM9PRO_2.1.0.img

Captured 2026-05-26 immediately after a clean USB Burning Tool restore from
`AM9PRO_2.1.0.img`, before any subsequent install/modification. Read-only
inspection from SD-booted CoreELEC.

## Device identity (preserved across burn)

- CID:    `ec29004133314d3843309ea834a5bc00`
- Model:  `A31M8C`
- Serial: `AM9PRO26010005693`
- ETH MAC: `90:0e:b3:fd:f8:55`
- Board:  `S6-s905x5-2605181448` (`umx5jyks`)
- eMMC size: 122,142,720 sectors = 58.24 GiB
- Bootloader version: `01.01.260518.144819`
- eMMC health: `life=0x01, pre_eol=0x01` (minimal wear)

## Findings

### `_a` slot is written, `_b` slot is left empty

Every `_b` partition is **all zeros** after the burn (verified by matching
the SHA256 of "N bytes of zeros" for each partition size). The Burning Tool
deliberately writes only the active slot. Lookalikes:

| Partition       | Size | Content   |
|-----------------|------|-----------|
| `bootloader_b`  | 8 MB | all zeros |
| `boot_b`        | 64 MB| all zeros |
| `init_boot_b`   | 8 MB | all zeros |
| `vendor_boot_b` | 64 MB| all zeros |
| `dtbo_b`        | 2 MB | all zeros |

This matches the capture-decoded protocol — the `oem mwrite` commands only
target `_a` slots.

### `vbmeta_*` are also all zeros

The four vbmeta partitions (`vbmeta_a/_b`, `vbmeta_system_a/_b`) are
**2 MB of zeros each**. AVB (Android Verified Boot) is not used by stock
Android on the AM9 Pro — the bootloader does not consult these. This is
consistent with the device shipping with an unlocked bootloader.

### `rsv`, `cri_data` are zero on a fresh flash

`rsv` (64 MB) and `cri_data` (8 MB) are not written by the Burning Tool.
They get populated later by Android during normal operation.

### `_aml_dtb` is at offset 0x400000 inside `reserved` (p1)

The capture-decoded `oem mwrite 0x14ff4 normal store _aml_dtb` command
writes a DTB to **offset 0x400000 (4 MiB) of partition p1 (`reserved`)** —
this is not a separate eMMC partition. DTB magic `d00d feed` confirmed at
that offset, with board id `s6_s905x5_umx5jyks`.

### Keystore at p1 offset 0x4000 is preserved across burn

The AMLNORMAL keystore at offset 0x4000 (16 KiB) inside `reserved` is
byte-identical to our previous backup of the same device. The first 4 MiB
of `reserved` is unchanged across a USB Burning Tool flash. The differences
begin at offset 0x400000 (the DTB region above), which the Burning Tool
writes from scratch.

### eMMC hardware boot partitions (`mmcblk0boot0` / `boot1`) are identical

Both are 4 MiB, both hash to `a1104a81deaeee29afb9a3beee97607b173c62371fe795e1c15cabe3937e4caa`.
These hold the BL2 / boot0 — Amlogic stores the same content in both for
hardware redundancy. SHA256 different from any zero-hash; this is real
bootloader content.

## Files in this snapshot

- `partition-map.txt` — every GPT partition with name, number, start, size
- `parted.txt`        — full `parted print all` output (includes SD too)
- `sha256.txt`        — SHA256 of every numbered partition + both HW boot
- `gpt-header.bin`, `gpt-entries.bin`, `gpt-backup-header.bin`, `gpt-backup-entries.bin`
- `dumps/p*.bin`      — full dumps of all small partitions (<=8 MB) plus
                        head of `super` (4 MB) and `userdata` (1 MB)
- `dumps/p1-reserved.bin` — full 64 MB of `reserved` (keystore + DTB)
- `dumps/p7-bootloader_a.bin` — factory-shipped bootloader from 2.1.0
- `dumps/p2-env.bin`  — factory U-Boot environment
- `dumps/mmcblk0boot0.bin`, `mmcblk0boot1.bin` — BL2 hardware boot partitions

## Cross-references

- Burn protocol vocabulary that produced this state: `dnl-protocol-from-capture.md`
- Install script that operates against this layout: `ce-emmc-install.sh`
- Keystore parser: `aml-keystore-tool.py`
