# Static analysis — Amlogic USB Burning Tool V3.3.3

Scripts to reverse-engineer the proprietary Amlogic USB burner.
The DLLs and decrypted Lua scripts are NOT in git (Amlogic copyright);
the analysis tools ARE in git so this work is reproducible.

## To reproduce

You need the installed `USB_Burning_Tool V3` from a Windows machine
that ran the official installer (`V3_setup_V3.3.3.exe`). Copy these
files from `C:\Amlogic\Aml_Burn_Tool\V3\` (and its `usb_flow/`
subdirectory) into this directory:

```
Aml_Burn_Tool.exe
AmlImagePack.dll
AmlImageCheck.dll
AmlDevManage.dll
aml_usb_flow.dll
libamlfastboot.dll
libamllibusb.dll
libaulextend.dll
libusb0.dll
usb_flow.aml      # AES-encrypted Lua burn-flow container
key_flow.aml      # AES-encrypted Lua keystore-flow container
```

Then:

```bash
.venv/bin/pip install pefile capstone cryptography
.venv/bin/python aml-analysis/dll-inspect.py
.venv/bin/python aml-analysis/find-decrypt-fn.py
.venv/bin/python aml-analysis/extract-usb-flow.py
```

## Scripts

- **`dll-inspect.py`** — PE header, exports, and SoC table inspection
  of `aml_usb_flow.dll`. First-pass reconnaissance.
- **`find-decrypt-fn.py`** — locates `lua_item_decrypt_and_load` in
  the DLL by string xref + disassembles its body. This is how we found
  the AES key derivation and the CRC verification.
- **`extract-usb-flow.py`** — decrypts `usb_flow.aml` and dumps each
  Lua script to `usb_flow_decrypted/`. This is the payoff.

## What we learned

The encryption is **AES-128-CBC** with key + IV embedded in
`aml_usb_flow.dll`:

```
KEY = 2c7d161728aed2a6abf7158809cf503c   (at DLL VA 0x1000b478)
IV  = 000102030505060708090a0b0c0d0e0f   (at DLL VA 0x1000b468)
```

The encrypted container at file offset 0x40 decrypts to:
- 12 × 64-byte item descriptors (magic 0x27051956, size, offset, name)
- 12 Lua source files (all plaintext after decryption)

The Lua source reveals that the so-called "blob transformation" we
measured on the wire is just a single `item.seek(4096)` between the
`firstsect` and `download:` commands — see `usb_flow_dnl.lua`'s
`romcode_flow` function. Cross-referenced and applied in
`aml_dnl_ops.py::load_ddr_firmware` in the project root.
