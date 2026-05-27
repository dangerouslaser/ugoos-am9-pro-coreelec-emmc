# Amlogic USB DNL protocol — decoded from a live S6 burn capture

Source: USBPcap capture of the official **USB Burning Tool v3.3.3** flashing
`AM9PRO_2.1.0.img` to the AM9 Pro (S905X5-J / S6 SoC family).

Captured on 2026-05-26 via VFIO passthrough of the host's Intel Raptor Lake
XHCI controller into a Windows VM. 186,543 USB frames / 1.13 GB / 215 s. The
raw `.pcapng` is gitignored (`captures/burn-2.1.0-windows-full.pcapng`); the
analyzer is `analyze-burn-capture.py`; the full transcript is at
`burn-protocol-decoded.txt`.

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
- **Tested both Linux (v2.6.3, 2021) and Mac universal (v2.7.5, 2024)
  versions of Khadas adnl against the AM9 Pro — BOTH refuse our S6
  device.** `adnl bl1_boot` rejects with `illegle device mode:06-00-00-16`;
  the checker (disassembled from the Mac arm64 binary at
  `_fb_bl1_boot+0xC3`) requires identify byte 0 ≥ 5 (we pass: S6 = 6)
  AND identify byte 3 == 0 (we fail: 0x10 = 16). `adnl bl2_boot`
  similarly rejects with `illegle fw mode 16 for bl2_boot`. Per
  pyamlboot's identify-reply documentation, the byte-4-of-identify
  field is the *protocol type* — values 3 (Optimus) and 5 (ADNL) are
  known; **our S6 device returns 6, a protocol variant that no
  open-source tool has implemented yet**. The S905X5 / S6 family is
  newer than Khadas's distributed tools support, and the rebuilt
  Amlogic SDK that does support it doesn't appear to be publicly
  available.
- **Our `.img` file** contains its own `usb_flow` item (212 KB, AML_RES
  container with 12 sub-items). The sub-item descriptors look scrambled
  (possibly encrypted/CRC'd in a way our V2 parser doesn't handle) —
  this is the per-device burn script Ugoos ships.

### Where the blob transformation lives (Option B static analysis)

Mounted the Windows VM disk on ollie and pulled out the installed
`USB_Burning_Tool V3` binaries for analysis. The complete picture:

```
Aml_Burn_Tool.exe         — GUI front-end
AmlImageCheck.dll         — sanity-checks .img validity
AmlDevManage.dll          — Windows USB driver enumeration

aml_usb_flow.dll          — loads & DECRYPTS usb_flow.aml (AES; Rijndael
                            Sbox at offset 0xa229-0xa338; key derivation
                            in lua_item_decrypt_and_load)
libaulextend.dll          — Lua VM + bindings:
                              l_aml_fastboot.cpp
                              l_aml_libusb.cpp
                              l_image_packer.cpp
AmlImagePack.dll          — parses outer .img container (read-only;
                            does NOT transform blob bytes)
libamlfastboot.dll        — protocol layer: download:HHHHHHHH +
                            sparse_file_* + fb_bl2_boot + fb_mwrite_data
libamllibusb.dll / libusb0.dll — USB transport

usb_flow.aml              — AML_RES container with 12 AES-ENCRYPTED
                            Lua items; this is where the actual burn
                            sequence + blob preprocessing lives
key_flow.aml              — companion AES-encrypted Lua container for
                            keystore operations
```

So the blob transformation (zeroing the @AML at 0x110, stripping the
0xC00 padding at 0x400) is implemented in **encrypted Lua scripts**,
not C code. To decode it we would need:

1. Reverse-engineer `lua_item_decrypt_and_load` in aml_usb_flow.dll
   (Ghidra job — find the AES key derivation)
