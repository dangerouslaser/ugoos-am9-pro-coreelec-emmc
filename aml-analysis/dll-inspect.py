#!/usr/bin/env python3
"""Static analysis pass over aml_usb_flow.dll — locate the encryption
entry point and the AES key, plus identify the calling sequence around it."""
import sys, struct, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pefile
import capstone

DLL = os.path.join(os.path.dirname(__file__), "aml_usb_flow.dll")

pe = pefile.PE(DLL, fast_load=False)
print(f"=== {os.path.basename(DLL)} ===")
print(f"machine:    {hex(pe.FILE_HEADER.Machine)} (0x14c=i386, 0x8664=x64)")
print(f"image base: {hex(pe.OPTIONAL_HEADER.ImageBase)}")
print(f"entry RVA:  {hex(pe.OPTIONAL_HEADER.AddressOfEntryPoint)}")

print(f"\n=== sections ===")
for s in pe.sections:
    name = s.Name.rstrip(b"\x00").decode()
    print(f"  {name:<8}  vaddr={hex(s.VirtualAddress):>10}  vsize={hex(s.Misc_VirtualSize):>8}  raw={hex(s.PointerToRawData):>8} rawsz={hex(s.SizeOfRawData):>8}")

print(f"\n=== exports ===")
if hasattr(pe, "DIRECTORY_ENTRY_EXPORT"):
    for exp in pe.DIRECTORY_ENTRY_EXPORT.symbols:
        name = exp.name.decode() if exp.name else "<noname>"
        print(f"  RVA={hex(exp.address):>10}  ord={exp.ordinal:>3}  {name}")
else:
    print("  (no exports)")

# Find strings cross-referenced near AES Sbox to identify decrypt function
# Sbox we already found at file offset 0xa229. Convert to RVA.
print(f"\n=== convert AES Sbox @ raw 0xa229 to RVA ===")
sbox_raw = 0xa229
sbox_rva = pe.get_rva_from_offset(sbox_raw)
print(f"  Sbox RVA = {hex(sbox_rva)}")
sbox_va = pe.OPTIONAL_HEADER.ImageBase + sbox_rva
print(f"  Sbox VA  = {hex(sbox_va)}")

# Find references to this Sbox in the .text section
print(f"\n=== searching for refs to Sbox VA in .text ===")
text = None
for s in pe.sections:
    if s.Name.startswith(b".text"):
        text = s
        break
text_data = text.get_data()
text_base = pe.OPTIONAL_HEADER.ImageBase + text.VirtualAddress

# Search for any 4-byte little-endian value that points near the Sbox area
sbox_va_bytes = struct.pack("<I", sbox_va)
refs = []
for i in range(0, len(text_data) - 4):
    if text_data[i:i+4] == sbox_va_bytes:
        refs.append(text_base + i)
# Also search for VAs close to Sbox (within +0x500 — AES tables nearby)
for off in range(0, 0x500, 4):
    candidate = sbox_va + off
    cb = struct.pack("<I", candidate)
    for i in range(0, len(text_data) - 4):
        if text_data[i:i+4] == cb:
            refs.append((text_base + i, candidate))

print(f"  exact Sbox refs:    {[hex(r) for r in refs if isinstance(r,int)][:10]}")
print(f"  near-Sbox refs:     {[(hex(r[0]), hex(r[1])) for r in refs if isinstance(r,tuple)][:10]}")
