# Installer design notes — why `ce-emmc-install.sh` does what it does

The reasoning behind the non-obvious parts of the eMMC installer and restore
script. User-facing instructions are in [`../INSTALL.md`](../INSTALL.md).

## Why CE_FLASH is at p28, not p27

U-Boot's `cfgloademmc` command scans eMMC partitions 1–31 looking for a FAT filesystem containing a `cfgload` script. It finds CE_FLASH by content, not by partition number. CE_FLASH at p28 works identically to p27.

The original install approach placed CE_FLASH at p27 (replacing `super`). The current approach keeps `super` at p27 and places CE_FLASH at p28 (replacing `rsv`). The boot process is unchanged.

## How the cfgload rebuild works

`cfgload` is a compiled U-Boot script in mkimage format with two CRC32 fields in its header (one over the header itself, one over the data). Editing it with a text editor or `sed` changes the content but leaves the old CRCs in place — U-Boot verifies them on load and silently rejects the script, failing without any obvious error.

The installer reads the stock cfgload from the boot media, performs the `FOLDER=/dev/CE_STORAGE` → `LABEL=CE_STORAGE` substitution in the inner script body, then rebuilds the mkimage container with correct CRCs. It does this in Python because `mkimage` is not installed on CoreELEC — the legacy-script format is straightforward to pack with `struct` and `zlib.crc32`. The result is byte-identical (modulo timestamp and header CRC) to what `mkimage -A arm64 -T script -O linux -C none -d <script> <out>` produces. The same packing logic is exposed as a standalone utility in [`scripts/make-cfgload.py`](../scripts/make-cfgload.py).

The rebuild step is idempotent: running it on an already-patched cfgload is a no-op.

## Why mount-storage.sh + nofsck is also installed

The cfgload patch above is one half of the story. The other half: **CoreELEC's nightly updater overwrites `cfgload` on every update** — `/usr/share/bootloader/update.sh` unconditionally does `cp -p /usr/share/bootloader/${DEVICE_CFGLOAD} /flash/cfgload`, reverting the rebuild. Without a second layer of defense, every nightly update would break eMMC boot until the user manually re-ran the patch.

CoreELEC's `/init` already supports a post-update hook at `/flash/user-update.sh`, which runs after `update_bootloader` returns and before `do_reboot`. An earlier version of this installer used that hook to re-apply the cfgload patch. **It does not work**: the hook runs in the initramfs context where only `busybox`, `sh`, and `splash-image` are present — Python isn't available, so the hook crashes immediately and the device bootloops.

The working solution: install two CE-supported customization files that live on `/flash` as user files (never touched by the updater):

- **`/flash/mount-storage.sh`** — sourced by `/init`'s `mount_storage()` function instead of the default `mount_part "$disk" "/storage"`. It runs `mount -t ext4 -o rw,noatime LABEL=CE_STORAGE /storage`, completely bypassing the broken `disk=FOLDER=/dev/CE_STORAGE` value in `bootargs`.
- **`nofsck` in `coreelec=` config.ini** — appended via `setenv bootargs`, suppresses CE's fsck retry loop on the bogus `/dev/CE_STORAGE` node.

The result: even if (when) a future CE nightly reverts cfgload back to its stock `FOLDER=/dev/CE_STORAGE` form, `mount-storage.sh` rescues the mount and the device keeps booting. The cfgload patch becomes a nice-to-have rather than survival-critical.

This is the same outcome `ceemmc` would produce natively if it supported this board — `LABEL=`-based mounting through the standard `mount_part` path with no hook scripts needed. We can't get there without upstream support, so we ship the hook + nofsck as a stable workaround.

## Why the installer clears the Amlogic MPT

