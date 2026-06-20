#!/usr/bin/env python3
"""Backfill ``comment_count`` into existing ``data/<author>.json`` files.

The author index HTML pages sort and display comment counts, but posts scraped
before comment counting was added (or without ``--comments``) have no
``comment_count`` field and render as "0 Comments" even when the post has
comments.

This script resolves each post's comment count from Substack's public posts API
(``{base}/api/v1/posts/{slug}``), writes it back into the JSON, and regenerates
the author's HTML index page.

Posts that already have a ``comment_count`` (e.g. scraped with ``--comments``,
which yields the accurate recursive count) are skipped by default.

Usage:
    python backfill_comment_counts.py aischoollibrarian
    python backfill_comment_counts.py news --base-url https://aakashgupta.substack.com/
    python backfill_comment_counts.py aischoollibrarian natesnewsletter  # multiple authors
"""
from __future__ import annotations

import argparse
import json
import os
import time

from substack_scraper import JSON_DATA_DIR, get_post_id_from_slug, generate_html_file


def slug_from_entry(entry: dict) -> str | None:
    """Derive a post slug from an essay entry's local file path."""
    for key in ("file_link", "html_link"):
        link = entry.get(key)
        if link:
            base = os.path.basename(link)
            stem, _ = os.path.splitext(base)
            if stem:
                return stem
    return None


def backfill_author(author: str, base_url: str, force: bool = False) -> None:
    json_path = os.path.join(JSON_DATA_DIR, f"{author}.json")
    if not os.path.exists(json_path):
        print(f"[SKIP] No data file for author '{author}' ({json_path})")
        return
    if not base_url.endswith("/"):
        base_url += "/"

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    updated = 0
    skipped = 0
    failed = 0
    for entry in data:
        if not force and entry.get("comment_count") is not None:
            skipped += 1
            continue
        slug = slug_from_entry(entry)
        if not slug:
            failed += 1
            continue
        try:
            meta = get_post_id_from_slug(base_url, slug)
        except Exception as exc:  # network/parse error
            print(f"[ERR] {slug}: {exc}")
            failed += 1
            continue
        if meta is None:
            failed += 1
            continue
        _post_id, comment_count, _perms = meta
        entry["comment_count"] = comment_count
        updated += 1
        # Be gentle with the public API to avoid "too many requests" blocks.
        time.sleep(0.4)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

    print(f"[{author}] updated={updated} skipped(existing)={skipped} failed={failed} of {len(data)}")
    generate_html_file(author)
    print(f"[{author}] regenerated HTML index")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("authors", nargs="+", help="Author name(s) (= data/<author>.json stem)")
    parser.add_argument(
        "--base-url",
        default=None,
        help="Publication base URL. Defaults to https://<author>.substack.com/. "
        "Provide when the writer name differs from the subdomain (e.g. news -> aakashgupta.substack.com).",
    )
    parser.add_argument("--force", action="store_true", help="Re-fetch even posts that already have a comment_count.")
    args = parser.parse_args()

    for author in args.authors:
        base_url = args.base_url or f"https://{author}.substack.com/"
        backfill_author(author, base_url, force=args.force)


if __name__ == "__main__":
    main()
