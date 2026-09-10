# Updating the Ugoos firmware in place, from CoreELEC

This guide covers `ugoos-fw-update.sh`: it takes a Ugoos factory image
(`AM9PRO_X.Y.Z.img`) and writes everything the Amlogic USB Burning Tool would
write to the eMMC, from inside the running CoreELEC, in about 30 seconds plus
a reboot. No USB-C cable, no Windows, no leaving CoreELEC, and CoreELEC's own
partitions (`CE_FLASH`, `CE_STORAGE`) and the GPT are never touched.

> **Unsupported by CoreELEC and by Ugoos.** Same rules as the rest of this
> repo: don't take problems from this to the CoreELEC forum. Read
> [Risk and recovery](#risk-and-recovery) before running it.

## When you need this

CoreELEC nightlies can start depending on a newer Ugoos firmware. The concrete
case that produced this tool: on 2026-09-09 vpeter announced that from
nightly **20260910** the AM9 Pro's Android firmware **must** be **2.2.0**. The
new CoreELEC DTB switched a set of interrupts from edge- to level-triggered
and added `amlogic,level-high-to-gic` on the GPIO interrupt controller,
matching the 2.2.0 Android DTB — and that only works when the running
bootloader/BL31 is the 2.2.0 one. The box still boots on the old firmware,
so the failure is subtle rather than a dead device.

If CoreELEC is installed on the eMMC with `ce-emmc-install.sh`, you can't
run the Ugoos OTA (Android never boots), and a USB Burning Tool restore wipes
CoreELEC. This tool is the third option.

Check what you're running:

```bash
tr ' ' '\n' </proc/cmdline | grep androidboot.bootloader
# androidboot.bootloader=01.01.260518.144819   ← firmware 2.1.0 (build date 2026-05-18)
# androidboot.bootloader=01.01.260903.151400   ← firmware 2.2.0 (build date 2026-09-03)
```

The stamp is the build timestamp of the bootloader that actually booted the
box. (`fw_printenv bootloader_version` shows a stale saved value — don't use
it.)

## What gets written

| Item in the `.img`                                   | Written to                                        | Verified by                                   |
|------------------------------------------------------|---------------------------------------------------|-----------------------------------------------|
| `boot_a`, `init_boot_a`, `vendor_boot_a`, `dtbo_a`, `logo`, `odm_ext_a`, `bootloader_a` | the matching GPT partitions `/dev/mmcblk0pN` | SHA1 of the partition after write vs the image's own `VERIFY` items |
| `super` (Android sparse image)                       | `/dev/mmcblk0p27`                                 | SHA1 of the sparse blob before write (Amlogic's own convention) |
| `_aml_dtb` (Android DTB)                             | both 256 KB slots at `reserved` + 4 MiB / + 4.25 MiB, in U-Boot's checksummed `aml_dtb_rsv` format | byte-exact read-back, FDT SHA1 vs `VERIFY` |
| `bootloader`                                         | eMMC hardware boot partitions `/dev/mmcblk0boot0` **and** `boot1`, behind the board's existing 512-byte info sector | byte-exact read-back, blob SHA1 vs `VERIFY` |

Writes happen in that order: partitions, DTB, boot0, boot1. The boot area is
last on purpose — a run that dies before it leaves the old, working
bootloader in place.

Not written: the GPT, `CE_FLASH`, `CE_STORAGE`, the U-Boot environment,
`reserved` outside the two DTB slots (the keystore with your MAC/serial stays),
and any of the USB-only items in the image (DDR/U-Boot loader stages).

**Why boot0/boot1 matter.** On the S905X5 the BootROM loads the bootloader
from the eMMC's hardware boot partitions, not from `bootloader_a`. Flashing
`bootloader_a` alone (what the first version of this tool did) changes
nothing at boot. The earlier notes in this repo that called boot0/boot1
"hardware write-protected" were wrong — the eMMC's `BOOT_WP` register is
clear on the AM9 Pro; the kernel merely exposes them read-only by default.
Details in [`research/firmware-update-in-place.md`](research/firmware-update-in-place.md).

## Prerequisites

- A Ugoos AM9 Pro running CoreELEC (from eMMC or from SD/USB) with SSH.
  SK4 / SK4 Pro share the layout but have **not** been tested — the script
  refuses unless you pass `--allow-untested-board`.
- The factory `.img` for **your** box. Ugoos publishes them on mega.nz
  (links in their support channels); the archive is ~1.5 GB, the `.img`
  inside ~1.7 GB. `curl` can't fetch from mega.nz, so download and extract
  on a computer (`7z x AM9PRO_2.2.0.7z`), then get the `.img` onto the box:

  ```bash
  scp AM9PRO_2.2.0.img root@<box>:/storage/
  # or serve it over the LAN and let the script download it:
  #   python3 -m http.server 8000      (in the folder with the .img)
  #   … then pass http://<your-pc>:8000/AM9PRO_2.2.0.img to the script
  ```

- ~2 GB free on `/storage` for the image. `python3` and `parted` ship with
  CoreELEC.

## Running it

Everything below runs on the box as root. The script fetches the two Python
tools it needs (`burn/aml-emmc-burn.py`, `lib/aml_img.py`) from this repo
into `/storage/.ugoos-fw-update/`.

1. **Look before you leap** — what differs, no writes:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/dangerouslaser/ugoos-am9-pro-coreelec-emmc/main/ugoos-fw-update.sh \
       | bash -s -- --check /storage/AM9PRO_2.2.0.img
   ```

   Exit code 0 means the eMMC already matches the image everywhere; 1 means
   an update is needed and the output lists what differs.

2. **Update** (from an interactive SSH session; it asks for a `YES`):

   ```bash
   curl -fsSL https://raw.githubusercontent.com/dangerouslaser/ugoos-am9-pro-coreelec-emmc/main/ugoos-fw-update.sh \
       | bash -s -- /storage/AM9PRO_2.2.0.img
   ```

   Non-interactively from your computer (no terminal → `--yes` is required):

   ```bash
   ssh root@<box> 'curl -fsSL https://raw.githubusercontent.com/dangerouslaser/ugoos-am9-pro-coreelec-emmc/main/ugoos-fw-update.sh | bash -s -- --yes /storage/AM9PRO_2.2.0.img'
   ```

   The flash itself runs under `nohup`, so a dropped SSH session does not
   interrupt a write. Progress is appended to `/storage/ugoos-fw-update.log`.
   The box reboots when done.

3. **Confirm after the reboot:**

   ```bash
   tr ' ' '\n' </proc/cmdline | grep androidboot.bootloader     # new stamp?
   curl -fsSL …/ugoos-fw-update.sh | bash -s -- --check /storage/AM9PRO_2.2.0.img   # exit 0
   ```

Keep the previous version's `.img` around: it is your rollback, applied with
the exact same command.

### Options

| Flag | Effect |
|------|--------|
| `--check` | Report differences only. Use before, and again after the reboot. |
| `--dry-run` | Print the full write plan and stop. |
| `--yes` | Skip the confirmation (needed when there is no terminal). |
| `--no-reboot` | Flash, don't reboot. |
| `--skip-boot1` | Two-phase update: write boot0 only, reboot, confirm the new stamp, then re-run without the flag to update boot1. If boot0 turns out bad the BootROM falls back to boot1. |
| `--no-dtb`, `--no-boot-area` | Leave the DTB slots / the boot area alone. With `--no-boot-area` the running bootloader does not change — only useful for experiments. |
| `--sha1 HEX` | Insist on this image hash. |
| `--allow-unknown-image` | Accept an image whose hash isn't in the script's list. Add the hash to `KNOWN_IMAGES` in the script if you've verified it. |
| `--allow-untested-board` | Run on SK4 / SK4 Pro. |
| `--force` | Rewrite everything even if the eMMC already matches. |
| `--ref REF` | Fetch the tools from another branch/tag of this repo. |

Known images (SHA1 of the `.img`):

| Image | SHA1 | Bootloader stamp |
|-------|------|------------------|
| `AM9PRO_2.2.0.img` | `a0b9adc543788205de03b1a04cfba88be1dc2300` | `01.01.260903.151400` |
| `AM9PRO_2.1.0.img` | `5c0920b3f9081e084e3370525d056411a7284847` | `01.01.260518.144819` |
| `AM9PRO_2.0.9.img` | `8b5734fe70bd7168914ae1e7880ae6ab25a026b9` | — |

### Running from a checkout instead of curl

If you `git clone` the repo onto the box (or `scp` the three files keeping
the `burn/` and `lib/` layout), `./ugoos-fw-update.sh` uses the tools next to
it and does not fetch anything. That is also how to test changes to the
Python tool before they land on `main`.

The underlying tool can be driven directly, too:

```bash
python3 /storage/.ugoos-fw-update/aml-emmc-burn.py /storage/AM9PRO_2.2.0.img --list
python3 /storage/.ugoos-fw-update/aml-emmc-burn.py /storage/AM9PRO_2.2.0.img --verify-only
python3 /storage/.ugoos-fw-update/aml-emmc-burn.py /storage/AM9PRO_2.2.0.img --ota     # flash + reboot
```

## Risk and recovery

- **Every write is read back and hash-checked**; a mismatch aborts before the
  next step. The bootloader blob and the DTB are checked against the image's
  own `VERIFY` hashes before they are written.
- **Bad boot0** → the BootROM boots boot1. If you used `--skip-boot1`, that is
  the previous firmware and the box comes up as before. If both copies were
  written from the same verified blob, "bad" would have to mean the image
  itself is broken for this board — which the SoC-family check against the
  `@AMLBOOT` manifest is there to prevent.
- **Wrong firmware written** → flash the previous `.img` the same way (from
  CoreELEC on eMMC, or from an SD card if the eMMC install no longer boots).
- **Everything on the eMMC unbootable** → the BootROM's USB burn mode is in
  mask ROM and does not depend on anything on the eMMC. Hold reset, plug in
  USB-C, and use the Amlogic USB Burning Tool with the same `.img` (or
  `burn/aml-dnl-burn.py` from Linux/macOS). That wipes CoreELEC from the
  eMMC; reinstall with `ce-emmc-install.sh`.
- **Identity** (MAC, serial) lives in the keystore region of `reserved` and
  in the Wi-Fi chip; neither is touched.
- **eMMC wear**: one update writes ~1.7 GB. Not a concern.

## Tested

| Board | Path | Firmware | CoreELEC | Date |
|-------|------|----------|----------|------|
| AM9 Pro | CoreELEC on eMMC (`ce-emmc-install.sh`) | 2.1.0 → 2.2.0, then 2.2.0 → 2.2.0 (`--force`) | 22.0-Piers nightly 20260910 | 2026-09-10 |

Full write-up of how the boot area and DTB formats were established, and
the test log, in [`research/firmware-update-in-place.md`](research/firmware-update-in-place.md).
