#!/usr/bin/env python3
"""Add or update a canonical layout csv from a built `partition-table.bin`.

    python3 tools/add_layout.py --slug espnetlink --table build/partition_table/partition-table.bin
        --name "v1.23 (two 5 MB OTA slots, settings, storage, coredump)"
        --first-seen 1.23.0-alfa.1 [--last-seen V] [--source "espnetlink_partitions_table.csv (main)"]

Writes `<slug>/firmware/layouts/<md5>.csv` (the md5 of the entries in flash
order, which must equal the digest stored in the table). An existing file
keeps its rows and gets the headers merged (`first_seen` lowered,
`last_seen` raised, the others replaced when given).
"""

from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from fwpub import layout as layout_mod, versions  # noqa: E402


def merge_headers(old, new):
    out = dict(old)
    for k in ("name", "source", "device_type"):
        if new.get(k):
            out[k] = new[k]
    for k, pick in (("first_seen", min), ("last_seen", max)):
        a = versions.parse_strict(old.get(k)) if old.get(k) else None
        b = versions.parse_strict(new.get(k)) if new.get(k) else None
        if a is not None and b is not None:
            out[k] = versions.to_str(pick(a, b))
        elif b is not None:
            out[k] = versions.to_str(b)
    return out


def write_layout(root, slug, entries, headers):
    """Returns (path, md5, created)."""
    md5 = layout_mod.table_md5(entries)
    rel = os.path.join(slug, "firmware", "layouts", md5 + ".csv")
    path = os.path.join(root, rel)
    created = not os.path.isfile(path)
    old = {}
    if not created:
        with open(path, "rb") as f:
            old, old_entries = layout_mod.read_csv(f.read().decode("utf-8"))
        if old_entries != entries:
            raise SystemExit("%s exists with different rows" % rel)
    headers = merge_headers(old, dict(headers, device_type=slug))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(layout_mod.write_csv(entries, headers).encode("utf-8"))
    return rel.replace("\\", "/"), md5, created


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--table", required=True, help="a built partition-table.bin")
    ap.add_argument("--name", default="")
    ap.add_argument("--first-seen", default="")
    ap.add_argument("--last-seen", default="")
    ap.add_argument("--source", default="")
    ap.add_argument("--root", default=os.path.dirname(HERE))
    a = ap.parse_args()
    with open(a.table, "rb") as f:
        raw = f.read()
    entries = layout_mod.parse_table(raw)
    if not entries:
        raise SystemExit("%s is not a partition table" % a.table)
    stored = layout_mod.stored_md5(raw)
    md5 = layout_mod.table_md5(entries)
    if stored is not None and stored != md5:
        raise SystemExit("the table's stored digest %s differs from the computed %s" % (stored, md5))
    for k in ("first_seen", "last_seen"):
        v = getattr(a, k)
        if v and versions.parse_strict(v) is None:
            raise SystemExit("--%s %r is not in the strict grammar" % (k.replace("_", "-"), v))
    rel, md5, created = write_layout(os.path.abspath(a.root), a.slug, entries,
                                     {"name": a.name, "first_seen": a.first_seen,
                                      "last_seen": a.last_seen or a.first_seen, "source": a.source})
    print("%s %s (%d rows)" % ("wrote" if created else "updated", rel, len(entries)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
