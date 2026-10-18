#!/usr/bin/env python3
"""Seed a manifest from a release that already exists on a firmware
repository's GitHub releases (the releases made before the publisher).

    python3 tools/import_github_release.py --slug wican_pro --repo meatpiHQ/wican-fw
        --tag v4.51p --version 4.51.0 --channel stable
        --summary "IDF 6.0.2 and an AutoPID fix for vehicle profiles (#710)."
        [--table build/partition_table/partition-table.bin | --layout <md5>]
        [--layout-name "factory v4.x (one 6 MB FAT storage)"] [--migrate]
        [--notes FILE] [--date YYYY-MM-DD] [--asset NAME] [--zip NAME]
        [--model "WiCAN PRO" --project-name wican-fw_obd_pro] (a new manifest)
        [--rehost --token-file PATH] [--cache DIR] [--root DIR] [--dry-run]

What it does: reads the release from GitHub, downloads the image (and the
serial flash zip when the release has one) into the cache directory, checks
each file's size and sha256 against what GitHub reports, reads `fw_version`
and `project_name` from the image's descriptor and refuses an image whose
checksum or appended digest does not hold, takes the partition table from
`--table`, else from a `partition-table.bin` asset, else from the zip,
else uses the layout csv named by `--layout`, writes or updates the layout
csv, hashes `bootloader.bin` when the zip or an asset carries it, computes
the verdicts for every other layout of the slug when `--migrate` says the
build carries the partition_migrate component (otherwise no `layout_from`:
every foreign layout is a serial flash, the device rule), converts the
release body into the notes file (or copies `--notes`), inserts the release
with `date` = the publication date, validates the whole tree and writes
the manifest. `--rehost` creates a release `<slug>-<version>` on the
devices repository, uploads the same files and points the URLs at them (a
token with Contents read and write; never in CI). Nothing is committed.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import json
import os
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from fwpub import appdesc, github, layout as layout_mod, manifest as manifest_mod, notes as notes_mod, versions  # noqa: E402
from add_layout import write_layout  # noqa: E402

DEVICES_REPO = "meatpiHQ/meatpi-devices"
SKIP_BINS = ("bootloader", "partition-table", "ota_data_initial")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_asset(gh, asset, cache):
    """The asset's bytes on disk (the cache is reused when the size
    matches), checked against GitHub's size and digest."""
    os.makedirs(cache, exist_ok=True)
    dest = os.path.join(cache, asset["name"])
    if not (os.path.isfile(dest) and os.path.getsize(dest) == asset["size"]):
        print("  downloading %s (%d B)" % (asset["name"], asset["size"]))
        gh.download(asset["browser_download_url"], dest, expect_size=asset["size"])
    digest = sha256_file(dest)
    want = github.asset_digest(asset)
    if want is not None and want != digest:
        raise SystemExit("%s: sha256 %s differs from GitHub's digest %s" % (asset["name"], digest, want))
    return dest, digest


def pick_image(release, name):
    if name:
        a = github.find_asset(release, name)
        if a is None:
            raise SystemExit("no asset named %s on the release" % name)
        return a
    bins = [a for a in release.get("assets") or []
            if a["name"].endswith(".bin") and not a["name"].startswith(SKIP_BINS)]
    if len(bins) != 1:
        raise SystemExit("expected one application image, found %s; pass --asset"
                         % [a["name"] for a in bins])
    return bins[0]


