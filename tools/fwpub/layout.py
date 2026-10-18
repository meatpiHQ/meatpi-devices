"""Partition layouts: the binary table IDF builds (`partition-table.bin`),
its id (the MD5 of the 32-byte entries in flash order, which is the digest
IDF appends as the 0xEBEB entry and what every device computes from the
3 KB at 0x8000), the canonical csv kept under `<slug>/firmware/layouts/`,
and the verdict for a device coming from another layout.

A line-for-line port of the firmware's partition_migrate_core.c
(meatpi-components), checked against the same real tables in self_test().

The rule: the bootloader picks the image from `otadata` and the app
entries, and `phy_init` / `nvs` hold calibration and keys, so those are
ANCHORED. A table migrates only when every anchored entry has a
byte-identical twin in the other table (both ways) and only other data
partitions differ; anything else is foreign (serial flash only). On a
migration, a data partition of the old table that has no identical entry
in the new one (moved, resized, removed, a changed subtype) is formatted
on the first boot: those labels are the `wipes`. Stdlib only.
"""

from __future__ import annotations

import hashlib
import struct

ENTRY_LEN = 32
TABLE_MAX_LEN = 0xC00
MAX_ENTRIES = 16
MAGIC = 0x50AA
MAGIC_MD5 = 0xEBEB
TYPE_APP = 0x00
TYPE_DATA = 0x01
DATA_OTA = 0x00
DATA_PHY = 0x01
DATA_NVS = 0x02

CSV_COLUMNS = "# name,type,subtype,offset,size,flags"
CSV_HEADER_KEYS = ("layout", "device_type", "name", "first_seen", "last_seen", "source")


class Entry:
    __slots__ = ("type", "subtype", "offset", "size", "flags", "label")

    def __init__(self, label, type_, subtype, offset, size, flags=0):
        self.label = str(label)
        self.type = int(type_)
        self.subtype = int(subtype)
        self.offset = int(offset)
        self.size = int(size)
        self.flags = int(flags)

    def as_tuple(self):
        return (self.label, self.type, self.subtype, self.offset, self.size, self.flags)

    def __eq__(self, other):
        return isinstance(other, Entry) and self.as_tuple() == other.as_tuple()

    def __hash__(self):
        return hash(self.as_tuple())

    def __repr__(self):
        return "Entry(%s,0x%02x,0x%02x,0x%x,0x%x,0x%x)" % self.as_tuple()

    def encode(self):
        raw = self.label.encode("utf-8")
        if len(raw) > 16:
            raise ValueError("label longer than 16 bytes: " + self.label)
        return struct.pack("<HBBII", MAGIC, self.type, self.subtype, self.offset, self.size) + \
            raw + b"\0" * (16 - len(raw)) + struct.pack("<I", self.flags)

    def csv_row(self):
        return "%s,0x%02x,0x%02x,0x%x,0x%x,0x%x" % self.as_tuple()


# ---- the binary table ---------------------------------------------------------------

def parse_table(buf):
    """The entries of a binary table, [] when the bytes are not a table
    (a bad magic, no entry, more than 16, a zero size). Stops at the MD5
    entry or the blank terminator, like the firmware."""
    out = []
    off = 0
    while off + ENTRY_LEN <= len(buf) and off < TABLE_MAX_LEN:
        e = buf[off:off + ENTRY_LEN]
        magic = struct.unpack_from("<H", e, 0)[0]
        if magic == MAGIC_MD5 or magic == 0xFFFF:
            break
        if magic != MAGIC or len(out) >= MAX_ENTRIES:
            return []
        type_, subtype, offset, size = struct.unpack_from("<BBII", e, 2)
        if size == 0:
            return []
        label = e[12:28].split(b"\0", 1)[0].decode("utf-8", errors="replace")
        flags = struct.unpack_from("<I", e, 28)[0]
        out.append(Entry(label, type_, subtype, offset, size, flags))
        off += ENTRY_LEN
    return out


def stored_md5(buf):
    """The digest of the 0xEBEB entry that follows the entries, or None."""
    n = len(parse_table(buf))
    if n == 0:
        return None
    off = n * ENTRY_LEN
    if off + ENTRY_LEN > len(buf):
        return None
    e = buf[off:off + ENTRY_LEN]
    if struct.unpack_from("<H", e, 0)[0] != MAGIC_MD5:
        return None
    return e[16:32].hex()


def table_md5(entries):
    """The layout id: the MD5 of the entries in order, as 32 hex characters."""
    return hashlib.md5(b"".join(e.encode() for e in entries)).hexdigest()


def encode_table(entries, with_md5=True):
    body = b"".join(e.encode() for e in entries)
    if with_md5:
        body += struct.pack("<H", MAGIC_MD5) + b"\xff" * 14 + hashlib.md5(body).digest()
    return body


# ---- the rule ---------------------------------------------------------------------

def is_anchored(e):
    if e.type == TYPE_APP:
        return True
    return e.type == TYPE_DATA and e.subtype in (DATA_OTA, DATA_PHY, DATA_NVS)


