#!/usr/bin/env python3
"""Deterministic build for TokenFish Skill packages (SPEC.md §5).

For each Skill package under skills/<slug>/ and each channel x locale
combination, this script:
  1. Stages the package files (SKILL.md, references/*.md, agents/openai.yaml,
     a manifest.json with channel/locale filled in, and a fresh copy of
     client/tokenfish_client.py under scripts/).
  2. Zips them deterministically (fixed timestamps, sorted entries, fixed
     permissions) so the same inputs always produce the same sha256.
  3. Writes the zip + install.json (+ install.md rendered from the template)
     under dist/.
  4. Mirrors the "primary" (skillhub/zh-CN) install.json/install.md into
     site/skills/<slug>/ and, for the all-capability package, into site/
     directly -- but ONLY if site/ already exists in the working tree.
     site/ is owned by a different branch/agent; this script must never
     create that top-level directory itself.
  5. When site/ exists, also copies every channel x locale zip's bytes
     (the same bytes already written under dist/, never re-zipped) into
     site/dist/<slug>/<channel>/<locale>/<version>/<slug>-<version>.zip so
     the /dist/... download URLs referenced by install.json/install.md
     actually resolve when site/ is served as the web root.

Stdlib only. Run twice with no source changes in between to confirm the
sha256 values are identical (SPEC.md §6 check 2).
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "skills"
CLIENT_PATH = REPO_ROOT / "client" / "tokenfish_client.py"
TEMPLATE_PATH = REPO_ROOT / "build" / "templates" / "install.md.tmpl"
DIST_DIR = REPO_ROOT / "dist"
SITE_DIR = REPO_ROOT / "site"

# v0 channel x locale matrix (SPEC.md §5).
CHANNELS = [("skillhub", "zh-CN"), ("canonical", "zh-CN")]
# The build shown on the package's own install page / mirrored to site/.
PRIMARY_CHANNEL = ("skillhub", "zh-CN")
# The package mirrored to the site root install.md/install.json (the
# top-level "安装 TokenFish" message does not name a specific sub-package).
DEFAULT_PACKAGE = "tokenfish"

# Fixed zip member timestamp so builds are byte-for-byte reproducible.
FIXED_DATE_TIME = (2020, 1, 1, 0, 0, 0)


def discover_packages() -> list[str]:
    return sorted(p.name for p in SKILLS_DIR.iterdir() if p.is_dir() and (p / "SKILL.md").is_file())


def load_manifest(slug: str) -> dict:
    path = SKILLS_DIR / slug / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def collect_package_files(slug: str) -> list[tuple[str, bytes]]:
    """Sorted (arcname, content) pairs for a package, excluding manifest.json
    (that one is built separately per channel/locale, see build_zip_bytes)."""
    pkg_dir = SKILLS_DIR / slug
    entries: list[tuple[str, bytes]] = []

    entries.append((f"{slug}/SKILL.md", (pkg_dir / "SKILL.md").read_bytes()))

    refs_dir = pkg_dir / "references"
    if refs_dir.is_dir():
        for ref_file in sorted(refs_dir.glob("*.md")):
            entries.append((f"{slug}/references/{ref_file.name}", ref_file.read_bytes()))

    agents_file = pkg_dir / "agents" / "openai.yaml"
    if agents_file.is_file():
        entries.append((f"{slug}/agents/openai.yaml", agents_file.read_bytes()))

    entries.append((f"{slug}/scripts/tokenfish_client.py", CLIENT_PATH.read_bytes()))

    entries.sort(key=lambda e: e[0])
    return entries


def build_zip_bytes(slug: str, channel: str, locale: str, manifest: dict) -> bytes:
    entries = list(collect_package_files(slug))

    manifest_copy = dict(manifest)
    manifest_copy["channel"] = channel
    manifest_copy["locale"] = locale
    manifest_bytes = (
        json.dumps(manifest_copy, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    entries.append((f"{slug}/manifest.json", manifest_bytes))
    entries.sort(key=lambda e: e[0])

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arcname, data in entries:
            info = zipfile.ZipInfo(filename=arcname, date_time=FIXED_DATE_TIME)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
    return buf.getvalue()


def render_template(template_text: str, tokens: dict) -> str:
    def _sub(match: "re.Match") -> str:
        key = match.group(1)
        if key not in tokens:
            raise KeyError(f"install.md.tmpl referenced an unknown placeholder: {{{{{key}}}}}")
        return str(tokens[key])

    return re.sub(r"\{\{(\w+)\}\}", _sub, template_text)


def build_package(slug: str) -> dict:
    manifest = load_manifest(slug)
    version = manifest["version"]
    display_name = manifest.get("display_name", slug)

    per_channel = {}
    zip_bytes_by_channel = {}
    for channel, locale in CHANNELS:
        zip_bytes = build_zip_bytes(slug, channel, locale, manifest)
        sha256 = hashlib.sha256(zip_bytes).hexdigest()

        archive_rel = f"/dist/{slug}/{channel}/{locale}/{version}/{slug}-{version}.zip"
        out_dir = DIST_DIR / slug / channel / locale / version
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{slug}-{version}.zip").write_bytes(zip_bytes)

        per_channel[(channel, locale)] = {
            "package": slug,
            "version": version,
            "channel": channel,
            "locale": locale,
            "archive": archive_rel,
            "archive_sha256": sha256,
        }
        zip_bytes_by_channel[(channel, locale)] = zip_bytes

    primary = per_channel[PRIMARY_CHANNEL]
    install_json_text = json.dumps(primary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    template_text = TEMPLATE_PATH.read_text(encoding="utf-8")
    download_lines = "\n".join(
        f"/dist/{slug}/{ch}/{loc}/{version}/{slug}-{version}.zip" for ch, loc in CHANNELS
    )
    install_md_text = render_template(
        template_text,
        {
            "slug": slug,
            "display_name": display_name,
            "version": version,
            "download_paths": download_lines,
            "archive_sha256": primary["archive_sha256"],
        },
    )

    pkg_dist_dir = DIST_DIR / slug
    pkg_dist_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dist_dir / "install.json").write_text(install_json_text, encoding="utf-8")
    (pkg_dist_dir / "install.md").write_text(install_md_text, encoding="utf-8")

    return {
        "slug": slug,
        "per_channel": per_channel,
        "zip_bytes_by_channel": zip_bytes_by_channel,
        "install_json_text": install_json_text,
        "install_md_text": install_md_text,
    }


def mirror_into_site(
    slug: str,
    install_json_text: str,
    install_md_text: str,
    is_default_package: bool,
    per_channel: dict,
    zip_bytes_by_channel: dict,
) -> list[str]:
    """Never creates site/ itself -- only writes into it if it already
    exists (site/ is owned by a different branch/agent).

    Also copies every channel's zip bytes to
    site/<archive_rel> (the same bytes already written to DIST_DIR --
    never re-zipped, so this stays byte-deterministic) so the /dist/...
    download URLs referenced by install.json/install.md resolve when
    site/ is served as the web root. Returns the list of site-relative
    archive paths written (empty if site/ doesn't exist)."""
    if not SITE_DIR.is_dir():
        return []
    pkg_site_dir = SITE_DIR / "skills" / slug
    pkg_site_dir.mkdir(parents=True, exist_ok=True)
    (pkg_site_dir / "install.json").write_text(install_json_text, encoding="utf-8")
    (pkg_site_dir / "install.md").write_text(install_md_text, encoding="utf-8")
    if is_default_package:
        (SITE_DIR / "install.json").write_text(install_json_text, encoding="utf-8")
        (SITE_DIR / "install.md").write_text(install_md_text, encoding="utf-8")

    written: list[str] = []
    for key, info in per_channel.items():
        archive_rel = info["archive"]
        site_archive_path = SITE_DIR / archive_rel.lstrip("/")
        site_archive_path.parent.mkdir(parents=True, exist_ok=True)
        site_archive_path.write_bytes(zip_bytes_by_channel[key])
        written.append(archive_rel)
    return written


def main() -> int:
    packages = discover_packages()
    if not packages:
        print("No skills/<slug>/SKILL.md packages found; aborting build.", file=sys.stderr)
        return 1

    results = []
    site_archives_written: list[str] = []
    for slug in packages:
        result = build_package(slug)
        written = mirror_into_site(
            slug,
            result["install_json_text"],
            result["install_md_text"],
            is_default_package=(slug == DEFAULT_PACKAGE),
            per_channel=result["per_channel"],
            zip_bytes_by_channel=result["zip_bytes_by_channel"],
        )
        site_archives_written.extend(written)
        results.append(result)

    report = {
        "packages": [
            {
                "slug": r["slug"],
                "builds": list(r["per_channel"].values()),
            }
            for r in results
        ],
        "site_dir_existed": SITE_DIR.is_dir(),
        "site_archives_written": site_archives_written,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
