"""The firmware manifest of one device type (`<slug>/firmware/manifest.json`,
schema 1): load, save, insert a release, recompute the channel pointers,
prune to the archive, and the validation CI runs. The schema is frozen for
the life of the fleet: within schema 1 only additive changes, and every
consumer ignores unknown fields, so this validator refuses them (a typo
would otherwise ship silently). Stdlib only.
"""

from __future__ import annotations

import datetime
import json
import os
import re

from . import layout as layout_mod
from . import notes as notes_mod
from . import versions

SCHEMA = 1
MAX_MANIFEST_BYTES = 65536
MAX_ARCHIVE_BYTES = 262144
KEEP_DEFAULT = 24
MIN_IMAGE_BYTES = 65536
YANKED_KEEP_DAYS = 366

TOP_KEYS = ("schema", "device_type", "model", "project_name", "source_repo", "updated",
            "channels", "releases")
ARCHIVE_TOP_KEYS = ("schema", "device_type", "releases")
RELEASE_KEYS = ("version", "fw_version", "fw_build", "commit", "tag", "channel", "promoted",
                "date", "summary", "notes", "requires", "image", "flash_zip", "bootloader",
                "layout", "layout_from", "yanked", "yank_reason")
RESERVED_KEYS = ("hw", "min_api_level", "signatures", "min_free_psram")
REQUIRED_RELEASE_KEYS = ("version", "fw_version", "tag", "channel", "date", "summary", "notes",
                         "image", "layout", "yanked")
IMAGE_KEYS = ("url", "size", "sha256", "project_name")
ZIP_KEYS = ("url", "size", "sha256")
BOOTLOADER_KEYS = ("sha256",)
LAYOUT_KEYS = ("md5", "table")
FROM_KEYS = ("migration", "wipes")
REQUIRES_KEYS = ("min_version",)
PROMOTED_KEYS = ("beta", "stable")
MIGRATIONS = ("lossless", "wipe", "serial")

SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
UPDATED_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
TAG_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
URL_RE = re.compile(r"^https://[^\s\"'<>\\]+$")


class ManifestError(ValueError):
    pass


# ---- files -------------------------------------------------------------------------

def load(path):
    with open(path, "rb") as f:
        raw = f.read()
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError("%s is not valid UTF-8 JSON: %s" % (path, exc)) from None


def dumps(doc):
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def save(path, doc):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        f.write(dumps(doc).encode("utf-8"))


def new_doc(slug, model, project_name, source_repo):
    return {"schema": SCHEMA, "device_type": slug, "model": model, "project_name": project_name,
            "source_repo": source_repo, "updated": now_text(),
            "channels": {c: {"latest": None} for c in versions.CHANNELS}, "releases": []}


def now_text():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today():
    return datetime.datetime.now(datetime.timezone.utc).date()


# ---- editing ------------------------------------------------------------------------

def sort_releases(doc):
    def key(r):
        v = versions.parse_strict(r.get("version"))
        if v is None:
            raise ManifestError("release version %r does not parse" % r.get("version"))
        return v.key()
    doc["releases"] = sorted(doc.get("releases") or [], key=key, reverse=True)


def find(doc, version):
    for r in doc.get("releases") or []:
        if r.get("version") == version:
            return r
    return None


def insert(doc, release):
    """Add a release (its version must be new), keep the order, recompute
    the pointers and stamp `updated`."""
    if versions.parse_strict(release.get("version")) is None:
        raise ManifestError("version %r is not in the strict grammar" % release.get("version"))
    if find(doc, release["version"]) is not None:
        raise ManifestError("the manifest already carries %s" % release["version"])
    doc.setdefault("releases", []).append(release)
    sort_releases(doc)
    recompute_channels(doc)
    doc["updated"] = now_text()


def ordered_release(fields):
    """A release object with its keys in the documented order."""
    out = {}
    for k in RELEASE_KEYS:
        if k in fields and fields[k] is not None:
            out[k] = fields[k]
    return out


def computed_channels(releases):
    out = {}
    for c in versions.CHANNELS:
        best = None
        for r in releases:
            if r.get("yanked", False) or not versions.channel_visible(c, r.get("channel")):
                continue
            v = versions.parse_strict(r.get("version"))
            if v is not None and (best is None or v > best):
                best = v
        out[c] = {"latest": versions.to_str(best) if best is not None else None}
    return out