[Issue #8](https://github.com/dangerouslaser/ugoos-am9-pro-coreelec-emmc/issues/8): an AM9 Pro on FW 2.2.0 finished the install cleanly, booted fine from the USB stick, and hung at the logo from the eMMC. U-Boot env, `cfgload` CRCs and the CE_FLASH contents were all identical to a working box. The difference was in what the kernel saw. Booted from USB on that box:

```
parted (on-disk GPT)            lsblk (kernel)
28  CE_FLASH    512 MiB         rsv        64M
29  CE_STORAGE  55299 MiB       userdata   54.4G
```

Amlogic keeps its own partition table, the "MPT", at eMMC byte `0x2400000` — offset 0 of `reserved` (p1). CoreELEC's kernel checks for it right after the normal GPT scan (`drivers/mmc/core/block.c`, `mmc_validate_mpt_partition`): if the first sector starts with `MPT\0`, `aml_emmc_partition_ops` (`common_drivers/drivers/mmc/host/mmc_partitions.c`) re-registers every eMMC partition from that table — names, offsets, sizes — and creates `/proc/inand`. With no MPT the GPT partitions stand. The installer only ever edited the GPT, so on a box with an MPT the kernel kept the Android `rsv`/`userdata` geometry. `boot=LABEL=CE_FLASH` found the FAT label on a 64 MiB device whose `SYSTEM` file lies beyond its end, and nothing covered CE_STORAGE. The USB boot hid all of it, because nothing on the eMMC is mounted there, and during the install `parted`'s table re-read had switched the kernel to the GPT view until the next reboot.

The two boxes differed only in the MPT. On the working AM9 Pro (and in its factory 2.1.0 dump) `reserved` is zero up to the AMLNORMAL keystore at `0x4000`, and there is no `/proc/inand`. The failing box had:

```
00000000: 4d50 5400 3031 2e30 302e 3030 0000 0000  MPT.01.00.00....
00000010: 1d00 0000 d4e8 d092 7265 7365 7276 6564  ........reserved
```

Where it came from isn't known. That box's bootloader build (`01.01.260903.162043`) differs from the one in the mega.nz 2.2.0 image (`…151400`). A USB Burning Tool flash, which partitions the eMMC through Amlogic's U-Boot, is the likeliest source, but that hasn't been checked.

Layout, from the kernel source, confirmed against that header: `char magic[4] = "MPT"`, `version[12]`, `int part_num`, `u32 checksum`, then `part_num` × `{char name[16]; u64 size; u64 offset; u32 mask_flags}` (40 bytes with padding). At most 24 + 32×40 = 1304 bytes. The checksum loop in `mmc_partition_tbl_checksum_calc` never advances its pointer, so it is `part_num` × the u32 word-sum of entry 0. For 29 entries with entry 0 = `reserved` (size `0x4000000`, offset `0x2400000`) that is `0x92d0e8d4`, exactly the value on the box.

**Clearing rather than rewriting.** A rewritten MPT with CE_FLASH/CE_STORAGE entries would also work, but clearing it returns `reserved` to the state of every working box we have, and the GPT is already correct. The failing box confirmed it: after zeroing the first 3 sectors of `reserved`, a reboot on the same bootloader showed CE_FLASH/CE_STORAGE in `lsblk` and no `/proc/inand` (U-Boot does not regenerate the table), and the eMMC install booted.

What the scripts do:

- **Install:** detects the MPT in preflight and lists it on the confirm screen. It refuses if anything but zeros follows the table inside the 3 sectors it would clear. It clears the MPT right after the repartition is verified (the GPT is final by then) and checks the magic is gone. `reserved_backup.bin`, taken before, holds the original. Only 1536 bytes are written; the keystore at `0x4000` and the Android DTB copies at 4 MiB are untouched.
- **`--clear-mpt`:** the same clear for boxes installed before this, after saving the 3 sectors to `mpt_backup.bin`. It refuses unless the GPT's p28 is CE_FLASH.
- **`--info`:** compares the kernel's p28 with the GPT, reports the MPT, and checks the hooks on the eMMC's CE_FLASH. It used to read `/flash`, which from USB is the stick, so it always warned about a missing `mount-storage.sh` and `nofsck`.
- **Restore:** puts the MPT back from the backup when its p28/p29 names and offsets match `partition_layout.txt`, so a restored box ends up exactly as it was.

## Why the board check reads `coreelec-dt-id` instead of hashing the DTB

The installer used to md5-compare the live `/flash/dtb.img` against every file in `/flash/device_trees/` and require an exact match. **That check false-negatives on essentially every box that has ever been configured.**

CoreELEC rewrites the live `dtb.img` at runtime via `/usr/lib/coreelec/dtb-xml` whenever EDID, remote, LED, or eMMC-timing settings are touched. The result no longer matches any shipped `.dtb` byte-for-byte. Measured on a stock AM9 Pro running `22.0-Piers_nightly_20260727`:

```
live /flash/dtb.img          84653 bytes   md5 31126f23…
stock s6_s905x5_ugoos_am9_pro.dtb  84657 bytes
→ matched 0 of 83 shipped device trees
```

Parsing both FDTs and diffing property-by-property shows 2792 properties on each side, identical except for one:

```
/amhdmitx/custom_edid    live = <empty>    stock = 0x00
```

A single 1-byte property, a 4-byte file delta after FDT alignment — enough to make the old gate refuse to install on a perfectly supported board.

The fix keys on the FDT root node's `coreelec-dt-id` property instead. That string is CoreELEC's own board identifier and equals the `device_trees/` filename stem, so it maps straight onto `SUPPORTED_BOARDS`:

| Live DTB | `coreelec-dt-id` | `model` |
|---|---|---|
| AM9 Pro | `s6_s905x5_ugoos_am9_pro` | `Ugoos AM9 Pro` |
| SK4 / SK4 Pro | `s7d_s905x5m_ugoos_sk4` | `Ugoos SK4` |
| AM9 (non-Pro) | `s6_s905x5_ugoos_am9` | `Ugoos AM9` |
| ODROID-C5 | `s7d_s905x5m_odroid_c5` | `Hardkernel ODROID-C5` |

`dtb-xml` never touches the root identity node, so this is stable across every rewrite it performs — and unlike a whitelist of "properties `dtb-xml` is allowed to change," it needs no maintenance as CoreELEC adds new knobs. If `coreelec-dt-id` can't be read (no `python3`, or a hand-built DTB), the installer warns and falls back to the old md5 compare rather than skipping the check.

Note that `compatible` is *not* usable for this — on the AM9 Pro it reads `amlogic, s6`, which is SoC-family level and identical across the AM9, AM9 Pro, and MagentaTV ONE.

## Why "am I booted from the eMMC?" compares device numbers

Both scripts refuse to run when `/flash` lives on the disk they're about to repartition. That guard used to be a string match for `mmcblk0` in the mount source — and it silently failed on exactly the boxes it exists to protect.

CoreELEC mounts these partitions from **label-named nodes**, which this installer itself creates. `/dev/CE_FLASH` is a real block device with the eMMC's major:minor and no `mmcblk0` anywhere in its name:

```
/flash mount source:  /dev/CE_FLASH
/dev/CE_FLASH      →  brw-rw---- 179, 28
/dev/mmcblk0p28    →  brw-rw---- 179, 28     ← same device
```

The same blind spot affected the pre-repartition unmount sweep, which is more consequential: an eMMC partition still mounted is what makes the kernel refuse the partition-table update, which is what makes `parted` stop early (see below). On a test box the old name match found **zero** eMMC mounts while device-number matching found two (`/flash` and `/storage`).

Both scripts now resolve every mount source to `major:minor` via `stat` and compare against the set of device numbers in `/proc/partitions` belonging to `mmcblk0`. They also sweep device-mapper: Android's `super` is a logical-partition container, and a live `dm` target holds the underlying partition open even with nothing mounted. `dmsetup` is present on CoreELEC (unlike `partx`, see below).

## Tooling that isn't on CoreELEC

CoreELEC ships busybox and a minimal util-linux. Notably **absent**: `partx`, `addpart`, `blockdev`, `sfdisk`, `sgdisk`. Present: `parted`, `partprobe`, `dmsetup`.

Two consequences worth knowing if you're modifying these scripts:

- An earlier version of `reread_and_make_nodes()` called `partx -u` as a fallback for when `partprobe`'s BLKRRPART fails. That was dead code — it never ran once. The real fallback is reading the new partitions out of sysfs and `mknod`-ing their device nodes directly, which is what the function actually relies on.
- Anything using GNU coreutils flags breaks. `df -B1` is rejected outright by busybox (`df: invalid option -- 'B'`), and because the call sites were `$(df -B1 … 2>/dev/null | awk …)` under `set -eo pipefail`, the failure **killed the install with no error message at all**. The `/storage` migration now uses `df -k` and `du -sxk` and scales by 1024. The `-x` / `-xdev` flags also matter: without them the size walk recurses into network shares mounted under `/storage`, which is slow, wildly inflates the total, and returns non-zero on anything unreadable — another silent abort.

