"""Release notes: the front matter of a firmware repo's
`release-notes/<version>.md`, the rules a published
`<slug>/firmware/notes/<version>.md` must keep (a DEVICE proxies the file to
a browser that may have no internet and renders it itself, so only a small
Markdown subset), the generated "Before you update" block, and the
conversion of a GitHub release body into a notes file. Stdlib only.
"""

from __future__ import annotations

import re

MAX_NOTES_BYTES = 16384
MAX_SUMMARY_CHARS = 140
LONG_DASHES = chr(0x2013) + chr(0x2014) + chr(0x2015)

LAYOUT_BLOCK_BEGIN = "<!-- release.py:layout -->"
LAYOUT_BLOCK_END = "<!-- /release.py:layout -->"

_HTML_TAG_RE = re.compile(r"<[a-zA-Z/][^>]*>")
_NUMBERED_RE = re.compile(r"^(\s*)\d+[.)]\s+")


def normalize(text):
    """LF line ends, no BOM, one trailing newline."""
    if text.startswith(chr(0xFEFF)):
        text = text[1:]
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.rstrip("\n") + "\n"


def replace_dashes(text):
    """No long dash anywhere: an en dash between digits reads "to" (a
    range), any other long dash becomes a comma."""
    text = re.sub(r"(?<=\d)\s*" + chr(0x2013) + r"\s*(?=\d)", " to ", text)
    for d in LONG_DASHES:
        text = text.replace(" " + d + " ", ", ").replace(d + " ", ", ").replace(" " + d, ", ").replace(d, ", ")
    return text


def has_long_dash(text):
    return any(d in text for d in LONG_DASHES)


# ---- front matter ----------------------------------------------------------------------

def split_front_matter(text):
    """(meta dict, body). The front matter is the block between a first
    line `---` and the next `---` line: `key: value` lines; a list value is
    `[a, b]`. Without front matter the meta is empty and the body is the
    whole text."""
    text = normalize(text)
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta = {}
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
        if ":" in lines[i]:
            k, v = lines[i].split(":", 1)
            k = k.strip()
            v = v.strip()
            if v.startswith("[") and v.endswith("]"):
                inner = v[1:-1].strip()
                meta[k] = [p.strip().strip("'\"") for p in inner.split(",")] if inner else []
            else:
                meta[k] = v.strip("'\"")
    if end is None:
        return {}, text
    body = "\n".join(lines[end + 1:]).lstrip("\n")
    return meta, normalize(body) if body.strip() else ""


def strip_front_matter(text):
    return split_front_matter(text)[1]


# ---- the published file's rules --------------------------------------------------------

def check_notes(text, model, version):
    """The problems of a published notes file (strings), empty when it
    keeps every rule: the first line `# <Model> <version>`, 1 to 16 KiB,
    no HTML, no images, no tables, headings `#` and `##` only, no long
    dash. A numbered list is reported as a warning prefixed `warning:`."""
    problems = []
    text = normalize(text)
    size = len(text.encode("utf-8"))
    if size > MAX_NOTES_BYTES:
        problems.append("notes are %d bytes, the cap is %d" % (size, MAX_NOTES_BYTES))
    if not text.strip():
        problems.append("notes are empty")
        return problems
    first = text.split("\n", 1)[0]
    want = "# %s %s" % (model, version)
    if first.rstrip() != want:
        problems.append("the first line must be %r, found %r" % (want, first[:80]))
    if _HTML_TAG_RE.search(text):
        problems.append("notes contain an HTML tag")
    if has_long_dash(text):
        problems.append("notes contain a long dash (U+2013, U+2014 or U+2015)")
    for n, line in enumerate(text.split("\n"), 1):
        s = line.lstrip()
        if s.startswith("!["):
            problems.append("line %d: images are not rendered on a device" % n)
        if s.startswith("|"):
            problems.append("line %d: tables are not rendered on a device" % n)
        if s.startswith("###"):
            problems.append("line %d: only # and ## headings render on a device" % n)
        if _NUMBERED_RE.match(line):
            problems.append("warning: line %d: a numbered list renders as plain lines; use - items" % n)
    return problems


