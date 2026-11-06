# BMW iDrive 6 emulator

This project boots the native BMW F32 iDrive 6 Light interface in patched
QEMU. It does not include BMW firmware. You supply an extracted PSdZData
directory containing the verified NBTevo X20511I packages.

## Quick start

The supported release host is Apple silicon macOS with Nix flakes enabled.
Clone the repository, copy the example config, and set `psdzdata` to the
absolute path of your extracted PSdZData directory:

```sh
cp idrive6.example.toml idrive6.toml
```

```toml
psdzdata = "/path/to/psdzdata"
```

For the first run, check and stage the firmware:

```sh
nix run .#idrive6 -- --prepare-only
```

Then start the F32 interface:

```sh
nix run .#idrive6
```

The config is read by Nix at launch. `psdzdata` must point to the directory
whose `swe/swfl` folder contains `swfl_00002719.bin.007_030_001`,
`swfl_0000271a.bin.007_030_001`, and `swfl_00002720.bin.007_030_001`.
The launcher checks each package's SHA-256 before extracting it. The first
staging pass verifies the BMW manifests, selects the F32 `id6l` assets, and
matches the eight home-menu widgets to HmiMain3D. Later launches reuse the
verified cache. By default it lives under `$XDG_CACHE_HOME/idrive6` or
`~/.cache/idrive6`.

The optional settings are `cacheDir`, `display` (`cocoa` or `none`), and
`cpuCount` (`1` or `2`). Keep PSdZData and the cache outside the repository;
the launcher rejects paths inside the checkout so local firmware and cache
files never enter the Nix source or store. Both paths may contain spaces.
The launcher reads `./idrive6.toml` by default; pass `--config /path/to/file`
to either command to use another config.

## Controls and logs

Use the mouse for CID touch. The controller mapping uses the wheel or Page
Up/Page Down to turn, arrow keys to tilt, Return or Space to select, Escape or
Backspace for Back, M for Menu, and O for Options. C or R sends Media/Radio,
T sends Communication, N sends Navigation, and V sends Map. Click the QEMU
window first to give it keyboard focus.

Each run gets its own directory below the cache's `runs/` folder. It contains
the guest serial stream, QEMU debug log, ANGLE server log, and a manifest of
the source and firmware inputs. OTF recording, network packet dumps, ANGLE
shader/draw/resource captures, `pdebug`, and the web inspector remain off by
default. For an already staged cache, set `IDRIVE_CACHE_DIR` to the same path
as `cacheDir` (or its default) before using the developer profile runner. For
example, with the default cache location:

```sh
export IDRIVE_CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/idrive6"
nix develop -c python3 tools/run-hmi-profile.py \
  profiles/f32-x20511i-id6-light.json \
  --set IDRIVE_OTF_CAPTURE=1 \
  --set IDRIVE_OTF_CAPTURE_OUTPUT=/tmp/idrive6-otf.bin \
  --set IDRIVE_OTF_CAPTURE_SERVER_LOG=/tmp/idrive6-otf.log
```

## Current limits

The F32 home interface, text, controller input, and CID touch run through
BMW's native HmiMain3D and QNX Screen. QEMU supplies an OMAP5430 board model
and a software display path. The PowerVR SGX540 is not fully emulated; the
host ANGLE bridge implements the graphics calls used by the tested HMI path.
Other vehicle variants and Linux hosts are not part of the verified release.

## Developer notes

- [QEMU patch workflow](docs/qemu-patches.md) explains how to update the four
  topic patches against the pinned QEMU in Nixpkgs.
- [Graphics contract](NATIVE_GRAPHICS_CONTRACT.md) documents the current
  native HMI display path and observed boundaries.
- [HMI startup trace](HMI_STARTUP_DEPENDENCY.md) records the measured service
  and startup dependencies.
- [Third-party notices](THIRD_PARTY_NOTICES.md) covers the QEMU patches and
  optional Vita3K-based shader diagnostics.
- `tools/` contains the retained firmware extraction, graphics, and QNX
  diagnostics. Enter `nix develop` for the corresponding build tools.

The exact PSdZData package SHA-256 values are pinned in
`tools/idrive6-launch.py`. A different firmware revision must be reviewed and
added as a distinct, verified profile before use. The checked-in shader data is
limited to the host-side ESSL reconstructions and hashes used by this profile;
the original BMW shader payloads are verified from PSdZData at runtime.

## Developer diagnostics

Run the firmware-free extractor, QNX bootstrap, and QEMU device tests with:

```sh
nix develop -c python3 -m unittest discover -s tests -v
```

The firmware staging diagnostics use the same verified input as the launcher;
`nix run .#idrive6 -- --prepare-only` checks and stages it without starting
QEMU. The device tests use QEMU directly and need no firmware or GUI.
