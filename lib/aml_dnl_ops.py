"""Layer 2 — operations on top of the raw protocol.

Composes Layer 1's primitives into atomic operations: parsing a `.img` file,
transitioning the device between burn stages, the CBW-driven U-Boot upload
loop, partition writes. Knows about the Amlogic image format and what a
burn stage is; doesn't know about specific *flows* (full restore vs OTA vs
single-partition) — those are Layer 3.

Functions here are direct ports of (named after) the AES-decrypted
`usb_flow.aml` Lua scripts in `aml-analysis/usb_flow_decrypted/`, mostly
from `usb_flow_dnl.lua` and `aml_mod_fastboot_dev.lua`.
"""
from __future__ import annotations

import os
import struct
import time
from dataclasses import dataclass
from typing import Callable, Optional

import usb.core

from aml_dnl_proto import (
    AmlogicDevice, AmlogicError, CBW, VENDOR_ID, PRODUCT_IDS,
    STAGE_BY_MODE, DEFAULT_CHUNK,
)
from aml_img import AmlogicImage, ImgItem  # noqa: F401 — re-exported for callers


# ── burnstep encoding ────────────────────────────────────────────────────────

# Per `usb_flow_decrypted/aml_mod_fastboot_dev.lua::usb_cmd_setvar_burnstep`:
#   steps = (0xc004 << 16) | (fwVer << 8) | part
# fwVer is the mode (0=romboot, 8=spl, 12=bl2e, 16=tpl) and `part` is a
# per-stage step index from RomSteps / TplSteps.

_FWVER_BY_STAGE = {v: k for k, v in STAGE_BY_MODE.items()}

# Step constants from usb_flow_dnl.lua
ROM_STEP_INIT          = 0
ROM_STEP_PRE_BL2_DOWN  = 1
ROM_STEP_AFTER_BL2     = 2
ROM_STEP_BL2_BOOT      = 3

TPL_STEP_DOWN_DTB      = 0x30
TPL_STEP_DISK_INIT     = 0x31
TPL_STEP_DOWN_PART     = 0x32


def encode_burnstep(stage: str, part: int) -> int:
    """`(0xc004 << 16) | (fwVer << 8) | part` per the Lua impl."""
    if stage not in _FWVER_BY_STAGE:
        raise ValueError(f"unknown stage {stage!r}; expected one of {list(_FWVER_BY_STAGE)}")
    if not 0 <= part <= 0xFF:
        raise ValueError(f"burnstep part {part} out of range [0..255]")
    return (0xc004 << 16) | (_FWVER_BY_STAGE[stage] << 8) | part


def set_burnstep(dev: AmlogicDevice, stage: str, part: int) -> None:
    """`setvar burnsteps <encoded>` (binary in romboot/spl, hex string via
    `oem setvar burnsteps 0x..` in tpl)."""
    val = encode_burnstep(stage, part)
    if stage == "tpl":
        dev.oem(f"setvar burnsteps {val:#x}")
    else:
        dev.setvar("burnsteps", val)


# ── stage transitions ────────────────────────────────────────────────────────

def reboot_to_romusb(dev: AmlogicDevice, *, timeout_s: float = 20.0,
                     poll_s: float = 0.2) -> AmlogicDevice:
    """Send `reboot-romusb`, close the handle, wait for the device to come
    back, return a new AmlogicDevice in the next burn stage.

    The Windows capture shows that `reboot-romusb` returns `OKAY` *before*
    the device disconnects, and the device then re-enumerates ~6 seconds
    later in the DDR-firmware-load stage. We use the same flow:
      1. send `reboot-romusb`, read the OKAY (so the device's IN buffer is
         clean and the device knows we ack'd)
      2. close our handle
      3. poll until either a NEW device handle works (responds to a probe)
         or the deadline elapses

    Re-enumeration detection is by *probing for a working handle*, not by
    watching bus/address — macOS often preserves the same enumeration
    address across a soft-reset, while Linux gives a new address; we
    accept either.
    """
    try:
        dev.cmd("reboot-romusb", timeout_ms=2000)
    except (AmlogicError, usb.core.USBError):
        pass
    try:
        dev.close()
    except Exception:
        pass
    # Settle. libusb caches descriptors briefly after a USB unplug on macOS.
    time.sleep(1.0)

    deadline = time.monotonic() + timeout_s
    last_error: Optional[str] = None
    while time.monotonic() < deadline:
        try:
            new_dev = AmlogicDevice.find()
        except AmlogicError as e:
            last_error = str(e)
            time.sleep(poll_s)
            continue
        try:
            new_dev.identify(timeout_ms=2000)
            return new_dev
        except (AmlogicError, usb.core.USBError) as e:
            last_error = str(e)
            try:
                new_dev.close()
            except Exception:
                pass
            time.sleep(poll_s)
    raise AmlogicError(
        f"device did not re-enumerate within {timeout_s}s after reboot-romusb "
        f"(last error: {last_error})"
    )


