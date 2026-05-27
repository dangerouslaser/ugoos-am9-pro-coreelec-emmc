# burn/ — active flashers

| Tool | Where it runs | What it does |
|------|--------------|--------------|
| [`aml-dnl-burn.py`](aml-dnl-burn.py) | Host (Linux/macOS, device in DNL/burn mode over USB-C OTG) | The native Amlogic burner — replaces the Windows-only USB Burning Tool for the AM9 Pro. Subcommands: `dry-run`, `ota-keep-ce` (update Android slot _a, preserve CE_FLASH/CE_STORAGE), `full-restore`. Destructive ops gated by `--yes-i-mean-it`. |
| [`aml-dnl-status.py`](aml-dnl-status.py) | Host (device in DNL mode) | Read-only DNL probe: identifies device, dumps chipinfo pages, prints stage/mode. Safe to run any time the device is in burn mode. |
| [`aml-emmc-burn.py`](aml-emmc-burn.py) | On the AM9 Pro under CoreELEC | In-device eMMC flasher. Parses an AML `.img` and writes Android-side partitions directly to `/dev/mmcblk0pN`, bypassing USB. CE_FLASH/CE_STORAGE and the GPT are never touched. Modes: `--list`, `--verify-only`, `--dry-run`, `--ota` (full Android-side flash + reboot in one shot). Use case: "install a Ugoos OTA from CoreELEC without leaving CE." |

All three import the shared libraries (`aml_dnl_proto`, `aml_dnl_ops`, `aml_dnl_flows`, `aml_img`) from the project root via a `sys.path` adjustment.

## Quick reference

### Host: USB-DNL burn

```bash
# Device in DNL mode (hold reset, plug USB-C OTG → host)
python3 burn/aml-dnl-status.py                                # confirm visible
python3 burn/aml-dnl-burn.py dry-run path/to/AM9PRO_X.Y.Z.img # preview plan
python3 burn/aml-dnl-burn.py ota-keep-ce  path/to/.img --yes-i-mean-it
python3 burn/aml-dnl-burn.py full-restore path/to/.img --yes-i-mean-it
```

### On-device: eMMC direct flash (from CoreELEC)

```bash
# Copy script + libraries + .img to /storage, then on the device:
python3 /storage/aml-emmc-burn.py /storage/AM9PRO_X.Y.Z.img --list
python3 /storage/aml-emmc-burn.py /storage/AM9PRO_X.Y.Z.img --verify-only
python3 /storage/aml-emmc-burn.py /storage/AM9PRO_X.Y.Z.img --ota   # flash + reboot
```