def recompute_channels(doc):
    doc["channels"] = computed_channels(doc.get("releases") or [])


def prune(doc, keep=KEEP_DEFAULT, on=None):
    """Move what no device needs to the archive: keeps the `keep` newest
    non-yanked releases, every pointed release, every stepping stone and
    every yanked release of the last twelve months. Returns the archived
    list (newest first)."""
    on = on or today()
    rels = list(doc.get("releases") or [])
    sort_releases(doc)
    rels = doc["releases"]
    keep_set = set()
    live = [r for r in rels if not r.get("yanked", False)]
    for r in live[:keep]:
        keep_set.add(id(r))
    for c in versions.CHANNELS:
        latest = computed_channels(rels)[c]["latest"]
        for r in rels:
            if r.get("version") == latest:
                keep_set.add(id(r))
    for r in rels:
        req = (r.get("requires") or {}).get("min_version")
        if not req:
            continue
        m = versions.parse_strict(req)
        rv = versions.parse_strict(r.get("version"))
        stone = None
        for s in rels:  # newest first
            sv = versions.parse_strict(s.get("version"))
            if s.get("yanked", False) or sv is None or m is None or rv is None:
                continue
            if m <= sv < rv and versions.channel_visible(r.get("channel"), s.get("channel")):
                stone = s
                break
        if stone is not None:
            keep_set.add(id(stone))
    for r in rels:
        if r.get("yanked", False):
            try:
                d = datetime.date.fromisoformat(r.get("date", ""))
            except ValueError:
                d = None
            if d is not None and (on - d).days <= YANKED_KEEP_DAYS:
                keep_set.add(id(r))
    archived = [r for r in rels if id(r) not in keep_set]
    doc["releases"] = [r for r in rels if id(r) in keep_set]
    recompute_channels(doc)
    return archived


def archive_add(archive_doc, slug, archived):
    if archive_doc is None:
        archive_doc = {"schema": SCHEMA, "device_type": slug, "releases": []}
    have = {r.get("version") for r in archive_doc.get("releases") or []}
    for r in archived:
        if r.get("version") not in have:
            archive_doc.setdefault("releases", []).append(r)
    sort_releases(archive_doc)
    return archive_doc


# ---- validation -----------------------------------------------------------------------

class Report:
    def __init__(self, where):
        self.where = where
        self.errors = []
        self.warnings = []

    def err(self, msg):
        self.errors.append("%s: %s" % (self.where, msg))

    def warn(self, msg):
        self.warnings.append("%s: %s" % (self.where, msg))


def _is_str(v, lo, hi):
    return isinstance(v, str) and lo <= len(v) <= hi


def _printable_ascii(s):
    return all(0x20 <= ord(c) <= 0x7E for c in s)


def _check_unknown(rep, obj, known, where):
    for k in obj:
        if k not in known:
            if k in RESERVED_KEYS:
                rep.err("%s: %r is reserved for a later schema and must not be used yet" % (where, k))
            else:
                rep.err("%s: unknown field %r" % (where, k))


def _read_csv_file(root, rel):
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        return None, None
    with open(path, "rb") as f:
        text = f.read().decode("utf-8", errors="replace")
    return layout_mod.read_csv(text)


