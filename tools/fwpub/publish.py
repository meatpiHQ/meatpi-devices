"""The publisher behind each firmware repository's `tools/release.py`:

    release.py check                        the local checks; prints the release it would add
    release.py publish --channel alfa       build, check, tag, upload, manifest, commit, push
    release.py publish --channel stable     the same, plus the stable mirror release on the firmware repo
    release.py promote 6.1.0-beta.2 --to stable
    release.py yank 6.1.0-alfa.1 --reason "boot loop with an SD card inserted" [--keep-assets]
    release.py notes 6.1.0-alfa.1 [--file PATH]   amend the notes after the release
    flags: --dry-run --no-build --no-push --resume --tag --date YYYY-MM-DD
           --summary "..." --token-file PATH --devices PATH

The firmware repository holds `version.txt`, `release-notes/<version>.md`
and `tools/release.json` (the product: slug, model, names, the mirror).
The devices checkout (meatpi-devices) receives the manifest commit. A
fine-grained token with Contents read and write on the devices repository
and the firmware repository comes from MEATPI_RELEASE_TOKEN or
--token-file; nothing here runs in CI. Stdlib only.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

from . import appdesc, flashset, github, gitops, layout as layout_mod, manifest as manifest_mod, notes as notes_mod, versions

DEVICES_REPO_DEFAULT = "meatpiHQ/meatpi-devices"


class Fatal(SystemExit):
    def __init__(self, msg):
        super().__init__("error: " + msg)


# ---- configuration -------------------------------------------------------------------

def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for k in ("slug", "model", "project_name", "source_repo", "asset_name"):
        if not cfg.get(k):
            raise Fatal("%s: missing %r" % (path, k))
    cfg.setdefault("devices_repo", DEVICES_REPO_DEFAULT)
    cfg.setdefault("hw_letter", "")
    cfg.setdefault("source_tag", "v{version_tagged}")
    cfg.setdefault("zip_name", "")
    cfg.setdefault("partition_words", {})
    cfg.setdefault("keep_in_manifest", manifest_mod.KEEP_DEFAULT)
    cfg.setdefault("chip", "esp32s3")
    return cfg


def version_tagged(cfg, version):
    """The version with the hardware letter after the patch: 6.1.0p-alfa.1."""
    letter = cfg.get("hw_letter") or ""
    if not letter:
        return version
    head, sep, pre = version.partition("-")
    return head + letter + (sep + pre if sep else "")


def names(cfg, version):
    vt = version_tagged(cfg, version)
    fmt = {"version": version, "version_tagged": vt}
    out = {"source_tag": cfg["source_tag"].format(**fmt),
           "asset": cfg["asset_name"].format(**fmt),
           "zip": cfg["zip_name"].format(**fmt) if cfg.get("zip_name") else "",
           "devices_tag": "%s-%s" % (cfg["slug"], version),
           "release_name": "%s %s" % (cfg["model"], version)}
    mirror = cfg.get("stable_mirror") or {}
    if mirror:
        out["mirror_tag"] = (mirror.get("tag") or "v{version_tagged}").format(**fmt)
        out["mirror_name"] = (mirror.get("name") or "{model} {version}").format(model=cfg["model"], **fmt)
    return out


def asset_url(repo, tag, name):
    return "https://github.com/%s/releases/download/%s/%s" % (repo, tag, name)


# ---- the preparation shared by check and publish ---------------------------------------

class Plan:
    """Everything a publish needs, computed up front so a dry run can show it."""

    def __init__(self):
        self.warnings = []
        self.actions = []


def say(msg):
    print(msg, flush=True)


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def prepare(a, repo_root, cfg, devices_root, dry_run):
    p = Plan()
    slug = cfg["slug"]

    # the version
    vpath = os.path.join(repo_root, "version.txt")
    if not os.path.isfile(vpath):
        raise Fatal("no version.txt in " + repo_root)
    with open(vpath, "r", encoding="utf-8") as f:
        version = f.read().strip()
    ver = versions.parse_strict(version)
    if ver is None:
        raise Fatal("version.txt %r is not MAJOR.MINOR.PATCH[-alfa|beta.N]" % version)
    p.version = version
    p.n = names(cfg, version)
    if a.channel == "stable" and ver.pre != versions.PRE_NONE:
        p.warnings.append("a stable release normally carries a version without a prerelease suffix")
    if a.channel != "stable" and ver.pre == versions.PRE_NONE:
        p.warnings.append("version %s has no prerelease suffix but goes to channel %s" % (version, a.channel))

    # the notes
    npath = os.path.join(repo_root, "release-notes", version + ".md")
    if not os.path.isfile(npath):
        raise Fatal("write release-notes/%s.md first (copy release-notes/TEMPLATE.md)" % version)
    meta, body = notes_mod.split_front_matter(_read(npath).decode("utf-8"))
    if "TODO" in body or "TODO" in json.dumps(meta):
        raise Fatal("release-notes/%s.md still contains TODO" % version)
    summary = a.summary or meta.get("summary") or ""
    bad = [x for x in notes_mod.check_summary(summary) if not x.startswith("warning:")]
    if bad:
        raise Fatal("summary (the front matter or --summary): " + "; ".join(bad))
    p.summary = summary
    p.contributors = meta.get("contributors") or []
    if isinstance(p.contributors, str):
        p.contributors = [c.strip() for c in p.contributors.split(",") if c.strip()]

    # the firmware checkout
    if not gitops.is_repo(repo_root):
        raise Fatal(repo_root + " is not a git checkout")
    dirty = gitops.dirty_paths(repo_root)
    if dirty:
        msg = "the firmware tree has %d uncommitted path(s): %s" % (len(dirty), ", ".join(d[3:] for d in dirty[:4]))
        if dry_run:
            p.warnings.append(msg)
        else:
            raise Fatal(msg + " (commit first)")
    p.head = gitops.head_sha(repo_root)
    at_head = gitops.tags_at_head(repo_root)
    if a.tag:
        if gitops.tag_exists(repo_root, p.n["source_tag"]):
            if gitops.tag_commit(repo_root, p.n["source_tag"]) != p.head:
                raise Fatal("tag %s exists on another commit" % p.n["source_tag"])
            p.actions.append("tag %s is already at HEAD" % p.n["source_tag"])
        else:
            p.actions.append("create the annotated tag %s at HEAD and push it" % p.n["source_tag"])
    elif p.n["source_tag"] not in at_head:
        other = [t for t in at_head if t.startswith("v")]
        msg = "HEAD is not tagged %s (pass --tag to create it)" % p.n["source_tag"]
        if other:
            msg += "; HEAD carries %s, which names another version than version.txt" % ", ".join(other)
        if dry_run:
            p.warnings.append(msg)
        else:
            raise Fatal(msg)

    # the devices checkout
    if not gitops.is_repo(devices_root):
        raise Fatal(devices_root + " is not a git checkout of the devices repository")
    if gitops.branch(devices_root) != "main":
        raise Fatal("the devices checkout is not on main")
    ddirty = gitops.dirty_paths(devices_root)
    if ddirty:
        msg = "the devices checkout has uncommitted changes: " + ", ".join(d[3:] for d in ddirty[:4])
        if dry_run:
            p.warnings.append(msg)
        else:
            raise Fatal(msg)
    if gitops.fetch(devices_root):
        behind = gitops.behind_upstream(devices_root)
        if behind:
            raise Fatal("the devices checkout is %d commit(s) behind origin/main: pull first" % behind)
    else:
        p.warnings.append("could not fetch the devices repository (offline?)")
    mpath = os.path.join(devices_root, slug, "firmware", "manifest.json")
    if os.path.isfile(mpath):
        doc = manifest_mod.load(mpath)
    else:
        doc = manifest_mod.new_doc(slug, cfg["model"], cfg["project_name"], cfg["source_repo"])
        p.warnings.append("no manifest for %s yet: this publish creates it" % slug)
    if manifest_mod.find(doc, version) is not None:
        raise Fatal("the manifest already carries %s (a rebuild is a new version)" % version)
    p.doc = doc
    p.mpath = mpath

    # the build
    build_dir = os.path.join(repo_root, "build")
    if not a.no_build:
        p.actions.append("build the firmware")
        if not dry_run:
            run_build(repo_root, cfg)
    pd_path = os.path.join(build_dir, "project_description.json")
    if not os.path.isfile(pd_path):
        raise Fatal("no build/project_description.json: build first, or drop --no-build")
    with open(pd_path, "r", encoding="utf-8") as f:
        pd = json.load(f)
    if pd.get("project_version") != version:
        raise Fatal("the build is version %r, version.txt says %r: rebuild" % (pd.get("project_version"), version))
    if pd.get("project_name") != cfg["project_name"]:
        raise Fatal("the build's project name is %r, release.json says %r" % (pd.get("project_name"), cfg["project_name"]))
    p.has_migrate = "partition_migrate" in (pd.get("build_components") or [])
    if a.no_build:
        p.warnings.append("--no-build: the image in build/ must come from HEAD (%s)" % p.head[:7])

    # the image
    image_path = os.path.join(build_dir, pd.get("app_bin") or "")
    if not os.path.isfile(image_path):
        raise Fatal("the image %s is missing" % image_path)
    image = _read(image_path)
    try:
        desc = appdesc.parse(image)
    except appdesc.ImageError as exc:
        raise Fatal("%s: %s" % (image_path, exc))
    if not appdesc.verify(image):
        raise Fatal("the image's checksum or appended digest does not hold")
    if desc["version"] != version:
        raise Fatal("the image's descriptor says version %r, not %r" % (desc["version"], version))
    if desc["project_name"] != cfg["project_name"]:
        raise Fatal("the image's descriptor says project %r, not %r" % (desc["project_name"], cfg["project_name"]))
    p.image = image
    p.image_sha = hashlib.sha256(image).hexdigest()
    p.fw_build = gitops.describe(repo_root)

    # the layout and its verdicts
    table_path = os.path.join(build_dir, "partition_table", "partition-table.bin")
    if not os.path.isfile(table_path):
        raise Fatal("no partition-table.bin in the build")
    table = _read(table_path)
    entries = layout_mod.parse_table(table)
    if not entries:
        raise Fatal("the build's partition table does not parse")
    stored = layout_mod.stored_md5(table)
    md5 = layout_mod.table_md5(entries)
    if stored is not None and stored != md5:
        raise Fatal("the table's stored digest %s differs from the computed %s" % (stored, md5))
    slot = layout_mod.min_app_slot(entries)
    if slot is not None and len(image) > slot:
        raise Fatal("the image (%d B) does not fit the smallest app slot (%d B)" % (len(image), slot))
    p.entries = entries
    p.md5 = md5
    layouts_dir = os.path.join(devices_root, slug, "firmware", "layouts")
    p.layout_from = {}
    p.layout_headers = {}
    if os.path.isdir(layouts_dir):
        for name in sorted(os.listdir(layouts_dir)):
            if not name.endswith(".csv"):
                continue
            headers, other = layout_mod.read_csv(_read(os.path.join(layouts_dir, name)).decode("utf-8"))
            p.layout_headers[name[:-4]] = headers
            if name[:-4] != md5:
                p.layout_from[name[:-4]] = layout_mod.verdict(other, entries, p.has_migrate)
    p.new_layout = md5 not in p.layout_headers
    if p.new_layout:
        p.actions.append("add the layout %s (new) under %s/firmware/layouts" % (md5, slug))

    # the flash set
    p.zip = None
    p.zip_sha = None
    p.bootloader_sha = None
    if p.n["zip"]:
        try:
            p.zip, p.bootloader_sha = flashset.build(build_dir, image, p.n["asset"], cfg.get("chip", "esp32s3"))
        except flashset.FlashSetError as exc:
            raise Fatal("the serial flash set: %s" % exc)
        p.zip_sha = hashlib.sha256(p.zip).hexdigest()

    # the notes, with the layout block rendered
    own = dict(p.layout_headers.get(md5) or {})
    own.setdefault("name", cfg.get("layout_name", ""))
    lines = notes_mod.layout_sentences(p.layout_from, dict(p.layout_headers, **{md5: own}),
                                       cfg.get("partition_words") or {}, md5)
    body = notes_mod.render_layout_block(body, lines)
    body = notes_mod.strip_layout_markers(body)
    problems = [x for x in notes_mod.check_notes(body, cfg["model"], version) if not x.startswith("warning:")]
    if problems:
        raise Fatal("release-notes/%s.md: %s" % (version, "; ".join(problems)))
    p.notes_text = body

    # the release object
    date = a.date or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    p.release = manifest_mod.ordered_release({
        "version": version, "fw_version": desc["version"], "fw_build": p.fw_build, "commit": p.head,
        "tag": p.n["source_tag"], "channel": a.channel, "date": date, "summary": summary,
        "notes": "%s/firmware/notes/%s.md" % (slug, version),
        "image": {"url": asset_url(cfg["devices_repo"], p.n["devices_tag"], p.n["asset"]),
                  "size": len(image), "sha256": p.image_sha, "project_name": desc["project_name"]},
        "flash_zip": ({"url": asset_url(cfg["devices_repo"], p.n["devices_tag"], p.n["zip"]),
                       "size": len(p.zip), "sha256": p.zip_sha} if p.zip else None),
        "bootloader": ({"sha256": p.bootloader_sha} if p.bootloader_sha else None),
        "layout": {"md5": md5, "table": "%s/firmware/layouts/%s.csv" % (slug, md5)},
        "layout_from": p.layout_from or None, "yanked": False,
    })

    # the manifest after the insert, validated without the files
    trial = json.loads(json.dumps(doc))
    manifest_mod.insert(trial, json.loads(json.dumps(p.release)))
    archived = manifest_mod.prune(trial, keep=int(cfg.get("keep_in_manifest") or manifest_mod.KEEP_DEFAULT))
    errors, warnings = manifest_mod.validate(trial, slug, root=None)
    if errors:
        raise Fatal("the manifest would not validate: " + "; ".join(errors[:4]))
    p.warnings.extend(w for w in warnings if "no release refers" not in w)
    p.channels_after = trial["channels"]
    p.archived = len(archived)
    p.actions.append("create the release %s on %s (%s), upload %s%s" % (
        p.n["devices_tag"], cfg["devices_repo"], "pre-release" if a.channel != "stable" else "release",
        p.n["asset"], (" and " + p.n["zip"]) if p.zip else ""))
    p.actions.append("insert %s into %s/firmware/manifest.json, pointers %s%s" % (
        version, slug, json.dumps(p.channels_after), (", %d older release(s) to archive.json" % p.archived) if p.archived else ""))
    p.actions.append("commit and push the devices checkout: '%s: publish %s (%s)'" % (slug, version, a.channel))
    if a.channel == "stable" and cfg.get("stable_mirror"):
        p.actions.append("create the mirror release %s (%s) on %s with the same files" % (
            p.n["mirror_tag"], p.n["mirror_name"], cfg["stable_mirror"]["repo"]))
    return p


def run_build(repo_root, cfg):
    cmd = cfg.get("build_command")
    if not cmd:
        if os.name == "nt" and os.path.isfile(os.path.join(repo_root, "build.ps1")):
            cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "build.ps1"]
        else:
            cmd = ["idf.py", "build"]
    log = os.path.join(repo_root, "build", "release_build.log")
    os.makedirs(os.path.dirname(log), exist_ok=True)
    say("building (%s), log %s" % (" ".join(cmd), log))
    with open(log, "wb") as f:
        rc = subprocess.call(cmd, cwd=repo_root, stdout=f, stderr=subprocess.STDOUT)
    if rc != 0:
        raise Fatal("the build failed (exit %d): see %s" % (rc, log))


def print_plan(p, cfg, a):
    say("release %s %s on channel %s (tag %s, build %s)" % (cfg["model"], p.version, a.channel, p.n["source_tag"], p.fw_build))
    say("  image %d B sha256 %s, layout %s%s" % (len(p.image), p.image_sha[:16], p.md5[:8], " (new)" if p.new_layout else ""))
    for md5, v in sorted(p.layout_from.items()):
        say("  from layout %s: %s%s" % (md5[:8], v["migration"], (" " + ", ".join(v.get("wipes") or [])) if v.get("wipes") else ""))
    say("  notes %d B, summary: %s" % (len(p.notes_text.encode("utf-8")), p.summary))
    say(json.dumps(p.release, indent=2))
    for w in p.warnings:
        say("warning: " + w)
    say("actions:")
    for x in p.actions:
        say("  - " + x)


# ---- GitHub steps ---------------------------------------------------------------------

def ensure_release(gh, repo, tag, name, body, prerelease, resume, dry_run):
    """The release on `repo` with `tag`: found (resume) or created."""
    if dry_run:
        return None
    rel = None
    try:
        rel = gh.release_by_tag(repo, tag)
    except github.GitHubError as exc:
        if exc.status != 404:
            raise
    if rel is not None:
        if not resume:
            raise Fatal("the release %s already exists on %s (pass --resume to finish it)" % (tag, repo))
        say("  reusing the release %s on %s" % (tag, repo))
        return rel
    say("  creating the release %s on %s" % (tag, repo))
    return gh.create_release(repo, tag, name, body, prerelease=prerelease, target="main")


def ensure_asset(gh, repo, rel, name, data, sha, dry_run):
    """Upload `data` as `name` unless an asset of that name with the same
    size and digest is already there; a different one is fatal."""
    if dry_run:
        return
    existing = github.find_asset(rel, name)
    if existing is None:
        for a in gh.assets(repo, rel["id"]):
            if a.get("name") == name:
                existing = a
    if existing is not None:
        d = github.asset_digest(existing)
        if existing.get("size") == len(data) and (d is None or d == sha):
            say("  %s is already uploaded (%d B)" % (name, len(data)))
            return
        raise Fatal("the asset %s on %s differs from the local file (size %s, digest %s): the wrong bytes are online"
                    % (name, rel.get("tag_name"), existing.get("size"), d))
    say("  uploading %s (%d B)" % (name, len(data)))
    up = gh.upload_asset(rel["upload_url"], name, data)
    d = github.asset_digest(up)
    if up.get("size") != len(data) or (d is not None and d != sha):
        raise Fatal("GitHub reports %s as %s B digest %s after the upload; expected %d B %s"
                    % (name, up.get("size"), d, len(data), sha))


def devices_body(cfg, version, channel):
    return ("%s firmware %s, channel %s.\nRelease notes: https://github.com/%s/blob/main/%s/firmware/notes/%s.md\n"
            "Install from the device's web page, or let the device update itself."
            % (cfg["model"], version, channel, cfg["devices_repo"], cfg["slug"], version))


def mirror_body(repo_root, cfg, notes_text, version, contributors):
    body = notes_text.rstrip("\n") + "\n"
    footer = (cfg.get("stable_mirror") or {}).get("footer")
    if footer:
        fpath = os.path.join(repo_root, footer)
        if os.path.isfile(fpath):
            with open(fpath, "r", encoding="utf-8") as f:
                body += "\n" + f.read().replace("{version}", version).rstrip("\n") + "\n"
    if contributors:
        body += "\nContributors: " + " ".join(contributors) + "\n"
    return body


# ---- commands ---------------------------------------------------------------------------

def cmd_check(a, repo_root, cfg, devices_root):
    p = prepare(a, repo_root, cfg, devices_root, dry_run=True)
    print_plan(p, cfg, a)
    say("check: OK (nothing written)")
    return 0


def cmd_publish(a, repo_root, cfg, devices_root, token):
    p = prepare(a, repo_root, cfg, devices_root, dry_run=a.dry_run)
    print_plan(p, cfg, a)
    if a.dry_run:
        say("dry run: nothing written")
        return 0
    if not token:
        raise Fatal("no token: set MEATPI_RELEASE_TOKEN or pass --token-file")
    slug = cfg["slug"]
    gh = github.GitHub(token=token)

    # 1. the files in the devices checkout
    from add_layout import write_layout  # the same writer the seeding tool uses
    rel_csv, _md5, created = write_layout(devices_root, slug, p.entries, {
        "name": cfg.get("layout_name", ""), "first_seen": p.version, "last_seen": p.version,
        "source": cfg.get("layout_source", "")})
    say("  %s %s" % ("wrote" if created else "updated", rel_csv))
    notes_path = os.path.join(devices_root, slug, "firmware", "notes", p.version + ".md")
    os.makedirs(os.path.dirname(notes_path), exist_ok=True)
    with open(notes_path, "wb") as f:
        f.write(p.notes_text.encode("utf-8"))
    say("  wrote %s/firmware/notes/%s.md" % (slug, p.version))

    # 2. the tag
    if a.tag and not gitops.tag_exists(repo_root, p.n["source_tag"]):
        gitops.create_tag(repo_root, p.n["source_tag"], "%s %s" % (cfg["model"], p.version))
        gitops.push(repo_root, p.n["source_tag"])
        say("  tagged %s and pushed the tag" % p.n["source_tag"])
        p.fw_build = gitops.describe(repo_root)
        p.release["fw_build"] = p.fw_build

    # 3. the release that holds the files
    rel = ensure_release(gh, cfg["devices_repo"], p.n["devices_tag"], p.n["release_name"],
                         devices_body(cfg, p.version, a.channel), a.channel != "stable", a.resume, False)
    ensure_asset(gh, cfg["devices_repo"], rel, p.n["asset"], p.image, p.image_sha, False)
    if p.zip:
        ensure_asset(gh, cfg["devices_repo"], rel, p.n["zip"], p.zip, p.zip_sha, False)

    # 4. the manifest
    doc = p.doc
    manifest_mod.insert(doc, p.release)
    archived = manifest_mod.prune(doc, keep=int(cfg.get("keep_in_manifest") or manifest_mod.KEEP_DEFAULT))
    apath = os.path.join(devices_root, slug, "firmware", "archive.json")
    if archived:
        adoc = manifest_mod.load(apath) if os.path.isfile(apath) else None
        manifest_mod.save(apath, manifest_mod.archive_add(adoc, slug, archived))
        say("  moved %d release(s) to archive.json" % len(archived))
    errors, warnings = manifest_mod.validate(doc, slug, root=devices_root)
    for w in warnings:
        if "no release refers" not in w:
            say("  warning: " + w)
    if errors:
        raise Fatal("the manifest does not validate; nothing committed: " + "; ".join(errors[:4]))
    manifest_mod.save(p.mpath, doc)
    say("  wrote %s/firmware/manifest.json, pointers %s" % (slug, json.dumps(doc["channels"])))

    # 5. commit and push the devices checkout
    msg = "%s: publish %s (%s)" % (slug, p.version, a.channel)
    sha = gitops.commit_paths(devices_root, [os.path.join(slug, "firmware")], msg)
    say("  committed %s: %s" % (sha[:7], msg))
    if a.no_push:
        say("  --no-push: push the devices checkout yourself")
    else:
        gitops.push(devices_root, "main")
        say("  pushed; devices on channel %s see %s at their next check" % (a.channel, p.version))

    # 6. the stable mirror
    if a.channel == "stable" and cfg.get("stable_mirror"):
        m = cfg["stable_mirror"]
        mrel = ensure_release(gh, m["repo"], p.n["mirror_tag"], p.n["mirror_name"],
                              mirror_body(repo_root, cfg, p.notes_text, p.version, p.contributors), False, a.resume, False)
        ensure_asset(gh, m["repo"], mrel, p.n["asset"], p.image, p.image_sha, False)
        if p.zip:
            ensure_asset(gh, m["repo"], mrel, p.n["zip"], p.zip, p.zip_sha, False)
        say("  mirror release %s on %s is complete" % (p.n["mirror_tag"], m["repo"]))
    say("published %s %s (%s)" % (cfg["model"], p.version, a.channel))
    return 0


def _load_doc_for_edit(cfg, devices_root, version, dry_run):
    if gitops.branch(devices_root) != "main":
        raise Fatal("the devices checkout is not on main")
    if gitops.dirty_paths(devices_root) and not dry_run:
        raise Fatal("the devices checkout has uncommitted changes")
    mpath = os.path.join(devices_root, cfg["slug"], "firmware", "manifest.json")
    doc = manifest_mod.load(mpath)
    rel = manifest_mod.find(doc, version)
    if rel is None:
        raise Fatal("the manifest does not carry %s" % version)
    return doc, rel, mpath


def _save_and_commit(doc, mpath, cfg, devices_root, msg, dry_run, no_push):
    errors, _w = manifest_mod.validate(doc, cfg["slug"], root=devices_root)
    if errors:
        raise Fatal("the manifest would not validate: " + "; ".join(errors[:4]))
    say("  pointers %s" % json.dumps(doc["channels"]))
    if dry_run:
        say("dry run: nothing written")
        return
    manifest_mod.save(mpath, doc)
    sha = gitops.commit_paths(devices_root, [os.path.join(cfg["slug"], "firmware")], msg)
    say("  committed %s: %s" % (sha[:7], msg))
    if not no_push:
        gitops.push(devices_root, "main")
        say("  pushed")


def cmd_promote(a, repo_root, cfg, devices_root, token):
    doc, rel, mpath = _load_doc_for_edit(cfg, devices_root, a.version, a.dry_run)
    frm = versions.channel_rank(rel.get("channel"))
    to = versions.channel_rank(a.to)
    if to is None or to >= frm:
        raise Fatal("%s is on %s; a promotion only moves up (beta or stable)" % (a.version, rel.get("channel")))
    if rel.get("yanked"):
        raise Fatal("%s is yanked" % a.version)
    rel["channel"] = a.to
    rel.setdefault("promoted", {})[a.to] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    rel2 = manifest_mod.ordered_release(rel)
    doc["releases"][[r.get("version") for r in doc["releases"]].index(a.version)] = rel2
    manifest_mod.recompute_channels(doc)
    doc["updated"] = manifest_mod.now_text()
    say("promote %s %s to %s" % (cfg["model"], a.version, a.to))
    _save_and_commit(doc, mpath, cfg, devices_root, "%s: promote %s to %s" % (cfg["slug"], a.version, a.to), a.dry_run, a.no_push)
    if a.dry_run:
        return 0
    if not token:
        say("  no token: the GitHub release flags and the mirror are left to you")
        return 0
    gh = github.GitHub(token=token)
    n = names(cfg, a.version)
    try:
        drel = gh.release_by_tag(cfg["devices_repo"], n["devices_tag"])
        gh.patch_release(cfg["devices_repo"], drel["id"], {"prerelease": a.to != "stable"})
        say("  %s on %s marked %s" % (n["devices_tag"], cfg["devices_repo"], "release" if a.to == "stable" else "pre-release"))
    except github.GitHubError as exc:
        say("  warning: could not update the devices release: %s" % exc)
        drel = None
    if a.to == "stable" and cfg.get("stable_mirror") and drel is not None:
        m = cfg["stable_mirror"]
        with tempfile.TemporaryDirectory(prefix="fwpub_") as tmp:
            img = os.path.join(tmp, n["asset"])
            gh.download(rel2["image"]["url"], img, expect_size=rel2["image"]["size"])
            data = _read(img)
            if hashlib.sha256(data).hexdigest() != rel2["image"]["sha256"]:
                raise Fatal("the stored image does not match its sha256")
            notes_path = os.path.join(devices_root, cfg["slug"], "firmware", "notes", a.version + ".md")
            body = mirror_body(repo_root, cfg, _read(notes_path).decode("utf-8"), a.version, [])
            mrel = ensure_release(gh, m["repo"], n["mirror_tag"], n["mirror_name"], body, False, True, False)
            ensure_asset(gh, m["repo"], mrel, n["asset"], data, rel2["image"]["sha256"], False)
            fz = rel2.get("flash_zip")
            if fz:
                zp = os.path.join(tmp, n["zip"])
                gh.download(fz["url"], zp, expect_size=fz["size"])
                ensure_asset(gh, m["repo"], mrel, n["zip"], _read(zp), fz["sha256"], False)
        say("  mirror release %s on %s is complete" % (n["mirror_tag"], m["repo"]))
    return 0


def cmd_yank(a, repo_root, cfg, devices_root, token):
    doc, rel, mpath = _load_doc_for_edit(cfg, devices_root, a.version, a.dry_run)
    if rel.get("yanked"):
        raise Fatal("%s is already yanked" % a.version)
    if not a.reason or len(a.reason) > 200 or notes_mod.has_long_dash(a.reason):
        raise Fatal("--reason: 1 to 200 characters, no long dash")
    rel["yanked"] = True
    rel["yank_reason"] = a.reason
    doc["releases"][[r.get("version") for r in doc["releases"]].index(a.version)] = manifest_mod.ordered_release(rel)
    manifest_mod.recompute_channels(doc)
    doc["updated"] = manifest_mod.now_text()
    say("yank %s %s: %s" % (cfg["model"], a.version, a.reason))
    _save_and_commit(doc, mpath, cfg, devices_root, "%s: yank %s" % (cfg["slug"], a.version), a.dry_run, a.no_push)
    if a.dry_run:
        return 0
    if not token:
        say("  no token: delete the assets and mark the releases yourself")
        return 0
    gh = github.GitHub(token=token)
    n = names(cfg, a.version)
    for repo, tag in ((cfg["devices_repo"], n["devices_tag"]),
                      ((cfg.get("stable_mirror") or {}).get("repo"), n.get("mirror_tag"))):
        if not repo or not tag:
            continue
        try:
            r = gh.release_by_tag(repo, tag)
        except github.GitHubError:
            continue
        fields = {"prerelease": True, "body": "Withdrawn: %s\n\n%s" % (a.reason, r.get("body") or "")}
        gh.patch_release(repo, r["id"], fields)
        if not a.keep_assets:
            for asset in gh.assets(repo, r["id"]):
                if asset.get("name") in (n["asset"], n["zip"]):
                    gh.delete_asset(repo, asset["id"])
                    say("  deleted %s from %s %s" % (asset["name"], repo, tag))
        say("  %s %s marked withdrawn" % (repo, tag))
    return 0


def cmd_notes(a, repo_root, cfg, devices_root, token):
    """Amend a published release's notes: re-render release-notes/<version>.md
    (or --file) into the devices checkout, keep the summary in step,
    validate, commit, push, and patch the stable mirror's body."""
    doc, rel, mpath = _load_doc_for_edit(cfg, devices_root, a.version, a.dry_run)
    src = a.file or os.path.join(repo_root, "release-notes", a.version + ".md")
    if not os.path.isfile(src):
        raise Fatal("no notes file %s (pass --file)" % src)
    meta, body = notes_mod.split_front_matter(_read(src).decode("utf-8"))
    if "TODO" in body:
        raise Fatal("the notes still contain TODO")
    summary = a.summary or meta.get("summary") or rel.get("summary")
    bad = [x for x in notes_mod.check_summary(summary) if not x.startswith("warning:")]
    if bad:
        raise Fatal("summary: " + "; ".join(bad))
    # the layout block from what the manifest already knows
    slug = cfg["slug"]
    layouts_dir = os.path.join(devices_root, slug, "firmware", "layouts")
    headers = {}
    if os.path.isdir(layouts_dir):
        for name in os.listdir(layouts_dir):
            if name.endswith(".csv"):
                headers[name[:-4]] = layout_mod.read_csv(_read(os.path.join(layouts_dir, name)).decode("utf-8"))[0]
    md5 = (rel.get("layout") or {}).get("md5", "")
    lines = notes_mod.layout_sentences(rel.get("layout_from") or {}, headers, cfg.get("partition_words") or {}, md5)
    body = notes_mod.strip_layout_markers(notes_mod.render_layout_block(body, lines))
    problems = [x for x in notes_mod.check_notes(body, cfg["model"], a.version) if not x.startswith("warning:")]
    if problems:
        raise Fatal("notes: " + "; ".join(problems))
    dest = os.path.join(devices_root, slug, "firmware", "notes", a.version + ".md")
    changed_notes = not os.path.isfile(dest) or _read(dest).decode("utf-8") != body
    changed_summary = summary != rel.get("summary")
    say("notes %s %s: %s%s" % (cfg["model"], a.version,
                                "notes changed" if changed_notes else "notes unchanged",
                                ", summary changed" if changed_summary else ""))
    if not changed_notes and not changed_summary:
        say("nothing to do")
        return 0
    if a.dry_run:
        say("dry run: would write %s/firmware/notes/%s.md (%d B)%s and commit '%s: notes %s'"
            % (slug, a.version, len(body.encode("utf-8")), " and the summary" if changed_summary else "", slug, a.version))
        return 0
    with open(dest, "wb") as f:
        f.write(body.encode("utf-8"))
    if changed_summary:
        rel["summary"] = summary
        doc["releases"][[r.get("version") for r in doc["releases"]].index(a.version)] = manifest_mod.ordered_release(rel)
    doc["updated"] = manifest_mod.now_text()
    _save_and_commit(doc, mpath, cfg, devices_root, "%s: notes %s" % (slug, a.version), False, a.no_push)
    mirror = cfg.get("stable_mirror") or {}
    if rel.get("channel") == "stable" and mirror and token:
        gh = github.GitHub(token=token)
        n = names(cfg, a.version)
        try:
            mrel = gh.release_by_tag(mirror["repo"], n["mirror_tag"])
            gh.patch_release(mirror["repo"], mrel["id"], {"body": mirror_body(repo_root, cfg, body, a.version, meta.get("contributors") or [])})
            say("  mirror release %s body updated" % n["mirror_tag"])
        except github.GitHubError as exc:
            say("  warning: the mirror release body was not updated: %s" % exc)
    elif rel.get("channel") == "stable" and mirror:
        say("  no token: update the mirror release body on %s yourself" % mirror["repo"])
    return 0