def check_summary(summary):
    problems = []
    if not isinstance(summary, str) or not summary.strip():
        return ["summary: must be a non-empty string"]
    if len(summary) > MAX_SUMMARY_CHARS:
        problems.append("summary: longer than %d characters" % MAX_SUMMARY_CHARS)
    if "\n" in summary or "\r" in summary:
        problems.append("summary: must be one line")
    if has_long_dash(summary):
        problems.append("summary: contains a long dash")
    if summary != summary.strip():
        problems.append("summary: has leading or trailing whitespace")
    if any(c in summary for c in "*`[") or summary.startswith("#"):
        problems.append("warning: summary: markdown characters are shown verbatim")
    return problems


# ---- the generated layout block ------------------------------------------------------------

def layout_sentences(verdicts, layouts, partition_words, this_md5=None):
    """The "Before you update" lines. `verdicts` maps a FROM layout md5 to
    {"migration": ..., "wipes": [...]}; `layouts` maps an md5 to its csv
    header dict (name, first_seen, last_seen); `partition_words` maps a
    partition label to the words a user reads for it."""
    lines = []
    if this_md5 is not None:
        own = layouts.get(this_md5) or {}
        lines.append("- Units already on this partition layout%s update in place."
                     % ((" (" + own["name"] + ")") if own.get("name") else ""))
    for md5 in sorted(verdicts, key=lambda m: (layouts.get(m) or {}).get("first_seen", "")):
        v = verdicts[md5]
        h = layouts.get(md5) or {}
        span = ""
        if h.get("first_seen") and h.get("last_seen") and h["first_seen"] != h["last_seen"]:
            span = "firmware %s to %s" % (h["first_seen"], h["last_seen"])
        elif h.get("first_seen"):
            span = "firmware %s" % h["first_seen"]
        else:
            span = "layout " + md5[:8]
        name = (" (" + h["name"] + ")") if h.get("name") else ""
        if v.get("migration") == "lossless":
            lines.append("- Units coming from %s%s: the first boot restarts once more to adopt the "
                         "new partition layout; nothing is lost." % (span, name))
        elif v.get("migration") == "wipe":
            words = [partition_words.get(w, w) for w in v.get("wipes") or []]
            lines.append("- Units coming from %s%s: the first boot restarts once more and the %s "
                         "are reset." % (span, name, ", ".join(words) if words else "data partitions"))
        else:
            lines.append("- Units coming from %s%s cannot update over the air: flash this release "
                         "over USB with the serial flash set." % (span, name))
    return lines


def render_layout_block(text, lines):
    """Replace what is between the layout markers with `lines`; without
    markers the text is returned unchanged."""
    text = normalize(text)
    a = text.find(LAYOUT_BLOCK_BEGIN)
    b = text.find(LAYOUT_BLOCK_END)
    if a < 0 or b < 0 or b < a:
        return text
    inner = "\n".join(lines)
    return text[:a + len(LAYOUT_BLOCK_BEGIN)] + "\n" + inner + "\n" + text[b:]


def strip_layout_markers(text):
    return normalize(text.replace(LAYOUT_BLOCK_BEGIN + "\n", "").replace(LAYOUT_BLOCK_BEGIN, "")
                     .replace(LAYOUT_BLOCK_END + "\n", "").replace(LAYOUT_BLOCK_END, ""))


# ---- a GitHub release body as notes ---------------------------------------------------------

def from_github_body(body, model, version, summary=None):
    """A notes file from a GitHub release body: LF ends, long dashes
    replaced, headings deeper than ## raised to ##, numbered items made
    `-` items, HTML tags removed, the heading line prepended, the size
    capped at a line boundary. An empty body yields the summary."""
    body = normalize(body or "")
    body = replace_dashes(body)
    body = _HTML_TAG_RE.sub("", body)
    out = []
    for line in body.split("\n"):
        s = line
        if s.lstrip().startswith("#"):
            stripped = s.lstrip()
            hashes = len(stripped) - len(stripped.lstrip("#"))
            if hashes > 2:
                s = "## " + stripped[hashes:].lstrip()
        m = _NUMBERED_RE.match(s)
        if m:
            s = m.group(1) + "- " + s[m.end():]
        out.append(s.rstrip())
    text = "\n".join(out).strip("\n")
    if not text:
        text = (summary or "").strip()
    text = "# %s %s\n\n%s\n" % (model, version, text) if text else "# %s %s\n" % (model, version)
    text = _cap(text, MAX_NOTES_BYTES)
    return normalize(text)