def validate_release(rep, r, i, slug, model, root, on, csv_cache, is_archive=False):
    """The rules of one release object. Returns the parsed Version or None."""
    where = "releases[%d]" % i
    if not isinstance(r, dict):
        rep.err("%s: must be an object" % where)
        return None
    _check_unknown(rep, r, RELEASE_KEYS, where)
    for k in REQUIRED_RELEASE_KEYS:
        if k not in r:
            rep.err("%s: missing %r" % (where, k))
    ver = versions.parse_strict(r.get("version"))
    if ver is None:
        rep.err("%s.version: %r is not MAJOR.MINOR.PATCH[-alfa|beta.N]" % (where, r.get("version")))
    else:
        where = "releases[%d] (%s)" % (i, r["version"])

    fw = r.get("fw_version")
    if not _is_str(fw, 1, 31) or not _printable_ascii(fw):
        rep.err("%s.fw_version: must be 1 to 31 printable ASCII characters" % where)
    fb = r.get("fw_build")
    if fb is not None and (not _is_str(fb, 1, 64) or not _printable_ascii(fb)):
        rep.err("%s.fw_build: must be 1 to 64 printable ASCII characters" % where)
    commit = r.get("commit")
    if commit is not None and not (isinstance(commit, str) and HEX40_RE.match(commit)):
        rep.err("%s.commit: must be 40 lowercase hex characters" % where)
    tag = r.get("tag")
    if not (isinstance(tag, str) and TAG_RE.match(tag)):
        rep.err("%s.tag: must be a git tag name of 1 to 64 characters" % where)
    ch = r.get("channel")
    if versions.channel_rank(ch) is None:
        rep.err("%s.channel: must be stable, beta or alfa" % where)
    elif ch == "stable" and ver is not None and ver.pre != versions.PRE_NONE:
        rep.warn("%s: a stable release normally carries a version without a prerelease suffix" % where)
    prom = r.get("promoted")
    if prom is not None:
        if not isinstance(prom, dict):
            rep.err("%s.promoted: must be an object" % where)
        else:
            _check_unknown(rep, prom, PROMOTED_KEYS, where + ".promoted")
            for k, v in prom.items():
                if k in PROMOTED_KEYS and not _valid_date(v, on):
                    rep.err("%s.promoted.%s: must be YYYY-MM-DD, not in the future" % (where, k))
            if ch == "alfa" and prom:
                rep.err("%s.promoted: an alfa release has not been promoted" % where)
            if ch == "beta" and "stable" in prom:
                rep.err("%s.promoted.stable: a beta release has not reached stable" % where)
    if not _valid_date(r.get("date"), on):
        rep.err("%s.date: must be YYYY-MM-DD, not in the future" % where)

    for p in notes_mod.check_summary(r.get("summary")):
        (rep.warn if p.startswith("warning:") else rep.err)("%s.%s" % (where, p.replace("warning: ", "")))

    notes_rel = r.get("notes")
    want_notes = "%s/firmware/notes/%s.md" % (slug, r.get("version"))
    if notes_rel != want_notes:
        rep.err("%s.notes: must be %r" % (where, want_notes))
    elif root is not None:
        path = os.path.join(root, notes_rel)
        if not os.path.isfile(path):
            rep.err("%s.notes: %s does not exist" % (where, notes_rel))
        else:
            with open(path, "rb") as f:
                text = f.read().decode("utf-8", errors="replace")
            for p in notes_mod.check_notes(text, model, r.get("version")):
                if p.startswith("warning:"):
                    rep.warn("%s: %s: %s" % (where, notes_rel, p[len("warning: "):]))
                else:
                    rep.err("%s: %s: %s" % (where, notes_rel, p))

    req = r.get("requires")
    min_v = None
    if req is not None:
        if not isinstance(req, dict):
            rep.err("%s.requires: must be an object" % where)
        else:
            _check_unknown(rep, req, REQUIRES_KEYS, where + ".requires")
            min_v = versions.parse_strict(req.get("min_version"))
            if min_v is None:
                rep.err("%s.requires.min_version: not in the strict grammar" % where)
            elif ver is not None and not (min_v < ver):
                rep.err("%s.requires.min_version: must be lower than the version" % where)

    entries = None
    lay = r.get("layout")
    if not isinstance(lay, dict):
        rep.err("%s.layout: must be an object" % where)
    else:
        _check_unknown(rep, lay, LAYOUT_KEYS, where + ".layout")
        md5 = lay.get("md5")
        if not (isinstance(md5, str) and HEX32_RE.match(md5)):
            rep.err("%s.layout.md5: must be 32 lowercase hex characters" % where)
        else:
            want_table = "%s/firmware/layouts/%s.csv" % (slug, md5)
            if lay.get("table") != want_table:
                rep.err("%s.layout.table: must be %r" % (where, want_table))
            elif root is not None:
                entries = _layout_entries(rep, root, slug, md5, csv_cache, where + ".layout")

    img = r.get("image")
    if not isinstance(img, dict):
        rep.err("%s.image: must be an object" % where)
    else:
        _check_unknown(rep, img, IMAGE_KEYS, where + ".image")
        for k in IMAGE_KEYS:
            if k not in img:
                rep.err("%s.image: missing %r" % (where, k))
        _check_url(rep, img.get("url"), where + ".image.url")
        size = img.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < MIN_IMAGE_BYTES:
            rep.err("%s.image.size: must be an integer of at least %d" % (where, MIN_IMAGE_BYTES))
        elif entries is not None:
            slot = layout_mod.min_app_slot(entries)
            if slot is not None and size > slot:
                rep.err("%s.image.size: %d does not fit the smallest app slot (%d) of its layout"
                        % (where, size, slot))
        if not (isinstance(img.get("sha256"), str) and HEX64_RE.match(img["sha256"])):
            rep.err("%s.image.sha256: must be 64 lowercase hex characters" % where)
        pn = img.get("project_name")
        if not _is_str(pn, 1, 31) or not _printable_ascii(pn):
            rep.err("%s.image.project_name: must be 1 to 31 printable ASCII characters" % where)

    fz = r.get("flash_zip")
    if fz is not None:
        if not isinstance(fz, dict):
            rep.err("%s.flash_zip: must be an object" % where)
        else:
            _check_unknown(rep, fz, ZIP_KEYS, where + ".flash_zip")
            _check_url(rep, fz.get("url"), where + ".flash_zip.url")
            if not isinstance(fz.get("size"), int) or isinstance(fz.get("size"), bool) or fz["size"] <= 0:
                rep.err("%s.flash_zip.size: must be a positive integer" % where)
            if not (isinstance(fz.get("sha256"), str) and HEX64_RE.match(fz["sha256"])):
                rep.err("%s.flash_zip.sha256: must be 64 lowercase hex characters" % where)
    bl = r.get("bootloader")
    if bl is not None:
        if not isinstance(bl, dict):
            rep.err("%s.bootloader: must be an object" % where)
        else:
            _check_unknown(rep, bl, BOOTLOADER_KEYS, where + ".bootloader")
            if not (isinstance(bl.get("sha256"), str) and HEX64_RE.match(bl["sha256"])):
                rep.err("%s.bootloader.sha256: must be 64 lowercase hex characters" % where)

    lf = r.get("layout_from")
    if lf is not None:
        if not isinstance(lf, dict):
            rep.err("%s.layout_from: must be an object" % where)
        else:
            own_md5 = (lay or {}).get("md5") if isinstance(lay, dict) else None
            for k, v in lf.items():
                w = "%s.layout_from[%s]" % (where, k[:8])
                if not (isinstance(k, str) and HEX32_RE.match(k)):
                    rep.err("%s: the key must be a layout md5" % w)
                    continue
                if k == own_md5:
                    rep.err("%s: the release's own layout is not a FROM layout" % w)
                    continue
                if not isinstance(v, dict):
                    rep.err("%s: must be an object" % w)
                    continue
                _check_unknown(rep, v, FROM_KEYS, w)
                mig = v.get("migration")
                if mig not in MIGRATIONS:
                    rep.err("%s.migration: must be lossless, wipe or serial" % w)
                    continue
                wipes = v.get("wipes")
                if mig == "wipe":
                    if not isinstance(wipes, list) or not wipes or not all(isinstance(x, str) for x in wipes):
                        rep.err("%s.wipes: a wipe names the partitions, a non-empty list" % w)
                        wipes = None
                elif wipes is not None:
                    rep.err("%s.wipes: only a wipe carries wipes" % w)
                if root is None:
                    continue
                from_entries = _layout_entries(rep, root, slug, k, csv_cache, w)
                if from_entries is None or entries is None:
                    continue
                if wipes is not None:
                    labels = {e.label for e in from_entries}
                    for x in wipes:
                        if x not in labels:
                            rep.err("%s.wipes: %r is not a partition of the FROM layout" % (w, x))
                computed = layout_mod.verdict(from_entries, entries, True)
                if mig != "serial":
                    if computed.get("migration") != mig or \
                            sorted(computed.get("wipes") or []) != sorted(wipes or []):
                        rep.err("%s: declared %s %s, the anchor rule computes %s %s"
                                % (w, mig, wipes or [], computed.get("migration"), computed.get("wipes") or []))

    yanked = r.get("yanked")
    if not isinstance(yanked, bool):
        rep.err("%s.yanked: must be a boolean" % where)
    reason = r.get("yank_reason")
    if yanked is True:
        if not _is_str(reason, 1, 200):
            rep.err("%s.yank_reason: a yanked release states why (1 to 200 characters)" % where)
        elif notes_mod.has_long_dash(reason):
            rep.err("%s.yank_reason: contains a long dash" % where)
    elif reason is not None:
        rep.err("%s.yank_reason: only a yanked release carries a reason" % where)
    return ver


