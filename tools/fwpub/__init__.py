"""fwpub: the shared library behind `tools/validate_firmware.py`,
`tools/import_github_release.py` and each firmware repository's
`tools/release.py`. Stdlib only, Python 3.10 or newer.

    versions   the grammar, the order, the channels, a device's selection
    layout     partition tables, the layout md5, the canonical csv, verdicts
    manifest   load, insert, pointers, prune, validate
    notes      front matter, the published file's rules, the layout block
    appdesc    the application descriptor and image integrity
    github     releases and assets over the REST API
    flashset   the serial flash zip from a build directory
    gitops     git through subprocess, for the publisher
    publish    the publisher behind each firmware repo's tools/release.py
"""

__version__ = "1.1.0"
__all__ = ["versions", "layout", "manifest", "notes", "appdesc", "github", "flashset", "gitops", "publish"]

from . import appdesc, flashset, github, gitops, layout, manifest, notes, publish, versions  # noqa: E402,F401


def self_test():
    """Every module's vectors; the count of modules that passed."""
    n = 0
    for mod in (versions, layout, appdesc, notes, github, manifest, flashset):
        mod.self_test()
        n += 1
    return n