def _cap(text, limit):
    if len(text.encode("utf-8")) <= limit:
        return text
    lines = text.split("\n")
    kept = []
    size = 0
    tail = "(shortened; the full text is on the GitHub release)"
    for line in lines:
        n = len(line.encode("utf-8")) + 1
        if size + n > limit - len(tail) - 16:
            break
        kept.append(line)
        size += n
    kept.append("")
    kept.append(tail)
    return "\n".join(kept) + "\n"


def self_test():
    meta, body = split_front_matter("---\nsummary: One line.\ncontributors: [a, b]\n---\n# X 1.0.0\n\ntext\n")
    assert meta == {"summary": "One line.", "contributors": ["a", "b"]}, meta
    assert body == "# X 1.0.0\n\ntext\n", repr(body)
    assert split_front_matter("# X 1.0.0\n")[0] == {}
    assert check_notes("# WiCAN PRO 6.1.0\n\n- a\n", "WiCAN PRO", "6.1.0") == []
    p = check_notes("# Wrong\n\n### deep\n<b>x</b>\n![i](u)\n| a |\n1. one\n" + chr(0x2014) + "\n", "WiCAN PRO", "6.1.0")
    assert len([x for x in p if not x.startswith("warning:")]) == 6, p
    assert any(x.startswith("warning:") for x in p), p
    assert "cap" in check_notes("# M 1.0.0\n" + "x" * 17000 + "\n", "M", "1.0.0")[0]
    assert check_summary("Fine.") == []
    assert any("140" in x for x in check_summary("x" * 141))
    assert replace_dashes("4.40" + chr(0x2013) + "4.52 and a " + chr(0x2014) + " b") == "4.40 to 4.52 and a, b"
    text = from_github_body("### New Features:\r\n1. one\r\n2. two\r\n\r\n#### Deep\r\n", "WiCAN PRO", "4.51.0")
    assert text == "# WiCAN PRO 4.51.0\n\n## New Features:\n- one\n- two\n\n## Deep\n", repr(text)
    assert check_notes(text, "WiCAN PRO", "4.51.0") == []
    assert from_github_body("", "ESPNetLink", "1.20.0", "Public release.") == "# ESPNetLink 1.20.0\n\nPublic release.\n"
    long = from_github_body("- x\n" * 9000, "M", "1.0.0")
    assert len(long.encode()) <= MAX_NOTES_BYTES and "shortened" in long
    lines = layout_sentences({"a" * 32: {"migration": "wipe", "wipes": ["storage"]},
                              "b" * 32: {"migration": "serial"},
                              "c" * 32: {"migration": "lossless"}},
                             {"a" * 32: {"name": "factory v4.x", "first_seen": "4.40.0", "last_seen": "4.51.0"},
                              "d" * 32: {"name": "v6"}},
                             {"storage": "stored files, logs and the saved configuration"}, "d" * 32)
    assert lines[0].startswith("- Units already on this partition layout (v6)"), lines
    assert any("4.40.0 to 4.51.0 (factory v4.x): the first boot restarts once more and the stored files" in l for l in lines), lines
    assert any("cannot update over the air" in l for l in lines) and any("nothing is lost" in l for l in lines)
    t = "# M 1.0.0\n\n## Before you update\n%s\nold\n%s\n\n## New features\n" % (LAYOUT_BLOCK_BEGIN, LAYOUT_BLOCK_END)
    r = render_layout_block(t, ["- new"])
    assert "old" not in r and "- new" in r and LAYOUT_BLOCK_END in r, r
    assert LAYOUT_BLOCK_BEGIN not in strip_layout_markers(r)
    return 1


if __name__ == "__main__":
    self_test()
    print("notes: OK")
