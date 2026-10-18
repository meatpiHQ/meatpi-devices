"""The version grammar, the order, the channels and the selection a device
makes. A line-for-line port of the firmware's fw_update_version.c and the
selection in fw_update_core.c (meatpi-components), so the publisher, the
validator and Home Assistant agree with every device.

Strict grammar (manifest `version`, `requires.min_version`,
`channels.*.latest`, `version.txt`):

    version := MAJOR "." MINOR "." PATCH [ "-" PRE "." N ]
    MAJOR, MINOR, PATCH, N := "0" | [1-9][0-9]*     (each < 65536)
    PRE := "alfa" | "beta"

Order: the three numbers, then a release above any prerelease of the same
number, then beta above alfa, then N. So
6.0.0-alfa.6 < 6.0.0-beta.1 < 6.0.0 < 6.0.1 < 6.1.0-alfa.1 < 6.1.0 < 6.10.0.

Tolerant grammar (what a device reports when it is not an exact
`fw_version` of the manifest; never accepted in a manifest):

    tolerant := [vV] MAJOR "." MINOR [ "." PATCH ] [ "p" | "u" ] [ SEP PRE SEP N ] [ BUILD ]
    SEP := "-" | "_" | "."      PRE also accepts "alpha"
    BUILD := "-" digits "-g" hex [ "-dirty" ]  or  "-dirty"   (a dev build)

Stdlib only. Python 3.10 or newer.
"""

from __future__ import annotations

import re

PRE_NONE = 0
PRE_ALFA = 1
PRE_BETA = 2

CHANNELS = ("stable", "beta", "alfa")
MAX_NUM = 65535

_STRICT_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-(alfa|beta)\.(0|[1-9][0-9]*))?$"
)


class Version:
    """A parsed version. `dev` marks a git-describe tail (a development
    build); it does not take part in the order, like the firmware."""

    __slots__ = ("major", "minor", "patch", "pre", "pre_n", "dev")

    def __init__(self, major, minor, patch, pre=PRE_NONE, pre_n=0, dev=False):
        self.major = int(major)
        self.minor = int(minor)
        self.patch = int(patch)
        self.pre = int(pre)
        self.pre_n = int(pre_n)
        self.dev = bool(dev)

    def key(self):
        """The order: numbers, a release above a prerelease, beta above
        alfa, then N."""
        return (self.major, self.minor, self.patch,
                1 if self.pre == PRE_NONE else 0, self.pre, self.pre_n)

    def __lt__(self, other):
        return self.key() < other.key()

    def __le__(self, other):
        return self.key() <= other.key()

    def __gt__(self, other):
        return self.key() > other.key()

    def __ge__(self, other):
        return self.key() >= other.key()

    def __eq__(self, other):
        return isinstance(other, Version) and self.key() == other.key()

    def __hash__(self):
        return hash(self.key())

    def __str__(self):
        return to_str(self)

    def __repr__(self):
        return "Version(%s%s)" % (to_str(self), ", dev" if self.dev else "")


def compare(a, b):
    """-1, 0 or 1, the firmware's fwu_version_cmp."""
    ka = a.key()
    kb = b.key()
    return -1 if ka < kb else (1 if ka > kb else 0)


def to_str(v):
    if v.pre == PRE_NONE:
        return "%d.%d.%d" % (v.major, v.minor, v.patch)
    return "%d.%d.%d-%s.%d" % (v.major, v.minor, v.patch,
                               "beta" if v.pre == PRE_BETA else "alfa", v.pre_n)


def parse_strict(s):
    """The manifest grammar. None when the text is not a version."""
    if not isinstance(s, str):
        return None
    m = _STRICT_RE.match(s)
    if m is None:
        return None
    major, minor, patch, pre, pre_n = m.groups()
    nums = [int(major), int(minor), int(patch), int(pre_n or 0)]
    if any(n > MAX_NUM for n in nums):
        return None
    return Version(nums[0], nums[1], nums[2],
                   PRE_NONE if pre is None else (PRE_BETA if pre == "beta" else PRE_ALFA),
                   nums[3])