# ---- entry ------------------------------------------------------------------------------

def find_devices_root(repo_root, explicit=None):
    if explicit:
        return os.path.abspath(explicit)
    env = os.environ.get("MEATPI_DEVICES_PATH")
    if env:
        return os.path.abspath(env)
    here = os.path.abspath(repo_root)
    for _ in range(7):
        cand = os.path.join(here, "meatpi-devices")
        if os.path.isfile(os.path.join(cand, "tools", "fwpub", "manifest.py")):
            return cand
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    raise Fatal("no meatpi-devices checkout found: set MEATPI_DEVICES_PATH or pass --devices")


def read_token(a):
    if a.token_file:
        with open(a.token_file, "r", encoding="utf-8") as f:
            return f.read().strip()
    return os.environ.get("MEATPI_RELEASE_TOKEN", "").strip() or None


def build_parser():
    ap = argparse.ArgumentParser(prog="release.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--devices", help="the meatpi-devices checkout (default: MEATPI_DEVICES_PATH or a parent folder)")
    ap.add_argument("--token-file")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(sp, build=True):
        sp.add_argument("--dry-run", action="store_true")
        sp.add_argument("--no-push", action="store_true")
        if build:
            sp.add_argument("--channel", default="alfa", choices=versions.CHANNELS)
            sp.add_argument("--no-build", action="store_true", help="reuse build/ (it must come from HEAD)")
            sp.add_argument("--tag", action="store_true", help="create the source tag at HEAD and push it")
            sp.add_argument("--resume", action="store_true", help="finish a publish whose release exists")
            sp.add_argument("--date", help="YYYY-MM-DD, default today (UTC)")
            sp.add_argument("--summary", help="overrides the front matter's summary")
    common(sub.add_parser("check", help="the local checks; writes nothing"))
    common(sub.add_parser("publish", help="build, check, tag, upload, manifest, commit, push"))
    pr = sub.add_parser("promote", help="move a release up a channel")
    pr.add_argument("version")
    pr.add_argument("--to", required=True, choices=("beta", "stable"))
    common(pr, build=False)
    yk = sub.add_parser("yank", help="withdraw a release")
    yk.add_argument("version")
    yk.add_argument("--reason", required=True)
    yk.add_argument("--keep-assets", action="store_true")
    common(yk, build=False)
    nt = sub.add_parser("notes", help="amend a published release's notes from release-notes/<version>.md")
    nt.add_argument("version")
    nt.add_argument("--file", help="another source file than release-notes/<version>.md")
    nt.add_argument("--summary", help="a new summary (default: the file's front matter)")
    common(nt, build=False)
    return ap


def main(argv, repo_root, config_path, devices_root=None):
    a = build_parser().parse_args(argv)
    repo_root = os.path.abspath(repo_root)
    cfg = load_config(config_path)
    devices_root = find_devices_root(repo_root, a.devices or devices_root)
    sys.path.insert(0, os.path.join(devices_root, "tools"))
    token = read_token(a)
    try:
        if a.cmd == "check":
            return cmd_check(a, repo_root, cfg, devices_root)
        if a.cmd == "publish":
            return cmd_publish(a, repo_root, cfg, devices_root, token)
        if a.cmd == "promote":
            return cmd_promote(a, repo_root, cfg, devices_root, token)
        if a.cmd == "yank":
            return cmd_yank(a, repo_root, cfg, devices_root, token)
        if a.cmd == "notes":
            return cmd_notes(a, repo_root, cfg, devices_root, token)
    except github.GitHubError as exc:
        raise Fatal(str(exc))
    except gitops.GitError as exc:
        raise Fatal(str(exc))
    return 1