def pick_zip(release, name):
    if name:
        return github.find_asset(release, name)
    zips = [a for a in release.get("assets") or [] if a["name"].endswith(".zip")]
    return zips[0] if len(zips) == 1 else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--repo", required=True, help="owner/repo of the firmware source")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--version", required=True, help="the strict version this release is")
    ap.add_argument("--channel", required=True, choices=versions.CHANNELS)
    ap.add_argument("--summary", required=True, help="one sentence, at most 140 characters")
    ap.add_argument("--notes", help="a ready notes file instead of the converted release body")
    ap.add_argument("--table", help="a partition-table.bin for the layout")
    ap.add_argument("--layout", help="the layout md5 when its csv already exists")
    ap.add_argument("--layout-name", default="")
    ap.add_argument("--layout-source", default="")
    ap.add_argument("--migrate", action="store_true",
                    help="the build carries partition_migrate: compute layout_from verdicts")
    ap.add_argument("--date", help="YYYY-MM-DD; default the GitHub publication date")
    ap.add_argument("--asset", help="the image asset name when there are several .bin files")
    ap.add_argument("--zip", help="the serial flash zip asset name")
    ap.add_argument("--model", help="for a new manifest (default: the catalog's model)")
    ap.add_argument("--project-name", help="the constant project name of new builds (a new manifest)")
    ap.add_argument("--rehost", action="store_true", help="re-host the files on %s" % DEVICES_REPO)
    ap.add_argument("--token-file")
    ap.add_argument("--cache", default=os.path.join(tempfile.gettempdir(), "meatpi-devices-assets"),
                    help="where downloaded assets are kept, one folder per release (outside the repo)")
    ap.add_argument("--root", default=os.path.dirname(HERE))
    ap.add_argument("--dry-run", action="store_true", help="print the release object, write nothing")
    a = ap.parse_args()

    root = os.path.abspath(a.root)
    a.cache = os.path.join(a.cache, a.repo.replace("/", "_"), a.tag)
    ver = versions.parse_strict(a.version)
    if ver is None:
        raise SystemExit("--version %r is not MAJOR.MINOR.PATCH[-alfa|beta.N]" % a.version)
    if a.channel == "stable" and ver.pre != versions.PRE_NONE:
        print("warning: a stable release normally carries no prerelease suffix")
    bad_summary = [p for p in notes_mod.check_summary(a.summary) if not p.startswith("warning:")]
    if bad_summary:
        raise SystemExit("; ".join(bad_summary))

    token = None
    if a.token_file:
        with open(a.token_file, "r", encoding="utf-8") as f:
            token = f.read().strip()
    if a.rehost and not token:
        raise SystemExit("--rehost needs --token-file")
    gh = github.GitHub(token=token)

    # the catalog and the manifest
    with open(os.path.join(root, "device_catalog.json"), "rb") as f:
        catalog = json.loads(f.read().decode("utf-8"))
    types = catalog.get("device_types") or {}
    if a.slug not in types:
        raise SystemExit("%s is not a device type of device_catalog.json" % a.slug)
    mpath = os.path.join(root, a.slug, "firmware", "manifest.json")
    if os.path.isfile(mpath):
        doc = manifest_mod.load(mpath)
    else:
        model = a.model or types[a.slug].get("model")
        if not a.project_name:
            raise SystemExit("a new manifest needs --project-name (the constant project name of new builds)")
        doc = manifest_mod.new_doc(a.slug, model, a.project_name, a.repo)
    model = doc["model"]
    if manifest_mod.find(doc, a.version) is not None:
        raise SystemExit("the manifest already carries %s" % a.version)

    # the release and its files
    print("release %s %s" % (a.repo, a.tag))
    release = gh.release_by_tag(a.repo, a.tag)
    if release.get("draft"):
        raise SystemExit("the release is a draft")
    image_asset = pick_image(release, a.asset)
    zip_asset = pick_zip(release, a.zip)
    image_path, image_sha = fetch_asset(gh, image_asset, a.cache)
    with open(image_path, "rb") as f:
        image = f.read()
    try:
        desc = appdesc.parse(image)
    except appdesc.ImageError as exc:
        raise SystemExit("%s: %s" % (image_asset["name"], exc))
    if not appdesc.verify(image):
        raise SystemExit("%s: the image's checksum or appended digest does not hold" % image_asset["name"])
    print("  image %s: fw_version %r, project_name %r, idf %s, %d B"
          % (image_asset["name"], desc["version"], desc["project_name"], desc["idf_ver"], len(image)))

    zip_path = zip_sha = None
    zip_members = {}
    if zip_asset is not None:
        zip_path, zip_sha = fetch_asset(gh, zip_asset, a.cache)
        with zipfile.ZipFile(zip_path) as z:
            for info in z.infolist():
                zip_members[os.path.basename(info.filename)] = z.read(info)
        print("  zip %s: %s" % (zip_asset["name"], ", ".join(sorted(zip_members))))

    # the bootloader
    bootloader_sha = None
    if "bootloader.bin" in zip_members:
        bootloader_sha = hashlib.sha256(zip_members["bootloader.bin"]).hexdigest()
    else:
        bl = github.find_asset(release, "bootloader.bin")
        if bl is not None:
            bootloader_sha = github.asset_digest(bl) or sha256_file(fetch_asset(gh, bl, a.cache)[0])

    # the layout
    table = None
    if a.table:
        with open(a.table, "rb") as f:
            table = f.read()
    elif github.find_asset(release, "partition-table.bin") is not None:
        table = open(fetch_asset(gh, github.find_asset(release, "partition-table.bin"), a.cache)[0], "rb").read()
    elif "partition-table.bin" in zip_members:
        table = zip_members["partition-table.bin"]
    layouts_dir = os.path.join(root, a.slug, "firmware", "layouts")
    if table is not None:
        entries = layout_mod.parse_table(table)
        if not entries:
            raise SystemExit("the partition table does not parse")
        stored = layout_mod.stored_md5(table)
        md5 = layout_mod.table_md5(entries)
        if stored is not None and stored != md5:
            raise SystemExit("the table's stored digest %s differs from the computed %s" % (stored, md5))
        if a.layout and a.layout != md5:
            raise SystemExit("--layout %s does not match the table's %s" % (a.layout, md5))
    elif a.layout:
        md5 = a.layout
        csv_path = os.path.join(layouts_dir, md5 + ".csv")
        if not os.path.isfile(csv_path):
            raise SystemExit("%s does not exist; pass --table" % csv_path)
        with open(csv_path, "rb") as f:
            entries = layout_mod.read_csv(f.read().decode("utf-8"))[1]
    else:
        raise SystemExit("no partition table: pass --table or --layout")
    if len(image) > (layout_mod.min_app_slot(entries) or 0):
        raise SystemExit("the image (%d B) does not fit the smallest app slot of its layout" % len(image))

    # the verdicts for the other layouts of this slug
    layout_from = None
    if a.migrate and os.path.isdir(layouts_dir):
        layout_from = {}
        for name in sorted(os.listdir(layouts_dir)):
            other = name[:-4]
            if not name.endswith(".csv") or other == md5:
                continue
            with open(os.path.join(layouts_dir, name), "rb") as f:
                other_entries = layout_mod.read_csv(f.read().decode("utf-8"))[1]
            layout_from[other] = layout_mod.verdict(other_entries, entries, True)
        if not layout_from:
            layout_from = None

    # the notes
    if a.notes:
        with open(a.notes, "rb") as f:
            notes_text = notes_mod.strip_front_matter(f.read().decode("utf-8"))
    else:
        notes_text = notes_mod.from_github_body(release.get("body") or "", model, a.version, a.summary)
    problems = [p for p in notes_mod.check_notes(notes_text, model, a.version) if not p.startswith("warning:")]
    if problems:
        raise SystemExit("notes: " + "; ".join(problems))
    summary = a.summary

    date = a.date or (release.get("published_at") or "")[:10]
    if not date:
        raise SystemExit("no publication date; pass --date")
    commit = gh.commit_for_tag(a.repo, a.tag)

    image_url = image_asset["browser_download_url"]
    zip_url = zip_asset["browser_download_url"] if zip_asset else None
    if a.rehost and not a.dry_run:
        tag = "%s-%s" % (a.slug, a.version)
        name = "%s %s" % (model, a.version)
        body = ("%s firmware %s, channel %s. Release notes: https://github.com/%s/blob/main/%s/firmware/notes/%s.md\n"
                "Install from the device's web page or let the device update itself."
                % (model, a.version, a.channel, DEVICES_REPO, a.slug, a.version))
        print("  re-hosting on %s as %s" % (DEVICES_REPO, tag))
        rel = gh.create_release(DEVICES_REPO, tag, name, body, prerelease=(a.channel != "stable"))
        up = gh.upload_asset(rel["upload_url"], image_asset["name"], image)
        if up.get("size") != len(image) or (github.asset_digest(up) not in (None, image_sha)):
            raise SystemExit("the re-hosted image differs: %s" % json.dumps(up)[:200])
        image_url = up["browser_download_url"]
        if zip_path:
            with open(zip_path, "rb") as f:
                zdata = f.read()
            upz = gh.upload_asset(rel["upload_url"], zip_asset["name"], zdata)
            if upz.get("size") != len(zdata) or (github.asset_digest(upz) not in (None, zip_sha)):
                raise SystemExit("the re-hosted zip differs: %s" % json.dumps(upz)[:200])
            zip_url = upz["browser_download_url"]

    rel_obj = manifest_mod.ordered_release({
        "version": a.version, "fw_version": desc["version"], "commit": commit, "tag": a.tag,
        "channel": a.channel, "date": date, "summary": summary,
        "notes": "%s/firmware/notes/%s.md" % (a.slug, a.version),
        "image": {"url": image_url, "size": len(image), "sha256": image_sha, "project_name": desc["project_name"]},
        "flash_zip": ({"url": zip_url, "size": zip_asset["size"], "sha256": zip_sha} if zip_asset else None),
        "bootloader": ({"sha256": bootloader_sha} if bootloader_sha else None),
        "layout": {"md5": md5, "table": "%s/firmware/layouts/%s.csv" % (a.slug, md5)},
        "layout_from": layout_from, "yanked": False,
    })
    print(json.dumps(rel_obj, indent=2))
    if a.dry_run:
        print("dry run: nothing written")
        return 0

    # write: the layout csv, the notes, the manifest (validated first)
    rel_csv, _md5, created = write_layout(root, a.slug, entries, {
        "name": a.layout_name, "first_seen": a.version, "last_seen": a.version, "source": a.layout_source})
    print("  %s %s" % ("wrote" if created else "updated", rel_csv))
    notes_path = os.path.join(root, a.slug, "firmware", "notes", "%s.md" % a.version)
    os.makedirs(os.path.dirname(notes_path), exist_ok=True)
    with open(notes_path, "wb") as f:
        f.write(notes_text.encode("utf-8"))
    print("  wrote %s/firmware/notes/%s.md (%d B)" % (a.slug, a.version, len(notes_text.encode("utf-8"))))
    manifest_mod.insert(doc, rel_obj)
    errors, warnings = manifest_mod.validate(doc, a.slug, root=root, catalog_slugs=set(types),
                                             path_bytes=len(manifest_mod.dumps(doc).encode("utf-8")))
    for w in warnings:
        print("  warning: " + w)
    if errors:
        for e in errors:
            print("  error: " + e)
        raise SystemExit("the manifest would not validate; nothing written to it")
    manifest_mod.save(mpath, doc)
    print("  wrote %s/firmware/manifest.json: %s now points %s"
          % (a.slug, a.slug, json.dumps(doc["channels"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
