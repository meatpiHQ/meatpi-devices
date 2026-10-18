# Firmware releases

Every MeatPi device that can update itself reads its release list from this
repository, under `<device_type>/firmware/`:

```
<device_type>/firmware/
  manifest.json          the release list (schema 1, frozen for the life of the fleet)
  archive.json           older releases moved out of the manifest (created on the first prune)
  notes/<version>.md     the release notes a device proxies to its own web page
  layouts/<md5>.csv      every partition layout a release was built with
```

A device composes the manifest URL from one setting, `base_url`
(default `https://raw.githubusercontent.com/meatpiHQ/meatpi-devices/main/`),
its compile-time `device_type` and the fixed path, so
`https://raw.githubusercontent.com/meatpiHQ/meatpi-devices/main/wican_pro/firmware/manifest.json`
is what a WiCAN PRO fetches. The notes URL is `base_url` plus the
release's `notes` path. Image and zip URLs are absolute and used as given.
A bench points `base_url` at a local server that serves a copy of this
tree; the committed tree is https only.

The device types are the slugs of [`device_catalog.json`](device_catalog.json)
(`wican_pro`, `espnetlink`, ...). The catalog itself belongs to the Home
Assistant integration and is never touched by the firmware tooling.

The images are not committed: they are the assets of GitHub releases, on
this repository for releases made by the publisher (tag
`<device_type>-<version>`, name `<Model> <version>`, `prerelease` for alfa
and beta, never marked latest) and on the firmware repository for the
releases that existed before. Stable WiCAN PRO releases keep being posted
on `meatpiHQ/wican-fw` as well; alfa and beta releases exist only here.

## What is in the manifest

Top level:

| Field | Rule |
|---|---|
| `schema` | exactly `1` |
| `device_type` | equals the folder name, present in the catalog |
| `model` | the display name ("WiCAN PRO", "ESPNetLink") |
| `project_name` | the constant application name new builds carry (`wican-fw_obd_pro`, `espnetlink-fw`) |
| `source_repo` | `owner/repo` of the firmware source |
| `updated` | `YYYY-MM-DDTHH:MM:SSZ`, written by the tools |
| `channels` | `stable`, `beta`, `alfa`, each `{"latest": "<version>" or null}`; derived, never hand-edited |
| `releases` | sorted newest first; `version`, `fw_version`, `tag` and `image.sha256` unique |

A release:

