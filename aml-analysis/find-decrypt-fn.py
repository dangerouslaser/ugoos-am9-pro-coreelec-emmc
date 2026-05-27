#!/usr/bin/env python3
"""Locate lua_item_decrypt_and_load by walking string xrefs, then
disassemble the function body to inspect what it actually does."""
import sys, os, struct
import pefile, capstone

DLL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aml_usb_flow.dll")
pe = pefile.PE(DLL, fast_load=False)

base = pe.OPTIONAL_HEADER.ImageBase  # 0x10000000

# Find the string "lua_item_decrypt_and_load" in the file
with open(DLL, "rb") as f:
    raw = f.read()
target = b"lua_item_decrypt_and_load\x00"
str_raw_off = raw.find(target)
print(f"string 'lua_item_decrypt_and_load' at raw offset: {hex(str_raw_off)}")
str_rva = pe.get_rva_from_offset(str_raw_off)
str_va = base + str_rva
print(f"  RVA: {hex(str_rva)}")
print(f"  VA:  {hex(str_va)}")

# Look up xrefs (push immediate followed by call) in .text
text_section = None
for s in pe.sections:
    if s.Name.startswith(b".text"):
        text_section = s
        break
text_data = text_section.get_data()
text_va = base + text_section.VirtualAddress

needle = struct.pack("<I", str_va)
xrefs = []
for i in range(0, len(text_data) - 4):
    if text_data[i:i+4] == needle:
        xrefs.append((text_va + i, i))
print(f"  xrefs in .text:    {[hex(va) for va, _ in xrefs]}")

# Disassemble around each xref
md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
md.detail = True

print()
print(f"=== context around first xref ===")
if xrefs:
    va, off = xrefs[0]
    # Show 60 bytes before and 60 after
    start = max(0, off - 60)
    end = min(len(text_data), off + 60)
    for ins in md.disasm(text_data[start:end], text_va + start):
        print(f"  {ins.address:#010x}  {ins.mnemonic:<8} {ins.op_str}")

# Find the function that contains this xref by walking backwards to find
# a `push ebp; mov ebp, esp` or similar function prologue.
print()
print(f"=== find function start by backtracking from {hex(xrefs[0][0]) if xrefs else 'N/A'} ===")
if xrefs:
    va_target, off_target = xrefs[0]
    # Look backwards for 0x55 0x89 0xe5 (push ebp; mov ebp, esp)
    fn_start = None
    # Try a broader backtrack — function could be larger
    for back in range(off_target, max(0, off_target - 0x2000), -1):
        # MSVC functions: push ebp; mov ebp, esp  OR  sub esp, X; push ebx
        if text_data[back:back+3] == b"\x55\x8b\xec":  # push ebp; mov ebp, esp (alt encoding)
            fn_start = text_va + back
            print(f"  function prologue (push ebp; mov ebp, esp [alt]) at VA {hex(fn_start)} off {hex(back)}")
            break
        if text_data[back:back+3] == b"\x55\x89\xe5":  # gcc-style
            fn_start = text_va + back
            print(f"  function prologue (gcc-style) at VA {hex(fn_start)} off {hex(back)}")
            break
    if fn_start:
        start_off = fn_start - text_va
        max_bytes = 0x1000
        end_off = min(start_off + max_bytes, len(text_data))
        for ins in md.disasm(text_data[start_off:end_off], fn_start):
            print(f"  {ins.address:#010x}  {ins.mnemonic:<8} {ins.op_str}")
            # End at ret only if past target
            if ins.mnemonic in ("ret", "retn") and ins.address > va_target:
                break

# Also disasm the CALL TARGET — 0x10002250 — that's called right before
# the error path, and 0x10009a20 — called with format string after.
print()
print(f"=== call target 0x10002250 (called from error path) ===")
target_va = 0x10002250
target_off = target_va - text_va
for ins in list(md.disasm(text_data[target_off:target_off+200], target_va))[:30]:
    print(f"  {ins.address:#010x}  {ins.mnemonic:<8} {ins.op_str}")
