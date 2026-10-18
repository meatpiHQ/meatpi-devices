#!/usr/bin/env python3
"""Strict validator for every `<slug>/firmware/` tree: the manifest, its
notes and layout files, and (on request) the hosted images.

Mirrors what a device does with the manifest (fw_update in
meatpi-components) and what the publisher promises; where a device would
ignore an unknown field or skip a release, this validator FAILS, so
problems are caught in review instead of shipping as silent no-ops.

Usage: python3 tools/validate_firmware.py [manifest.json ...]
                [--self-test] [--online] [--download] [--pointed-only] [--root DIR]

  (no paths)      every */firmware/manifest.json under the repository root
  --self-test     run the fwpub vectors first (CI does)
  --online        follow every non-yanked image and zip URL: final 200 and
                  Content-Length equal to the declared size (never on PRs)
  --download      with --online: download and check the sha256 too
  --pointed-only  with --online: only the releases a channel points at

Exit code 0 = valid (`OK: N manifests`); 1 = problems (`FAIL: k problem(s)`).
Warnings are printed and do not fail the run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fwpub  # noqa: E402
from fwpub import github, manifest as manifest_mod  # noqa: E402


def catalog_slugs(root):
    path = os.path.join(root, "device_catalog.json")
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        doc = json.loads(f.read().decode("utf-8"))
    types = doc.get("device_types") if isinstance(doc, dict) else None
    return set(types) if isinstance(types, dict) else None


def discover(root):
    out = []
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name, "firmware", "manifest.json")
        if os.path.isfile(p):
            out.append(p)
    return out


def slug_of(path, root):
    rel = os.path.relpath(os.path.abspath(path), root).replace("\\", "/")
    parts = rel.split("/")
    if len(parts) != 3 or parts[1] != "firmware" or parts[2] != "manifest.json":
        return None
    return parts[0]


def check_online(doc, download, pointed_only, errors):
    gh = github.GitHub()
    pointed = {c.get("latest") for c in (doc.get("channels") or {}).values() if isinstance(c, dict)}
    for r in doc.get("releases") or []:
        if not isinstance(r, dict) or r.get("yanked", False):
            continue
        if pointed_only and r.get("version") not in pointed:
            continue
        for key in ("image", "flash_zip"):
            obj = r.get(key)
            if not isinstance(obj, dict) or not obj.get("url"):
                continue
            where = "%s %s.%s" % (doc.get("device_type"), r.get("version"), key)
            try:
                status, length, final = gh.head(obj["url"])
            except Exception as exc:  # noqa: BLE001
                errors.append("%s: %s is not reachable: %s" % (where, obj["url"], exc))
                continue
            if status != 200:
                errors.append("%s: final status %s for %s" % (where, status, obj["url"]))
                continue
            if length is not None and length != obj.get("size"):
                errors.append("%s: Content-Length %s differs from the declared size %s"
                              % (where, length, obj.get("size")))
                continue
            if not download:
                print("  online: %s %s B at %s" % (where, length, final.split("?", 1)[0][:90]))
                continue
            with tempfile.TemporaryDirectory(prefix="fwval_") as tmp:
                dest = os.path.join(tmp, "asset.bin")
                try:
                    n = gh.download(obj["url"], dest, expect_size=obj.get("size"))
                except Exception as exc:  # noqa: BLE001
                    errors.append("%s: download failed: %s" % (where, exc))
                    continue
                h = hashlib.sha256()
                with open(dest, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
                if h.hexdigest() != obj.get("sha256"):
                    errors.append("%s: sha256 %s differs from the declared %s"
                                  % (where, h.hexdigest(), obj.get("sha256")))
                else:
                    print("  online: %s %d B, sha256 ok" % (where, n))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("manifests", nargs="*")
    ap.add_argument("--root", default=os.path.dirname(HERE))
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--online", action="store_true")
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--pointed-only", action="store_true")
    a = ap.parse_args()
    root = os.path.abspath(a.root)

    if a.self_test:
        n = fwpub.self_test()
        print("self-test: OK (%d modules)" % n)

    paths = a.manifests or discover(root)
    slugs = catalog_slugs(root)
    errors = []
    warnings = []
    checked = 0
    for path in paths:
        slug = slug_of(path, root)
        if slug is None:
            errors.append("%s: not at <slug>/firmware/manifest.json under %s" % (path, root))
            continue
        try:
            doc = manifest_mod.load(path)
        except manifest_mod.ManifestError as exc:
            errors.append(str(exc))
            continue
        e, w = manifest_mod.validate(doc, slug, root=root, catalog_slugs=slugs,
                                     path_bytes=os.path.getsize(path))
        errors.extend(e)
        warnings.extend(w)
        arc = os.path.join(root, slug, "firmware", "archive.json")
        if os.path.isfile(arc):
            try:
                adoc = manifest_mod.load(arc)
                e, w = manifest_mod.validate_archive(adoc, slug, root=root, path_bytes=os.path.getsize(arc))
                errors.extend(e)
                warnings.extend(w)
            except manifest_mod.ManifestError as exc:
                errors.append(str(exc))
        # a layout csv nobody references is allowed, but say so
        lay_dir = os.path.join(root, slug, "firmware", "layouts")
        if os.path.isdir(lay_dir) and isinstance(doc, dict):
            used = set()
            for r in doc.get("releases") or []:
                if isinstance(r, dict):
                    if isinstance(r.get("layout"), dict):
                        used.add(r["layout"].get("md5"))
                    if isinstance(r.get("layout_from"), dict):
                        used.update(r["layout_from"].keys())
            for name in sorted(os.listdir(lay_dir)):
                if name.endswith(".csv") and name[:-4] not in used:
                    warnings.append("%s/firmware/layouts/%s: no release refers to this layout yet" % (slug, name))
        checked += 1
        if a.online and isinstance(doc, dict) and not e:
            check_online(doc, a.download, a.pointed_only, errors)

    for w in warnings:
        print("WARN: " + w)
    if errors:
        print("FAIL: %d problem(s):" % len(errors))
        for problem in errors:
            print("  - " + problem)
        return 1
    print("OK: %d manifests" % checked)
    return 0


if __name__ == "__main__":
    sys.exit(main())
