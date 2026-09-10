# research/ — technical research notes & snapshots

The investigative writeups and frozen evidence files that the tooling in
the rest of the repo was built from. Kept alongside the code so the
"why" behind each design decision stays discoverable.

> **Scope: this is AM9 Pro evidence.** Every measurement, dump, hex offset,
> partition map, and USB capture in this directory was taken from one physical
> Ugoos AM9 Pro (`s6_s905x5_ugoos_am9_pro`). The eMMC installer built on top of
> it is separately tested on the SK4 and SK4 Pro (`s7d_s905x5m_ugoos_sk4`), and
> the structural conclusions that installer depends on — the 29-partition layout
> with `super`/`rsv`/`userdata` at p27/p28/p29, the AMLNORMAL keystore in
> `reserved` (p1), the U-Boot env at p2 — hold on those boards too, because the
> installer verifies them at runtime and refuses to proceed otherwise. Nothing
> else here was re-derived on S905X5M silicon. Treat byte offsets, chip IDs,
> partition sizes, and factory-image contents as AM9 Pro figures unless a
> document says otherwise.

| File | What's in it |
|------|--------------|
| [`emmc-research.md`](emmc-research.md) | Main research notes — eMMC hardware, partition layout, encryption, identity provenance, U-Boot env, the manual install method, the CE auto-update interaction with cfgload, USB burn-mode protocol findings. The reference document for the project. |
| [`firmware-update-in-place.md`](firmware-update-in-place.md) | How the eMMC hardware boot partitions (boot0/boot1: info sector + bootloader blob, not write-protected) and the `reserved` DTB slots (`aml_dtb_rsv`, checksummed) work, established while taking an AM9 Pro from firmware 2.1.0 to 2.2.0 in place from CoreELEC. Test log included. |
| [`factory-investigation.md`](factory-investigation.md) | Standalone investigation into where MAC addresses, serial, and other per-device identity actually live on the AM9 Pro. Conclusion: ETH MAC + serial are in plaintext on `reserved` (p1)'s AMLNORMAL keystore; WLAN/BT MACs are in the BCM4389 chip's own OTP. Drives the installer's identity cross-check and the "do not wipe p1" warnings. |
| [`dnl-protocol-from-capture.md`](dnl-protocol-from-capture.md) | Wire-protocol decoding for Amlogic USB DNL (the format `aml-dnl-burn.py` speaks). Reconstructed from a USBPcap capture of the official Windows USB Burning Tool flashing AM9PRO_2.1.0. The polished writeup; the raw transcript is [`burn-protocol-decoded.txt`](burn-protocol-decoded.txt). |
| [`factory-2.1.0-snapshot.md`](factory-2.1.0-snapshot.md) | Snapshot of the device immediately after a clean USB Burning Tool restore from `AM9PRO_2.1.0.img`. Notes which partitions are zero-filled (`_b` slots, vbmeta_*), which the burn tool touches, and where `_aml_dtb` actually lives inside `reserved`. |
| [`factory-2.1.0-partition-map.txt`](factory-2.1.0-partition-map.txt) | Partition map captured during the snapshot above (sizes, offsets, names). |
| [`factory-2.1.0-sha256.txt`](factory-2.1.0-sha256.txt) | SHA-256 of every numbered partition + both eMMC HW boot partitions at the snapshot time — useful as a reference set when validating restores. |
| [`burn-protocol-decoded.txt`](burn-protocol-decoded.txt) | Raw decoded transcript from `analyze-burn-capture.py` run against the USB Burning Tool pcap — the unprocessed evidence behind `dnl-protocol-from-capture.md`. |
