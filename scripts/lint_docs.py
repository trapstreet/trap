#!/usr/bin/env python3
"""Lint docs/manifest.json and cross-doc links for the live docs site (issue #19).

trapstreet.run renders every page under docs/ from this repo's latest release tag,
driven by docs/manifest.json. This guard keeps a broken manifest or link from reaching
a release tag:

- every manifest ``path`` exists, sits under docs/, and ends ``.md``;
- every ``docs/**/*.md`` file is listed in the manifest (no orphan pages);
- every relative Markdown link in docs/ resolves to an existing ``.md`` within docs/;
- with ``--expect-version`` (release only), manifest ``cli_version`` equals the tag.

Stdlib only; run via ``uv run --no-project python scripts/lint_docs.py``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = (REPO_ROOT / "docs").resolve()
MANIFEST = DOCS / "manifest.json"

# Markdown links [text](target) and images ![alt](target). Only *relative* targets are
# validated — external URLs, mailto, and pure #anchors are the site's/browser's concern.
_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
# Fenced code blocks — stripped before link scanning so link-like text inside shell/yaml
# examples isn't treated as a doc link.
_FENCE = re.compile(r"^```.*?^```", re.DOTALL | re.MULTILINE)
_EXTERNAL = ("http://", "https://", "mailto:", "tel:", "//", "#")


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def _load_manifest() -> tuple[dict | None, list[str]]:
    try:
        return json.loads(MANIFEST.read_text()), []
    except FileNotFoundError:
        return None, [f"{_rel(MANIFEST)} not found"]
    except json.JSONDecodeError as e:
        return None, [f"{_rel(MANIFEST)} is not valid JSON: {e}"]


def _manifest_errors(data: dict) -> list[str]:
    errs: list[str] = []
    listed: set[Path] = set()
    for section in data.get("sections", []):
        for item in section.get("items", []):
            raw = item.get("path")
            if not raw:
                errs.append(f"manifest item missing `path`: {item!r}")
                continue
            path = (REPO_ROOT / raw).resolve()
            if DOCS not in path.parents:
                errs.append(f"path escapes docs/: {raw}")
            elif not path.is_file():
                errs.append(f"path does not exist: {raw}")
            elif path.suffix != ".md":
                errs.append(f"path is not a .md file: {raw}")
            else:
                listed.add(path)

    # No orphan pages: every docs page must be reachable from the nav.
    for md in DOCS.rglob("*.md"):
        if md.resolve() not in listed:
            errs.append(f"docs page not listed in manifest: {_rel(md)}")
    return errs


def _link_errors() -> list[str]:
    errs: list[str] = []
    for md in sorted(DOCS.rglob("*.md")):
        text = _FENCE.sub("", md.read_text())
        for raw in _LINK.findall(text):
            target = raw.strip()
            if target.startswith(_EXTERNAL):
                continue
            file_part = target.split("#", 1)[0].split("?", 1)[0]
            if not file_part:
                continue
            if file_part.startswith("/"):
                errs.append(f"{_rel(md)}: absolute link not supported: {target}")
                continue
            resolved = (md.parent / file_part).resolve()
            if not file_part.endswith(".md"):
                errs.append(f"{_rel(md)}: relative link must point at a .md: {target}")
            elif DOCS not in resolved.parents:
                errs.append(f"{_rel(md)}: link escapes docs/: {target}")
            elif not resolved.is_file():
                errs.append(f"{_rel(md)}: link target does not exist: {target}")
    return errs


def _version_errors(data: dict, expect: str) -> list[str]:
    got = str(data.get("cli_version", ""))
    if got != expect:
        return [
            f"manifest cli_version {got!r} != release version {expect!r} "
            "— bump docs/manifest.json before tagging"
        ]
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--expect-version",
        help="assert manifest cli_version equals this (release workflow only; PR CI omits it)",
    )
    args = parser.parse_args()

    data, errors = _load_manifest()
    if data is not None:
        errors += _manifest_errors(data)
        errors += _link_errors()
        if args.expect_version:
            errors += _version_errors(data, args.expect_version)

    if errors:
        print("docs lint failed:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1
    print("docs lint OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