def _valid_date(v, on):
    if not (isinstance(v, str) and DATE_RE.match(v)):
        return False
    try:
        d = datetime.date.fromisoformat(v)
    except ValueError:
        return False
    return d <= on


def _check_url(rep, url, where):
    if not (isinstance(url, str) and URL_RE.match(url)):
        rep.err("%s: must be an absolute https URL" % where)


def _layout_entries(rep, root, slug, md5, cache, where):
    """The entries of `<slug>/firmware/layouts/<md5>.csv`, checked once:
    the file exists, parses, and re-encodes to the md5 it is named after."""
    rel = "%s/firmware/layouts/%s.csv" % (slug, md5)
    if rel in cache:
        return cache[rel]
    cache[rel] = None
    try:
        headers, entries = _read_csv_file(root, rel)
    except ValueError as exc:
        rep.err("%s: %s: %s" % (where, rel, exc))
        return None
    if entries is None:
        rep.err("%s: %s does not exist" % (where, rel))
        return None
    if not entries:
        rep.err("%s: %s has no partition rows" % (where, rel))
        return None
    got = layout_mod.table_md5(entries)
    if got != md5:
        rep.err("%s: %s re-encodes to %s, not the md5 it is named after" % (where, rel, got))
        return None
    if headers.get("layout") != md5:
        rep.err("%s: %s: the '# layout:' header must be %s" % (where, rel, md5))
    if headers.get("device_type") not in (None, slug):
        rep.err("%s: %s: the '# device_type:' header must be %s" % (where, rel, slug))
    if len(entries) > layout_mod.MAX_ENTRIES:
        rep.err("%s: %s has more than %d rows" % (where, rel, layout_mod.MAX_ENTRIES))
    cache[rel] = entries
    return entries