def load_ddr_firmware(dev: AmlogicDevice, ddr_blob: bytes,
                      ddr_size: Optional[int] = None) -> int:
    """Upload the DDR init portion of a bootloader blob (romboot stage).

    Ports `usb_flow_dnl.lua::romcode_flow`'s DDR-load sub-sequence:

        infData = read(4096 bytes)              -- 4 KB working buffer
        firstsect(infData)                      -- device asks for N bytes
        item.seek(4096)                         -- !!! skip to 0x1000 !!!
        bl2Size = getvar('downloadsize')
        download(item.read(bl2Size))            -- sends [0x1000 : 0x1000+bl2Size]

    The ~0x1000 byte shift between the on-disk `DDR.USB` blob and what hits
    the wire is just that seek — bytes [0x400..0x1000] are zero-padding inside
    the @AML container before the next signed sub-block at 0x1000, and the
    burn tool never uploads them. No content transformation is done.

    If `ddr_size` is None, the size is queried from the device via
    `getvar:downloadsize` (which is what the OEM tool does). Pass an
    explicit `ddr_size` only to override.

    Returns the actual bytes-after-firstsect that were downloaded.
    """
    SECT_BUF = 0x1000
    if len(ddr_blob) < SECT_BUF:
        raise AmlogicError(f"DDR blob too short: need >= {SECT_BUF}, have {len(ddr_blob)}")

    dev.firstsect(ddr_blob[:SECT_BUF])

    if ddr_size is None:
        # `getvar:downloadsize` returns a string like "0x00042800"
        try:
            sz_str = dev.getvar_str("downloadsize")
            ddr_size = int(sz_str, 16) if sz_str.startswith("0x") else int(sz_str, 16)
        except (AmlogicError, ValueError) as e:
            raise AmlogicError(f"could not query downloadsize: {e}") from e

    if len(ddr_blob) < SECT_BUF + ddr_size:
        raise AmlogicError(
            f"DDR blob too short for ddr_size {ddr_size:#x}: "
            f"need {SECT_BUF + ddr_size}, have {len(ddr_blob)}"
        )
    dev.download(ddr_blob[SECT_BUF: SECT_BUF + ddr_size])
    return ddr_size


# ── CBW-driven U-Boot load (spl → tpl) ───────────────────────────────────────

def _aml_addsum(data: bytes) -> int:
    """Amlogic's addsum: sum of u32 little-endian words, mod 2^32.

    Verified empirically against a live S6 device by sending a known
    16-byte buffer through `mwrite:verify=addsum` and trying candidate
    algorithms until the device replied OKAY. The Lua name is
    `aml_buf_addsum` (in libaulextend.dll).

    For inputs not a multiple of 4 bytes the tail is implicitly padded
    with zeros for the purposes of the sum (we just pad in Python).
    """
    pad = (-len(data)) & 3
    if pad:
        data = bytes(data) + b"\x00" * pad
    total = 0
    for i in range(0, len(data), 4):
        total += int.from_bytes(data[i:i + 4], "little")
    return total & 0xFFFFFFFF


