"""Amlogic `.img` (AML_PACK_v2) file-format parser.

Standalone module with no USB / device dependencies — safe to import from
in-device tooling that doesn't have pyusb available.

Verified empirically against `AM9PRO_2.0.9.img` and `AM9PRO_2.1.0.img`;
same layout as the upstream `aml_image_v2_packer`. See `aml-img-tool.py`
for the full format docs.
"""
from __future__ import annotations

import mmap
import os
import struct
from dataclasses import dataclass
from typing import Optional


_IMG_MAGIC_V2 = 0x27B51956
_ITEM_TABLE_OFFSET = 0x40
_ITEM_DESC_SIZE = 0x240
_ITEM_OFFSET_OFF = 0x10
_ITEM_SIZE_OFF = 0x18
_ITEM_TYPE_OFF = 0x20
_ITEM_TYPE_LEN = 32
_ITEM_NAME_OFF = 0x120
_ITEM_NAME_LEN = 32
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
        # mmap rather than read() — a Ugoos firmware .img is ~1.6 GB; on a
        # 4 GB-RAM AM9 Pro running CE+Kodi we want page-on-demand, not a
        # full materialise.
        self._fd = os.open(path, os.O_RDONLY)
        size = os.fstat(self._fd).st_size
        self._data = mmap.mmap(self._fd, size, prot=mmap.PROT_READ)
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

    def read_at(self, item: ImgItem, offset: int, size: int) -> bytes:
        """Read `size` bytes from item starting at `offset` within the item."""
        if offset < 0 or offset + size > item.size:
            raise ValueError(f"read out of range for item {item.name}")
        return self._data[item.offset + offset: item.offset + offset + size]


# ── Android sparse image format ──────────────────────────────────────────────
# Used by `super` (and some other big partitions) inside an Amlogic .img.
# Reference: AOSP system/core/libsparse/sparse_format.h

SPARSE_MAGIC = 0xED26FF3A
CHUNK_TYPE_RAW       = 0xCAC1
CHUNK_TYPE_FILL      = 0xCAC2
CHUNK_TYPE_DONT_CARE = 0xCAC3
CHUNK_TYPE_CRC32     = 0xCAC4


def is_sparse(blob: bytes | memoryview) -> bool:
    """Cheap sniff — magic + minimum-header-size check."""
    if len(blob) < 28:
        return False
    return struct.unpack_from("<I", blob, 0)[0] == SPARSE_MAGIC


def unpack_sparse_size(blob: bytes | memoryview) -> int:
    """Return the unpacked size in bytes without decoding the data."""
    magic, maj, _mi, fhsz, chsz, blk_sz, total_blks, _total_chunks, _csum = \
        struct.unpack_from("<IHHHHIIII", blob, 0)
    if magic != SPARSE_MAGIC:
        raise ValueError(f"not a sparse image (magic={magic:#x})")
    if maj != 1 or fhsz != 28 or chsz != 12:
        raise ValueError(f"unsupported sparse layout maj={maj} fhsz={fhsz} chsz={chsz}")
    return total_blks * blk_sz


def iter_sparse_chunks(blob: bytes | memoryview):
    """Yield (kind, payload_or_size, blk_sz) for each chunk.

    For RAW   : yields ('raw', bytes,    blk_sz)   — write `bytes` (length is chunk_blocks * blk_sz)
    For FILL  : yields ('fill', (fill_word: bytes[4], n_bytes), blk_sz)
    For DONT_CARE: yields ('skip', n_bytes, blk_sz)
    CRC32 chunks are silently consumed (informational only).
    """
    magic, maj, _mi, fhsz, chsz, blk_sz, _total_blks, total_chunks, _csum = \
        struct.unpack_from("<IHHHHIIII", blob, 0)
    if magic != SPARSE_MAGIC:
        raise ValueError(f"not a sparse image (magic={magic:#x})")
    pos = 28
    for _ in range(total_chunks):
        chunk_type, _resv, chunk_blocks, total_sz = struct.unpack_from(
            "<HHII", blob, pos)
        pos += 12
        payload_sz = total_sz - 12
        n_bytes = chunk_blocks * blk_sz
        if chunk_type == CHUNK_TYPE_RAW:
            yield ("raw", blob[pos: pos + payload_sz], blk_sz)
            pos += payload_sz
        elif chunk_type == CHUNK_TYPE_FILL:
            fill = bytes(blob[pos: pos + 4])
            yield ("fill", (fill, n_bytes), blk_sz)
            pos += 4
        elif chunk_type == CHUNK_TYPE_DONT_CARE:
            yield ("skip", n_bytes, blk_sz)
        elif chunk_type == CHUNK_TYPE_CRC32:
            pos += 4  # consume + ignore
        else:
            raise ValueError(f"unknown sparse chunk type {chunk_type:#x}")