def validate(doc, slug, root=None, catalog_slugs=None, on=None, path_bytes=None):
    """(errors, warnings) for a manifest document of folder `slug`. `root`
    is the repository root for the notes and layout files (None skips the
    file checks); `catalog_slugs` the device types of device_catalog.json;
    `path_bytes` the file's size on disk."""
    on = on or today()
    rep = Report("%s/firmware/manifest.json" % slug)
    if path_bytes is not None and path_bytes > MAX_MANIFEST_BYTES:
        rep.err("%d bytes, the cap is %d (prune to the archive)" % (path_bytes, MAX_MANIFEST_BYTES))
    if not isinstance(doc, dict):
        rep.err("the document must be a JSON object")
        return rep.errors, rep.warnings
    _check_unknown(rep, doc, TOP_KEYS, "document")
    for k in TOP_KEYS:
        if k not in doc:
            rep.err("document: missing %r" % k)
    if doc.get("schema") != SCHEMA:
        rep.err("schema: must be %d" % SCHEMA)
    dt = doc.get("device_type")
    if not (isinstance(dt, str) and SLUG_RE.match(dt)):
        rep.err("device_type: must match %s" % SLUG_RE.pattern)
    elif dt != slug:
        rep.err("device_type: %r does not equal the folder name %r" % (dt, slug))
    elif catalog_slugs is not None and dt not in catalog_slugs:
        rep.err("device_type: %r is not in device_catalog.json" % dt)
    model = doc.get("model")
    if not _is_str(model, 1, 64) or model != model.strip():
        rep.err("model: must be 1 to 64 characters without surrounding whitespace")
        model = str(model)
    pn = doc.get("project_name")
    if not _is_str(pn, 1, 31) or not _printable_ascii(pn):
        rep.err("project_name: must be 1 to 31 printable ASCII characters")
    if not (isinstance(doc.get("source_repo"), str) and REPO_RE.match(doc["source_repo"])):
        rep.err("source_repo: must look like owner/repo")
    if not (isinstance(doc.get("updated"), str) and UPDATED_RE.match(doc["updated"])):
        rep.err("updated: must be YYYY-MM-DDTHH:MM:SSZ")

    rels = doc.get("releases")
    if not isinstance(rels, list) or not rels:
        rep.err("releases: must be a non-empty array")
        return rep.errors, rep.warnings

    csv_cache = {}
    parsed = []
    for i, r in enumerate(rels):
        parsed.append(validate_release(rep, r, i, slug, model, root, on, csv_cache))

    # order and uniqueness
    for i in range(1, len(parsed)):
        if parsed[i - 1] is not None and parsed[i] is not None and not (parsed[i - 1] > parsed[i]):
            rep.err("releases: not sorted strictly descending at index %d (%s then %s)"
                    % (i, rels[i - 1].get("version"), rels[i].get("version")))
    for field, getter in (("version", lambda r: r.get("version")),
                          ("fw_version", lambda r: r.get("fw_version")),
                          ("tag", lambda r: r.get("tag")),
                          ("image.sha256", lambda r: (r.get("image") or {}).get("sha256")
                           if isinstance(r.get("image"), dict) else None)):
        seen = {}
        for i, r in enumerate(rels):
            if not isinstance(r, dict):
                continue
            v = getter(r)
            if v is None:
                continue
            if v in seen:
                rep.err("releases[%d].%s: duplicates releases[%d]" % (i, field, seen[v]))
            else:
                seen[v] = i

    # the pointers
    want = computed_channels([r for r in rels if isinstance(r, dict)])
    chans = doc.get("channels")
    if not isinstance(chans, dict) or set(chans) != set(versions.CHANNELS) or \
            any(not isinstance(chans[c], dict) or set(chans[c]) != {"latest"} for c in chans):
        rep.err("channels: must be exactly {stable, beta, alfa}, each {\"latest\": version or null}")
    elif chans != want:
        rep.err("channels: must be the recomputed pointers %s" % json.dumps(want))
    lat = [versions.parse_strict(want[c]["latest"]) if want[c]["latest"] else None for c in versions.CHANNELS]
    for a, b, na, nb in ((lat[2], lat[1], "alfa", "beta"), (lat[1], lat[0], "beta", "stable")):
        if a is not None and b is not None and a < b:
            rep.err("channels: %s.latest is below %s.latest" % (na, nb))

    # stepping stones
    for i, r in enumerate(rels):
        if not isinstance(r, dict) or parsed[i] is None:
            continue
        req = (r.get("requires") or {}).get("min_version") if isinstance(r.get("requires"), dict) else None
        m = versions.parse_strict(req) if req else None
        if m is None:
            continue
        found = False
        for j, s in enumerate(rels):
            if j == i or not isinstance(s, dict) or parsed[j] is None or s.get("yanked", False):
                continue
            if m <= parsed[j] < parsed[i] and versions.channel_visible(r.get("channel"), s.get("channel")):
                found = True
                break
        if not found:
            rep.err("releases[%d] (%s): requires %s first, but no non-yanked release between %s and it is "
                    "visible from channel %s" % (i, r.get("version"), req, req, r.get("channel")))
    return rep.errors, rep.warnings


