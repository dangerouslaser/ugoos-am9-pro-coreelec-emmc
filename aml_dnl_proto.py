"""Layer 1 — Amlogic DNL wire protocol over USB.

Knows commands and responses. Doesn't know burns, partitions, or .img files.
The protocol is text-mode fastboot with Amlogic OEM extensions; see
`dnl-protocol-from-capture.md` for the full decoded vocabulary.

Typical use:

    with AmlogicDevice.find() as dev:
        status, payload = dev.cmd("getvar:identify")
        assert status == "OKAY"
        print(payload.decode())
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import usb.core
import usb.util

VENDOR_ID = 0x1B8E
# c003 = fresh BootROM ("GX-CHIP"), c004 = S6 ADNL (already in U-Boot).
# The same protocol works against both — the device just exposes different
# command subsets per stage.
PRODUCT_IDS = (0xC003, 0xC004)

# Fastboot status prefixes (first 4 bytes of every IN response).
STATUS_OKAY = "OKAY"
STATUS_DATA = "DATA"
STATUS_FAIL = "FAIL"
STATUS_INFO = "INFO"
STATUSES = (STATUS_OKAY, STATUS_DATA, STATUS_FAIL, STATUS_INFO)

# Default bulk-transfer chunk size matches what the official tool uses
# (`download:00004000`) for the bulk of partition writes.
DEFAULT_CHUNK = 0x4000


class AmlogicError(Exception):
    """Anything the device or transport said no to."""


@dataclass
class _Endpoints:
    out: int  # bulk OUT address
    in_: int  # bulk IN address
    max_packet_out: int
    max_packet_in: int


class AmlogicDevice:
    """A single Amlogic device in DNL mode.

    Use AmlogicDevice.find() to construct; that handles enumeration, interface
    claiming, and endpoint discovery. The instance is also a context manager
    so resources get released cleanly.
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
                    f"no Amlogic DNL device found (VID=0x{VENDOR_ID:04x}, "
                    f"PID in {[hex(p) for p in PRODUCT_IDS]})"
                )
            time.sleep(0.1)

    @classmethod
    def _open(cls, dev: usb.core.Device, pid: int) -> "AmlogicDevice":
        # On Linux, the kernel sometimes auto-claims new USB devices (or
        # has stale grabs from a previous detach). libusb refuses
        # set_configuration() if anyone else holds the interface, so
        # detach proactively.
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except (NotImplementedError, usb.core.USBError):
            # macOS doesn't implement is_kernel_driver_active; that's fine
            # because macOS doesn't auto-claim these devices either.
            pass
        try:
            dev.set_configuration()
        except usb.core.USBError as e:
            # Configuration may already be set — fall through and try to claim.
            if e.errno not in (16,):  # 16 = EBUSY
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
            tt = usb.util.endpoint_type(ep.bmAttributes)
            dirn = usb.util.endpoint_direction(ep.bEndpointAddress)
            if tt == usb.util.ENDPOINT_TYPE_BULK:
                if dirn == usb.util.ENDPOINT_OUT:
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
    # reports success on the partial transfer. Larger payloads must be
    # split into multiple pyusb.write() calls (each one URB) below the
    # cap. The Amlogic device counts cumulative bytes per `download:`
    # command, so multiple URBs that sum to the announced size work.
    _WRITE_MAX_URB = 64 * 1024

    def _write(self, data: bytes, timeout_ms: int = 5000) -> int:
        total = 0
        view = memoryview(data)
        while total < len(data):
            end = min(total + self._WRITE_MAX_URB, len(data))
            chunk = view[total:end].tobytes()
            n = self._dev.write(self._eps.out, chunk, timeout=timeout_ms)
            if n == 0:
                raise AmlogicError(
                    f"bulk OUT stalled at {total} of {len(data)} bytes"
                )
            total += int(n)
        return total

    def _read(self, size: int, timeout_ms: int = 5000) -> bytes:
        return bytes(self._dev.read(self._eps.in_, size, timeout=timeout_ms))

    # ── protocol ──────────────────────────────────────────────────────────

    def cmd(self, text: str, *, read_size: int = 4096,
            timeout_ms: int = 5000) -> tuple[str, bytes]:
        """Send an ASCII command, read one fastboot response.

        Returns (status, payload) where status is one of OKAY/FAIL/DATA/INFO
        and payload is whatever bytes followed (may be empty).

        Raises AmlogicError if the device returns FAIL or sends an
        unrecognised status prefix.
        """
        if isinstance(text, bytes):
            payload = text
        else:
            payload = text.encode("ascii")
        self._write(payload, timeout_ms=timeout_ms)
        reply = self._read(read_size, timeout_ms=timeout_ms)
        if len(reply) < 4:
            raise AmlogicError(
                f"short reply to {text!r}: {reply!r}"
            )
        status = reply[:4].decode("ascii", errors="replace")
        body = reply[4:]
        if status not in STATUSES:
            raise AmlogicError(
                f"unknown status {status!r} in reply to {text!r}: {reply!r}"
            )
        if status == STATUS_FAIL:
            raise AmlogicError(
                f"device returned FAIL on {text!r}: {body.decode('ascii', errors='replace')}"
            )
        return status, body

    def download(self, data: bytes, *,
                 chunk_size: int = DEFAULT_CHUNK,
                 timeout_ms: int = 30_000) -> None:
        """Stream `data` to the device using the fastboot download flow.

        Flow (per the capture-decoded protocol):
          host → "download:HHHHHHHH"           (8 hex chars = size)
          dev  → "DATA<HHHHHHHH>"              (must echo same size)
          host → <raw bytes, in chunk_size USB transfers>
          dev  → "OKAY"

        Does NOT issue `boot` — caller decides what happens to the staged
        data. Raises AmlogicError on any size mismatch, FAIL, or unexpected
        status.
        """
        n = len(data)
        if n == 0:
            raise AmlogicError("download() called with empty data")
        size_hex = f"{n:08x}"
        status, body = self.cmd(f"download:{size_hex}", timeout_ms=timeout_ms)
        if status != STATUS_DATA:
            raise AmlogicError(
                f"expected DATA in reply to download:{size_hex}, got {status} {body!r}"
            )
        ack_size_str = body[:8].decode("ascii", errors="replace")
        try:
            ack_size = int(ack_size_str, 16)
        except ValueError:
            raise AmlogicError(f"DATA reply did not contain a hex size: {body!r}")
        if ack_size != n:
            raise AmlogicError(
                f"device acked {ack_size:#x} bytes but host asked for {n:#x}"
            )
        # Stream the bytes. libusb-1.0 on Linux caps each URB at ~240KB
        # and pyusb's sync write doesn't expose async batching, so our
        # _write() chunks below the cap. The Amlogic device accepts
        # multiple URBs as long as they sum to the announced size.
        del chunk_size
        self._write(bytes(data), timeout_ms=timeout_ms)
        # If the total transfer size is a multiple of the endpoint's
        # max-packet, USB 2.0 requires a zero-length packet to signal
        # end-of-transfer. Without it some bulk peers (including the
        # Amlogic BootROM here) keep waiting and the subsequent ACK
        # comes back but the device doesn't actually consume the data.
        if len(data) % self._eps.max_packet_out == 0:
            try:
                self._dev.write(self._eps.out, b"", timeout=timeout_ms)
            except usb.core.USBError:
                pass
        # Final OKAY.
        final = self._read(64, timeout_ms=timeout_ms)
        if len(final) < 4:
            raise AmlogicError(f"no final OKAY after download (got {final!r})")
        fstatus = final[:4].decode("ascii", errors="replace")
        if fstatus == STATUS_FAIL:
            raise AmlogicError(
                f"device FAILed after download: {final[4:].decode('ascii', errors='replace')}"
            )
        if fstatus != STATUS_OKAY:
            raise AmlogicError(f"expected OKAY after download, got {final!r}")

    def getvar(self, name: str) -> bytes:
        """Convenience wrapper for `getvar:<name>` — returns OKAY payload.

        Most getvars reply with `OKAY<value>` directly. A few (chipinfo) reply
        with `DATA<size>` first and then deliver the value on a subsequent
        read; that pattern is handled in higher layers, not here.
        """
        status, body = self.cmd(f"getvar:{name}")
        if status != STATUS_OKAY:
            raise AmlogicError(
                f"getvar:{name} returned {status} (expected OKAY): {body!r}"
            )
        return body

    # ── observability ─────────────────────────────────────────────────────

    def describe(self) -> str:
        d = self._dev
        return (
            f"Amlogic DNL  vid={d.idVendor:#06x} pid={d.idProduct:#06x}  "
            f"bus={d.bus} addr={d.address}  "
            f"ep_out=0x{self._eps.out:02x}/{self._eps.max_packet_out}B "
            f"ep_in=0x{self._eps.in_:02x}/{self._eps.max_packet_in}B  "
            f"stage={'BootROM' if self.pid == 0xC003 else 'ADNL (U-Boot)'}"
        )