def _read_num(s, i, strict):
    """Digits at s[i:], as (value, next index); (None, i) when none, more
    than 65535, or a strict leading zero."""
    j = i
    v = 0
    while j < len(s) and s[j].isdigit():
        v = v * 10 + (ord(s[j]) - 48)
        if v > MAX_NUM:
            return None, i
        j += 1
    if j == i or (strict and j - i > 1 and s[i] == "0"):
        return None, i
    return v, j


def _is_sep(c):
    return c in "-_."


def _read_pre(s, i, strict):
    """(pre kind, word length) at s[i:], or (None, 0)."""
    if s.startswith("alfa", i):
        return PRE_ALFA, 4
    if s.startswith("beta", i):
        return PRE_BETA, 4
    if not strict and s.startswith("alpha", i):
        return PRE_ALFA, 5
    return None, 0


def _is_build_tail(s):
    """A git describe tail: -<digits>-g<hex>[-dirty], or -dirty alone.
    IDF cuts the version at 31 characters, so a prefix of "-dirty" counts."""
    if s == "" or s == "-dirty":
        return True
    if len(s) < 2 or s[0] != "-" or not s[1].isdigit():
        return False
    i = 1
    while i < len(s) and s[i].isdigit():
        i += 1
    if s[i:i + 2] != "-g" or i + 2 >= len(s) or not _is_hex(s[i + 2]):
        return False
    i += 2
    while i < len(s) and _is_hex(s[i]):
        i += 1
    rest = s[i:]
    return rest == "" or "-dirty".startswith(rest)


def _is_hex(c):
    return c in "0123456789abcdefABCDEF"


def parse_tolerant(s):
    """What a device reports, placed on the order. None when unparsable."""
    if not isinstance(s, str) or s == "":
        return None
    i = 0
    if s[0] in "vV":
        i = 1
    major, i = _read_num(s, i, False)
    if major is None or i >= len(s) or s[i] != ".":
        return None
    minor, i = _read_num(s, i + 1, False)
    if minor is None:
        return None
    patch = 0
    if i < len(s) and s[i] == "." and i + 1 < len(s) and s[i + 1].isdigit():
        patch, i = _read_num(s, i + 1, False)
    if i < len(s) and s[i] in "pu":
        i += 1  # the hardware letter of a wican-fw tag
    pre = PRE_NONE
    pre_n = 0
    if i < len(s) and _is_sep(s[i]):
        kind, n = _read_pre(s, i + 1, False)
        if kind is not None and i + 1 + n < len(s) and _is_sep(s[i + 1 + n]):
            num, j = _read_num(s, i + 2 + n, False)
            if num is not None:
                pre = kind
                pre_n = num
                i = j
    dev = False
    if i < len(s):
        if not _is_build_tail(s[i:]):
            return None
        dev = True
    return Version(major, minor, patch, pre, pre_n, dev)


# ---- channels ------------------------------------------------------------------------

def channel_rank(name):
    """stable 0, beta 1, alfa 2; None for anything else."""
    try:
        return CHANNELS.index(name)
    except ValueError:
        return None


def channel_visible(device_channel, release_channel):
    """A device on `device_channel` sees releases up to its own rank:
    visible(stable) = {stable}, visible(beta) = {stable, beta},
    visible(alfa) = all three."""
    d = channel_rank(device_channel)
    r = channel_rank(release_channel)
    return d is not None and r is not None and r <= d


def visible_set(device_channel):
    d = channel_rank(device_channel)
    return set() if d is None else set(CHANNELS[:d + 1])


# ---- the selection a device makes ----------------------------------------------------

def _candidate(rel, channel):
    return channel_visible(channel, rel.get("channel")) and not rel.get("yanked", False)


