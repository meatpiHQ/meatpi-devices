"""Git through subprocess, for the publisher: the state of a checkout, tags,
commits and pushes. Every function takes the checkout root. Stdlib only.
"""

from __future__ import annotations

import subprocess


class GitError(RuntimeError):
    pass


def run(root, *args, check=True, strip=True):
    """`git -C root args...`, stdout stripped (trailing newlines only with
    strip=False, for porcelain output whose leading column matters). Raises
    GitError on failure."""
    p = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise GitError("git %s: %s" % (" ".join(args), (p.stderr or p.stdout).strip()[:400]))
    return p.stdout.strip() if strip else p.stdout.rstrip("\r\n")


def is_repo(root):
    try:
        return run(root, "rev-parse", "--is-inside-work-tree") == "true"
    except GitError:
        return False


def dirty_paths(root):
    """The porcelain status lines, `XY path` (empty when the tree is clean)."""
    out = run(root, "status", "--porcelain", strip=False)
    return [line.rstrip("\r") for line in out.split("\n") if line.strip()]


def head_sha(root):
    return run(root, "rev-parse", "HEAD")


def branch(root):
    return run(root, "rev-parse", "--abbrev-ref", "HEAD")


def describe(root):
    return run(root, "describe", "--tags", "--always", "--dirty")


def tags_at_head(root):
    out = run(root, "tag", "--points-at", "HEAD")
    return [t for t in out.split("\n") if t.strip()]


def tag_exists(root, name):
    return run(root, "tag", "-l", name) == name


def tag_commit(root, name):
    return run(root, "rev-parse", name + "^{commit}")


def create_tag(root, name, message):
    run(root, "tag", "-a", name, "-m", message)


def push(root, *refspecs):
    run(root, "push", "origin", *refspecs)


def fetch(root):
    try:
        run(root, "fetch", "--quiet", "origin")
        return True
    except GitError:
        return False


def behind_upstream(root):
    """How many commits origin/<branch> is ahead of HEAD (None when
    unknown, e.g. no upstream or offline)."""
    b = branch(root)
    try:
        return int(run(root, "rev-list", "--count", "HEAD..origin/" + b))
    except (GitError, ValueError):
        return None


def commit_paths(root, paths, message):
    """Stage only `paths` and commit them; returns the new sha."""
    run(root, "add", "-A", "--", *paths)
    staged = run(root, "diff", "--cached", "--name-only")
    if not staged:
        raise GitError("nothing to commit under " + ", ".join(paths))
    run(root, "commit", "-q", "-m", message, "--", *paths)
    return head_sha(root)


def remote_url(root):
    try:
        return run(root, "remote", "get-url", "origin")
    except GitError:
        return ""