def validate_archive(doc, slug, root=None, on=None, path_bytes=None):
    on = on or today()
    rep = Report("%s/firmware/archive.json" % slug)
    if path_bytes is not None and path_bytes > MAX_ARCHIVE_BYTES:
        rep.err("%d bytes, the cap is %d" % (path_bytes, MAX_ARCHIVE_BYTES))
    if not isinstance(doc, dict):
        rep.err("the document must be a JSON object")
        return rep.errors, rep.warnings
    _check_unknown(rep, doc, ARCHIVE_TOP_KEYS, "document")
    if doc.get("schema") != SCHEMA:
        rep.err("schema: must be %d" % SCHEMA)
    if doc.get("device_type") != slug:
        rep.err("device_type: must equal the folder name %r" % slug)
    rels = doc.get("releases")
    if not isinstance(rels, list):
        rep.err("releases: must be an array")
        return rep.errors, rep.warnings
    cache = {}
    for i, r in enumerate(rels):
        validate_release(rep, r, i, slug, "", None, on, cache, is_archive=True)
    return rep.errors, rep.warnings


# ---- self test -------------------------------------------------------------------------

def _demo_doc():
    doc = new_doc("wican_pro", "WiCAN PRO", "wican-fw_obd_pro", "meatpiHQ/wican-fw")
    a = "a" * 32
    b = "b" * 32
    base = {"tag": "v6.1.0p", "channel": "stable", "date": "2026-01-02", "summary": "One.",
            "image": {"url": "https://x/y.bin", "size": 1000000, "sha256": "1" * 64, "project_name": "p"},
            "layout": {"md5": a, "table": "wican_pro/firmware/layouts/%s.csv" % a}, "yanked": False}
    for v, fw, tag, ch in (("6.0.0", "6.0.0", "v6.0.0p", "stable"), ("6.1.0", "6.1.0", "v6.1.0p", "beta"),
                           ("6.2.0-alfa.1", "6.2.0-alfa.1", "v6.2.0p-alfa.1", "alfa")):
        r = dict(base)
        r.update({"version": v, "fw_version": fw, "tag": tag, "channel": ch,
                  "notes": "wican_pro/firmware/notes/%s.md" % v,
                  "image": dict(base["image"], sha256=(v[0] + v[2] + v[4]) * 21 + "x"[:1] * 0 + "0" * (64 - 63))})
        r["image"]["sha256"] = ("%02d" % len(doc["releases"])) * 32
        insert(doc, r)
    doc["releases"][0]["layout"] = {"md5": b, "table": "wican_pro/firmware/layouts/%s.csv" % b}
    doc["releases"][0]["layout_from"] = {a: {"migration": "serial"}}
    return doc


