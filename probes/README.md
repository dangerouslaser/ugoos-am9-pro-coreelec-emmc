# probes/ — diagnostic & bring-up scripts

One-off scripts written during reverse-engineering of the Amlogic S6 ADNL/DNL
USB protocol. They're not part of the user-facing burner workflow — kept here
as reference for protocol exploration, debugging, or extending the burner.

All probes import the burner libraries (`aml_dnl_proto`, `aml_dnl_ops`,
`aml_dnl_flows`) from the project root.

| Script | Purpose |
|--------|---------|
| `aml-dnl-probe.py` | Initial device-IO sanity check: open USB endpoint, identify, drain INFO messages |
| `aml-dnl-probe-stage14.py` | Same but tries TPL-stage (mode 14) commands — used to map which commands the S6 secure-boot whitelist allows |
| `aml-dnl-ddr-test.py` | Loads a DDR firmware image via `download`/`firstsect` to verify the SECT_BUF=0x1000 seek-and-skip behavior |
| `aml-dnl-download-test.py` | Smallest possible `download` command exercise — used to discover the data-out wire format (`OUT 0x10 0x0`, `DATAOUT0x10:0x0`, etc.) |
| `aml-dnl-firstsect-test.py` | First-sector handoff after DDR firmware load — exercises the 4 KB skip between firstsect and download |
| `aml-dnl-reboot-test.py` | Issues `reboot romusb` and verifies the device re-enumerates back into DNL mode |
| `aml-dnl-self-flash.py` | Generic self-flash test — write any partition's CURRENT bytes back over itself using `device-backups/factory-2.1.0/dumps/*.bin` |
| `aml-dnl-self-flash-dtbo.py` | The first successful flash test (dtbo_a) — kept as a known-working reference invocation |
| `analyze-burn-capture.py` | Parses USB pcap captures of Windows USB Burning Tool sessions to extract the request/response sequence |

## Adding a new probe

Copy any existing probe as a template; the sys.path line resolves the project
root for library imports:

```python
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aml_dnl_proto import AmlogicDevice, AmlogicError
```