def layout_verdict(rel, flashed_md5):
    """The device rule: same md5 = none; a `layout_from` hit = that verdict;
    no md5 or a miss = unknown. Returns (verdict, wipes)."""
    if not flashed_md5:
        return "unknown", []
    layout = rel.get("layout") or {}
    if layout.get("md5") == flashed_md5:
        return "none", []
    hit = (rel.get("layout_from") or {}).get(flashed_md5)
    if isinstance(hit, dict) and hit.get("migration") in ("lossless", "wipe", "serial"):
        return hit["migration"], list(hit.get("wipes") or [])
    return "unknown", []


def select(releases, channel, installed_fw_version, flashed_md5=None):
    """fwu_select: which release a device on `channel` running
    `installed_fw_version` is offered. `releases` are manifest objects
    sorted newest first (as the manifest is kept). Returns a dict with
    `target` (the release object or None), `latest`, `me`, `available`,
    `step`, `reason`, `installed` (a Version or None), `dev_build`,
    `installed_yanked`, `verdict`, `wipes`, `installable`."""
    out = {"target": None, "latest": None, "me": None, "available": False,
           "step": False, "reason": "", "installed": None, "dev_build": False,
           "installed_yanked": False, "verdict": "unknown", "wipes": [],
           "installable": False}
    parsed = {}
    for r in releases:
        v = parse_strict(r.get("version"))
        if v is not None:
            parsed[id(r)] = v

    me = None
    if installed_fw_version is not None:
        for r in releases:
            if r.get("fw_version") == installed_fw_version and id(r) in parsed:
                me = r
                break
    installed = None
    if me is not None:
        installed = parsed[id(me)]
        out["me"] = me
        out["installed_yanked"] = bool(me.get("yanked", False))
    else:
        installed = parse_tolerant(installed_fw_version)
        if installed is not None:
            out["dev_build"] = installed.dev
    out["installed"] = installed

    latest = None
    for r in releases:
        if id(r) in parsed and _candidate(r, channel):
            latest = r
            break
    out["latest"] = latest
    if latest is None:
        out["reason"] = "no_release"
        return out
    if installed is None:
        out["reason"] = "unknown_version"
        return out
    if parsed[id(latest)] <= installed and not out["installed_yanked"]:
        out["reason"] = "up_to_date"
        return out

    target = latest
    while True:
        req = (target.get("requires") or {}).get("min_version")
        min_v = parse_strict(req) if req else None
        if min_v is None or min_v <= installed:
            break
        between = None
        for r in releases:  # newest first
            v = parsed.get(id(r))
            if v is None or not _candidate(r, channel):
                continue
            if installed < v < parsed[id(target)]:
                between = r
                break
        if between is None:
            out["reason"] = "no_path"
            return out
        target = between
        out["step"] = True

    out["target"] = target
    out["available"] = True
    verdict, wipes = layout_verdict(target, flashed_md5)
    out["verdict"] = verdict
    out["wipes"] = wipes
    out["installable"] = verdict in ("none", "lossless", "wipe")
    if verdict == "serial":
        out["reason"] = "layout_serial"
    elif verdict == "unknown":
        out["reason"] = "layout_unknown"
    return out


# ---- self test -----------------------------------------------------------------------

