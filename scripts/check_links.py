#!/usr/bin/env python3
"""Verify every relative href/src in site/ resolves under a real HTTP server.

Implements SPEC.md §6 check 3:
  "站点所有相对链接在 `python -m http.server -d site` 下可达（写个小脚本检查）。"

Stdlib only. Starts `http.server` bound to the `site/` directory on a free
localhost port, walks every *.html file under site/, extracts href="" and
src="" attribute values via html.parser, skips absolute/external references
(http(s)://, //, mailto:, tel:, javascript:) and pure-fragment links (#foo),
resolves everything else relative to the page's own path, strips any
query string / fragment, and issues a real HTTP GET against the running
server. Prints one line per checked link and a final PASS/FAIL summary.

Against the same running server, this script also checks the actual
download artifacts referenced from install.json/install.md files under
site/ (recursively), since the HTML href/src walk above never looks
inside those:
  a. For every install.json under site/: GETs its "archive" path,
     requires HTTP 200, and requires the sha256 of the response body to
     equal the "archive_sha256" field recorded in that install.json.
  b. For every install.md under site/: finds lines that, after stripping
     whitespace, match `^/dist/\\S+\\.zip$`, and GETs each, requiring
     HTTP 200.

This script only reports results; it does not decide "the site is done".
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.parse
from functools import partial
from html.parser import HTMLParser
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

SITE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "site"))

SKIP_SCHEMES = ("http://", "https://", "//", "mailto:", "tel:", "javascript:", "data:")


class LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        for attr_name in ("href", "src"):
            value = attrs_dict.get(attr_name)
            if value:
                self.links.append(value)


def find_html_files(root: str) -> list[str]:
    out = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.endswith(".html"):
                out.append(os.path.join(dirpath, name))
    return sorted(out)


def find_files_named(root: str, filename: str) -> list[str]:
    """All paths under `root` (recursive) whose basename is exactly `filename`."""
    out = []
    for dirpath, _dirnames, filenames in os.walk(root):
        if filename in filenames:
            out.append(os.path.join(dirpath, filename))
    return sorted(out)


ZIP_LINE_RE = re.compile(r"^/dist/\S+\.zip$")


def find_zip_lines(md_path: str) -> list[str]:
    """Lines in an install.md that, after stripping whitespace, are a
    bare site-root-relative zip download path (SPEC.md §5 download block)."""
    with open(md_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    return [line.strip() for line in lines if ZIP_LINE_RE.match(line.strip())]


def extract_links(html_path: str) -> list[str]:
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    parser = LinkExtractor()
    parser.feed(content)
    return parser.links


def is_external(link: str) -> bool:
    lower = link.strip().lower()
    if lower.startswith("#"):
        return True  # pure fragment, nothing to fetch
    return lower.startswith(SKIP_SCHEMES)


def resolve_link(page_path: str, link: str) -> str:
    """Resolve `link` found in `page_path` to a URL path under the site root."""
    # Strip fragment and query for the purpose of resolving a file.
    link_no_frag = link.split("#", 1)[0]
    link_path_part = link_no_frag.split("?", 1)[0]
    if link_path_part == "":
        # e.g. "#section" already filtered out by is_external, but be safe
        return None

    page_rel_dir = os.path.dirname(os.path.relpath(page_path, SITE_DIR))
    if link_path_part.startswith("/"):
        # Site-root-relative path.
        url_path = link_path_part
    else:
        joined = os.path.normpath(os.path.join(page_rel_dir, link_path_part))
        url_path = "/" + joined.replace(os.sep, "/")
    return url_path


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class QuietHandler(SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler that doesn't spam stderr with access logs."""

    def log_message(self, format, *args):  # noqa: A002 (stdlib signature)
        pass


