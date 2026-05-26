# Amlogic USB DNL protocol — decoded from a live S6 burn capture

Source: USBPcap capture of the official **USB Burning Tool v3.3.3** flashing
`AM9PRO_2.1.0.img` to the AM9 Pro (S905X5-J / S6 SoC family).

Captured on 2026-05-26 via VFIO passthrough of the host's Intel Raptor Lake
XHCI controller into a Windows VM. 186,543 USB frames / 1.13 GB / 215 s. The
raw `.pcapng` is gitignored (`captures/burn-2.1.0-windows-full.pcapng`); the
analyzer is `analyze-burn-capture.py`; the full transcript is at
`docs/burn-protocol-decoded.txt`.

This supersedes the speculation in `emmc-research.md` about the DNL protocol
being a custom binary format. **The protocol is text-mode fastboot with
Amlogic-specific `oem` extensions**, run over USB bulk endpoints.

## Wire format

Each request is a single bulk-OUT write of an ASCII command (no terminator).
Each response is a single bulk-IN read whose first four bytes are a fastboot
status prefix:

| Prefix | Meaning                                              |
|--------|------------------------------------------------------|
| `OKAY` | success; optional payload after the 4-byte prefix    |
| `DATA` | device ready to receive N bytes; payload is `HHHHHHHH` (8 hex chars, big-endian size) |
| `FAIL` | (not observed in this capture; standard fastboot)    |
| `INFO` | (not observed in this capture; standard fastboot)    |

Some `OKAY` payloads observed:

- `OKAY01.01.260518.144819` — burn-tool/U-Boot version string
- `OKAY0008 / 0040 Chunks` — sparse-image chunk progress
- bare `OKAY` — ack only

## Command vocabulary

### Standard fastboot

```
getvar:<name>
download:<HHHHHHHH>     # download N bytes; data follows on bulk OUT
boot                    # execute loaded image
reboot-romusb           # reset back to BootROM stage (S6 also uses this for stage transitions)
```

### Amlogic-specific (`getvar:`)

| Variable | Purpose |
|----------|---------|
| `identify`        | stage discriminator — issued at the start of each enumeration cycle |
| `serialno`        | device serial number |
| `getchipinfo-0` … `-5` | chip info pages 0–5 (we saw 6 pages on S6; previously we tried 0–4 in pyamlboot, never page 5) |
| `downloadsize`    | maximum bytes per `download:` block |
| `cbw`             | unknown — possibly USB Mass Storage Class "Command Block Wrapper" emulation hook |

### Amlogic-specific OEM commands

```
oem disk_initial 1                          # initialize eMMC layout (creates partitions)
oem get_bootloaderversion                    # raw version string
oem env_get bootloader_version              # read environment variable
oem save_setting                             # persist bootloader env to eMMC
oem setvar burnsteps 0xc0041030..32         # progress checkpoint (host writes to env var)
oem sheader_need                             # query if sparse header is required
oem verify sha1sum <40-hex-chars>            # verify previously-written data against SHA1
oem mwrite <SIZE_HEX> (normal|sparse) (store|mem) <partition>
```

`oem mwrite` is the heart of a flash. Syntax:

- `<SIZE_HEX>` — payload size in hex (no `0x` prefix is NOT used in the protocol — observed forms always carry the `0x` prefix, e.g. `0x5e327200`)
- `normal` — raw bytes
- `sparse` — Android sparse image format (`super` is the only one here)
- `store` — write to the named eMMC partition
- `mem`   — load into DRAM only (not flashed), used for `gpt`, `dtb`, `sheader`

After issuing `oem mwrite ...`, the device replies `DATA<size>`, then the
host streams the data over bulk OUT (chunked into `download:HHHHHHHH` blocks
of typically 0x4000 / 16 KB, larger for `getvar:downloadsize` ceiling),
then the device replies `OKAY`.

### `mwrite:verify=addsum`

Used 1,267 times in the capture. Appears to be the per-block transfer
command during a single `oem mwrite` partition write — the `addsum`
verification is a running checksum the device validates incrementally.

### Other

```
firstsect           # S6-only — answers what was the "1sect not send" undocumented preamble we noted in earlier research. Sent right after the chipinfo queries on stage 2 (DDR firmware load).
setvar:checksum     # uploaded after a batch of 7 download blocks; finalize per-block checksum
```

## Partition flow observed during a full restore

