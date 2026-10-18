"""GitHub releases and assets over the REST API with urllib only (no `gh`
on the Git Bash PATH here). Reads work without a token (60 requests an
hour); creating, patching, uploading and deleting need a fine-grained
token with Contents read and write on the repository, passed by the
caller (never read from a file by this module, never used by CI).

    gh = GitHub(token=None)
    rel = gh.release_by_tag("meatpiHQ/wican-fw", "v4.51p")
    gh.download(asset["browser_download_url"], dest_path, expect_size=asset["size"])
    gh.create_release(repo, tag, name, body, prerelease=True, target="main")
    gh.upload_asset(release["upload_url"], "x.bin", data)
    gh.patch_release(repo, release_id, {"prerelease": True, "body": "..."})
    gh.delete_asset(repo, asset_id)
    asset_digest(asset) -> the sha256 hex GitHub reports, or None
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
USER_AGENT = "meatpi-devices-fwpub"
DOWNLOAD_CAP = 64 * 1024 * 1024


class GitHubError(RuntimeError):
    def __init__(self, status, text, url):
        super().__init__("GitHub %s on %s: %s" % (status, url, text[:300]))
        self.status = status
        self.text = text
        self.url = url


class GitHub:
    def __init__(self, token=None, timeout=60):
        self.token = token
        self.timeout = timeout

    # ---- plumbing ---------------------------------------------------------------------

    def _headers(self, extra=None):
        h = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT,
             "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        if extra:
            h.update(extra)
        return h

    def _call(self, method, url, body=None, headers=None, raw=False):
        if not url.startswith("https://"):
            url = API + url
        data = None
        hdrs = self._headers(headers)
        if body is not None and not raw:
            data = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        elif body is not None:
            data = body
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                text = r.read().decode("utf-8", errors="replace")
                return r.status, (json.loads(text) if text.strip() else None)
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", errors="replace")
            raise GitHubError(e.code, text, url) from None

    # ---- reads -----------------------------------------------------------------------

    def release_by_tag(self, repo, tag):
        return self._call("GET", "/repos/%s/releases/tags/%s" % (repo, urllib.parse.quote(tag)))[1]

    def release(self, repo, release_id):
        return self._call("GET", "/repos/%s/releases/%d" % (repo, release_id))[1]

    def releases(self, repo, per_page=30):
        return self._call("GET", "/repos/%s/releases?per_page=%d" % (repo, per_page))[1] or []

    def commit_for_tag(self, repo, tag):
        """The commit sha a tag points at (through an annotated tag too)."""
        try:
            r = self._call("GET", "/repos/%s/commits/%s" % (repo, urllib.parse.quote(tag)))[1]
        except GitHubError:
            return None
        return (r or {}).get("sha")

    def download(self, url, dest, expect_size=None, cap=DOWNLOAD_CAP):
        """Stream an asset to `dest` (redirects followed by urllib). Returns
        the byte count; refuses more than `cap` bytes and a size other than
        `expect_size` when given."""
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept": "application/octet-stream"})
        n = 0
        with urllib.request.urlopen(req, timeout=self.timeout) as r, open(dest, "wb") as f:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                n += len(chunk)
                if n > cap:
                    raise GitHubError(0, "download over the %d byte cap" % cap, url)
                f.write(chunk)
        if expect_size is not None and n != expect_size:
            os.remove(dest)
            raise GitHubError(0, "downloaded %d bytes, expected %d" % (n, expect_size), url)
        return n

    def head(self, url):
        """Follow redirects with a GET that reads nothing: (status,
        content_length or None, final url)."""
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            length = r.headers.get("Content-Length")
            return r.status, (int(length) if length and length.isdigit() else None), r.geturl()

    # ---- writes (a token) -------------------------------------------------------------

    def create_release(self, repo, tag, name, body, prerelease, target="main", draft=False,
                       make_latest="false"):
        payload = {"tag_name": tag, "target_commitish": target, "name": name, "body": body,
                   "draft": draft, "prerelease": bool(prerelease), "make_latest": make_latest}
        return self._call("POST", "/repos/%s/releases" % repo, payload)[1]

    def patch_release(self, repo, release_id, fields):
        return self._call("PATCH", "/repos/%s/releases/%d" % (repo, release_id), fields)[1]

    def delete_release(self, repo, release_id):
        self._call("DELETE", "/repos/%s/releases/%d" % (repo, release_id))

    def upload_asset(self, upload_url, name, data, content_type="application/octet-stream"):
        """`upload_url` is the release's templated upload_url."""
        base = upload_url.split("{", 1)[0]
        url = base + "?" + urllib.parse.urlencode({"name": name})
        return self._call("POST", url, data, headers={"Content-Type": content_type}, raw=True)[1]

    def delete_asset(self, repo, asset_id):
        self._call("DELETE", "/repos/%s/releases/assets/%d" % (repo, asset_id))

    def assets(self, repo, release_id):
        return self._call("GET", "/repos/%s/releases/%d/assets?per_page=100" % (repo, release_id))[1] or []


def asset_digest(asset):
    """The sha256 hex GitHub reports for an asset (`digest`), or None."""
    d = (asset or {}).get("digest") or ""
    if d.startswith("sha256:") and len(d) == 7 + 64:
        return d[7:].lower()
    return None


def find_asset(release, name):
    for a in (release or {}).get("assets") or []:
        if a.get("name") == name:
            return a
    return None


def self_test():
    assert asset_digest({"digest": "sha256:" + "ab" * 32}) == "ab" * 32
    assert asset_digest({"digest": "md5:x"}) is None and asset_digest({}) is None
    rel = {"assets": [{"name": "a.bin", "size": 1}, {"name": "b.zip", "size": 2}]}
    assert find_asset(rel, "b.zip")["size"] == 2 and find_asset(rel, "c") is None
    gh = GitHub(token="t")
    h = gh._headers({"X": "1"})
    assert h["Authorization"] == "Bearer t" and h["X"] == "1" and "User-Agent" in h
    assert GitHub()._headers().get("Authorization") is None
    return 1


if __name__ == "__main__":
    self_test()
    print("github: OK")
