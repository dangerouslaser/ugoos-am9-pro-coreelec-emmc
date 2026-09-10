# Ugoos S905X5 — CoreELEC on eMMC, firmware tooling, research

Tooling and research for the Ugoos boxes built on the Amlogic S905X5 family:

| Device | SoC | CoreELEC board ID |
|--------|-----|-------------------|
| Ugoos AM9 Pro | S905X5-J (S6) | `s6_s905x5_ugoos_am9_pro` |
| Ugoos SK4 | S905X5M-J (S7D) | `s7d_s905x5m_ugoos_sk4` |
| Ugoos SK4 Pro | S905X5M-J (S7D) | `s7d_s905x5m_ugoos_sk4` (same DTB as the SK4) |

CoreELEC supports all three, but its `ceemmc` install tool does not, and a
CoreELEC-on-eMMC box cannot run the Ugoos OTA. This repo fills those gaps.
Everything here documents what was done to specific units; it is published as
a technical reference, not a recommendation.

## What's here

| Task | Tool | Guide | Tested on |
|------|------|-------|-----------|
| Install CoreELEC to the internal eMMC | `ce-emmc-install.sh` | [INSTALL.md](INSTALL.md) | AM9 Pro, SK4, SK4 Pro |
| Restore the stock Android layout | `ce-emmc-restore.sh` | [INSTALL.md → Restoring Android](INSTALL.md#restoring-android) | AM9 Pro, SK4, SK4 Pro |
| Update the Ugoos firmware in place, from CoreELEC | `ugoos-fw-update.sh` | [FIRMWARE-UPDATE.md](FIRMWARE-UPDATE.md) | AM9 Pro |
| Flash a factory image over USB without Windows | `burn/aml-dnl-burn.py` | [burn/README.md](burn/README.md) | AM9 Pro |
| Inspect factory images, keystore, bootloader, logo | `img-tools/` | [img-tools/README.md](img-tools/README.md) | AM9 Pro |

## Read this first

- **Not supported by CoreELEC or Ugoos.** Support requests, bug reports or
  forum posts about an install done this way will be closed or removed by the
  CoreELEC team. Don't ask there if something goes wrong.
- **The install destroys Android `userdata`** (encrypted with hardware-bound
  keys — a raw backup cannot be decrypted) and `rsv`. `super` and the rest of
  Android stay on the eMMC, so Android can be restored, but it comes back
  factory-reset.
- **It was tested on specific units and specific CoreELEC nightlies.** A
  different hardware revision, a future firmware, or a change in CoreELEC's
  cfgload format or `mount_storage` init path may break it.
- **It can't permanently brick the box.** The installer never writes the
  eMMC hardware boot partitions, and USB burn mode lives in the SoC's mask ROM,
  so the Amlogic USB Burning Tool can always restore stock Android. Per-device
  identity (ETH MAC and serial in `reserved`, WLAN/BT MAC in the Wi-Fi chip's
  OTP) is never touched. A bad install means an SD-card recovery cycle.
- If any of that is a problem, run CoreELEC from an SD card or USB stick.

## Quick start

**Install CoreELEC to eMMC** — from CoreELEC booted off an SD card / USB stick:

```bash
scp ce-emmc-install.sh root@<box>:/storage/
ssh root@<box> 'bash /storage/ce-emmc-install.sh --dry-run'   # preview
ssh root@<box> 'bash /storage/ce-emmc-install.sh'             # install, then reboot without the card
```

**Restore Android** — from CoreELEC on the SD card / USB stick, with the
installer's backup set still on it:

```bash
ssh root@<box> 'bash /storage/ce-emmc-restore.sh'
```

**Update the Ugoos firmware** — on the box, with the factory `.img` already
in `/storage` (Ugoos ships them on mega.nz). Needed when a CoreELEC nightly
starts requiring a newer firmware, as nightly 20260910 did (AM9 Pro → 2.2.0):

```bash
curl -fsSL https://raw.githubusercontent.com/dangerouslaser/ugoos-am9-pro-coreelec-emmc/main/ugoos-fw-update.sh \
    | bash -s -- --check /storage/AM9PRO_2.2.0.img      # what differs, no writes
curl -fsSL https://raw.githubusercontent.com/dangerouslaser/ugoos-am9-pro-coreelec-emmc/main/ugoos-fw-update.sh \
    | bash -s -- /storage/AM9PRO_2.2.0.img              # update + reboot
```

Each guide covers options, what exactly gets written, rollback and recovery.

## How it works, briefly

- **eMMC install.** All three boxes share the Amlogic 29-partition Android
  layout (`super` p27, `rsv` p28, `userdata` p29, keystore in `reserved` p1,
  U-Boot env p2). The installer backs up the small unit-specific partitions,
  replaces `rsv` + `userdata` with `CE_FLASH` + `CE_STORAGE`, copies the boot
  files, and installs a `mount-storage.sh` hook that survives CoreELEC's
  nightly updater rewriting `cfgload`. Design notes:
  [`research/installer-design-notes.md`](research/installer-design-notes.md).
- **Firmware update.** The S905X5 boots its bootloader from the eMMC hardware
  boot partitions (`boot0`/`boot1`), which are not write-protected. The updater
  writes the Android partitions, the Android DTB slots in `reserved`, and the
  boot partitions straight from the running CoreELEC, every write read back
  and hash-checked. Details:
  [`research/firmware-update-in-place.md`](research/firmware-update-in-place.md).
- **USB burner.** A Linux/macOS port of the parts of the Windows USB Burning
  Tool needed to OTA-flash or fully restore from a factory `.img`, built from
  a USB capture of the real tool. Protocol notes:
  [`research/dnl-protocol-from-capture.md`](research/dnl-protocol-from-capture.md).

## Scope of testing

| Component | AM9 Pro | SK4 | SK4 Pro |
|-----------|---------|-----|---------|
| `ce-emmc-install.sh` — install to eMMC | ✅ | ✅ | ✅ |
| `ce-emmc-restore.sh` — restore Android layout | ✅ | ✅ | ✅ |
| Survival across CoreELEC nightly auto-updates | ✅ | ✅ | ✅ |
| `ugoos-fw-update.sh` / `aml-emmc-burn.py` — in-place firmware update (2.1.0 → 2.2.0) | ✅ | ❌ untested | ❌ untested |
| `burn/` — USB DNL burner | ✅ | ❌ untested | ❌ untested |
| `img-tools/` — keystore / bootloader / logo / img parsing | ✅ | partial¹ | partial¹ |
| `research/` — protocol captures, factory image analysis | ✅ | ❌ not repeated | ❌ not repeated |

¹ `aml-keystore-tool.py` runs on every board as part of the installer's
identity check. The logo, bootloader and `AML_PACK_v2` tooling was only
pointed at AM9 Pro artifacts.

AM9 Pro installs were verified on CoreELEC nightlies `22.0-Piers_nightly_20260514`
through `20260527` including the auto-update path; the firmware update on
`20260910`. SK4 and SK4 Pro were verified on the nightlies current at their
installs. Everything in `burn/` and `research/` was built from an AM9 Pro and
`AM9PRO_2.1.0.img`; the DNL protocol is SoC-generic, but nothing there has been
pointed at an SK4 — start with `dry-run`.

## Repository layout

| Path | What's in it |
|------|--------------|
| `ce-emmc-install.sh`, `ce-emmc-restore.sh` | The eMMC installer and its restore counterpart. Board-gated on `SUPPORTED_BOARDS`; restore is board-agnostic (rebuilds from the backup set). |
| `ugoos-fw-update.sh` | In-place firmware updater, curl-able. Fetches the two Python tools it needs, checks board / image / hash, drives `burn/aml-emmc-burn.py`. |
| `INSTALL.md`, `FIRMWARE-UPDATE.md` | The user guides. |
| [`burn/`](burn/README.md) | `aml-dnl-burn.py` (USB DNL from a host), `aml-dnl-status.py` (read-only USB probe), `aml-emmc-burn.py` (in-device flasher: partitions, DTB slots, eMMC boot0/boot1). |
| [`img-tools/`](img-tools/README.md) | File-format CLIs for local files: `aml-img-tool.py`, `aml-bootloader-tool.py`, `aml-keystore-tool.py`, `aml-logo-tool.py`. The installer uses them for identity, `--info` and logo work. |
| [`lib/`](lib/) | Shared Python: `aml_dnl_proto.py` (USB transport + DNL wire protocol), `aml_dnl_ops.py` (partition flash, addsum, DDR load, CBW upload), `aml_dnl_flows.py` (composable plans), `aml_img.py` (`AML_PACK_v2` parser + Android sparse decoder, mmap-backed). |
| [`scripts/`](scripts/README.md) | Small utilities, e.g. `make-cfgload.py` (pure-Python `mkimage -T script`). |
| [`probes/`](probes/README.md) | One-off diagnostic and bring-up scripts used to map the DNL protocol. Reference only. |
| [`research/`](research/README.md) | The write-ups and frozen evidence the tools were built from: eMMC layout and identity, factory image analysis, DNL protocol capture, installer design notes, in-place firmware update. |
| [`aml-analysis/`](aml-analysis/README.md) | Reverse-engineering artifacts for the Windows `Aml_Burn_Tool.exe` and its embedded Lua flows. |
| `am9pro-usb-restore.sh` | Older read-only wrapper around the closed-source `adnl` binary. Predates the native burner; kept for its verification path. |