def compare(from_entries, to_entries):
    """(verdict, why): "same", "migrate" or "foreign", the firmware's
    pm_table_compare with FROM as the table in flash and TO as the build's."""
    if not to_entries:
        return "foreign", "the target table does not parse"
    if not from_entries:
        return "foreign", "no partition table"
    if len(from_entries) == len(to_entries) and all(a == b for a, b in zip(from_entries, to_entries)):
        return "same", "the same table"
    to_set = set(to_entries)
    from_set = set(from_entries)
    for e in from_entries:
        if is_anchored(e) and e not in to_set:
            return "foreign", "%s of the old layout has no match in the new one" % e.label
    for e in to_entries:
        if is_anchored(e) and e not in from_set:
            return "foreign", "%s of the new layout has no match in the old one" % e.label
    gone = ", ".join("%s %d KB at 0x%X" % (e.label, e.size // 1024, e.offset)
                     for e in from_entries if e not in to_set)
    new = ", ".join("%s %d KB at 0x%X" % (e.label, e.size // 1024, e.offset)
                    for e in to_entries if e not in from_set)
    return "migrate", "%s -> %s" % (gone, new)


def verdict(from_entries, to_entries, has_partition_migrate=True):
    """What installing a build with layout TO costs a unit on layout FROM:
    {"migration": "lossless" | "wipe" | "serial", "wipes": [labels]}
    (`wipes` only with "wipe"). A build without the partition_migrate
    component cannot rewrite the table: every differing layout is serial."""
    kind, _why = compare(from_entries, to_entries)
    if kind == "same":
        return {"migration": "lossless"}
    if kind == "foreign" or not has_partition_migrate:
        return {"migration": "serial"}
    to_set = set(to_entries)
    wipes = [e.label for e in from_entries
             if not is_anchored(e) and e.type == TYPE_DATA and e not in to_set]
    if wipes:
        return {"migration": "wipe", "wipes": wipes}
    return {"migration": "lossless"}


# ---- the canonical csv ---------------------------------------------------------------

def write_csv(entries, headers):
    """The canonical text: `# key: value` headers (layout first, always the
    computed md5), the column comment, one row per entry with hex numbers
    that re-encode byte-exactly."""
    md5 = table_md5(entries)
    lines = ["# layout: " + md5]
    for k in CSV_HEADER_KEYS[1:]:
        if headers.get(k):
            lines.append("# %s: %s" % (k, headers[k]))
    lines.append(CSV_COLUMNS)
    lines.extend(e.csv_row() for e in entries)
    return "\n".join(lines) + "\n"


def read_csv(text):
    """(headers dict, entries). Raises ValueError on a malformed row."""
    headers = {}
    entries = []
    for n, raw in enumerate(text.replace("\r\n", "\n").split("\n"), 1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            body = line[1:].strip()
            if body.startswith("name,"):
                continue
            if ":" in body:
                k, v = body.split(":", 1)
                headers[k.strip()] = v.strip()
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 6:
            raise ValueError("line %d: expected 6 fields, found %d" % (n, len(parts)))
        try:
            entries.append(Entry(parts[0], int(parts[1], 0), int(parts[2], 0),
                                 int(parts[3], 0), int(parts[4], 0), int(parts[5], 0)))
        except ValueError:
            raise ValueError("line %d: a number does not parse" % n) from None
        if not parts[0] or len(parts[0].encode("utf-8")) > 16:
            raise ValueError("line %d: the label must be 1 to 16 bytes" % n)
    return headers, entries


def min_app_slot(entries):
    """The smallest app partition, or None."""
    sizes = [e.size for e in entries if e.type == TYPE_APP]
    return min(sizes) if sizes else None


# ---- self test ----------------------------------------------------------------------

LEGACY_PRO = [  # 225242e6822a60aa93a9a67abd9442ed, the WiCAN PRO factory v4.x table
    Entry("nvs", 0x01, 0x02, 0x9000, 0x4000), Entry("otadata", 0x01, 0x00, 0xd000, 0x2000),
    Entry("phy_init", 0x01, 0x01, 0xf000, 0x1000), Entry("ota_0", 0x00, 0x10, 0x10000, 0x4ee000),
    Entry("ota_1", 0x00, 0x11, 0x500000, 0x4ee000), Entry("storage", 0x01, 0x81, 0x9ee000, 0x600000),
]
V6_PRO = [  # 35c16cb5677477a27c662bd912582329, the WiCAN PRO v6 table
    Entry("nvs", 0x01, 0x02, 0x9000, 0x4000), Entry("otadata", 0x01, 0x00, 0xd000, 0x2000),
    Entry("phy_init", 0x01, 0x01, 0xf000, 0x1000), Entry("ota_0", 0x00, 0x10, 0x10000, 0x4ee000),
    Entry("ota_1", 0x00, 0x11, 0x500000, 0x4ee000), Entry("settings", 0x01, 0x83, 0x9ee000, 0x40000),
    Entry("storage", 0x01, 0x83, 0xa2e000, 0x5c0000),
]
DONGLE_V120 = [  # fc90a384ddaa5a6c032cf16e27bdbb05, the ESPNetLink v1.20 release (no OTA slots)
    Entry("nvs", 0x01, 0x02, 0x9000, 0x20000), Entry("otadata", 0x01, 0x00, 0x29000, 0x2000),
    Entry("phy_init", 0x01, 0x01, 0x2b000, 0x1000), Entry("factory", 0x00, 0x00, 0x30000, 0x500000),
    Entry("storage", 0x01, 0x83, 0x530000, 0x400000),
]
DONGLE_V123 = [  # 4ab851ed091a20b0eb35046eba065189, the ESPNetLink OTA table
    Entry("nvs", 0x01, 0x02, 0x9000, 0x20000), Entry("otadata", 0x01, 0x00, 0x29000, 0x2000),
    Entry("phy_init", 0x01, 0x01, 0x2b000, 0x1000), Entry("ota_0", 0x00, 0x10, 0x30000, 0x500000),
    Entry("ota_1", 0x00, 0x11, 0x530000, 0x500000), Entry("settings", 0x01, 0x83, 0xa30000, 0x40000),
    Entry("storage", 0x01, 0x83, 0xa70000, 0x400000), Entry("coredump", 0x01, 0x03, 0xe70000, 0x40000),
]


def self_test():
    assert table_md5(LEGACY_PRO) == "225242e6822a60aa93a9a67abd9442ed"
    assert table_md5(V6_PRO) == "35c16cb5677477a27c662bd912582329"
    assert table_md5(DONGLE_V120) == "fc90a384ddaa5a6c032cf16e27bdbb05"
    assert table_md5(DONGLE_V123) == "4ab851ed091a20b0eb35046eba065189"
    # the binary round trip, the stored digest, the terminators
    raw = encode_table(V6_PRO)
    assert parse_table(raw) == V6_PRO and stored_md5(raw) == table_md5(V6_PRO)
    flash = raw + b"\xff" * (TABLE_MAX_LEN - len(raw))
    assert parse_table(flash) == V6_PRO
    assert parse_table(encode_table(V6_PRO, with_md5=False) + b"\xff" * 32) == V6_PRO
    assert stored_md5(encode_table(V6_PRO, with_md5=False)) is None
    assert parse_table(b"\xff" * 64) == [] and parse_table(b"\x00" * 64) == []
    zero = bytearray(encode_table(V6_PRO, with_md5=False))
    struct.pack_into("<I", zero, 8, 0)
    assert parse_table(bytes(zero)) == []
    assert parse_table(b"".join(Entry("p%d" % i, 1, 0x80, 0x10000 * (i + 1), 0x1000).encode()
                                for i in range(17))) == []
    # the verdicts
    assert verdict(LEGACY_PRO, V6_PRO) == {"migration": "wipe", "wipes": ["storage"]}
    assert verdict(V6_PRO, LEGACY_PRO) == {"migration": "wipe", "wipes": ["settings", "storage"]}
    assert verdict(DONGLE_V120, DONGLE_V123) == {"migration": "serial"}
    assert verdict(DONGLE_V123, DONGLE_V120) == {"migration": "serial"}
    assert verdict(V6_PRO, V6_PRO) == {"migration": "lossless"}
    added = V6_PRO + [Entry("coredump", 0x01, 0x03, 0xfee000, 0x10000)]
    assert verdict(V6_PRO, added) == {"migration": "lossless"}
    assert verdict(added, V6_PRO) == {"migration": "wipe", "wipes": ["coredump"]}
    moved = list(V6_PRO)
    moved[4] = Entry("ota_1", 0x00, 0x11, 0x510000, 0x4ee000)
    assert verdict(V6_PRO, moved) == {"migration": "serial"}
    resub = list(V6_PRO)
    resub[6] = Entry("storage", 0x01, 0x81, 0xa2e000, 0x5c0000)
    assert verdict(V6_PRO, resub) == {"migration": "wipe", "wipes": ["storage"]}
    assert verdict(LEGACY_PRO, V6_PRO, has_partition_migrate=False) == {"migration": "serial"}
    assert compare(V6_PRO, V6_PRO)[0] == "same" and compare([], V6_PRO)[0] == "foreign"
    assert compare(LEGACY_PRO, V6_PRO)[0] == "migrate"
    # the csv round trip
    text = write_csv(LEGACY_PRO, {"device_type": "wican_pro", "name": "factory v4.x",
                                  "first_seen": "4.40.0", "last_seen": "4.51.0", "source": "x.csv"})
    headers, entries = read_csv(text)
    assert entries == LEGACY_PRO and headers["layout"] == table_md5(LEGACY_PRO)
    assert headers["name"] == "factory v4.x" and headers["device_type"] == "wican_pro", headers
    assert table_md5(entries) == headers["layout"]
    assert min_app_slot(LEGACY_PRO) == 0x4ee000 and min_app_slot(DONGLE_V120) == 0x500000
    try:
        read_csv("a,b,c\n")
        raise AssertionError("a short row parsed")
    except ValueError:
        pass
    return 1


if __name__ == "__main__":
    self_test()
    print("layout: OK")