2. Decrypt the 12 items in usb_flow.aml
3. Unluac/luadec the resulting Lua bytecode (Lua 5.3 per the
   `liblua53.so` reference in Khadas's tools)
4. Parse the actual burn script and extract the transformation logic

That's a substantial project — easily days of careful RE work — and
would need to be redone every time Amlogic ships a new tool version
(the keys and Lua bytecode change).

### Pragmatic recommendation

**Skip the encryption decode.** Take the empirical path: re-capture a
Windows burn with `usbmon_max_pkt_size` raised on ollie, get the full
272384 bytes of the actual upload, and diff against our DDR blob
byte-for-byte to derive the transformation rule directly. This costs
one more burn cycle (~10 min) but avoids days of RE work and gives us
the same answer (probably "skip @AML duplicates and align to next
0x1000 boundary"). Then build a small Layer-2 preprocessor in our
Python code that applies the rule, fix the URB submission to one big
URB (via libusb async API or usbfs ioctls), and we have a working
burner.

The encryption RE path is worth doing only if we want to track Amlogic
SDK changes long-term or contribute the algorithm back to pyamlboot.

The wire captures from this session (`captures/round{1,2,3,4}.pcap`) are
saved for direct comparison against the working Windows pcap.

## RESOLVED — full protocol decoded from decrypted Lua

Several earlier sections in this doc are now superseded. Final answer
based on the AES-decrypted `usb_flow.aml` Lua scripts (see
`aml-analysis/README.md` for the decryption + extraction):

### Stage / mode naming

The `identify` reply's byte 4 (mode) maps to a stage name per
`aml_mod_fastboot_dev.lua::usbStages`:

```lua
usbStages = { [0] = 'romboot', [8] = 'spl', [12] = 'bl2e', [16] = 'tpl' }
```

So our AM9 Pro, which returns `06 00 00 10 00 00 00 00` on identify,
is in mode 16 = **`tpl` (U-Boot already running)**. Our earlier
"stage 14" labeling in this repo was wrong — the device boots into
TPL stage directly when it enters DNL mode. The S6 BootROM transitions
through romboot → spl → tpl automatically before the host first sees
it on USB. That's the protocol-6 difference vs S5.

Implication: **most flash operations don't need to traverse the
romcode→bl2 chain**. We can issue `oem mwrite` directly in TPL stage
and get the result. The Khadas adnl tool's hardcoded check "byte 3
must == 0" is therefore wrong for S6 — they're checking for the OLD
behavior where the device shows up in romboot mode.

### The "blob transformation" (resolved)

It was never a transformation. From `usb_flow_dnl.lua::romcode_flow`:

```lua
local infData = bufMan.subBuf(0, 4096)         -- 4 KB working buffer
hItem.item_read(infData)                        -- read first 4 KB of bootloader
usbDev:FirstSect(infData, isS7dRva)             -- device asks for N bytes,
                                                --   send first N from infData
hItem.item_seek(4096)                           -- *** SEEK PAST 4 KB ***
local _, result = usbDev:GetVar("downloadsize") -- device tells us bl2Size
local bl2Size = tonumber(result.info[1])
... -- then Download(bl2Size bytes) starting from offset 4096
```

The ~0x1000-byte shift between the `.img` `DDR.USB` blob and what
Windows actually uploads is just `item.seek(4096)`. Bytes 0x400..0x1000
inside the @AML container are zero-padding before the next signed
sub-block aligned at 0x1000; the burn tool reads them but never
uploads them. The "duplicate @AML at offset 0x110" we noticed was
just the inner signed sub-block at file offset 0x110 of the @AML
container.

Already applied in `aml_dnl_ops.py::load_ddr_firmware`.

### Wire format of every command

Captured from `aml_mod_fastboot_dev.lua` + `libamlfastboot.dll`. All
commands are ASCII text on bulk OUT, responses are 4-byte ASCII status
on bulk IN followed by optional payload.

**Standard fastboot:**

| Command | Behavior |
|---------|----------|
| `getvar:NAME` | OKAY + value, or FAIL |
| `download:HHHHHHHH` | DATA<size>, then host streams `size` bytes, then OKAY |
| `setvar:NAME` | DATA00000004, then host streams 4 bytes (u32 LE), then OKAY |
| `boot` | OKAY (device may re-enumerate) |
| `reboot-romusb` | OKAY then device re-enumerates into romboot stage |

**Amlogic-specific:**

| Command | Notes |
|---------|-------|
| `firstsect` | Special: device replies DATA<size>+OKAY; host MUST upload `size` bytes between them. S6 always asks for 1024 (0x400). |
| `getvar:cbw` | Returns the 24-byte CBW struct (see below). Only valid in spl/bl2e/tpl stages. |
| `getvar:identify` | 8-byte response. Layout: `[proto, minor, ?, mode, ?, needPwd, pwdOk, pagesMap]` |
| `getvar:getchipinfo-N` | N = 0..7 page index. 64-byte response. Page 0 = INDX magic + pagemap; page 1 = CHIP info; page 2 = CID; page 3 = SGVR; page 5 = security bits (new on S6). |
| `getvar:serialno` | Chip die serial (NOT board serial). |
| `getvar:downloadsize` | Returns hex string like "0x00042800". |
| `oem CMD` | Generic OEM command channel. Many subcommands; see below. |

**OEM commands (all sent as `oem <subcmd>`):**

| Subcmd | Purpose |
|--------|---------|
| `disk_initial N` | Initialize partition table. N=0 keep, N>0 erase. |
| `get_bootloaderversion` | Return version string |
| `env_get NAME` | Read U-Boot env var |
| `save_setting` | Persist env to eMMC |
| `setvar burnsteps 0xVALUE` | Progress checkpoint. Encoding: `0xC004 << 16 \| fwVer << 8 \| part` |
| `sheader_need` | Returns OKAY if device wants sheader pre-write |
| `verify sha1sum HEX` | Verify last-written partition against SHA1 |
| `mwrite SIZE_HEX (normal\|sparse) (store\|mem) PARTNAME` | Begin partition write; followed by `mwrite:verify=addsum` rounds |
| `mread ...` | Partition read-back (mread:status=request/upload/finish) |
| `rpmb_init` | Initialize RPMB (Replay Protected Memory Block) |

**Burnsteps encoding** (from `usb_cmd_setvar_burnstep`):

```
steps = (0xC004 << 16) | (fwVer << 8) | part
```

Where `fwVer` is the stage number from usbStages (0/8/12/16) and
`part` is the per-stage step index. In our burn capture we saw
0xc0041030, 0xc0041031, 0xc0041032 — that's stage 16 (tpl), parts
0x30 (DownDtb), 0x31 (DiskInit), 0x32 (DownLgcPart) — per `TplSteps`.

### CBW (Control Block Word) structure

The device-driven burn protocol used in spl/bl2e/tpl stages. Per
`aml_mod_fastboot_dev.lua::usb_cmd_get_cbw`:

```
offset  size  field
 0      4     magic "AMLC"
 4      4     transferSequence  (u32 LE)
 8      4     transferSize      (u32 LE)
12      4     startOffset       (u32 LE — file offset to read from)
16      1     flags             (bit 0: needCheckSum if 0)
17      1     direction         (bit 7: bulk-IN/upload if set)
18      1     requestType       (0=normal data, 1=end, 0xFF=wait, else=error)
19+     ...   (unused, total 24 bytes typical)
```

So `usbDev:GetCbw()` returns a struct:

```python
{
    'sequence': transferSequence,
    'transferSize': transferSize,
    'startOffset': startOffset,
    'needCheckSum': (flags & 1) == 0,
    'isUpload': (direction & 0x80) != 0,
    'theEnd': requestType == 1,
    'waitContinue': requestType == 0xFF,
}
```

Error if `requestType > 1` (and not 0xFF).

### Response parser nuances (`usb_check_cmd`)

The first 4 bytes are status; "INFO" responses can be sent
ASYNCHRONOUSLY by the bootloader (e.g. progress messages) and the host
MUST drain them until it sees a terminal `OKAY`/`FAIL`/`DATA`. There's
a 25-retry busy limit.

DATA payload format:

- `DATA<8hex>` → host should send `dataSize` bytes
- `DATA OUT <hex> <hex>` → explicit OUT direction, dataSize + fileOffset
- `DATA IN <hex> <hex>` → device wants to send to host (upload)

This means our existing `download()` in aml_dnl_proto.py is over-
specialized — should be generalized to parse all three DATA formats.

### Burn flow summary

```
ENTRY (TPL stage, mode=16, S6 boots directly here)
  ↓ if doing full restore:
reboot-romusb → wait for re-enum into romboot
  ↓
romcode_flow:
  GetVar(identify), DumpFeats (all chipinfo pages),
  SecureBoot check,
  SetSteps(rom, Init),
  SetSteps(rom, PreBl2Down),
  read 4 KB → FirstSect, item.seek(4096),
  GetVar(downloadsize) → bl2Size,
  Download(bl2Size bytes from offset 4096),
  SetSteps(rom, Bl2boot),
  boot
  ↓ device re-enumerates into spl stage (mode 8)
bl2_boot:
  Identify, SocType, SetSteps(spl, ?),
  loop:
    GetCbw → {sequence, transferSize, startOffset, needCheckSum, theEnd}
    if theEnd: break
    if waitContinue: sleep 500ms, Identify, GetCbw again
    seek to startOffset in UBOOT item
    read+Download in 16KB chunks (compute addsum)
    if needCheckSum: SetVar(checksum, addsum)
  ↓ device re-enumerates into tpl stage (mode 16) — already here on S6
tpl_flow:
  SecureBoot check,
  SetSteps(tpl, DownDtb),
  flash dtb to mem,
  flash gpt to mem,
  (optional) flash sheader to mem,
  OemCmd("disk_initial N"),
  SetSteps(tpl, DiskInit),
  SetSteps(tpl, DownLgcPart),
  for each partition (in burnParts order, bootloader LAST):
    OemCmd("mwrite SIZE (normal|sparse) (store|mem) NAME"),
    fb_mwrite_data: addsum-tracked download loop,
    OemCmd(verify cmd) — usually "verify sha1sum HEX"
  OemCmd("save_setting")
  if RPMB needed: OemCmd("rpmb_init")
  burn keys from img if present
  done
```

### What every protocol-6 burn tool needs

To talk to an S6 device beyond what pyamlboot/Khadas adnl currently
supports, an open implementation needs:

1. Identify-byte mapping: `mode=16` → `tpl` (currently rejected as
   "illegle device mode" by Khadas).
2. The `firstsect` command in non-S7d form (sent as bare `firstsect`
   without size suffix; device replies DATA<size>).
3. The `item.seek(4096)` between firstsect and download.
4. Proper INFO-message draining in the response parser (`usb_check_cmd`).
5. The CBW protocol with the 24-byte struct above.
6. The `setvar` binary 4-byte upload path (vs `oem setvar` hex string).
7. Burnsteps encoding `(0xC004 << 16) | (fwVer << 8) | part`.
8. Acceptance that TPL is the entry state — `bl1_boot`/`bl2_boot` are
   not always needed; many flash operations work directly in TPL.

Items 4-7 are now implemented in `aml_dnl_proto.py` + `aml_dnl_ops.py` —
the full burner CLI lives at `burn/aml-dnl-burn.py` and has been verified
end-to-end against a live AM9 Pro (1.6 GB full restore including sparse
super, individual partition OTAs, and idempotent self-flashes). Items
1-3, 8 are insights to contribute upstream.
