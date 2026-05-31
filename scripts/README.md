# scripts/ — small standalone utilities

| Script | Purpose |
|--------|---------|
| `make-cfgload.py` | Wraps a U-Boot script (plain text) in a legacy `mkimage -A arm64 -T script -O linux -C none` uImage container with correct CRC32 headers. Equivalent to running `mkimage -d input.txt -T script ... output`, but pure-Python with no external dependency. Useful when editing `/flash/cfgload` by hand or generating a replacement from scratch on a system without `mkimage`. |
| `emmc-benchmark.py` | Sequential + random R/W benchmark for raw block devices. Read tests go directly against the block device (read-only, safe on `/dev/mmcblk0` or `/dev/sda`); write tests go through a temp file in `--workdir`. Uses `O_DIRECT` to bypass the page cache, drops caches between tests, reports MB/s and IOPS. |
| `artwork-benchmark.py` | Workload-shaped benchmark modelling Kodi skin-widget artwork access: populates a sharded directory with files in realistic Kodi thumbnail / poster / fanart / 4K-fanart size distribution, then times random whole-file reads with dropped caches. Reports throughput, files/sec, and p50/p90/p95/p99/max open-to-close latency. The honest benchmark for "how fast does my widget render." |
| `multi-angle-benchmark.py` | Multi-scenario probe (4 GB sustained reads, 4 GB sustained writes, 8 GB SLC-cache stress write, 8-thread concurrent random reads, single-large-file streaming, 10-second mixed read+write). Reveals scenarios where simple `dd` or O_DIRECT benchmarks understate one storage type — particularly eMMC's SLC cache exhaustion and mixed-IO collapse. |

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