def self_test():
    """Raises AssertionError on the first failed vector."""
    chain = ["6.0.0-alfa.6", "6.0.0-beta.1", "6.0.0", "6.0.1", "6.1.0-alfa.1", "6.1.0", "6.10.0"]
    parsed = [parse_strict(s) for s in chain]
    assert all(p is not None for p in parsed), chain
    for a, b in zip(parsed, parsed[1:]):
        assert a < b, (a, b)
    assert [to_str(p) for p in parsed] == chain
    for bad in ("6.01.0", "v6.1.0", "6.1.0-alpha.1", "6.1.0-alfa.01", "6.1", "6.1.0-alfa",
                "6.1.0-alfa.1x", "65536.0.0", "", "6.1.0 "):
        assert parse_strict(bad) is None, bad
    tol = {
        "v4.51p": ("4.51.0", False), "v6.00p_alfa-06": ("6.0.0-alfa.6", False),
        "v6.00p_alfa-04-2-g2a40245-dirty": ("6.0.0-alfa.4", True),
        "v1.22-61-gcc81591": ("1.22.0", True), "v6.1.0p-alfa.1": ("6.1.0-alfa.1", False),
        "1.23.0-alfa.1": ("1.23.0-alfa.1", False), "V6.1": ("6.1.0", False),
        "6.1.0-dirty": ("6.1.0", True), "6.1.0-3-gabc": ("6.1.0", True),
        "6.1.0-3-gabc-dir": ("6.1.0", True), "4.21u": ("4.21.0", False),
        "v6.00p.alpha.3": ("6.0.0-alfa.3", False), "6.1.0-beta.2-1-gdeadbee": ("6.1.0-beta.2", True),
    }
    for text, (want, dev) in tol.items():
        v = parse_tolerant(text)
        assert v is not None, text
        assert to_str(v) == want and v.dev == dev, (text, to_str(v), v.dev)
    for bad in ("6", "abc", "6.1.0-3-gabc-dix", "v", "6.x", "6.1.0+1"):
        assert parse_tolerant(bad) is None, bad
    assert parse_tolerant("6.1.0-alfa.1") == parse_strict("6.1.0-alfa.1")
    assert channel_visible("alfa", "stable") and channel_visible("beta", "beta")
    assert not channel_visible("stable", "beta") and not channel_visible("beta", "alfa")
    assert channel_rank("nightly") is None

    rels = [
        {"version": "6.2.0", "fw_version": "6.2.0", "channel": "stable", "yanked": False,
         "requires": {"min_version": "6.1.0"}, "layout": {"md5": "b" * 32},
         "layout_from": {"a" * 32: {"migration": "wipe", "wipes": ["storage"]}}},
        {"version": "6.1.0", "fw_version": "6.1.0", "channel": "stable", "yanked": False,
         "layout": {"md5": "a" * 32}},
        {"version": "6.0.0", "fw_version": "6.0.0", "channel": "stable", "yanked": True,
         "layout": {"md5": "a" * 32}},
        {"version": "5.9.0", "fw_version": "v5.9", "channel": "stable", "yanked": False,
         "layout": {"md5": "a" * 32}},
    ]
    s = select(rels, "stable", "v5.9", "a" * 32)
    assert s["available"] and s["step"] and s["target"]["version"] == "6.1.0", s
    assert s["verdict"] == "none" and s["installable"], s
    s = select(rels, "stable", "6.1.0", "a" * 32)
    assert s["target"]["version"] == "6.2.0" and s["verdict"] == "wipe" and s["wipes"] == ["storage"], s
    s = select(rels, "stable", "6.2.0", "b" * 32)
    assert not s["available"] and s["reason"] == "up_to_date", s
    # a yanked installed release is replaced, and 6.2.0 still wants 6.1.0 first
    s = select(rels, "stable", "6.0.0", "a" * 32)
    assert s["available"] and s["installed_yanked"] and s["step"] and s["target"]["version"] == "6.1.0", s
    s = select(rels, "stable", "v6.0.0p-alfa.2-3-gabc", "a" * 32)
    assert s["dev_build"] and s["target"]["version"] == "6.1.0" and s["step"], s
    s = select(rels, "stable", "garbage", "a" * 32)
    assert s["reason"] == "unknown_version", s
    s = select(rels, "stable", "6.1.0", "c" * 32)
    assert s["available"] and s["verdict"] == "unknown" and not s["installable"], s
    s = select([], "stable", "6.1.0", "a" * 32)
    assert s["reason"] == "no_release", s
    s = select(rels, "stable", "5.9.0", None)
    assert s["available"] and s["verdict"] == "unknown", s
    only = [rels[0], rels[3]]
    s = select(only, "stable", "v5.9", "a" * 32)
    assert not s["available"] and s["reason"] == "no_path", s
    return 1


if __name__ == "__main__":
    self_test()
    print("versions: OK")