def load_uboot_via_cbw(dev: AmlogicDevice, uboot_blob: bytes, *,
                       chunk_size: int = DEFAULT_CHUNK,
                       resend_attempts: int = 3,
                       on_progress: Optional[Callable[[int, int], None]] = None
                       ) -> None:
    """Port of `usb_flow_dnl.lua::bl2_boot` — the device-driven U-Boot upload.

    In spl/bl2e stages the device tells us via `getvar:cbw` which bytes from
    the UBOOT item to upload. Loop until the CBW signals `the_end`. After
    each round, if `need_checksum` is set, send the running addsum via
    `setvar checksum`.

    `uboot_blob` is the entire UBOOT item (typically same blob as DDR.USB —
    the bootloader carries both stages, but the device requests different
    offset windows for each).

    `on_progress(bytes_sent, total_bytes)` is called between chunks if given.
    """
    total = 0
    while True:
        cbw = dev.get_cbw(timeout_ms=5000)
        if cbw is None:
            raise AmlogicError("load_uboot_via_cbw: device returned no CBW")
        if cbw.wait_continue:
            time.sleep(0.5)
            ident = dev.identify(timeout_ms=2000)
            if ident.stage != "spl":
                raise AmlogicError(
                    f"load_uboot_via_cbw: stage degraded to {ident.stage!r} during wait"
                )
            continue
        if cbw.the_end:
            return
        if cbw.is_upload:
            raise AmlogicError(
                "load_uboot_via_cbw: CBW asked for upload (device→host); "
                "not supported by this routine"
            )
        if cbw.start_offset + cbw.transfer_size > len(uboot_blob):
            raise AmlogicError(
                f"CBW asks for [{cbw.start_offset:#x}..{cbw.start_offset + cbw.transfer_size:#x}] "
                f"but blob is {len(uboot_blob):#x}"
            )

        # Try up to resend_attempts times; the Lua does the same.
        for attempt in range(1, resend_attempts + 1):
            offset = cbw.start_offset
            remaining = cbw.transfer_size
            addsum = 0
            while remaining > 0:
                this = min(remaining, chunk_size)
                buf = uboot_blob[offset: offset + this]
                addsum = (addsum + _aml_addsum(buf)) & 0xFFFFFFFF
                dev.download(buf)
                offset += this
                remaining -= this
                total += this
                if on_progress:
                    on_progress(total, len(uboot_blob))
            if not cbw.need_checksum:
                break
            try:
                dev.setvar("checksum", addsum)
                break  # success
            except AmlogicError as e:
                if attempt >= resend_attempts:
                    raise AmlogicError(
                        f"CBW seq {cbw.sequence}: checksum FAIL after {attempt} tries: {e}"
                    ) from e


# ── partition write (tpl stage) ──────────────────────────────────────────────

