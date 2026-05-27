"""Layer 1 — Amlogic DNL wire protocol over USB.

Implements the protocol primitives surfaced by the AES-decrypted
Lua scripts inside `usb_flow.aml` (see `aml-analysis/`). The protocol
is text-mode fastboot with Amlogic-specific commands; this module
knows about the wire format and nothing about burns, partitions, or
.img files (Layer 2's job).

Reference is `usb_flow_decrypted/aml_mod_fastboot_dev.lua`
(in particular `usb_send_cmd`, `usb_check_cmd`, `usb_cmd_get_cbw`,
`usb_cmd_identify`, `usb_cmd_set_var`).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import usb.core
import usb.util


VENDOR_ID = 0x1B8E
# 0xc003 = old GX-CHIP BootROM. 0xc004 = newer ADNL (S5/S6 etc.); on S6
# the device boots straight into TPL stage and exposes this PID.
PRODUCT_IDS = (0xC003, 0xC004)

STATUS_OKAY = "OKAY"
STATUS_DATA = "DATA"
STATUS_FAIL = "FAIL"
STATUS_INFO = "INFO"
TERMINAL_STATUSES = (STATUS_OKAY, STATUS_DATA, STATUS_FAIL)
ALL_STATUSES = TERMINAL_STATUSES + (STATUS_INFO,)

# Stage names per usb_flow_decrypted/aml_mod_fastboot_dev.lua::usbStages
# (mode byte of getvar:identify reply).
STAGE_BY_MODE = {
    0:  "romboot",  # BL1 / BootROM
    8:  "spl",      # BL2 / DDR firmware load
    12: "bl2e",     # BL2 extended
    16: "tpl",      # TPL / U-Boot — S6 boots straight here in DNL mode
}

# fastboot command framing: max 64-byte command + 64-byte response
# header from upstream Android fastboot spec.
FB_COMMAND_SZ = 128

# Per-`download:` size cap returned in a typical bl2-stage downloadsize
# query; bigger transfers should be split into multiple download cycles.
DEFAULT_CHUNK = 0x4000


class AmlogicError(Exception):
    """Anything the device or transport said no to."""


@dataclass
class _Endpoints:
    out: int
    in_: int
    max_packet_out: int
    max_packet_in: int


@dataclass
class DataAck:
    """Parsed `DATA<…>` response from the device.

    Format possibilities (per `usb_check_cmd` in the Lua):
      `DATA00042800`            → size only (most common; download flow)
      `DATA OUT 0x42800 0x1000` → explicit OUT, size + fileOffset
      `DATA IN 0x100`           → device wants to send bytes to host (upload)
    """
    size: int
    file_offset: int = 0
    is_download: bool = True  # True = host→device, False = device→host


@dataclass
class Identify:
    """Parsed `getvar:identify` reply (8 bytes)."""
    bytes_: bytes
    protocol: int      # byte 1 (1-indexed) — 3=Optimus, 5=ADNL, 6=S6 ADNL
    minor_ver: int     # byte 2
    mode: int          # byte 4
    pages_map: int     # byte 8

    @property
    def stage(self) -> str:
        return STAGE_BY_MODE.get(self.mode, f"mode{self.mode:#x}")


@dataclass
class CBW:
    """Parsed Control Block Word from `getvar:cbw` (24 bytes, magic 'AMLC').

    Drives the device-led burn loop in spl/bl2e/tpl stages. The device
    tells the host what to upload, the host obliges, repeat until
    `the_end` is set.
    """
    sequence: int
    transfer_size: int
    start_offset: int
    need_checksum: bool
    is_upload: bool       # bit 7 of direction byte
    the_end: bool         # requestType == 1
    wait_continue: bool   # requestType == 0xFF
    raw: bytes


class AmlogicDevice:
    """A single Amlogic device in DNL mode.

    Construct via `AmlogicDevice.find()`. Use as a context manager so
    resources get released.
    """

    def __init__(self, dev: usb.core.Device, eps: _Endpoints, pid: int):
        self._dev = dev
        self._eps = eps
        self.pid = pid

    # ── lifecycle ─────────────────────────────────────────────────────────

    @classmethod
    def find(cls, timeout_s: float = 0.0) -> "AmlogicDevice":
        deadline = time.monotonic() + timeout_s
        while True:
            for pid in PRODUCT_IDS:
                dev = usb.core.find(idVendor=VENDOR_ID, idProduct=pid)
                if dev is not None:
                    return cls._open(dev, pid)
            if time.monotonic() >= deadline:
                raise AmlogicError(
                    f"no Amlogic DNL device found "
                    f"(VID=0x{VENDOR_ID:04x}, PID in {[hex(p) for p in PRODUCT_IDS]})"
                )
            time.sleep(0.1)

    @classmethod
    def _open(cls, dev: usb.core.Device, pid: int) -> "AmlogicDevice":
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except (NotImplementedError, usb.core.USBError):
            pass
        try:
            dev.set_configuration()
        except usb.core.USBError as e:
            if e.errno not in (16,):
                raise
        cfg = dev.get_active_configuration()
        intf = cfg[(0, 0)]
        try:
            usb.util.claim_interface(dev, intf)
        except usb.core.USBError as e:
            raise AmlogicError(
                f"failed to claim interface 0 (errno {e.errno}): {e}"
            ) from e
        ep_out = ep_in = None
        for ep in intf:
            if usb.util.endpoint_type(ep.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK:
                if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_OUT:
                    ep_out = ep
                else:
                    ep_in = ep
        if ep_out is None or ep_in is None:
            raise AmlogicError("device does not expose a bulk OUT + bulk IN endpoint pair")
        return cls(dev, _Endpoints(
            out=ep_out.bEndpointAddress,
            in_=ep_in.bEndpointAddress,
            max_packet_out=ep_out.wMaxPacketSize,
            max_packet_in=ep_in.wMaxPacketSize,
        ), pid)

    def close(self) -> None:
        try:
            usb.util.release_interface(self._dev, 0)
        except Exception:
            pass
        usb.util.dispose_resources(self._dev)

    def __enter__(self) -> "AmlogicDevice":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── low-level transport ───────────────────────────────────────────────

    # Linux libusb sync bulk_transfer silently caps URBs at ~240 KB and
    # reports the full request as transferred regardless. Larger payloads
    # must be split into multiple pyusb.write() calls (each = one URB)
    # below the cap. The Amlogic device counts cumulative bytes per
    # `download:` round, so multiple URBs summing to the announced size
    # work fine in practice.
    _WRITE_MAX_URB = 64 * 1024

    def _write(self, data: bytes, timeout_ms: int = 5000) -> int:
        total = 0
        view = memoryview(data)
        while total < len(data):
            end = min(total + self._WRITE_MAX_URB, len(data))
            chunk = view[total:end].tobytes()
            n = self._dev.write(self._eps.out, chunk, timeout=timeout_ms)
            if n == 0:
                raise AmlogicError(f"bulk OUT stalled at {total} of {len(data)} bytes")
            total += int(n)
        return total

    def _read(self, size: int, timeout_ms: int = 5000) -> bytes:
        return bytes(self._dev.read(self._eps.in_, size, timeout=timeout_ms))

    # ── response parser ───────────────────────────────────────────────────

    @staticmethod
    def _parse_data(body: bytes) -> DataAck:
        """Parse the payload after a `DATA` status prefix.

        Three forms exist on the wire:
          `<8 hex chars>`               — simple size, OUT direction
          `OUT <hex> <hex>`             — explicit OUT, size + offset
          `IN <hex> <hex>`              — device wants to UPLOAD to host

        Numbers may or may not have a 0x prefix.
        """
        text = body.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()
        is_download = True
        if text.startswith("OUT "):
            text = text[4:]
        elif text.startswith("IN "):
            text = text[3:]
            is_download = False
        # Tokenise: tokens are whitespace-separated hex (with or without 0x).
        tokens = [t for t in text.split() if t]
        if not tokens:
            raise AmlogicError(f"empty DATA payload: {body!r}")

        def _hex(t: str) -> int:
            t = t[2:] if t.lower().startswith("0x") else t
            return int(t, 16)
        size = _hex(tokens[0])
        offset = _hex(tokens[1]) if len(tokens) > 1 else 0
        return DataAck(size=size, file_offset=offset, is_download=is_download)

    def _read_response(self, *, drain_info_max: int = 25,
                       read_size: int = FB_COMMAND_SZ,
                       timeout_ms: int = 5000
                       ) -> tuple[str, bytes, Optional[DataAck]]:
        """Read fastboot responses, draining INFO messages until terminal.

        Returns (status, body, data_ack). `data_ack` is set iff status==DATA.
        Raises AmlogicError on FAIL (with the device's message) or anything
        else unexpected.
        """
        info_count = 0
        while True:
            reply = self._read(read_size, timeout_ms=timeout_ms)
            if len(reply) < 4:
                raise AmlogicError(f"short response (< 4 bytes): {reply!r}")
            status = reply[:4].decode("ascii", errors="replace")
            body = reply[4:]
            if status not in ALL_STATUSES:
                raise AmlogicError(f"unknown status prefix {status!r}: {reply!r}")
            if status == STATUS_INFO:
                info_count += 1
                if info_count > drain_info_max:
                    raise AmlogicError(
                        f"too many INFO messages ({info_count}); device stuck?"
                    )
                continue
            if status == STATUS_FAIL:
                msg = body.split(b"\x00", 1)[0].decode("ascii", errors="replace")
                raise AmlogicError(f"FAIL: {msg}")
            if status == STATUS_DATA:
                return status, body, self._parse_data(body)
            return status, body, None  # OKAY

    # ── basic commands ────────────────────────────────────────────────────

    def cmd(self, text, *, read_size: int = FB_COMMAND_SZ,
            timeout_ms: int = 5000) -> tuple[str, bytes]:
        """Send an ASCII command, read the terminal response.

        Returns (status, payload). status is OKAY or DATA — FAIL raises.
        For DATA responses, the caller is responsible for the follow-up
        transfer. Use `download()` / `setvar()` / etc. for the common
        patterns.
        """
        payload = text if isinstance(text, bytes) else text.encode("ascii")
        self._write(payload, timeout_ms=timeout_ms)
        status, body, _ = self._read_response(
            read_size=read_size, timeout_ms=timeout_ms
        )
        return status, body

    # ── identify + chip info ──────────────────────────────────────────────

    def identify(self, timeout_ms: int = 2000) -> Identify:
        """Return parsed `getvar:identify` (8 bytes)."""
        status, body = self.cmd("getvar:identify", timeout_ms=timeout_ms)
        if status != STATUS_OKAY or len(body.rstrip(b"\x00")) < 4:
            raise AmlogicError(f"identify: bad reply {status} {body!r}")
        # Some firmwares pad with NULs; trust at least the first 8 bytes.
        b = (body + b"\x00" * 8)[:8]
        # Lua uses 1-based; we use 0-based: byte 1 (Lua) == b[0] (here).
        return Identify(
            bytes_=b,
            protocol=b[0],
            minor_ver=b[1],
            mode=b[3],
            pages_map=b[7],
        )

    def getvar_str(self, name: str, *, timeout_ms: int = 5000) -> str:
        """getvar:NAME → trimmed ASCII string."""
        status, body = self.cmd(f"getvar:{name}", timeout_ms=timeout_ms)
        if status != STATUS_OKAY:
            raise AmlogicError(f"getvar:{name}: got {status}")
        return body.split(b"\x00", 1)[0].decode("ascii", errors="replace")

    def getvar_bytes(self, name: str, *, expect_len: Optional[int] = None,
                     timeout_ms: int = 5000) -> bytes:
        """getvar:NAME → raw bytes (some replies are binary, e.g. getchipinfo)."""
        status, body = self.cmd(f"getvar:{name}", read_size=512, timeout_ms=timeout_ms)
        if status != STATUS_OKAY:
            raise AmlogicError(f"getvar:{name}: got {status}")
        if expect_len is not None and len(body.rstrip(b"\x00")) < expect_len:
            raise AmlogicError(f"getvar:{name} too short ({len(body)} < {expect_len})")
        return body

    def getchipinfo(self, page: int) -> bytes:
        """getvar:getchipinfo-N for N in 0..7. Returns up to 64 bytes."""
        if not 0 <= page <= 7:
            raise AmlogicError(f"chipinfo page {page} out of range [0..7]")
        return self.getvar_bytes(f"getchipinfo-{page}", expect_len=4)

    # ── data-transfer primitives ──────────────────────────────────────────

    def download(self, data: bytes, *, timeout_ms: int = 30_000) -> None:
        """fastboot download: announce size, stream data, await OKAY."""
        n = len(data)
        if n == 0:
            raise AmlogicError("download() called with empty data")
        self._write(f"download:{n:08x}".encode("ascii"), timeout_ms=timeout_ms)
        status, body, ack = self._read_response(timeout_ms=timeout_ms)
        if status != STATUS_DATA:
            raise AmlogicError(f"download: expected DATA, got {status} {body!r}")
        if ack.size != n:
            raise AmlogicError(
                f"download: device wants {ack.size:#x} bytes but host has {n:#x}"
            )
        self._write(bytes(data), timeout_ms=timeout_ms)
        if n % self._eps.max_packet_out == 0:
            # USB 2.0 short-packet convention: zero-length packet marks end
            # when total transfer is a multiple of max-packet.
            try:
                self._dev.write(self._eps.out, b"", timeout=timeout_ms)
            except usb.core.USBError:
                pass
        status, body, _ = self._read_response(timeout_ms=timeout_ms)
        if status != STATUS_OKAY:
            raise AmlogicError(f"download: expected OKAY, got {status} {body!r}")

    def firstsect(self, data_buf: bytes, *, timeout_ms: int = 5000) -> int:
        """S6-only handshake — device asks for N bytes via DATA<size>.

        Send `firstsect`; device replies DATA<size>; we upload `size`
        bytes from the front of `data_buf`; device replies OKAY.

        Returns the size the device asked for (typically 0x400 on S6).
        """
        if len(data_buf) < 1:
            raise AmlogicError("firstsect: empty buffer")
        self._write(b"firstsect", timeout_ms=timeout_ms)
        status, body, ack = self._read_response(timeout_ms=timeout_ms)
        if status != STATUS_DATA:
            raise AmlogicError(f"firstsect: expected DATA, got {status} {body!r}")
        if ack.size > len(data_buf):
            raise AmlogicError(
                f"firstsect: device wants {ack.size} bytes, have {len(data_buf)}"
            )
        self._write(bytes(data_buf[:ack.size]), timeout_ms=timeout_ms)
        status, body, _ = self._read_response(timeout_ms=timeout_ms)
        if status != STATUS_OKAY:
            raise AmlogicError(f"firstsect: expected OKAY, got {status} {body!r}")
        return ack.size

    def setvar(self, name: str, value: int, *, timeout_ms: int = 5000) -> None:
        """setvar:NAME — uploads 4-byte LE u32 value.

        Wire flow:
          host → "setvar:NAME"
          dev  → DATA00000004
          host → <value as 4 bytes LE>
          dev  → OKAY
        """
        self._write(f"setvar:{name}".encode("ascii"), timeout_ms=timeout_ms)
        status, body, ack = self._read_response(timeout_ms=timeout_ms)
        if status != STATUS_DATA:
            raise AmlogicError(f"setvar:{name}: expected DATA, got {status} {body!r}")
        if ack.size != 4:
            raise AmlogicError(f"setvar:{name}: device wants {ack.size} bytes (expected 4)")
        self._write(value.to_bytes(4, "little"), timeout_ms=timeout_ms)
        status, body, _ = self._read_response(timeout_ms=timeout_ms)
        if status != STATUS_OKAY:
            raise AmlogicError(f"setvar:{name}: expected OKAY, got {status} {body!r}")

    def oem(self, cmd: str, *, timeout_ms: int = 30_000) -> bytes:
        """oem CMD — generic OEM command channel.

        Returns the response payload (after OKAY). Useful for
        `oem env_get NAME`, `oem get_bootloaderversion`, `oem sheader_need`
        etc. — anything that returns a value but doesn't require a data
        transfer phase.
        """
        status, body = self.cmd(f"oem {cmd}", timeout_ms=timeout_ms)
        if status != STATUS_OKAY:
            raise AmlogicError(f"oem {cmd}: got {status}")
        return body

    # ── CBW (device-driven burn loop) ─────────────────────────────────────

    def get_cbw(self, *, timeout_ms: int = 5000) -> Optional[CBW]:
        """getvar:cbw — fetch the Control Block Word.

        Used in spl/bl2e/tpl stages to let the device tell the host what
        bytes to upload next. Returns None on hard FAIL (so the caller can
        decide whether to abort), otherwise a parsed CBW.
        """
        status, body = self.cmd("getvar:cbw", read_size=256, timeout_ms=timeout_ms)
        if status != STATUS_OKAY:
            raise AmlogicError(f"get_cbw: expected OKAY, got {status}")
        # The CBW is whatever follows the 4-byte OKAY prefix — we already
        # stripped that. Strip trailing NUL padding.
        cbw = body.rstrip(b"\x00")
        if len(cbw) < 19:
            raise AmlogicError(f"get_cbw: too short ({len(cbw)} bytes): {body!r}")
        if cbw[:4] != b"AMLC":
            raise AmlogicError(f"get_cbw: missing AMLC magic: {cbw[:4]!r}")
        sequence    = int.from_bytes(cbw[4:8], "little")
        xfer_size   = int.from_bytes(cbw[8:12], "little")
        start_off   = int.from_bytes(cbw[12:16], "little")
        flags       = cbw[16]
        direction   = cbw[17]
        request_typ = cbw[18]
        if request_typ > 1 and request_typ != 0xFF:
            raise AmlogicError(f"get_cbw: device error code {request_typ}")
        return CBW(
            sequence=sequence,
            transfer_size=xfer_size,
            start_offset=start_off,
            need_checksum=(flags & 0x01) == 0,
            is_upload=(direction & 0x80) != 0,
            the_end=request_typ == 1,
            wait_continue=request_typ == 0xFF,
            raw=cbw,
        )

    # ── observability ─────────────────────────────────────────────────────

    def describe(self) -> str:
        d = self._dev
        try:
            ident = self.identify(timeout_ms=1500)
            stage = ident.stage
        except Exception:
            stage = "?"
        return (
            f"Amlogic DNL  vid={d.idVendor:#06x} pid={d.idProduct:#06x}  "
            f"bus={d.bus} addr={d.address}  stage={stage}"
        )
