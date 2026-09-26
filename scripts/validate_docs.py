"""Validate locally rendered MkDocs links, anchors, images, and source references."""

from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"


class PageLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.anchors: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        for attribute in ("id", "name"):
            if values.get(attribute):
                self.anchors.add(str(values[attribute]))
        attribute = "src" if tag in {"img", "script", "source"} else "href"
        if values.get(attribute):
            self.links.append((attribute, str(values[attribute])))


def check_rendered_links() -> list[str]:
    errors: list[str] = []
    pages = {path.resolve(): path for path in BUILD.rglob("*.html")}
    parsed: dict[Path, PageLinks] = {}
    for page in pages.values():
        parser = PageLinks()
        parser.feed(page.read_text(encoding="utf-8"))
        parsed[page.resolve()] = parser

    for page, parser in parsed.items():
        for kind, value in parser.links:
            url = urlsplit(value)
            if url.scheme or url.netloc or value.startswith(("mailto:", "tel:", "data:")):
                continue
            target_path = unquote(url.path)
            if target_path.startswith("/"):
                prefix = "/btc-timesfm/"
                if target_path.startswith(prefix):
                    target_path = target_path[len(prefix) :]
                else:
                    errors.append(
                        f"{page.relative_to(BUILD)}: {kind} escapes project base URL: {value}"
                    )
                    continue
            destination = (page.parent / target_path).resolve() if target_path else page
            if destination.is_dir():
                destination = destination / "index.html"
            if not destination.is_file():
                errors.append(f"{page.relative_to(BUILD)}: broken {kind}: {value}")
                continue
            if url.fragment:
                target_page = parsed.get(destination.resolve())
                if target_page and unquote(url.fragment) not in target_page.anchors:
                    errors.append(
                        f"{page.relative_to(BUILD)}: missing anchor #{url.fragment} in "
                        f"{destination.relative_to(BUILD)}"
                    )
    return errors


def check_source_references() -> list[str]:
    errors: list[str] = []
    reference = re.compile(r"(?<![\w/])((?:src|scripts|tests)/[\w./-]+\.(?:py|yml|yaml|json))")
    for page in (ROOT / "docs").rglob("*.md"):
        if page.name == "DOCUMENTATION_AUDIT.md":
            # This retained audit intentionally cites paths that were missing at its baseline.
            continue
        for match in reference.finditer(page.read_text(encoding="utf-8")):
            source = ROOT / match.group(1)
            if not source.is_file():
                errors.append(f"{page.relative_to(ROOT)}: missing code reference {match.group(1)}")
    return errors


def main() -> int:
    if not (BUILD / "index.html").is_file():
        print("Docs validation: build/index.html is missing; run `mkdocs build --strict` first.")
        return 1
    errors = check_rendered_links() + check_source_references()
    if errors:
        print("Documentation validation failed:")
        print("\n".join(f"- {error}" for error in errors))
        return 1
    print("Documentation validation passed: rendered links, anchors, assets, and code references.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