Listed in burn order with the `oem mwrite` size argument:

| Step | Partition       | Size (hex)      | Mode             |
|------|-----------------|-----------------|------------------|
| 1    | `_aml_dtb` (dtb in DRAM) | `0x14ff4`         | normal mem |
| 2    | `gpt` (DRAM)             | `0x8600`          | normal mem |
| 3    | `gpt` (eMMC)             | `0x8600`          | normal store |
| 4    | `sheader` (DRAM)         | `0x3e8400`        | normal mem |
| 5    | `bootloader`             | `0x3e8400`        | normal store |
| 6    | `bootloader_a`           | `0x3e8400`        | normal store |
| 7    | `dtb` (eMMC `_aml_dtb`)  | `0x14ff4`         | normal store |
| 8    | `dtbo_a`                 | `0x1b6`           | normal store |
| 9    | `logo`                   | `0x1a7710`        | normal store |
| 10   | `boot_a`                 | `0x3300000`       | normal store |
| 11   | `init_boot_a`            | `0x29f000`        | normal store |
| 12   | `vendor_boot_a`          | `0x32db800`       | normal store |
| 13   | `super`                  | `0x5e327200`      | **sparse** store |
| 14   | `odm_ext_a`              | `0x1000000`       | normal store |

Each step is bracketed by `oem setvar burnsteps 0xc004103X` and ended with
`oem verify sha1sum <hash>`.

## Stage transitions

The device re-enumerates several times during a burn. Each re-enumeration
gives it a new USB bus address (we observed 14 → 15 → 16 → 17 → 18 in this
capture). Sequence:

1. **BootROM (stage 14)** — initial enumeration after entering burn mode.
   Host sends `getvar:identify` and `reboot-romusb`. Device drops, re-enumerates.
2. **DDR firmware load (stage 15)** — chipinfo queries, `firstsect`,
   `download:00042800` (~272 KB DDR firmware), `boot`. Device drops, re-enumerates.
3. **U-Boot load (stage 16)** — chipinfo queries again, then ~600 KB of
   16 KB chunks (`download:00004000`) constituting U-Boot itself, then
   `setvar:checksum`. Device may drop, re-enumerate.
4. **U-Boot active (stage 17/18)** — `oem disk_initial 1`, then the
   `oem mwrite` partition-by-partition flash.

## Implementation notes for a Linux burner

- Endpoint discovery: the device has one bulk-OUT and one bulk-IN endpoint
  (interface 0, class 0xff vendor-specific). Same on all stages.
- Synchronization: every host command should be followed by a bulk-IN read
  until `OKAY` is seen — sometimes `DATA<size>` comes first.
- `download:HHHHHHHH` size is the *total* bytes the host will write before
  the next `OKAY` reply. The actual stream is broken into max-packet-sized
  USB bulk transfers but the device counts the cumulative size.
- The `firstsect` quirk: on S6, this is required **before** the first
  `download:` of the DDR firmware stage. Without it, the first sector of
  the firmware is silently dropped (this matches the "1sect not send" hint
  we saw in earlier pyamlboot probe traces).
- `chipinfo` page 5 exists on S6 and was missing from our pyamlboot probes.
- Sparse `super` write uses Android sparse format — already supported by
  `simg2img` / standard tooling.
- `oem verify sha1sum` is optional from the device's perspective but the
  Windows tool always issues it. If we skip it, a flash may still succeed
  but we lose end-to-end integrity verification.

## Open issue — `boot` after stage-15 `download:0x42800` fails to transition

Status as of 2026-05-26 session-2: **bisected to USB transport, not protocol.**

Reproduced on both macOS (libusb-1.0.29) and Linux (libusb-1.0.27). The
device acks `boot` with `OKAY` but never transitions to stage 16 (no
re-enumeration, `cbw` stays unavailable).

Confirmed via usbmon side-by-side with the Windows-via-VFIO capture:

1. **Byte content matches.** Our `firstsect` 1024-byte upload and the
   `download:0x42800` 272384-byte upload are byte-identical to what the
   Windows tool sends (verified by extracting payloads from both pcaps).

