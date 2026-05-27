# scripts/ — small standalone utilities

| Script | Purpose |
|--------|---------|
| `make-cfgload.py` | Wraps a U-Boot script (plain text) in a legacy `mkimage -A arm64 -T script -O linux -C none` uImage container with correct CRC32 headers. Equivalent to running `mkimage -d input.txt -T script ... output`, but pure-Python with no external dependency. Useful when editing `/flash/cfgload` by hand or generating a replacement from scratch on a system without `mkimage`. |

## make-cfgload.py — usage

```bash
python3 scripts/make-cfgload.py <input-script.txt> <output-uimage>
```

The input is plain text — your U-Boot script (the same content you'd write
into the body of a `mkimage -T script` invocation). Output is a 64-byte
legacy uImage header followed by `[4B BE script-length][4B zeros][script text]`.
Both header and data CRC32s are computed correctly so U-Boot accepts the
result.

The format was verified by round-tripping CoreELEC's stock `cfgload`
byte-for-byte.