def self_test():
    doc = _demo_doc()
    assert [r["version"] for r in doc["releases"]] == ["6.2.0-alfa.1", "6.1.0", "6.0.0"]
    assert doc["channels"] == {"stable": {"latest": "6.0.0"}, "beta": {"latest": "6.1.0"},
                               "alfa": {"latest": "6.2.0-alfa.1"}}, doc["channels"]
    errors, warnings = validate(doc, "wican_pro", root=None, catalog_slugs={"wican_pro"},
                                on=datetime.date(2026, 10, 19))
    assert errors == [], errors
    bad = json.loads(dumps(doc))
    bad["releases"][1]["hw"] = "rev2"
    bad["releases"][1]["yank_reason"] = "x"
    bad["releases"][2]["channel"] = "alfa"
    bad["channels"]["stable"]["latest"] = "6.0.0"
    bad["releases"][0]["requires"] = {"min_version": "6.1.5"}
    bad["releases"][0]["date"] = "2030-01-01"
    errors, _ = validate(bad, "wican_pro", root=None, catalog_slugs={"wican_pro"},
                         on=datetime.date(2026, 10, 19))
    text = "\n".join(errors)
    for needle in ("reserved", "only a yanked release", "recomputed pointers", "requires 6.1.5 first",
                   "not in the future"):
        assert needle in text, (needle, errors)
    try:
        insert(doc, {"version": "6.1.0"})
        raise AssertionError("a duplicate was inserted")
    except ManifestError:
        pass
    # pruning keeps the pointed and the newest, archives the rest
    big = new_doc("x", "X", "x-fw", "o/r")
    for i in range(30):
        r = {"version": "1.0.%d" % i, "fw_version": "1.0.%d" % i, "tag": "v1.0.%d" % i, "channel": "stable",
             "date": "2026-01-01", "summary": "s", "notes": "x/firmware/notes/1.0.%d.md" % i,
             "image": {"url": "https://x/y", "size": 70000, "sha256": "%02d" % i * 32, "project_name": "p"},
             "layout": {"md5": "a" * 32, "table": "x/firmware/layouts/%s.csv" % ("a" * 32)}, "yanked": False}
        insert(big, r)
    big["releases"][-1]["yanked"] = True
    big["releases"][-1]["yank_reason"] = "old"
    big["releases"][-1]["date"] = "2020-01-01"
    archived = prune(big, keep=5, on=datetime.date(2026, 10, 19))
    kept = [r["version"] for r in big["releases"]]
    assert kept == ["1.0.29", "1.0.28", "1.0.27", "1.0.26", "1.0.25"], kept
    assert len(archived) == 25 and archived[-1]["version"] == "1.0.0"
    arc = archive_add(None, "x", archived)
    assert len(arc["releases"]) == 25 and arc["releases"][0]["version"] == "1.0.24"
    e, _ = validate_archive(arc, "x", on=datetime.date(2026, 10, 19))
    assert e == [], e
    assert ordered_release({"yanked": False, "version": "1.0.0", "tag": "t", "fw_build": None}) == \
        {"version": "1.0.0", "tag": "t", "yanked": False}
    return 1


if __name__ == "__main__":
    self_test()
    print("manifest: OK")
