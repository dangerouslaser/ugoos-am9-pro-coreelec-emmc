"""Layer 2 — operations on top of the raw protocol.

Composes Layer 1's `cmd()` / `download()` primitives into atomic operations:
parsing a `.img` file, transitioning the device between burn stages, writing
to named partitions. Knows what an Amlogic image is and what a burn stage is;
doesn't know about specific flows (full restore vs OTA vs single-partition) —
those are Layer 3.
"""
from __future__ import annotations

import os
import struct
import time
from dataclasses import dataclass
from typing import Optional

import usb.core

from aml_dnl_proto import AmlogicDevice, AmlogicError, VENDOR_ID, PRODUCT_IDS

# ── .img file format ─────────────────────────────────────────────────────────
# Verified empirically against AM9PRO_2.0.9.img and 2.1.0.img; same layout as
# the upstream `aml_image_v2_packer`. See `aml-img-tool.py` for the full
# format docs.

_IMG_MAGIC_V2 = 0x27B51956
_ITEM_TABLE_OFFSET = 0x40
_ITEM_DESC_SIZE = 0x240
# Per aml-img-tool.py (verified against AM9PRO_2.0.9.img):
_ITEM_OFFSET_OFF = 0x10
_ITEM_SIZE_OFF = 0x18
_ITEM_TYPE_OFF = 0x20
_ITEM_TYPE_LEN = 32
_ITEM_NAME_OFF = 0x120
_ITEM_NAME_LEN = 32
# Header item-count field — at 0x18, not 0x1C:
_HDR_ITEM_NUM_OFF = 0x18


@dataclass(frozen=True)
class ImgItem:
    index: int
    type: str
    name: str
    offset: int
    size: int


class AmlogicImage:
    """Mmap-style accessor for an Amlogic `.img` archive."""

    def __init__(self, path: str):
        with open(path, "rb") as f:
            self._data = f.read()
        magic = struct.unpack_from("<I", self._data, 0x08)[0]
        if magic != _IMG_MAGIC_V2:
            raise ValueError(f"{path}: not a v2 Amlogic image (magic={magic:#x})")
        item_num = struct.unpack_from("<I", self._data, _HDR_ITEM_NUM_OFF)[0]
        items = []
        for i in range(item_num):
            base = _ITEM_TABLE_OFFSET + i * _ITEM_DESC_SIZE
            type_b = self._data[base + _ITEM_TYPE_OFF: base + _ITEM_TYPE_OFF + _ITEM_TYPE_LEN]
            name_b = self._data[base + _ITEM_NAME_OFF: base + _ITEM_NAME_OFF + _ITEM_NAME_LEN]
            offset = struct.unpack_from("<Q", self._data, base + _ITEM_OFFSET_OFF)[0]
            size = struct.unpack_from("<Q", self._data, base + _ITEM_SIZE_OFF)[0]
            items.append(ImgItem(
                index=i,
                type=type_b.rstrip(b"\x00").decode("ascii", "replace"),
                name=name_b.rstrip(b"\x00").decode("ascii", "replace"),
                offset=offset,
                size=size,
            ))
        self.items = items
        self.path = path

    def find(self, name: str, item_type: Optional[str] = None) -> ImgItem:
        for it in self.items:
            if it.name == name and (item_type is None or it.type == item_type):
                return it
        raise KeyError(
            f"item not found: name={name!r} type={item_type!r} in {self.path}"
        )

    def blob(self, name: str, item_type: Optional[str] = None) -> bytes:
        it = self.find(name, item_type)
        return self._data[it.offset: it.offset + it.size]


# ── stage transitions ────────────────────────────────────────────────────────