| Field | Required | Rule |
|---|---|---|
| `version` | yes | `MAJOR.MINOR.PATCH[-alfa.N or -beta.N]`, the comparable version |
| `fw_version` | yes | the exact string the device reports as its firmware version (the application descriptor's version) |
| `fw_build` | no | the git describe of the build |
| `commit` | no | the source commit, 40 hex |
| `tag` | yes | the git tag in `source_repo` |
| `channel` | yes | `alfa`, `beta` or `stable`: the most stable channel the release has reached |
| `promoted` | no | `{"beta": "YYYY-MM-DD", "stable": "YYYY-MM-DD"}` as applicable |
| `date` | yes | first publication, `YYYY-MM-DD`, not in the future |
| `summary` | yes | one plain sentence, at most 140 characters |
| `notes` | yes | `<device_type>/firmware/notes/<version>.md`, 1 to 16 KiB |
| `requires` | no | `{"min_version": "<version>"}`: a unit below it installs a stepping stone first |
| `image` | yes | `url` (https), `size`, `sha256`, `project_name` (what the image's descriptor says) |
| `flash_zip` | no | `url`, `size`, `sha256` of the serial flash set (bootloader, table, otadata, image) |
| `bootloader` | no | `sha256` of the bootloader in that set (a bootloader is never updated over the air) |
| `layout` | yes | `md5` of the partition table the image was built for, `table` = its csv |
| `layout_from` | no | per other layout md5: `{"migration": "lossless" or "wipe" or "serial", "wipes": [labels]}` |
| `yanked` | yes | a yanked release is never offered; a unit running it is told to update |
| `yank_reason` | when yanked | 1 to 200 characters |

Unknown fields fail validation (a consumer would ignore them silently).
`hw`, `min_api_level`, `signatures` and `min_free_psram` are reserved for
later and refused today.

## Versions and channels

Order: the three numbers, then a release above any prerelease of the same
number, then beta above alfa, then N:
`6.0.0-alfa.6 < 6.0.0-beta.1 < 6.0.0 < 6.0.1 < 6.1.0-alfa.1 < 6.1.0 < 6.10.0`.

A device on channel `stable` sees stable releases; on `beta` stable and
beta; on `alfa` all three. `channels.<c>.latest` is the greatest non-yanked
release visible from `c`. A device finds itself by its exact `fw_version`;
a string that is not in the list (a development build, an old release
scheme such as `v4.51p` or `v6.00p_alfa-06`) is placed by a tolerant
parser (`v4.51p` reads as 4.51.0, `v6.00p_alfa-06` as 6.0.0-alfa.6). It is
offered the channel's latest when that is newer, or when its own release
is yanked; when the target `requires` a version it does not have, it is
offered the newest release between the two first. The same selection runs
in `tools/fwpub/versions.py`.

A channel only moves up (`alfa` to `beta` to `stable`); the way down is a
yank, which keeps the record (so a unit can recognise it runs the yanked
build) and removes the files.

## Partition layouts

The layout id is the MD5 that ESP-IDF appends to every partition table
(the digest of the 32-byte entries in flash order). A device computes it
from its own table at boot; the tools compute it from the build's
`partition-table.bin`; both must agree, and the csv under `layouts/` must
re-encode to the md5 it is named after (the validator checks).

The verdict for a unit coming from another layout follows the firmware's
own rule: the app slots, `otadata`, `phy_init` and `nvs` are anchored and
must be identical in both tables; then every other data partition of the
old table that has no identical entry in the new one is formatted on the
first boot (`wipe`, with the labels in `wipes`); nothing formatted is
`lossless`; a differing anchor is `serial` (flash over USB). A release
built without the partition_migrate component carries no `layout_from`:
every foreign layout is a serial flash for it. The validator recomputes
every declared verdict; `serial` is always accepted (a verdict may be
made safer by hand, never more optimistic).

Known layouts:

| md5 | device | content |
|---|---|---|
| `225242e6822a60aa93a9a67abd9442ed` | wican_pro | factory v4.x: one 6 MB FAT `storage` |
| `35c16cb5677477a27c662bd912582329` | wican_pro | v6: `settings` and `storage` LittleFS |
| `fc90a384ddaa5a6c032cf16e27bdbb05` | espnetlink | the v1.20 release: one factory app slot, no OTA |
| `4ab851ed091a20b0eb35046eba065189` | espnetlink | v1.23: two 5 MB OTA slots, settings, storage, coredump |

A v4.x WiCAN PRO moving to v6 keeps its calibration and keys and loses
`storage` (the stored files, logs and the saved configuration). A v1.20
ESPNetLink cannot update over the air at all (its table has no OTA slots):
one serial flash of a 1.23 or newer release first.

## Release notes

`notes/<version>.md` is rendered by the device's own web page, which may
have no internet, so: the first line `# <Model> <version>`, then `##`
headings, `-` lists, `**bold**` and `[links](https://...)` only; no HTML,
images or tables; at most 16 KiB; no long dash anywhere (the validator
refuses U+2013 and U+2014 in a notes file and in every manifest string).
The publisher writes the "Before you update" block from the layout
verdicts, so a user reads a sentence about their own units instead of an
md5.

## Tools

Stdlib Python 3.10 or newer, nothing to install.

```
python3 tools/validate_firmware.py [--self-test] [--online [--download] [--pointed-only]]
```

CI runs `validate_firmware.py --self-test` on every push and pull request
(`.github/workflows/validate.yml`): every `*/firmware/manifest.json`, its
notes and layouts, and the `fwpub` vectors. `--online` follows every
non-yanked image and zip URL (final 200, the declared size); `--download`
checks the sha256 as well. `.github/workflows/firmware-online.yml` runs
the online check on request and weekly for the pointed releases, never on
pull requests.

```
python3 tools/import_github_release.py --slug wican_pro --repo meatpiHQ/wican-fw
    --tag v4.51p --version 4.51.0 --channel stable --summary "..."
    [--table partition-table.bin | --layout <md5>] [--layout-name "..."] [--migrate]
    [--notes FILE] [--rehost --token-file PATH] [--dry-run]
```

Seeds a release that already exists on a firmware repository: downloads
the image (and the serial flash zip), checks size and sha256 against what
GitHub reports, reads `fw_version` and `project_name` from the image's
descriptor, takes the partition table from the release, writes the layout
csv, converts the release body into the notes file, inserts the release
and validates the tree. `--rehost` creates the `<device_type>-<version>`
release here and uploads the same files (a token with Contents read and
write, never in CI).

```
python3 tools/add_layout.py --slug espnetlink --table partition-table.bin
    --name "..." --first-seen 1.23.0-alfa.1 [--source "..."]
```

Writes or updates a layout csv from a built table.

`tools/fwpub/` is the library behind them, imported by each firmware
repository's `tools/release.py` as well (the publisher: build, check,
upload, manifest, commit, promote, yank): `versions` (grammar, order,
selection), `layout` (tables, md5, csv, verdicts), `manifest` (load,
insert, pointers, prune, validate), `notes`, `appdesc` (the application
descriptor and image integrity), `github` (releases and assets over the
REST API with urllib).

## Rules of the tree

- Everything a device reads is under `<device_type>/firmware/`; the
  publisher stages only that path and never touches `device_catalog.json`.
- `manifest.json` stays schema 1. A breaking change would be a new file
  beside it (`manifest.v2.json`), generated alongside, never a change to
  this one.
- A release is immutable: a rebuild gets a new version, never a replaced
  asset.
- `manifest.json` stays under 64 KiB: the tools keep the 24 newest
  non-yanked releases plus every pointed release, stepping stone and
  yanked release of the last twelve months, and move the rest to
  `archive.json` (devices read only the manifest).
- Git LFS is never used (raw URLs would serve the pointer file).
