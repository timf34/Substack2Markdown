#!/usr/bin/env python3
"""Re-render existing scraped posts into the classic Substack look (no re-scraping).

Posts are scraped once and saved as Markdown/MDX (``substack_md_files/<author>``) with
structured metadata in ``data/<author>.json``. This script re-renders the per-post HTML
pages (``substack_html_pages/<author>``) from that on-disk content using the classic
Substack theme (Spectral, orange links, centered header) — so the new look can be applied
to posts scraped before the structured renderer existed, or re-applied after the theme is
tweaked, **without any network calls**.

When a cached comment thread exists (``substack_comments/<author>/<slug>.comments.json``,
created by ``--comments``), it is baked into the rendered page.

Usage:
    python render_posts.py aischoollibrarian            # re-render one author
    python render_posts.py aischoollibrarian news       # re-render multiple authors
    python render_posts.py --all                        # every data/*.json author
"""
from __future__ import annotations

import argparse
import json
import os

from substack_scraper import (
    COMMENTS_DATA_DIR,
    JSON_DATA_DIR,
    count_all_comments,
    render_post_to_html_file,
)


def detect_frontmatter_format(md_text: str) -> str:
    """``mdx`` if the file starts with a YAML frontmatter block, else ``legacy``."""
    return "mdx" if md_text.lstrip().startswith("---") else "legacy"


def render_author(author: str, force: bool = True) -> None:
    """Re-render every post for one author from its on-disk md + data json."""
    json_path = os.path.join(JSON_DATA_DIR, f"{author}.json")
    if not os.path.exists(json_path):
        print(f"[SKIP] No data file for author '{author}' ({json_path})")
        return

    with open(json_path, "r", encoding="utf-8") as f:
        entries = json.load(f)

    rendered = 0
    skipped = 0
    failed = 0

    for entry in entries:
        md_path = entry.get("file_link")
        html_path = entry.get("html_link")
        if not md_path or not html_path:
            failed += 1
            continue
        if not os.path.exists(md_path):
            print(f"[SKIP] Missing source markdown: {md_path}")
            failed += 1
            continue

        # Skip if the HTML is already newer than its source (unless --force).
        if not force and os.path.exists(html_path) \
                and os.path.getmtime(html_path) >= os.path.getmtime(md_path):
            skipped += 1
            continue

        try:
            with open(md_path, "r", encoding="utf-8") as f:
                md_text = f.read()
            fmt = detect_frontmatter_format(md_text)
            _meta, body = _split_for_render(md_text, fmt)

            # Structured metadata comes from the data json (richer than what's in the md).
            meta = {
                "title": entry.get("title", ""),
                "subtitle": entry.get("subtitle", ""),
                "author": entry.get("author", ""),
                "date": entry.get("date", ""),
                "cover_image": entry.get("cover_image", ""),
            }

            comments_list = _load_cached_comments(author, md_path)
            render_post_to_html_file(
                html_path, body, meta=meta, comments_list=comments_list, frontmatter_format=fmt
            )
            rendered += 1
        except Exception as exc:  # keep going; one bad post shouldn't abort the author
            print(f"[ERR] {md_path}: {exc}")
            failed += 1

    print(f"[{author}] rendered={rendered} skipped(up-to-date)={skipped} failed={failed} of {len(entries)}")


def _split_for_render(md_text: str, fmt: str):
    """Local import to avoid a circular reference at module load time."""
    from substack_scraper import split_metadata_and_body
    return split_metadata_and_body(md_text, fmt)


def _load_cached_comments(author: str, md_path: str) -> list:
    """Load a cached comment thread for a post, if present (no network)."""
    slug, _ = os.path.splitext(os.path.basename(md_path))
    cache_path = os.path.join(COMMENTS_DATA_DIR, author, f"{slug}.comments.json")
    if not os.path.exists(cache_path):
        return []
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            comments = json.load(f)
        if isinstance(comments, list) and count_all_comments(comments) > 0:
            return comments
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[WARN] Corrupt comments cache {cache_path}: {exc}")
    return []


def discover_authors() -> list:
    """Return every author stem found under ``data/`` (``<author>.json``)."""
    if not os.path.isdir(JSON_DATA_DIR):
        return []
    return sorted(
        os.path.splitext(name)[0]
        for name in os.listdir(JSON_DATA_DIR)
        if name.endswith(".json")
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "authors", nargs="*", help="Author name(s) (= data/<author>.json stem)."
    )
    parser.add_argument(
        "--all", action="store_true", help="Re-render every author found in data/."
    )
    parser.add_argument(
        "--force", action="store_true", default=True,
        help="Overwrite HTML even when it is newer than its source markdown (default).",
    )
    parser.add_argument(
        "--no-force", dest="force", action="store_false",
        help="Skip posts whose HTML is already newer than the source markdown.",
    )
    args = parser.parse_args()

    if args.all:
        authors = discover_authors()
        if not authors:
            print("[SKIP] No authors found under data/.")
        for author in authors:
            render_author(author, force=args.force)
    elif args.authors:
        for author in args.authors:
            render_author(author, force=args.force)
    else:
        parser.error("Provide one or more authors, or use --all.")


if __name__ == "__main__":
    main()