2. **URB structure differs.** Windows sends the 272384-byte download as
   ONE USB Request Block. Linux libusb on our side delivers it as
   multiple URBs — the cap is empirically ~245760 bytes (15 × 16384) per
   sync `libusb_bulk_transfer`, even though pyusb reports the full byte
   count as transferred. Various chunk-size strategies fail:

   - 16 KB chunks (round 1): all 272384 bytes hit the wire across 17 URBs
   - single big transfer (round 2/3): only 245760 bytes hit the wire,
     pyusb falsely reports success
   - 64 KB chunks (round 4): all bytes delivered across 5 URBs
   - ZLP after a full-payload transfer: also no transition

   In all cases `boot` returns `OKAY` but the device doesn't execute the
   uploaded DDR init code.

**Likely root cause:** the Amlogic BootROM treats the `download:SIZE`
data exchange as a single atomic transfer; arrival of the same byte
count split across multiple URBs leaves the device in a "got the bytes
but the validating state machine never closed" state, and `boot` runs
against a half-committed buffer.

**Update (later this session):** byte-for-byte hash comparison of what
Windows actually uploads vs what we read from the `.img` reveals that
**the URB structure isn't the only difference — the byte content
differs too.**

The Windows tool **does NOT upload the .img's `DDR.USB` blob verbatim**.
First-64KB diff:
- Our blob: has a duplicate @AML sub-header at offset 0x110 and 0xC00
  bytes of zero padding at [0x400..0xFFF]
- Windows upload: those are stripped — the whole upload is shifted by
  0x1000 relative to our blob

The same pattern shows up against our 2.1.0 `bootloader_a` dumped
directly from eMMC after a clean factory burn — so it's not a
2.0.9-vs-2.1.0 issue, it's a "the upload is a TRANSFORMED view of the
on-disk blob" issue. The Amlogic USB Burning Tool appears to strip
signature blocks / padding before transmission.

usbmon's per-URB truncation (default 64KB) means we only have the first
~64KB of the 272384-byte upload; we can't reverse the full transformation
from this capture alone.

**Paths forward (prioritized):**

1. **Run Khadas's `adnl` against our device on ollie.** Khadas ships a
   working Linux x86-64 binary that drives the exact same protocol:
   <https://github.com/khadas/utils> →
   `aml-flash-tool/tools/adnl/{adnl,usb_flow/}`. It's not stripped
   (so debuggable) and uses Lua-scripted burn flows in `usb_flow/`. If
   it works on the AM9 Pro, we can read its source-equivalent (symbols
   + strings + Lua) and/or capture its USB traffic to see exactly how
   a *working* Linux implementation submits URBs. Skips all the
   Windows-tool reverse engineering.
2. **Re-capture a Windows burn with `usbmon_max_pkt_size` raised** so
   we get all 272384 bytes of the DDR upload. Empirical comparison
   confirms whatever theory (1) reveals.
3. **Static analysis of `V3_setup_V3.3.3.exe`** as a fallback if (1)
   doesn't work. The installed-tool binary on the Windows VM is
   easier to parse than the installer itself.

Observations from a brief survey of the open-source tools:

- **pyamlboot** (Baylibre, `pyamlboot/adnl.py::burn_bl2`) implements
  the older S5-family BL2 boot flow. No `firstsect` — that's S6-only.
  Useful reference for the `burnsteps` and `setvar:checksum` patterns
  but doesn't cover our exact case.
- **Khadas tools** include `adnl` (the actual Amlogic binary, x86-64
  ELF, not stripped), `adnl_burn_pkg`, and a `usb_flow/` directory of
  Lua-driven shared libraries (`libamlfastboot.so`, `libaml_usb_flow.so`,
  `liblua53.so`, `AmlImagePack.so`). The burn sequencing is in Lua,
  not hardcoded.
- **Our `.img` file** contains its own `usb_flow` item (212 KB, AML_RES
  container with 12 sub-items). The sub-item descriptors look scrambled
  (possibly encrypted/CRC'd in a way our V2 parser doesn't handle) —
  this is the per-device burn script Ugoos ships.

The wire captures from this session (`captures/round{1,2,3,4}.pcap`) are
saved for direct comparison against the working Windows pcap.

## Open questions for follow-up captures

- What does `getvar:cbw` actually return? (Issued repeatedly during U-Boot
  load — looks like a polling/handshake mechanism.)
- The 4 `setvar:checksum` writes correspond to checkpoint commits — what
  exactly is being committed? (Probably the per-block addsum from the
  preceding `mwrite:verify=addsum` stream.)
- What are the chipinfo page contents? (Decode payload of `OKAY` replies
  to `getvar:getchipinfo-N`. Page 5 is new for S6.)
