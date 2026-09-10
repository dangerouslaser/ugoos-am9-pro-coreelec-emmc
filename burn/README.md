# burn/ — active flashers

> **Scope: AM9 Pro only.** The CoreELEC eMMC installer at the repo root is tested
> on AM9 Pro, SK4, and SK4 Pro, but everything in this directory was built and
> exercised against AM9 Pro hardware and `AM9PRO_*.img` factory images. The
> ADNL/DNL protocol is SoC-generic and the S905X5M in the SK4 / SK4 Pro should
> speak it identically, but that is inference, not a test result. If you point
> these at an SK4, start with `aml-dnl-status.py` and `dry-run`, and treat a
> `full-restore` as unverified.

| Tool | Where it runs | What it does |
|------|--------------|--------------|
| [`aml-dnl-burn.py`](aml-dnl-burn.py) | Host (Linux/macOS, device in DNL/burn mode over USB-C OTG) | The native Amlogic burner — replaces the Windows-only USB Burning Tool. Subcommands: `dry-run`, `ota-keep-ce` (update Android slot _a, preserve CE_FLASH/CE_STORAGE), `full-restore`. Destructive ops gated by `--yes-i-mean-it`. |
| [`aml-dnl-status.py`](aml-dnl-status.py) | Host (device in DNL mode) | Read-only DNL probe: identifies device, dumps chipinfo pages, prints stage/mode. Safe to run any time the device is in burn mode. |
| [`aml-emmc-burn.py`](aml-emmc-burn.py) | On the device under CoreELEC | In-device firmware flasher. Parses an AML `.img` and writes the Android-side partitions to `/dev/mmcblk0pN`, the Android DTB into both `reserved` slots (U-Boot's checksummed `aml_dtb_rsv` format) and the bootloader into the eMMC hardware boot partitions boot0/boot1 — which is where the S905X5 actually boots from. CE_FLASH/CE_STORAGE and the GPT are never touched. Modes: `--list`, `--verify-only`, `--dry-run`, `--ota` (everything + reboot); `--no-dtb`, `--no-boot-area`, `--skip-boot1` to hold parts back. Driven by the root-level [`ugoos-fw-update.sh`](../ugoos-fw-update.sh); see [`FIRMWARE-UPDATE.md`](../FIRMWARE-UPDATE.md). |

All three import the shared libraries (`aml_dnl_proto`, `aml_dnl_ops`, `aml_dnl_flows`, `aml_img`) from `../lib/` via a `sys.path` adjustment.

Factory images are per-model and not interchangeable — flash only the `.img`
that matches the box in front of you.

## Quick reference

### Host: USB-DNL burn

```bash
# Device in DNL mode (hold reset, plug USB-C OTG → host)
python3 burn/aml-dnl-status.py                                # confirm visible
python3 burn/aml-dnl-burn.py dry-run path/to/AM9PRO_X.Y.Z.img # preview plan
python3 burn/aml-dnl-burn.py ota-keep-ce  path/to/.img --yes-i-mean-it
python3 burn/aml-dnl-burn.py full-restore path/to/.img --yes-i-mean-it
```

### On-device: firmware update from CoreELEC

The easy way is the wrapper, which fetches this tool for you:

```bash
curl -fsSL https://raw.githubusercontent.com/dangerouslaser/ugoos-am9-pro-coreelec-emmc/main/ugoos-fw-update.sh \
    | bash -s -- --check /storage/AM9PRO_X.Y.Z.img     # what differs
curl -fsSL https://raw.githubusercontent.com/dangerouslaser/ugoos-am9-pro-coreelec-emmc/main/ugoos-fw-update.sh \
    | bash -s -- /storage/AM9PRO_X.Y.Z.img             # update + reboot
```

Driving the tool directly (copy `aml-emmc-burn.py` and `../lib/aml_img.py` into
the same directory on the box — it looks for the library in `../lib/` and next
to itself):

```bash
python3 /storage/aml-emmc-burn.py /storage/AM9PRO_X.Y.Z.img --list
python3 /storage/aml-emmc-burn.py /storage/AM9PRO_X.Y.Z.img --verify-only
python3 /storage/aml-emmc-burn.py /storage/AM9PRO_X.Y.Z.img --ota   # partitions + DTB + boot0 + boot1, then reboot
```

Writes go partitions → DTB slots → boot0 → boot1, each read back and hash-checked,
so an interrupted run leaves the previous bootloader in place. Verified on AM9 Pro,
2.1.0 → 2.2.0 (2026-09-10).
