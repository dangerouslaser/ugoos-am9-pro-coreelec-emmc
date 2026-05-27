# img-tools/ — AML file-format inspection tools

Standalone CLIs for inspecting and (where applicable) repacking Amlogic-specific
file formats. None of these tools talk to a device — they all operate on local
files. None of them depend on the `aml_dnl_*` or `aml_img` libraries in the
project root.

| Tool | Format / partition | What it does |
|------|--------------------|--------------|
| [`aml-img-tool.py`](aml-img-tool.py) | `AML_PACK_v2` `.img` (the format Ugoos ships for factory restores) | Inspect, unpack, and rebuild USB Burning Tool `.img` archives. Round-trip verified byte-identical against `AM9PRO_2.0.9.img`. |
| [`aml-bootloader-tool.py`](aml-bootloader-tool.py) | `@AMLBOOT` manifest in `bootloader_a` (p7) | Decode the manifest and unpack sections. Contents are encrypted at rest, so the unpacker yields encrypted blobs — useful for layout/version inspection, not for disassembly. |
| [`aml-keystore-tool.py`](aml-keystore-tool.py) | `AMLNORMAL` keystore in `reserved` (p1) | Dump header, list every populated slot with name/attribute/type/value/hash, and extract each slot's raw value to its own file. Used by `ce-emmc-install.sh` to read the device's ETH MAC + serial for the identity cross-check. |
| [`aml-logo-tool.py`](aml-logo-tool.py) | `AML_RES!` boot-logo container in `logo` (p10) | Unpack to NN_name.bmp files; repack from a directory of BMPs. Used by `ce-emmc-install.sh --restore-logo PATH`. |

The CE installer (`ce-emmc-install.sh`) looks these up via a multi-candidate
search — `${SCRIPT_DIR}/`, `${SCRIPT_DIR}/img-tools/`, then
`${SCRIPT_DIR}/../img-tools/`. Works whether you run from the repo root or
from `/storage` after copying everything alongside the installer.