# fileFmt values per `AmlogicImage` `file_type` (per aml-img-tool docs):
#   0x00 = "normal" (raw bytes)
#   0xfe = "sparse" (Android sparse image format)
def flash_partition(dev: AmlogicDevice, part_name: str, blob: bytes, *,
                    media: str = "store",
                    file_fmt: str = "normal",
                    verify_cmd: Optional[str] = None,
                    on_progress: Optional[Callable[[int, int], None]] = None
                    ) -> None:
    """Port of `usb_flow_dnl.lua::tpl_flashOnePartition` + the C-level
    `fb_mwrite_data` (libamlfastboot.dll). Verified against the live S6.

    Wire flow (two-phase, device-driven chunking):

        host → "oem mwrite SIZE_HEX normal store PARTNAME"
        dev  → OKAY                       (transaction accepted)

        loop:
          host → "mwrite:verify=addsum"
          dev  → "DATAOUT<chunk_size>:<offset>"   or   OKAY (= done)
          if DATA:
            host → <chunk_size bytes from blob[offset:offset+chunk_size]>
            host → <4-byte addsum (u32 LE word sum)>
            dev  → OKAY
          else (OKAY):
            break

    After the loop, optionally run `oem <verify_cmd>` (typically
    `verify sha1sum HEX`).

    Args:
        media: 'store' (eMMC), 'mem' (DRAM only), or 'key' (RPMB).
        file_fmt: 'normal' or 'sparse'. Only 'normal' supported here for now.
        verify_cmd: e.g. "verify sha1sum HEX...". If provided, `oem` is
            prepended automatically. Leave as None to skip post-write verify.
    """
    # The host doesn't need to decode sparse format — Amlogic's bootloader
    # does that itself when the `sparse` keyword is in the mwrite command.
    # We just hand it the raw bytes from the .img and the device unpacks
    # chunks while writing to the partition. Verified empirically: same
    # CBW-driven addsum loop as `normal`, just with `sparse` in the cmd.
    if file_fmt not in ("normal", "sparse"):
        raise ValueError(f"unknown file_fmt {file_fmt!r}")
    if media not in ("store", "mem", "key"):
        raise ValueError(f"invalid media {media!r}")

    size = len(blob)

    # Phase 1: announce the transaction.
    dev._write(f"oem mwrite {size:#x} {file_fmt} {media} {part_name}".encode("ascii"))  # noqa: SLF001
    status, body, _ = dev._read_response(timeout_ms=60_000)  # noqa: SLF001
    if status != "OKAY":
        raise AmlogicError(
            f"oem mwrite {part_name}: expected OKAY, got {status} {body!r}"
        )

    # Phase 2: device-chunked data loop. Each `mwrite:verify=addsum` either
    # returns DATAOUT<chunk_size>:<offset> (more data needed) or OKAY (done).
    bytes_sent = 0
    while True:
        dev._write(b"mwrite:verify=addsum")  # noqa: SLF001
        status, body, ack = dev._read_response(timeout_ms=60_000)  # noqa: SLF001
        if status == "OKAY":
            break
        if status != "DATA":
            raise AmlogicError(
                f"mwrite:verify=addsum: expected DATA or OKAY, got {status} {body!r}"
            )
        if not ack.is_download:
            raise AmlogicError(
                f"mwrite asked for upload (device→host) — not supported here"
            )
        chunk_size = ack.size
        chunk_offset = ack.file_offset
        if chunk_offset + chunk_size > size:
            raise AmlogicError(
                f"mwrite chunk out of range: offset={chunk_offset:#x} "
                f"size={chunk_size:#x} blob_size={size:#x}"
            )
        chunk = blob[chunk_offset: chunk_offset + chunk_size]
        addsum = _aml_addsum(chunk)
        dev._write(chunk)  # noqa: SLF001
        dev._write(addsum.to_bytes(4, "little"))  # noqa: SLF001
        status, body, _ = dev._read_response(timeout_ms=60_000)  # noqa: SLF001
        if status != "OKAY":
            raise AmlogicError(
                f"mwrite chunk @{chunk_offset:#x}: expected OKAY after data+addsum, "
                f"got {status} {body!r}"
            )
        bytes_sent += chunk_size
        if on_progress:
            on_progress(bytes_sent, size)

    if verify_cmd:
        v = verify_cmd[4:] if verify_cmd.startswith("oem ") else verify_cmd
        dev.oem(v, timeout_ms=60_000)


# ── identity / state helpers ─────────────────────────────────────────────────

def dump_chipinfo_pages(dev: AmlogicDevice) -> dict[int, bytes]:
    """Read all chipinfo pages (0..7), respecting page 0's pageMap bitfield.

    Page 0 has magic "INDX" + a u8 pageMap; bit N of pageMap means page N
    is populated. Returns a dict mapping populated page index to its 64
    raw bytes.
    """
    out: dict[int, bytes] = {}
    page0 = dev.getchipinfo(0)
    if len(page0) < 5 or page0[:4] != b"INDX":
        raise AmlogicError(f"page0 magic missing or short: {page0[:8].hex()}")
    if not (page0[4] & 1):
        raise AmlogicError(f"page0 pageMap (0x{page0[4]:02x}) doesn't claim page 0")
    out[0] = page0
    pages_map = page0[4]
    for n in range(1, 8):
        if pages_map & (1 << n):
            try:
                out[n] = dev.getchipinfo(n)
            except AmlogicError:
                continue
    return out