def firstsect(dev: AmlogicDevice, header_bytes: bytes,
              *, timeout_ms: int = 5000) -> None:
    """Run the `firstsect` handshake (S6-family bootloader-load preamble).

    Stage-15 quirk: before any normal `download:`, the device wants the
    first 1024 bytes of the @AML boot blob uploaded via a separate command.
    Protocol:

        host → "firstsect"
        dev  → "DATA00000400"   (always 1024 bytes)
        host → <1024 bytes>
        dev  → "OKAY"

    Raises AmlogicError on any framing mismatch. `header_bytes` must be at
    least 1024 bytes (caller passes the front of the DDR/UBOOT blob; only
    the first 1024 are sent).
    """
    status, body = dev.cmd("firstsect", timeout_ms=timeout_ms)
    if status != "DATA":
        raise AmlogicError(f"firstsect: expected DATA, got {status} {body!r}")
    try:
        wanted = int(body[:8].decode("ascii"), 16)
    except (UnicodeDecodeError, ValueError):
        raise AmlogicError(f"firstsect: bad DATA payload {body!r}")
    if wanted != 0x400:
        # Defensive — the capture always shows 0x400. If the device ever
        # asks for something else we want to know loudly.
        raise AmlogicError(f"firstsect: device wants {wanted:#x} bytes, expected 0x400")
    if len(header_bytes) < wanted:
        raise AmlogicError(
            f"firstsect: need {wanted} bytes of header, got {len(header_bytes)}"
        )
    dev._write(header_bytes[:wanted], timeout_ms=timeout_ms)  # noqa: SLF001
    reply = dev._read(64, timeout_ms=timeout_ms)               # noqa: SLF001
    if not reply.startswith(b"OKAY"):
        raise AmlogicError(
            f"firstsect: expected OKAY after upload, got {reply!r}"
        )


def load_ddr_firmware(dev: AmlogicDevice, ddr_blob: bytes,
                      ddr_size: int = 0x42800) -> None:
    """Upload the DDR init portion of a bootloader blob (stage 15).

    Combines `firstsect` (first 1024 bytes) + `download:` (next `ddr_size`
    bytes) and leaves the device ready to receive `boot`. Does NOT send
    `boot` — caller does that after deciding whether to continue.

    `ddr_blob` is the full @AML blob (e.g. from `AmlogicImage.blob("DDR")`).
    `ddr_size` is the bytes-after-header to download; 0x42800 matches the
    Windows tool for AM9 Pro on AM9PRO_2.x.y images.
    """
    # Per the Lua flow we decrypted from usb_flow.aml::usb_flow_dnl.lua
    # `romcode_flow`:
    #
    #   infData = read(4096 bytes)              -- 4 KB working buffer
    #   firstsect(infData)                      -- device asks for N bytes,
    #                                              we send first N from this
    #   item.seek(4096)                         -- !!! SKIP TO 0x1000 !!!
    #   bl2Size = getvar('downloadsize')
    #   download(item.read(bl2Size))            -- sends bytes [0x1000:]
    #
    # The "transformation" we measured on the wire (0x1000-byte shift, zero
    # padding stripped) is just this seek-and-skip — bytes [0x400..0x1000]
    # are never uploaded. They're typically zero-padding inside the @AML
    # outer header anyway, since the next signed sub-block is aligned at
    # offset 0x1000.
    SECT_BUF = 0x1000          # 4 KB working buffer the OEM tool uses
    if len(ddr_blob) < SECT_BUF + ddr_size:
        raise AmlogicError(
            f"DDR blob too short: need {SECT_BUF + ddr_size}, "
            f"have {len(ddr_blob)}"
        )
    firstsect(dev, ddr_blob[:SECT_BUF])
    try:
        dev.cmd("getvar:downloadsize", timeout_ms=2000)
    except AmlogicError:
        pass
    dev.download(ddr_blob[SECT_BUF : SECT_BUF + ddr_size])


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
        status, body = dev.cmd("reboot-romusb", timeout_ms=2000)
    except (AmlogicError, usb.core.USBError):
        # Acceptable — some devices drop the connection before sending OKAY.
        pass
    try:
        dev.close()
    except Exception:
        pass

    # The device may take a few seconds to come back. Sleep first so we
    # don't immediately reconnect to the OLD handle (libusb sometimes
    # caches stale device descriptors briefly on macOS).
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
        # Probe — if we can identify, the handle is real.
        try:
            new_dev.cmd("getvar:identify", timeout_ms=2000)
            return new_dev
        except (AmlogicError, usb.core.USBError) as e:
            last_error = str(e)
            try:
                new_dev.close()
            except Exception:
                pass
            time.sleep(poll_s)
    raise AmlogicError(
        f"device did not re-enumerate within {timeout_s}s after "
        f"reboot-romusb (last error: {last_error})"
    )