def start_server(port: int) -> ThreadingHTTPServer:
    handler = partial(QuietHandler, directory=SITE_DIR)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def wait_for_server(port: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            conn.request("GET", "/")
            conn.getresponse()
            conn.close()
            return
        except (ConnectionRefusedError, OSError):
            time.sleep(0.05)
    raise RuntimeError("server did not start in time")


def fetch_url(port: int, url_path: str) -> tuple[int, bytes]:
    """GET `url_path` from the running server; return (status, body)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        quoted = urllib.parse.quote(url_path)
        conn.request("GET", quoted)
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, body
    finally:
        conn.close()


def check_url(port: int, url_path: str) -> int:
    status, _body = fetch_url(port, url_path)
    return status


def main() -> int:
    if not os.path.isdir(SITE_DIR):
        print(f"FAIL: site directory not found at {SITE_DIR}", file=sys.stderr)
        return 1

    html_files = find_html_files(SITE_DIR)
    if not html_files:
        print(f"FAIL: no .html files found under {SITE_DIR}", file=sys.stderr)
        return 1

    port = find_free_port()
    httpd = start_server(port)
    try:
        wait_for_server(port)

        total = 0
        failures = []

        for html_path in html_files:
            rel_page = os.path.relpath(html_path, SITE_DIR).replace(os.sep, "/")
            for link in extract_links(html_path):
                if is_external(link):
                    print(f"SKIP  {rel_page} -> {link} (external/fragment)")
                    continue
                url_path = resolve_link(html_path, link)
                if url_path is None:
                    continue
                total += 1
                try:
                    status = check_url(port, url_path)
                except Exception as exc:  # noqa: BLE001
                    print(f"ERROR {rel_page} -> {link} (resolved {url_path}): {exc}")
                    failures.append((rel_page, link, url_path, str(exc)))
                    continue
                if status >= 400:
                    print(f"FAIL  {rel_page} -> {link} (resolved {url_path}): HTTP {status}")
                    failures.append((rel_page, link, url_path, f"HTTP {status}"))
                else:
                    print(f"OK    {rel_page} -> {link} (resolved {url_path}): HTTP {status}")

        install_json_files = find_files_named(SITE_DIR, "install.json")
        for json_path in install_json_files:
            rel_json = os.path.relpath(json_path, SITE_DIR).replace(os.sep, "/")
            with open(json_path, "r", encoding="utf-8") as f:
                install_data = json.load(f)
            archive = install_data["archive"]
            expected_sha256 = install_data["archive_sha256"]
            total += 1
            try:
                status, body = fetch_url(port, archive)
            except Exception as exc:  # noqa: BLE001
                print(f"ERROR {rel_json} -> {archive}: {exc}")
                failures.append((rel_json, archive, archive, str(exc)))
                continue
            if status >= 400:
                print(f"FAIL  {rel_json} -> {archive}: HTTP {status}")
                failures.append((rel_json, archive, archive, f"HTTP {status}"))
                continue
            actual_sha256 = hashlib.sha256(body).hexdigest()
            if actual_sha256 != expected_sha256:
                reason = f"sha256 mismatch: install.json says {expected_sha256}, archive is {actual_sha256}"
                print(f"FAIL  {rel_json} -> {archive}: {reason}")
                failures.append((rel_json, archive, archive, reason))
            else:
                print(f"OK    {rel_json} -> {archive}: HTTP {status}, sha256 matches")

        install_md_files = find_files_named(SITE_DIR, "install.md")
        for md_path in install_md_files:
            rel_md = os.path.relpath(md_path, SITE_DIR).replace(os.sep, "/")
            for zip_path in find_zip_lines(md_path):
                total += 1
                try:
                    status = check_url(port, zip_path)
                except Exception as exc:  # noqa: BLE001
                    print(f"ERROR {rel_md} -> {zip_path}: {exc}")
                    failures.append((rel_md, zip_path, zip_path, str(exc)))
                    continue
                if status >= 400:
                    print(f"FAIL  {rel_md} -> {zip_path}: HTTP {status}")
                    failures.append((rel_md, zip_path, zip_path, f"HTTP {status}"))
                else:
                    print(f"OK    {rel_md} -> {zip_path}: HTTP {status}")

        print()
        print(f"Checked {total} relative link(s) across {len(html_files)} page(s).")
        if failures:
            print(f"RESULT: {len(failures)} broken link(s) found.")
            for rel_page, link, url_path, reason in failures:
                print(f"  - {rel_page} -> {link} (resolved {url_path}): {reason}")
            return 1
        print("RESULT: all relative links resolved.")
        return 0
    finally:
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    sys.exit(main())
