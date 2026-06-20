import argparse
import hashlib
import json
import mimetypes
import os
import random
import re
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from urllib.parse import unquote, urlparse
from typing import List, Optional, Tuple
from time import sleep

import html2text
import markdown
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from tqdm import tqdm
from xml.etree import ElementTree as ET

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    SessionNotCreatedException,
    TimeoutException,
    WebDriverException,
    InvalidSessionIdException,
)

from config import EMAIL, PASSWORD

USE_PREMIUM: bool = True
BASE_SUBSTACK_URL: str = "https://niallferguson.substack.com/"
BASE_MD_DIR: str = "substack_md_files"
BASE_HTML_DIR: str = "substack_html_pages"
BASE_IMAGE_DIR: str = "substack_images"
COMMENTS_DATA_DIR: str = "substack_comments"
HTML_TEMPLATE: str = "author_template.html"
JSON_DATA_DIR: str = "data"
NUM_POSTS_TO_SCRAPE: int = 0
COMMENTS_SORT: str = "best"


def resolve_image_url(url: str) -> str:
    """Get the original image URL from a Substack CDN URL."""
    if url.startswith("https://substackcdn.com/image/fetch/"):
        parts = url.split("/https%3A%2F%2F")
        if len(parts) > 1:
            url = "https://" + unquote(parts[1])
    return url


def clean_linked_images(md_content: str) -> str:
    """Converts markdown linked images [![alt](img)](link) to ![alt](img)."""
    pattern = r'\[!\[(.*?)\]\((.*?)\)\]\(.*?\)'
    return re.sub(pattern, r'![\1](\2)', md_content)


def count_images_in_markdown(md_content: str) -> int:
    """Count number of image references in markdown content."""
    cleaned_content = clean_linked_images(md_content)
    pattern = r'!\[.*?\]\((.*?)\)'
    matches = re.findall(pattern, cleaned_content)
    return len(matches)


def is_post_url(url: str) -> bool:
    """Check if URL points to a specific post (contains /p/)."""
    return "/p/" in url


def get_publication_url(url: str) -> str:
    """Extract the base publication URL from a post URL."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/"


def get_post_slug(url: str) -> str:
    """Extract the post slug from a Substack post URL."""
    match = re.search(r'/p/([^/]+)', url)
    return match.group(1) if match else 'unknown_post'


def sanitize_image_filename(url: str) -> str:
    """Create a safe filename from an image URL."""
    url = resolve_image_url(url)
    filename = url.split("/")[-1]
    filename = filename.split("?")[0]
    filename = re.sub(r'[<>:"/\\|?*]', '', filename)

    if len(filename) > 100 or not filename:
        hash_object = hashlib.md5(url.encode())
        ext = mimetypes.guess_extension(
            requests.head(url).headers.get('content-type', '')
        ) or '.jpg'
        filename = f"{hash_object.hexdigest()}{ext}"

    return filename


def download_image(url: str, save_path: Path, pbar=None) -> Optional[str]:
    """Download image from URL and save to path."""
    try:
        response = requests.get(url, stream=True)
        if response.status_code == 200:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            with open(save_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            if pbar:
                pbar.update(1)
            return str(save_path)
    except Exception as e:
        msg = f"Error downloading image {url}: {str(e)}"
        if pbar:
            pbar.write(msg)
        else:
            print(msg)
    return None


def process_markdown_images(md_content: str, author: str, post_slug: str, pbar=None) -> str:
    """Process markdown content to download images and update references."""
    image_dir = Path(BASE_IMAGE_DIR) / author / post_slug
    md_content = clean_linked_images(md_content)

    def replace_image(match):
        url = match.group(0).strip('()')
        resolved_url = resolve_image_url(url)
        filename = sanitize_image_filename(url)
        save_path = image_dir / filename
        if not save_path.exists():
            download_image(resolved_url, save_path, pbar)

        rel_path = os.path.relpath(save_path, Path(BASE_MD_DIR) / author)
        return f"({rel_path})"

    pattern = r'\(https://substackcdn\.com/image/fetch/[^\s\)]+\)'
    return re.sub(pattern, replace_image, md_content)


# =============================================================================
# COMMENT HELPERS
# =============================================================================

def _request_json_with_rate_limit_retry(
    url: str,
    session: Optional[requests.Session] = None,
    max_attempts: int = 5,
) -> Optional[dict]:
    """GET a JSON URL, retrying on rate-limiting with exponential backoff.

    Returns parsed JSON on success, or ``None`` on non-retryable failure / after exhausting
    retries. A shared ``session`` may be passed to carry auth cookies (premium scraper).
    """
    requester = session if session is not None else requests
    for attempt in range(1, max_attempts + 1):
        try:
            response = requester.get(url)
            if not response.ok:
                text_lower = (response.text or "").lower()
                if response.status_code == 429 or "too many requests" in text_lower:
                    if attempt == max_attempts:
                        print(f"[WARN] Max attempts reached for URL: {url}. Too many requests.")
                        return None
                    base = 2 ** attempt
                    delay = base + random.uniform(-0.2 * base, 0.2 * base)
                    print(f"[{attempt}/{max_attempts}] Too many requests. Retrying in {delay:.2f}s...")
                    sleep(delay)
                    continue
                return None
            return response.json()
        except Exception as e:
            if attempt == max_attempts:
                print(f"[WARN] Error fetching JSON {url}: {e}")
                return None
            base = 2 ** attempt
            delay = base + random.uniform(-0.2 * base, 0.2 * base)
            sleep(delay)
    return None


def get_post_id_from_slug(
    base_url: str, slug: str, session: Optional[requests.Session] = None
) -> Optional[Tuple[int, int, str]]:
    """Fetch post metadata needed for comments via the public post API.

    Returns ``(post_id, comment_count, write_comment_permissions)`` or ``None`` on failure.
    """
    api_url = f"{base_url}api/v1/posts/{slug}"
    data = _request_json_with_rate_limit_retry(api_url, session=session)
    if not isinstance(data, dict):
        return None
    post_id = data.get("id")
    if post_id is None:
        return None
    permissions = data.get("write_comment_permissions", "") or ""
    if isinstance(permissions, list):
        permissions = ",".join(str(p) for p in permissions)
    return (
        int(post_id),
        int(data.get("comment_count", 0) or 0),
        str(permissions),
    )


def fetch_comments(
    base_url: str,
    post_id: int,
    sort: str = COMMENTS_SORT,
    session: Optional[requests.Session] = None,
) -> List[dict]:
    """Fetch the (nested) comment thread for a post via the public comments API."""
    api_url = f"{base_url}api/v1/post/{post_id}/comments?all_comments=true&sort={sort}"
    data = _request_json_with_rate_limit_retry(api_url, session=session)
    if not isinstance(data, dict):
        return []
    return data.get("comments", []) or []


def count_all_comments(comments: List[dict]) -> int:
    """Count comments including nested children."""
    total = 0
    for c in comments:
        total += 1
        total += count_all_comments(c.get("children", []) or [])
    return total


def _format_comment_date(date_str: str) -> str:
    if not date_str:
        return ""
    try:
        date_obj = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return date_obj.strftime("%b %d, %Y")
    except (ValueError, TypeError):
        return date_str


def _format_reactions(comment: dict) -> str:
    reactions = comment.get("reactions") or {}
    if isinstance(reactions, dict) and reactions:
        total = sum(int(v) for v in reactions.values() if isinstance(v, (int, float)))
    else:
        total = int(comment.get("reaction_count", 0) or 0)
    return f" ❤ {total}" if total > 0 else ""


def render_comments_markdown(comments: List[dict], depth: int = 0) -> str:
    """Render a nested comment thread as Markdown.

    Top-level comments are rendered as normal blocks; replies are nested as blockquotes.
    """
    if not comments:
        return ""
    blocks = []
    for c in comments:
        name = c.get("name", "Anonymous") or "Anonymous"
        is_author = bool((c.get("metadata") or {}).get("is_author"))
        author_flag = " (author)" if is_author else ""

        date_str = _format_comment_date(c.get("date", ""))
        edited = " (edited)" if c.get("edited_at") else ""
        reactions = _format_reactions(c)

        header = f"**{name}**{author_flag}"
        meta_bits = []
        if date_str:
            meta_bits.append(date_str + edited)
        tail = " · ".join(meta_bits) + reactions
        if tail.strip():
            header += f" · {tail.strip()}"

        body = (c.get("body", "") or "").strip()
        if c.get("deleted"):
            body = "_[comment deleted]_"
        block = f"{header}\n\n{body}" if body else header

        children = c.get("children", []) or []
        if children:
            child_md = render_comments_markdown(children, depth + 1)
            if child_md:
                indented = "\n".join(f"> {ln}" if ln else ">" for ln in child_md.splitlines())
                block += f"\n\n{indented}"

        blocks.append(block)

    return "\n\n".join(blocks)


def _html_escape(text: str) -> str:
    """Minimal HTML escaping for safe insertion into attribute/text content."""
    if not text:
        return ""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_comments_html(comments: List[dict]) -> str:
    """Render a nested comment thread as semantic HTML for embedding in a post page.

    Returns ``""`` for an empty thread (caller should then render no section). Each comment
    renders an avatar (when available), author name, optional "Author" flag, date, reaction
    count, the comment body (converted from its markdown-ish text to HTML), and any nested
    replies inside ``<ul class="comment-children">``.
    """
    if not comments:
        return ""

    def render_one(c: dict) -> str:
        name = c.get("name", "Anonymous") or "Anonymous"
        is_author = bool((c.get("metadata") or {}).get("is_author"))
        date_str = _format_comment_date(c.get("date", ""))
        photo_url = (c.get("photo_url") or "").strip()

        reactions = c.get("reactions") or {}
        if isinstance(reactions, dict) and reactions:
            react_total = sum(int(v) for v in reactions.values() if isinstance(v, (int, float)))
        else:
            react_total = int(c.get("reaction_count", 0) or 0)

        avatar_html = (
            f'<img class="comment-avatar" src="{_html_escape(photo_url)}" alt="" loading="lazy">'
            if photo_url else ""
        )
        author_flag_html = (
            '<span class="comment-author-flag">Author</span>' if is_author else ""
        )
        date_html = (
            f'<span class="comment-date">{_html_escape(date_str)}</span>' if date_str else ""
        )
        reactions_html = (
            f'<span class="comment-reactions">❤ {react_total}</span>' if react_total > 0 else ""
        )

        header = (
            f'<div class="comment-header">{avatar_html}'
            f'<span class="comment-author">{_html_escape(name)}</span>'
            f'{author_flag_html}{date_html}{reactions_html}</div>'
        )

        body = (c.get("body", "") or "").strip()
        if c.get("deleted"):
            body = "_[comment deleted]_"
        body_html = md_to_html_static(body)

        children = c.get("children", []) or []
        children_html = ""
        if children:
            inner = "".join(f"<li>{render_one(child)}</li>" for child in children)
            children_html = f'<ul class="comment-children">{inner}</ul>'

        return f'{header}<div class="comment-body">{body_html}</div>{children_html}'

    total = count_all_comments(comments)
    items = "".join(f"<li class=\"comment\">{render_one(c)}</li>" for c in comments)
    return (
        f'<section class="comments">'
        f'<h2>Comments ({total})</h2>'
        f'<ul class="comment-thread">{items}</ul>'
        f'</section>'
    )


def md_to_html_static(md_content: str) -> str:
    """Module-level Markdown → HTML conversion (mirrors BaseSubstackScraper.md_to_html)."""
    if not md_content:
        return ""
    return markdown.markdown(md_content, extensions=['extra'])


# =============================================================================
# STRUCTURED HEADER RENDERING (classic Substack article look)
# =============================================================================

def _format_header_date(date_str: str) -> str:
    """Format an ISO date (``YYYY-MM-DD``) for display in the byline.

    Falls back to the raw string (including the sentinel ``"Date not found"``).
    Mirrors the legacy header date formatting in ``combine_metadata_and_content``.
    """
    if not date_str:
        return ""
    try:
        return datetime.fromisoformat(date_str).strftime("%b %d, %Y")
    except ValueError:
        return date_str


def build_post_header(meta: dict) -> str:
    """Render a Substack-style centered post header from structured metadata.

    ``meta`` is a dict that may contain: ``title``, ``subtitle``, ``author``,
    ``date`` (ISO ``YYYY-MM-DD``), ``cover_image``. Any missing/empty field is
    simply omitted. Returns an HTML string for a ``<header class="post-header">``
    block, or ``""`` if there is nothing to render (no title, subtitle, author
    or date). The cover image is shown above the title when present.
    """
    if not isinstance(meta, dict) or not meta:
        return ""

    cover_image = (meta.get("cover_image") or "").strip()
    title = (meta.get("title") or "").strip()
    subtitle = (meta.get("subtitle") or "").strip()
    author = (meta.get("author") or "").strip()
    date_str = (meta.get("date") or "").strip()

    if not (title or subtitle or author or date_str):
        return ""

    parts = []
    if cover_image:
        parts.append(
            f'<img class="post-cover" src="{_html_escape(cover_image)}" alt="" loading="eager">'
        )
    if title:
        parts.append(f'<h1 class="post-title">{_html_escape(title)}</h1>')
    if subtitle:
        parts.append(f'<h3 class="post-subtitle">{_html_escape(subtitle)}</h3>')

    byline_bits = []
    if author:
        byline_bits.append(_html_escape(author))
    display_date = _format_header_date(date_str)
    if display_date:
        byline_bits.append(_html_escape(display_date))
    if byline_bits:
        parts.append(
            f'<p class="post-byline">{" · ".join(byline_bits)}</p>'
        )

    return f'<header class="post-header">{"".join(parts)}</header>'


def split_metadata_and_body(md_content: str, frontmatter_format: str = "legacy") -> Tuple[dict, str]:
    """Inverse of ``combine_metadata_and_content``: recover metadata + body.

    Used by ``render_posts.py`` to re-render on-disk markdown into the structured
    Substack look without re-scraping. Returns ``(meta_dict, body_md)`` where
    ``meta_dict`` has keys ``title``, ``subtitle``, ``author``, ``date``,
    ``cover_image``, and ``like_count`` (any that aren't found are absent).

    - ``mdx``: strip the leading YAML frontmatter (``---\\n…\\n---``) and parse it.
    - ``legacy``: strip the leading ``# title`` line, an optional ``## subtitle``,
      the ``**<display date>**`` line, and the ``**Likes:** N`` line.

    If the content doesn't match the expected pattern, it is returned unchanged as
    the body with an empty metadata dict (so rendering is never destructive).
    """
    if not md_content:
        return {}, ""

    meta: dict = {}

    if frontmatter_format == "mdx":
        m = re.match(r'^---\s*\n(.*?)\n---\s*\n?(.*)$', md_content, re.DOTALL)
        if m:
            for line in m.group(1).splitlines():
                if ":" not in line:
                    continue
                key, _, raw = line.partition(":")
                key = key.strip()
                val = raw.strip()
                # Strip surrounding YAML quotes.
                if (val.startswith('"') and val.endswith('"')) \
                        or (val.startswith("'") and val.endswith("'")):
                    val = val[1:-1]
                if key and val:
                    if key == "image":
                        meta["cover_image"] = val
                    else:
                        meta[key] = val
            return meta, m.group(2).lstrip("\n")

    # legacy format
    lines = md_content.split("\n")
    idx = 0

    # Title: "# ..."
    if idx < len(lines) and lines[idx].startswith("# "):
        meta["title"] = lines[idx][2:].strip()
        idx += 1
        # Skip the blank line after the title.
        if idx < len(lines) and lines[idx].strip() == "":
            idx += 1
        # Subtitle: "## ..."
        if idx < len(lines) and lines[idx].startswith("## "):
            meta["subtitle"] = lines[idx][3:].strip()
            idx += 1
            if idx < len(lines) and lines[idx].strip() == "":
                idx += 1
        # Date: "**...**"
        date_match = re.match(r'^\*\*(.+?)\*\*$', lines[idx]) if idx < len(lines) else None
        if date_match:
            meta["date"] = date_match.group(1).strip()
            idx += 1
            if idx < len(lines) and lines[idx].strip() == "":
                idx += 1
        # Likes: "**Likes:** N"
        likes_match = re.match(r'^\*\*Likes:\*\*\s*(\d+)\s*$', lines[idx]) if idx < len(lines) else None
        if likes_match:
            meta["like_count"] = likes_match.group(1)
            idx += 1
            if idx < len(lines) and lines[idx].strip() == "":
                idx += 1

    body = "\n".join(lines[idx:]).lstrip("\n")
    return meta, body


def build_post_document(
    html_dir: str,
    body_html: str,
    comments_html: str = "",
    header_html: str = "",
    title: Optional[str] = None,
) -> str:
    """Assemble the full HTML document for a post page (classic Substack shell).

    Shared by the scraper's ``save_to_html_file`` and the standalone ``render_posts.py``
    so both produce identical markup: Spectral webfont, the essay stylesheet, an optional
    structured header above the body, and optional comments below it.
    """
    css_path = os.path.relpath("./assets/css/essay-styles.css", html_dir)
    css_path = css_path.replace("\\", "/")

    doc_title = _html_escape(title) if title else "Markdown Content"
    header_block = f"\n                {header_html}" if header_html else ""
    comments_block = f"\n                {comments_html}" if comments_html else ""

    return f"""
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>{doc_title}</title>
                <link rel="preconnect" href="https://fonts.googleapis.com">
                <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
                <link href="https://fonts.googleapis.com/css2?family=Spectral:ital,wght@0,400;0,600;0,700;1,400&display=swap" rel="stylesheet">
                <link rel="stylesheet" href="{css_path}">
            </head>
            <body>
                <main class="markdown-content">{header_block}
                {body_html}{comments_block}
                </main>
            </body>
            </html>
        """


def render_post_to_html_file(
    html_filepath: str,
    body_md: str,
    meta: Optional[dict] = None,
    comments_list: Optional[list] = None,
    frontmatter_format: str = "legacy",
) -> None:
    """Re-render a post page from markdown + structured metadata + cached comments.

    Network-free: reads only local content. Used by ``render_posts.py`` (and the
    ``--render-only`` CLI path) to apply the classic Substack look to posts that were
    scraped before the structured renderer existed, without re-scraping.

    ``body_md`` is rendered as the post body; ``meta`` (title/subtitle/author/date/
    cover_image) becomes the header. If ``body_md`` still contains a legacy/mdx header
    (i.e. it's the full on-disk markdown), pass ``split=True``... otherwise it is split
    via ``split_metadata_and_body`` automatically when ``meta`` is empty.
    """
    body = body_md
    header_meta = meta or {}

    # If no structured meta was supplied, try to recover it from the markdown itself.
    if not header_meta:
        header_meta, body = split_metadata_and_body(body_md, frontmatter_format)

    body_html = md_to_html_static(body)
    comments_html = render_comments_html(comments_list) if comments_list else ""
    header_html = build_post_header(header_meta)
    title = header_meta.get("title")

    html_dir = os.path.dirname(html_filepath)
    document = build_post_document(
        html_dir, body_html, comments_html=comments_html, header_html=header_html, title=title
    )
    with open(html_filepath, "w", encoding="utf-8") as f:
        f.write(document)


def extract_main_part(url: str) -> str:
    parts = urlparse(url).netloc.split('.')
    return parts[1] if parts[0] == 'www' else parts[0]


def generate_html_file(author_name: str) -> None:
    """Generates a HTML file for the given author."""
    if not os.path.exists(BASE_HTML_DIR):
        os.makedirs(BASE_HTML_DIR)

    json_path = os.path.join(JSON_DATA_DIR, f'{author_name}.json')
    with open(json_path, 'r', encoding='utf-8') as file:
        essays_data = json.load(file)

    embedded_json_data = json.dumps(essays_data, ensure_ascii=False, indent=4)

    with open(HTML_TEMPLATE, 'r', encoding='utf-8') as file:
        html_template = file.read()

    html_with_data = html_template.replace('<!-- AUTHOR_NAME -->', author_name).replace(
        '<script type="application/json" id="essaysData"></script>',
        f'<script type="application/json" id="essaysData">{embedded_json_data}</script>'
    )
    html_with_author = html_with_data.replace('author_name', author_name)

    html_output_path = os.path.join(BASE_HTML_DIR, f'{author_name}.html')
    with open(html_output_path, 'w', encoding='utf-8') as file:
        file.write(html_with_author)


# =============================================================================
# BROWSER/DRIVER UTILITIES
# =============================================================================

class BrowserManager:
    """
    Handles browser detection, driver management, and initialization.
    Supports Chrome (preferred) and Edge with robust fallback logic.
    
    Key insight: Instead of trying to move/delete system drivers (requires admin),
    we download to a local cache and use explicit paths, bypassing PATH entirely.
    """
    
    SUPPORTED_BROWSERS = ['chrome', 'edge']
    CACHE_DIR = os.path.join(os.path.expanduser('~'), '.substack_scraper', 'drivers')
    
    @classmethod
    def get_cache_dir(cls) -> str:
        """Get or create the local driver cache directory."""
        if not os.path.exists(cls.CACHE_DIR):
            os.makedirs(cls.CACHE_DIR)
        return cls.CACHE_DIR
    
    @staticmethod
    def get_browser_version(browser: str) -> Optional[str]:
        """
        Attempts to detect the installed browser version.
        Returns version string or None if not found.
        """
        version = None
        
        if browser == 'chrome':
            if os.name == 'nt':  # Windows
                paths = [
                    r'C:\Program Files\Google\Chrome\Application\chrome.exe',
                    r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
                    os.path.expandvars(r'%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe'),
                ]
                for path in paths:
                    if os.path.exists(path):
                        try:
                            result = subprocess.run(
                                ['powershell', '-Command', f'(Get-Item "{path}").VersionInfo.FileVersion'],
                                capture_output=True, text=True, timeout=10
                            )
                            if result.returncode == 0:
                                version = result.stdout.strip()
                                break
                        except Exception:
                            pass
            else:  # macOS/Linux
                candidates = [
                    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                    "google-chrome",  # Linux PATH fallback
                    "chromium",
                    "chromium-browser",
                ]
                for candidate in candidates:
                    try:
                        result = subprocess.run(
                            [candidate, "--version"],
                            capture_output=True, text=True, timeout=10
                        )
                        if result.returncode == 0:
                            match = re.search(r'(\d+\.\d+\.\d+\.\d+)', result.stdout)
                            if match:
                                version = match.group(1)
                                break
                    except Exception:
                        continue
                    
        elif browser == 'edge':
            if os.name == 'nt':  # Windows
                paths = [
                    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
                    r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
                ]
                for path in paths:
                    if os.path.exists(path):
                        try:
                            result = subprocess.run(
                                ['powershell', '-Command', f'(Get-Item "{path}").VersionInfo.FileVersion'],
                                capture_output=True, text=True, timeout=10
                            )
                            if result.returncode == 0:
                                version = result.stdout.strip()
                                break
                        except Exception:
                            pass
            else:  # macOS/Linux
                candidates = [
                    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                    os.path.expanduser("~/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
                    "microsoft-edge",  # Linux PATH fallback
                ]
                for candidate in candidates:
                    try:
                        result = subprocess.run(
                            [candidate, "--version"],
                            capture_output=True, text=True, timeout=10
                        )
                        if result.returncode == 0:
                            match = re.search(r'(\d+\.\d+\.\d+\.\d+)', result.stdout)
                            if match:
                                version = match.group(1)
                                break
                    except Exception:
                        continue
        
        return version
    
    @staticmethod
    def get_driver_version(driver_path: str) -> Optional[str]:
        """Get the version of a webdriver executable."""
        if not os.path.exists(driver_path):
            return None
        try:
            result = subprocess.run(
                [driver_path, '--version'],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                match = re.search(r'(\d+\.\d+\.\d+\.\d+)', result.stdout)
                if match:
                    return match.group(1)
        except Exception:
            pass
        return None
    
    @staticmethod
    def versions_compatible(browser_version: str, driver_version: str) -> bool:
        """Check if browser and driver major versions match."""
        if not browser_version or not driver_version:
            return False
        try:
            browser_major = int(browser_version.split('.')[0])
            driver_major = int(driver_version.split('.')[0])
            return browser_major == driver_major
        except (ValueError, IndexError):
            return False
    
    @staticmethod
    def find_stale_drivers() -> List[str]:
        """Find potentially stale driver executables in common PATH locations."""
        stale_paths = []
        common_locations = [
            r'C:\Windows\msedgedriver.exe',
            r'C:\Windows\chromedriver.exe',
            r'C:\Windows\System32\msedgedriver.exe',
            r'C:\Windows\System32\chromedriver.exe',
        ]
        for path in common_locations:
            if os.path.exists(path):
                stale_paths.append(path)
        return stale_paths
    
    @staticmethod
    def get_user_data_dir(browser: str) -> str:
        """Returns a custom user data directory for browser session persistence."""
        base_dir = os.path.join(os.path.expanduser('~'), '.substack_scraper')
        if not os.path.exists(base_dir):
            os.makedirs(base_dir)
        return os.path.join(base_dir, f'{browser}_profile')

    @staticmethod
    def _driver_platform(browser: str) -> str:
        """Return the Chrome-for-Testing / Edge driver platform string for the current OS/arch.

        Detects ARM vs Intel on macOS so Apple Silicon gets mac-arm64 instead of mac-x64.
        """
        if os.name == 'nt':
            return 'win64'
        if sys.platform == 'darwin':
            is_arm = os.uname().machine == 'arm64'
            if browser == 'edge':
                # Edge driver uses a different naming convention: mac64 vs mac64_m1
                return 'mac64_m1' if is_arm else 'mac64'
            return 'mac-arm64' if is_arm else 'mac-x64'
        return 'linux64'

    @classmethod
    def download_driver_with_requests(cls, browser: str, browser_version: str, quiet: bool = False) -> Optional[str]:
        """
        Download the correct driver directly using requests.
        This bypasses webdriver_manager issues and gives us full control.
        Returns the path to the downloaded driver, or None if failed.
        """
        import zipfile
        import io
        
        major_version = browser_version.split('.')[0]
        cache_dir = cls.get_cache_dir()
        
        if browser == 'chrome':
            # Chrome for Testing JSON endpoint
            driver_name = 'chromedriver.exe' if os.name == 'nt' else 'chromedriver'
            driver_path = os.path.join(cache_dir, f'chromedriver-{major_version}', driver_name)
            
            # Check if we already have a compatible driver cached
            if os.path.exists(driver_path):
                cached_version = cls.get_driver_version(driver_path)
                if cached_version and cls.versions_compatible(browser_version, cached_version):
                    if not quiet:
                        print(f"Using cached chromedriver {cached_version}")
                    return driver_path
            
            try:
                # Get the latest driver version for this Chrome version
                if not quiet:
                    print(f"Fetching Chrome driver info for version {major_version}...")
                
                # Try the Chrome for Testing endpoints
                endpoints = [
                    f"https://googlechromelabs.github.io/chrome-for-testing/LATEST_RELEASE_{major_version}",
                    "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json",
                ]
                
                driver_version = None
                download_url = None
                
                # Try LATEST_RELEASE endpoint first
                try:
                    resp = requests.get(endpoints[0], timeout=30)
                    if resp.ok:
                        driver_version = resp.text.strip()
                        # Construct download URL
                        platform = cls._driver_platform(browser)
                        download_url = f"https://storage.googleapis.com/chrome-for-testing-public/{driver_version}/{platform}/chromedriver-{platform}.zip"
                except Exception:
                    pass
                
                # Fallback to JSON endpoint
                if not download_url:
                    resp = requests.get(endpoints[1], timeout=30)
                    if resp.ok:
                        data = resp.json()
                        channels = data.get('channels', {})
                        stable = channels.get('Stable', {})
                        driver_version = stable.get('version', '')
                        
                        if driver_version.startswith(major_version):
                            downloads = stable.get('downloads', {}).get('chromedriver', [])
                            platform = cls._driver_platform(browser)
                            for d in downloads:
                                if d.get('platform') == platform:
                                    download_url = d.get('url')
                                    break
                
                if not download_url:
                    print(f"Could not find chromedriver download URL for Chrome {major_version}")
                    return None
                
                if not quiet:
                    print(f"Downloading chromedriver {driver_version}...")
                resp = requests.get(download_url, timeout=120)
                if not resp.ok:
                    print(f"Download failed: HTTP {resp.status_code}")
                    return None
                
                # Extract the driver
                extract_dir = os.path.join(cache_dir, f'chromedriver-{major_version}')
                if os.path.exists(extract_dir):
                    shutil.rmtree(extract_dir)
                os.makedirs(extract_dir)
                
                with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                    # Find the chromedriver executable in the zip
                    for name in zf.namelist():
                        if name.endswith(driver_name):
                            # Extract to our directory
                            source = zf.open(name)
                            target_path = os.path.join(extract_dir, driver_name)
                            with open(target_path, 'wb') as target:
                                target.write(source.read())
                            # Make executable on Unix
                            if os.name != 'nt':
                                os.chmod(target_path, 0o755)
                            if not quiet:
                                print(f"[OK] Chromedriver downloaded to: {target_path}")
                            return target_path
                
                print("Could not find chromedriver in downloaded archive")
                return None
                
            except Exception as e:
                print(f"Failed to download chromedriver: {e}")
                return None
                
        elif browser == 'edge':
            driver_name = 'msedgedriver.exe' if os.name == 'nt' else 'msedgedriver'
            driver_path = os.path.join(cache_dir, f'msedgedriver-{major_version}', driver_name)
            
            # Check cache
            if os.path.exists(driver_path):
                cached_version = cls.get_driver_version(driver_path)
                if cached_version and cls.versions_compatible(browser_version, cached_version):
                    if not quiet:
                        print(f"Using cached msedgedriver {cached_version}")
                    return driver_path
            
            try:
                # Get latest Edge driver version
                if not quiet:
                    print(f"Fetching Edge driver info for version {major_version}...")
                
                # Edge driver download URL pattern
                platform = cls._driver_platform(browser)
                
                # Try to get the exact version
                version_url = f"https://msedgedriver.azureedge.net/LATEST_RELEASE_{major_version}"
                try:
                    resp = requests.get(version_url, timeout=30)
                    if resp.ok:
                        driver_version = resp.text.strip()
                    else:
                        driver_version = browser_version  # Fall back to browser version
                except Exception:
                    driver_version = browser_version
                
                download_url = f"https://msedgedriver.azureedge.net/{driver_version}/edgedriver_{platform}.zip"
                
                if not quiet:
                    print(f"Downloading msedgedriver {driver_version}...")
                resp = requests.get(download_url, timeout=120)
                if not resp.ok:
                    print(f"Download failed: HTTP {resp.status_code}")
                    return None
                
                # Extract
                extract_dir = os.path.join(cache_dir, f'msedgedriver-{major_version}')
                if os.path.exists(extract_dir):
                    shutil.rmtree(extract_dir)
                os.makedirs(extract_dir)
                
                with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                    for name in zf.namelist():
                        if name.endswith(driver_name):
                            source = zf.open(name)
                            target_path = os.path.join(extract_dir, driver_name)
                            with open(target_path, 'wb') as target:
                                target.write(source.read())
                            if os.name != 'nt':
                                os.chmod(target_path, 0o755)
                            if not quiet:
                                print(f"[OK] msedgedriver downloaded to: {target_path}")
                            return target_path
                
                print("Could not find msedgedriver in downloaded archive")
                return None
                
            except Exception as e:
                print(f"Failed to download msedgedriver: {e}")
                return None
        
        return None

    @classmethod
    def create_driver(
        cls,
        browser: str = 'chrome',
        headless: bool = False,
        driver_path: Optional[str] = None,
        browser_path: Optional[str] = None,
        user_agent: Optional[str] = None,
        use_persistent_profile: bool = False,
        quiet: bool = False,
    ) -> webdriver.Remote:
        """
        Creates a WebDriver instance with smart fallback logic.
        
        Strategy:
        1. Use explicit driver path if provided
        2. Check our local cache for a compatible driver
        3. Download driver directly to our cache (bypasses PATH issues)
        4. Fall back to webdriver_manager
        5. Fall back to Selenium Manager

        Args:
            quiet: Suppress informational prints (used during periodic driver restarts).
                   Warnings/errors are still printed.
        """
        browser = browser.lower()
        if browser not in cls.SUPPORTED_BROWSERS:
            raise ValueError(f"Unsupported browser: {browser}. Use one of: {cls.SUPPORTED_BROWSERS}")
        
        # Check for stale drivers (for warning purposes only)
        stale_drivers = cls.find_stale_drivers()
        if stale_drivers:
            print(f"WARNING: Found old drivers in system PATH that may cause issues if other methods fail:")
            for p in stale_drivers:
                v = cls.get_driver_version(p) or "unknown"
                print(f"   - {p} (version: {v})")
            print("   We'll try to bypass these by using our own driver cache.\n")
        
        # Detect browser version
        browser_version = cls.get_browser_version(browser)
        if not quiet:
            print(f"Detected {browser.title()} version: {browser_version or 'unknown'}")
        
        if not browser_version:
            print(f"WARNING: Could not detect {browser.title()} version. Make sure it's installed.")
        
        # Build options
        if browser == 'chrome':
            options = ChromeOptions()
        else:
            options = EdgeOptions()
            
        if headless:
            options.add_argument("--headless=new")
        
        if browser_path:
            options.binary_location = browser_path
            
        if user_agent:
            options.add_argument(f"user-agent={user_agent}")
        
        if use_persistent_profile:
            profile_dir = cls.get_user_data_dir(browser)
            options.add_argument(f"user-data-dir={profile_dir}")
            if not quiet:
                print(f"Using persistent profile at: {profile_dir}")
        
        # Common options for stability
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        
        errors = []
        
        # Strategy 1: Explicit driver path
        if driver_path and os.path.exists(driver_path):
            try:
                if not quiet:
                    print(f"Using explicit driver path: {driver_path}")
                driver_version = cls.get_driver_version(driver_path)
                if driver_version:
                    print(f"Driver version: {driver_version}")
                    if browser_version and not cls.versions_compatible(browser_version, driver_version):
                        print(f"WARNING: Driver version may not match browser version")
                
                if browser == 'chrome':
                    service = ChromeService(executable_path=driver_path)
                    return webdriver.Chrome(service=service, options=options)
                else:
                    service = EdgeService(executable_path=driver_path)
                    return webdriver.Edge(service=service, options=options)
            except Exception as e:
                errors.append(f"Explicit driver path failed: {e}")
                print(f"[FAIL] Explicit driver path failed: {e}")
        
        # Strategy 2: Download to our cache (primary method - bypasses PATH issues)
        if browser_version:
            if not quiet:
                print(f"\nDownloading driver to local cache (bypasses system PATH)...")
            try:
                downloaded_path = cls.download_driver_with_requests(browser, browser_version, quiet=quiet)
                if downloaded_path and os.path.exists(downloaded_path):
                    if not quiet:
                        print(f"Using downloaded driver: {downloaded_path}")
                    if browser == 'chrome':
                        service = ChromeService(executable_path=downloaded_path)
                        return webdriver.Chrome(service=service, options=options)
                    else:
                        service = EdgeService(executable_path=downloaded_path)
                        return webdriver.Edge(service=service, options=options)
            except Exception as e:
                errors.append(f"Direct download failed: {e}")
                print(f"[FAIL] Direct download failed: {e}")
        
        # Strategy 3: webdriver_manager with explicit path
        if not quiet:
            print("\nTrying webdriver_manager...")
        try:
            if browser == 'chrome':
                from webdriver_manager.chrome import ChromeDriverManager
                from webdriver_manager.core.os_manager import ChromeType
                mgr = ChromeDriverManager()
                driver_path_wdm = mgr.install()
                if not quiet:
                    print(f"webdriver_manager installed driver to: {driver_path_wdm}")
                # Reject known non-executable artifacts (THIRD_PARTY_NOTICES / LICENSE) returned
                # by webdriver_manager on some platforms before trying to exec them.
                if driver_path_wdm and os.path.isfile(driver_path_wdm) \
                        and not driver_path_wdm.endswith("THIRD_PARTY_NOTICES.chromedriver") \
                        and not driver_path_wdm.endswith("LICENSE.chromedriver"):
                    service = ChromeService(executable_path=driver_path_wdm)
                    return webdriver.Chrome(service=service, options=options)
                print(f"[SKIP] webdriver_manager returned a non-driver file, falling through.")
            else:
                from webdriver_manager.microsoft import EdgeChromiumDriverManager
                mgr = EdgeChromiumDriverManager()
                driver_path_wdm = mgr.install()
                if not quiet:
                    print(f"webdriver_manager installed driver to: {driver_path_wdm}")
                if driver_path_wdm and os.path.isfile(driver_path_wdm) \
                        and not driver_path_wdm.endswith("THIRD_PARTY_NOTICES.msedgedriver") \
                        and not driver_path_wdm.endswith("LICENSE.msedgedriver"):
                    service = EdgeService(executable_path=driver_path_wdm)
                    return webdriver.Edge(service=service, options=options)
                print(f"[SKIP] webdriver_manager returned a non-driver file, falling through.")
        except Exception as e:
            errors.append(f"webdriver_manager failed: {e}")
            print(f"[FAIL] webdriver_manager failed: {e}")
        
        # Strategy 4: Let Selenium Manager try (last resort)
        if not quiet:
            print("\nTrying Selenium Manager (last resort)...")
        try:
            if browser == 'chrome':
                return webdriver.Chrome(options=options)
            else:
                return webdriver.Edge(options=options)
        except Exception as e:
            errors.append(f"Selenium Manager failed: {e}")
            print(f"[FAIL] Selenium Manager failed: {e}")
        
        # All strategies failed
        error_msg = cls._build_error_message(browser, browser_version, stale_drivers, errors)
        raise RuntimeError(error_msg)
    
    @classmethod
    def _build_error_message(
        cls, 
        browser: str, 
        browser_version: Optional[str],
        stale_drivers: List[str],
        errors: List[str]
    ) -> str:
        """Build a helpful error message when driver creation fails."""
        
        lines = [
            "",
            "=" * 70,
            "BROWSER DRIVER SETUP FAILED",
            "=" * 70,
            "",
            f"Could not start {browser.title()} WebDriver.",
            "",
        ]
        
        if browser_version:
            lines.append(f"Your {browser.title()} version: {browser_version}")
            major_version = browser_version.split('.')[0]
        else:
            lines.append(f"Could not detect your {browser.title()} version.")
            major_version = "XXX"
        
        lines.append("")
        
        if stale_drivers:
            lines.extend([
                "STALE DRIVERS IN SYSTEM PATH:",
                "These old drivers may have interfered with automatic setup:",
            ])
            for path in stale_drivers:
                driver_ver = cls.get_driver_version(path) or "unknown version"
                lines.append(f"   - {path} (version: {driver_ver})")
            lines.extend([
                "",
                "To fix: Open an Administrator command prompt and delete these files,",
                "or rename them (e.g., chromedriver.exe.bak)",
                "",
            ])
        
        lines.extend([
            "HOW TO FIX:",
            "",
            "Option 1: Download the correct driver manually",
        ])
        
        if browser == 'chrome':
            lines.extend([
                f"   1. Go to: https://googlechromelabs.github.io/chrome-for-testing/",
                f"   2. Download chromedriver for version {major_version} (win64)",
                f"   3. Extract chromedriver.exe somewhere (e.g., C:\\tools\\chromedriver.exe)",
                f"   4. Run with: --chrome-driver-path C:\\tools\\chromedriver.exe",
            ])
        else:
            lines.extend([
                f"   1. Go to: https://developer.microsoft.com/en-us/microsoft-edge/tools/webdriver/",
                f"   2. Download msedgedriver for version {major_version}",
                f"   3. Extract msedgedriver.exe somewhere (e.g., C:\\tools\\msedgedriver.exe)",
                f"   4. Run with: --edge-driver-path C:\\tools\\msedgedriver.exe",
            ])
        
        lines.extend([
            "",
            "Option 2: Try a different browser",
            f"   python substack_scraper.py --premium --browser {'edge' if browser == 'chrome' else 'chrome'}",
            "",
            "Option 3: Delete stale drivers (requires Administrator)",
            "   Open cmd as Administrator and run:",
        ])
        for path in stale_drivers:
            lines.append(f"   del \"{path}\"")
        
        lines.extend([
            "",
            "-" * 70,
            "Debug info (errors encountered):",
        ])
        
        for i, error in enumerate(errors, 1):
            error_short = str(error)[:300] + "..." if len(str(error)) > 300 else str(error)
            lines.append(f"   {i}. {error_short}")
        
        lines.extend(["", "=" * 70])
        
        return "\n".join(lines)


# =============================================================================
# BASE SCRAPER CLASS
# =============================================================================

class BaseSubstackScraper(ABC):
    def __init__(
        self,
        base_substack_url: str,
        md_save_dir: str,
        html_save_dir: str,
        download_images: bool = False,
        frontmatter_format: str = "legacy",
        fetch_comments_flag: bool = False,
        comments_sort: str = COMMENTS_SORT,
    ):
        if frontmatter_format not in ("legacy", "mdx"):
            raise ValueError("frontmatter_format must be 'legacy' or 'mdx'")
        self.frontmatter_format: str = frontmatter_format
        self.is_single_post: bool = is_post_url(base_substack_url)
        self.post_slug: Optional[str] = get_post_slug(base_substack_url) if self.is_single_post else None
        original_url = base_substack_url

        if self.is_single_post:
            base_substack_url = get_publication_url(base_substack_url)

        if not base_substack_url.endswith("/"):
            base_substack_url += "/"
        self.base_substack_url: str = base_substack_url

        self.writer_name: str = extract_main_part(base_substack_url)
        md_save_dir: str = f"{md_save_dir}/{self.writer_name}"

        self.md_save_dir: str = md_save_dir
        self.html_save_dir: str = f"{html_save_dir}/{self.writer_name}"

        if not os.path.exists(md_save_dir):
            os.makedirs(md_save_dir)
            print(f"Created md directory {md_save_dir}")
        if not os.path.exists(self.html_save_dir):
            os.makedirs(self.html_save_dir)
            print(f"Created html directory {self.html_save_dir}")

        self.download_images: bool = download_images
        self.image_dir = Path(BASE_IMAGE_DIR) / self.writer_name

        self.fetch_comments: bool = fetch_comments_flag
        self.comments_sort: str = comments_sort
        self.comments_save_dir: str = os.path.join(COMMENTS_DATA_DIR, self.writer_name)
        if self.fetch_comments and not os.path.exists(self.comments_save_dir):
            os.makedirs(self.comments_save_dir)
            print(f"Created comments directory {self.comments_save_dir}")

        if self.is_single_post:
            self.post_urls: List[str] = [original_url]
        else:
            self.keywords: List[str] = ["about", "archive", "podcast"]
            self.post_urls: List[str] = self.get_all_post_urls()

    def get_all_post_urls(self) -> List[str]:
        """Attempts to fetch URLs from sitemap.xml, falling back to feed.xml if necessary."""
        urls = self.fetch_urls_from_sitemap()
        if not urls:
            urls = self.fetch_urls_from_feed()
        return self.filter_urls(urls, self.keywords)

    def fetch_urls_from_sitemap(self) -> List[str]:
        """Fetches URLs from sitemap.xml."""
        sitemap_url = f"{self.base_substack_url}sitemap.xml"
        response = requests.get(sitemap_url)

        if not response.ok:
            print(f'Error fetching sitemap at {sitemap_url}: {response.status_code}')
            return []

        root = ET.fromstring(response.content)
        urls = [element.text for element in root.iter('{http://www.sitemaps.org/schemas/sitemap/0.9}loc')]
        return urls

    def fetch_urls_from_feed(self) -> List[str]:
        """Fetches URLs from feed.xml."""
        print('Falling back to feed.xml. This will only contain up to the 22 most recent posts.')
        feed_url = f"{self.base_substack_url}feed.xml"
        response = requests.get(feed_url)

        if not response.ok:
            print(f'Error fetching feed at {feed_url}: {response.status_code}')
            return []

        root = ET.fromstring(response.content)
        urls = []
        for item in root.findall('.//item'):
            link = item.find('link')
            if link is not None and link.text:
                urls.append(link.text)

        return urls

    @staticmethod
    def filter_urls(urls: List[str], keywords: List[str]) -> List[str]:
        """Filters out URLs that contain certain keywords."""
        return [url for url in urls if all(keyword not in url for keyword in keywords)]

    @staticmethod
    def html_to_md(html_content: str) -> str:
        """Converts HTML to Markdown."""
        if not isinstance(html_content, str):
            raise ValueError("html_content must be a string")
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.body_width = 0
        return h.handle(html_content)

    @staticmethod
    def save_to_file(filepath: str, content: str) -> None:
        """Saves content to a file."""
        if not isinstance(filepath, str):
            raise ValueError("filepath must be a string")
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        if os.path.exists(filepath):
            print(f"File already exists: {filepath}")
            return
        with open(filepath, 'w', encoding='utf-8') as file:
            file.write(content)

    @staticmethod
    def md_to_html(md_content: str) -> str:
        """Converts Markdown to HTML."""
        return md_to_html_static(md_content)

    def save_to_html_file(
        self,
        filepath: str,
        content: str,
        comments_html: str = "",
        header_html: str = "",
        title: Optional[str] = None,
    ) -> None:
        """Saves HTML content to a file with a link to the external CSS file.

        Renders the classic Substack article shell: Spectral webfont, the essay
        stylesheet, and the body inside ``<main class="markdown-content">``.

        - ``header_html`` (optional): a structured post header block (see
          ``build_post_header``) rendered above the body. When omitted the body is
          rendered as-is (legacy flat-markdown behaviour).
        - ``title`` (optional): used for ``<title>``/document title.
        - ``comments_html`` (optional): appended inside ``<main>`` after the body
          (renders the fetched comment thread on the individual post page).
        """
        if not isinstance(filepath, str):
            raise ValueError("filepath must be a string")
        if not isinstance(content, str):
            raise ValueError("content must be a string")

        html_dir = os.path.dirname(filepath)
        html_content = build_post_document(
            html_dir,
            content,
            comments_html=comments_html,
            header_html=header_html,
            title=title,
        )

        with open(filepath, 'w', encoding='utf-8') as file:
            file.write(html_content)

    @staticmethod
    def get_filename_from_url(url: str, filetype: str = ".md") -> str:
        """Gets the filename from the URL."""
        if not isinstance(url, str):
            raise ValueError("url must be a string")
        if not isinstance(filetype, str):
            raise ValueError("filetype must be a string")
        if not filetype.startswith("."):
            filetype = f".{filetype}"
        return url.split("/")[-1] + filetype

    @staticmethod
    def combine_metadata_and_content(
        title: str,
        subtitle: str,
        date: str,
        author: str,
        cover_image: str,
        like_count: str,
        content: str,
        frontmatter_format: str = "legacy",
    ) -> str:
        """Combines metadata and content using the selected frontmatter format.

        Args:
            date: ISO date string (``YYYY-MM-DD``) or the literal ``"Date not found"``.
            frontmatter_format: ``"mdx"`` for YAML frontmatter, ``"legacy"`` for the
                original ``# title`` / ``**date**`` / ``**Likes:** N`` header.
        """
        if not isinstance(title, str):
            raise ValueError("title must be a string")
        if not isinstance(content, str):
            raise ValueError("content must be a string")

        if frontmatter_format == "mdx":
            safe_title = title.replace('"', '\\"')
            safe_subtitle = subtitle.replace('"', '\\"') if subtitle else ""
            safe_author = author.replace('"', '\\"') if author else ""

            frontmatter = '---\n'
            frontmatter += f'title: "{safe_title}"\n'
            if safe_subtitle:
                frontmatter += f'subtitle: "{safe_subtitle}"\n'
            frontmatter += f'date: "{date}"\n'
            frontmatter += f'author: "{safe_author}"\n'
            if cover_image:
                frontmatter += f'image: "{cover_image}"\n'
            frontmatter += '---\n\n'
            return frontmatter + content

        # legacy format
        display_date = date
        if date and date != "Date not found":
            try:
                display_date = datetime.fromisoformat(date).strftime("%b %d, %Y")
            except ValueError:
                pass

        metadata = f"# {title}\n\n"
        if subtitle:
            metadata += f"## {subtitle}\n\n"
        metadata += f"**{display_date}**\n\n"
        metadata += f"**Likes:** {like_count}\n\n"
        return metadata + content

    def extract_post_data(self, soup: BeautifulSoup, url: str = "") -> Tuple[str, str, str, str, str, str, str, str, str]:
        """Converts a Substack post soup to markdown.

        Returns:
            ``(title, subtitle, author, date, cover_image, like_count, comment_count,
            md_content, body_md)``. ``md_content`` is the body merged with the selected
            frontmatter header (what gets saved to disk); ``body_md`` is the post body
            alone, used by the structured HTML renderer.
        """
        # Title
        title_element = soup.select_one("h1.post-title, h2")
        title = title_element.text.strip() if title_element else "Untitled"
        title_found = title_element is not None

        # Subtitle
        subtitle_element = soup.select_one("h3.subtitle, div.subtitle-HEEcLo")
        subtitle = subtitle_element.text.strip() if subtitle_element else ""

        # Date, Author, and Cover Image from ld+json (most reliable source)
        date = ""
        author = ""
        cover_image = ""
        comment_count = "0"
        script_tag = soup.find("script", {"type": "application/ld+json"})
        if script_tag and script_tag.string:
            try:
                ld_json = json.loads(script_tag.string)
                if "datePublished" in ld_json:
                    date_str = ld_json["datePublished"]
                    date_obj = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                    date = date_obj.strftime("%Y-%m-%d")
                if "author" in ld_json:
                    authors = ld_json["author"]
                    if isinstance(authors, list) and authors:
                        author = authors[0].get("name", "")
                    elif isinstance(authors, dict):
                        author = authors.get("name", "")
                if "image" in ld_json:
                    images = ld_json["image"]
                    if isinstance(images, list) and images:
                        img = images[0]
                        cover_image = img.get("url", "") if isinstance(img, dict) else str(img)
                    elif isinstance(images, dict):
                        cover_image = images.get("url", "")
                # Comment count: prefer the top-level field, then the CommentAction
                # statistic in interactionStatistic (matches the public API value).
                raw_cc = ld_json.get("comment_count")
                if raw_cc is None:
                    for stat in ld_json.get("interactionStatistic") or []:
                        if not isinstance(stat, dict):
                            continue
                        if "CommentAction" in str(stat.get("interactionType", "")):
                            raw_cc = stat.get("userInteractionCount")
                            break
                if raw_cc is not None and str(raw_cc).strip().lstrip("-").isdigit():
                    comment_count = str(int(raw_cc))
            except (json.JSONDecodeError, ValueError, KeyError):
                pass

        if not date:
            date = "Date not found"

        # Like count
        like_count_element = soup.select_one('div.like-button-container button div.label')
        like_count = (
            like_count_element.text.strip()
            if like_count_element and like_count_element.text.strip().isdigit()
            else "0"
        )

        # Content
        content_element = soup.select_one("div.available-content")
        content_html = str(content_element) if content_element else ""
        md = self.html_to_md(content_html)

        # Diagnostic: detect extraction failure (missing title or empty content) and dump page
        if not title_found or not content_element:
            paywall = soup.select_one("h2.paywall-title")
            ld_script = soup.find("script", {"type": "application/ld+json"})
            print(f"[EXTRACT FAIL] url={url}")
            print(f"  title_found={title_found} title={title!r}")
            print(f"  content_element_found={content_element is not None}")
            print(f"  paywall_present={paywall is not None}")
            print(f"  ld_json_present={ld_script is not None}")
            print(f"  date={date!r} author={author!r}")
            try:
                debug_dir = os.path.join(os.path.dirname(self.md_save_dir), "_debug", self.writer_name)
                os.makedirs(debug_dir, exist_ok=True)
                slug = (get_post_slug(url) if url and is_post_url(url) else (url.rstrip('/').split('/')[-1] or "unknown"))
                debug_path = os.path.join(debug_dir, f"{slug}.html")
                with open(debug_path, "w", encoding="utf-8") as f:
                    f.write(str(soup))
                print(f"  dumped raw HTML -> {debug_path}")
            except Exception as dump_err:
                print(f"  failed to dump debug HTML: {dump_err}")

        md_content = self.combine_metadata_and_content(
            title, subtitle, date, author, cover_image, like_count, md, self.frontmatter_format
        )

        return title, subtitle, author, date, cover_image, like_count, comment_count, md_content, md

    @abstractmethod
    def get_url_soup(self, url: str) -> str:
        raise NotImplementedError

    def save_essays_data_to_json(self, essays_data: list) -> None:
        """Saves essays data to a JSON file for a specific author."""
        data_dir = os.path.join(JSON_DATA_DIR)
        if not os.path.exists(data_dir):
            os.makedirs(data_dir)

        json_path = os.path.join(data_dir, f'{self.writer_name}.json')
        if os.path.exists(json_path):
            with open(json_path, 'r', encoding='utf-8') as file:
                existing_data = json.load(file)
            essays_data = existing_data + [data for data in essays_data if data not in existing_data]
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(essays_data, f, ensure_ascii=False, indent=4)

    def _get_session(self) -> Optional[requests.Session]:
        """Return a requests session for JSON API calls (used for comments).

        Default is ``None`` (unauthenticated, free scraper). Premium scrapers override this
        to build a session seeded with the logged-in browser's cookies so paid-only comment
        threads can be fetched.
        """
        return None

    def scrape_comments_for_post(self, url: str) -> Optional[dict]:
        """Fetch (and cache) a post's comment thread, returning the comment list.

        Behavior:
        - If ``{slug}.comments.json`` already exists on disk, load and return it (cache hit,
          **no network calls**) — this makes ``--comments`` cheap to re-run on already-scraped
          publications.
        - Otherwise resolve the post id via the public posts API, fetch the nested thread,
          and persist it to ``{slug}.comments.json`` (raw payload, machine fidelity).
        - Returns ``None`` when there are no comments, when a paid-only thread can't be read
          without ``--premium`` (logged once), or on fetch failure.
        - Returns ``{"total_comments": int, "comments": list, "json_path": str}`` on success.

        The rendered comments live in the individual post HTML page (see ``scrape_posts``);
        no separate ``.comments.md`` file is written.
        """
        slug = get_post_slug(url) if is_post_url(url) else (url.rstrip('/').split('/')[-1] or "unknown_post")

        json_path = os.path.join(self.comments_save_dir, f"{slug}.comments.json")

        # Cache hit: load from disk without hitting the network.
        if os.path.exists(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                if isinstance(cached, list):
                    return {
                        "total_comments": count_all_comments(cached),
                        "comments": cached,
                        "json_path": json_path,
                    }
            except (json.JSONDecodeError, OSError) as e:
                print(f"[WARN] Corrupt comments cache {json_path}: {e}. Refetching.")

        session = self._get_session()

        meta = get_post_id_from_slug(self.base_substack_url, slug, session=session)
        if meta is None:
            print(f"[SKIP] Could not resolve post metadata for comments: {url}")
            return None
        post_id, comment_count, permissions = meta

        if comment_count == 0:
            return None

        # Small delay between the post-lookup and the comments call to avoid 429s.
        sleep(random.uniform(1.0, 2.0))

        comments = fetch_comments(self.base_substack_url, post_id, sort=self.comments_sort, session=session)

        if not comments:
            # comment_count > 0 but empty payload → likely a paid-only thread without auth.
            if permissions and "only_paid" in permissions:
                print(
                    f"[SKIP] {comment_count} comments on {url} are paid-only "
                    f"— rerun with --premium to fetch them."
                )
            return None

        os.makedirs(self.comments_save_dir, exist_ok=True)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(comments, f, ensure_ascii=False, indent=4)

        return {
            "total_comments": count_all_comments(comments),
            "comments": comments,
            "json_path": json_path,
        }

    def _write_post_html(
        self,
        html_filepath: str,
        md_content: str,
        comments_list: Optional[list] = None,
        meta: Optional[dict] = None,
    ) -> None:
        """Convert markdown to HTML and write the post page, optionally baking in comments.

        Rendering modes:

        - **Structured** (``meta`` given): ``md_content`` is treated as the post *body* only.
          Metadata (title/subtitle/author/date/cover) is rendered into a Substack-style
          header via ``build_post_header`` and placed above the body, so the title/date are
          no longer inlined in the body text. This is the classic Substack look.
        - **Flat** (``meta`` is ``None``): ``md_content`` is the merged title+metadata+body
          markdown and is rendered wholesale (the historical behaviour). Existing callers and
          unit tests that pass the merged markdown rely on this path.

        When ``comments_list`` is non-empty, the thread is rendered (via
        ``render_comments_html``) and injected into the page's ``<main>`` after the article
        body. An empty/None list produces a page without a comments section.
        """
        body_html = self.md_to_html(md_content)
        comments_html = render_comments_html(comments_list) if comments_list else ""
        header_html = build_post_header(meta) if meta else ""
        title = (meta or {}).get("title") if meta else None
        self.save_to_html_file(
            html_filepath,
            body_html,
            comments_html=comments_html,
            header_html=header_html,
            title=title,
        )

    def scrape_posts(self, num_posts_to_scrape: int = 0) -> None:
        """Iterates over all posts and saves them as markdown and html files."""
        essays_data = []
        count = 0
        total = num_posts_to_scrape if num_posts_to_scrape != 0 else len(self.post_urls)
        with tqdm(total=total, desc="Scraping posts") as pbar:
            for url in self.post_urls:
                try:
                    md_filename = self.get_filename_from_url(url, filetype=".md")
                    html_filename = self.get_filename_from_url(url, filetype=".html")
                    md_filepath = os.path.join(self.md_save_dir, md_filename)
                    html_filepath = os.path.join(self.html_save_dir, html_filename)

                    if not os.path.exists(md_filepath):
                        soup = self.get_url_soup(url)
                        if soup is None:
                            # Body is paywalled/unavailable. Still attempt comments so the
                            # free scraper can report paid-only threads (the post metadata
                            # API is public even when the body is not).
                            if self.fetch_comments:
                                try:
                                    self.scrape_comments_for_post(url)
                                except Exception as ce:
                                    pbar.write(f"[WARN] Comments failed for {url}: {ce}")
                            total += 1
                            pbar.total = total
                            pbar.refresh()
                            continue

                        title, subtitle, author, date, cover_image, like_count, comment_count, md, body_md = self.extract_post_data(soup, url)

                        # Skip writing if extraction clearly failed — leaves no stale file so reruns retry.
                        content_element = soup.select_one("div.available-content")
                        if title == "Untitled" or content_element is None:
                            pbar.write(f"[SKIP] Extraction failed for {url} (title={title!r}, content_present={content_element is not None}). See _debug dump.")
                            count += 1
                            pbar.update(1)
                            if num_posts_to_scrape != 0 and count == num_posts_to_scrape:
                                break
                            continue

                        if self.download_images:
                            total_images = count_images_in_markdown(md)
                            slug = get_post_slug(url) if is_post_url(url) else url.rstrip('/').split('/')[-1]
                            with tqdm(
                                total=total_images,
                                desc=f"Downloading images for {slug}",
                                leave=False,
                            ) as img_pbar:
                                md = process_markdown_images(md, self.writer_name, slug, img_pbar)
                                # Re-apply to the raw body so the rendered HTML body uses the
                                # same local image paths. Downloads are skipped (files exist).
                                body_md = process_markdown_images(body_md, self.writer_name, slug)

                        self.save_to_file(md_filepath, md)

                        # Fetch comments BEFORE rendering the HTML so they can be baked into
                        # the individual post page. The .md source stays clean.
                        comments_result = None
                        if self.fetch_comments:
                            try:
                                comments_result = self.scrape_comments_for_post(url)
                            except Exception as ce:
                                pbar.write(f"[WARN] Comments failed for {url}: {ce}")
                        comments_list = comments_result["comments"] if comments_result else []

                        # Structured render: metadata becomes a Substack-style header, the
                        # body is rendered separately (no inlined # title / **date** block).
                        post_meta = {
                            "title": title,
                            "subtitle": subtitle,
                            "author": author,
                            "date": date,
                            "cover_image": cover_image,
                        }
                        self._write_post_html(html_filepath, body_md, comments_list, meta=post_meta)

                        essay_entry = {
                            "title": title,
                            "subtitle": subtitle,
                            "author": author,
                            "date": date,
                            "cover_image": cover_image,
                            "like_count": like_count,
                            # Top-level comment count from the page's ld+json; always
                            # available (no extra request). When --comments scrapes the
                            # full thread below, total_comments overrides this with the
                            # recursive count (includes nested replies).
                            "comment_count": comment_count,
                            "file_link": md_filepath,
                            "html_link": html_filepath
                        }
                        if comments_result:
                            essay_entry["comment_count"] = comments_result.get("total_comments")
                            essay_entry["total_comments"] = comments_result.get("total_comments")
                            essay_entry["comments_json_link"] = comments_result.get("json_path")
                        essays_data.append(essay_entry)

                        # Periodic driver restart to shed accumulated state/leaks before they
                        # destabilize the renderer. Only applies to scrapers with a driver.
                        if hasattr(self, "_recreate_driver"):
                            self._scrape_counter += 1
                            if self._scrape_counter % 40 == 0:
                                pbar.write("[MAINT] Periodic driver restart to shed state...")
                                self._recreate_driver()
                                sleep(random.uniform(4, 8))
                    else:
                        pbar.write(f"File already exists: {md_filepath}")
                        # Fetch comments independently of the post body so --comments can be
                        # added to an already-scraped publication without re-scraping posts.
                        # Re-render the HTML (from the on-disk md) with comments baked in.
                        if self.fetch_comments:
                            try:
                                comments_result = self.scrape_comments_for_post(url)
                                comments_list = comments_result["comments"] if comments_result else []
                                with open(md_filepath, "r", encoding="utf-8") as f:
                                    md_text = f.read()
                                on_disk_meta, on_disk_body = split_metadata_and_body(
                                    md_text, self.frontmatter_format
                                )
                                self._write_post_html(
                                    html_filepath, on_disk_body, comments_list, meta=on_disk_meta
                                )
                            except Exception as ce:
                                pbar.write(f"[WARN] Comments failed for {url}: {ce}")
                except Exception as e:
                    pbar.write(f"Error scraping post: {e}")

                count += 1
                pbar.update(1)
                if num_posts_to_scrape != 0 and count == num_posts_to_scrape:
                    break
        self.save_essays_data_to_json(essays_data=essays_data)
        generate_html_file(author_name=self.writer_name)


# =============================================================================
# FREE CONTENT SCRAPER
# =============================================================================

class SubstackScraper(BaseSubstackScraper):
    def __init__(
        self,
        base_substack_url: str,
        md_save_dir: str,
        html_save_dir: str,
        download_images: bool = False,
        frontmatter_format: str = "legacy",
        fetch_comments_flag: bool = False,
        comments_sort: str = COMMENTS_SORT,
    ):
        super().__init__(
            base_substack_url,
            md_save_dir,
            html_save_dir,
            download_images,
            frontmatter_format,
            fetch_comments_flag=fetch_comments_flag,
            comments_sort=comments_sort,
        )

    def get_url_soup(self, url: str, max_attempts: int = 5) -> Optional[BeautifulSoup]:
        """Gets soup from URL using requests, with retry on rate limiting."""
        for attempt in range(1, max_attempts + 1):
            try:
                page = requests.get(url, headers=None)
                soup = BeautifulSoup(page.content, "html.parser")

                if soup.find("h2", class_="paywall-title"):
                    print(f"Skipping premium article: {url}")
                    return None

                pre = soup.select_one("body > pre")
                if pre and "too many requests" in pre.text.lower():
                    if attempt == max_attempts:
                        raise RuntimeError(f"Max attempts reached for URL: {url}. Too many requests.")
                    base = 2 ** attempt
                    delay = base + random.uniform(-0.2 * base, 0.2 * base)
                    print(f"[{attempt}/{max_attempts}] Too many requests. Retrying in {delay:.2f} seconds...")
                    sleep(delay)
                    continue

                return soup
            except RuntimeError:
                raise
            except Exception as e:
                raise ValueError(f"Error fetching page: {e}") from e

        raise RuntimeError(f"Failed to fetch page after {max_attempts} attempts: {url}")


# =============================================================================
# PREMIUM CONTENT SCRAPER
# =============================================================================

class PremiumSubstackScraper(BaseSubstackScraper):
    def __init__(
        self,
        base_substack_url: str,
        md_save_dir: str,
        html_save_dir: str,
        download_images: bool = False,
        browser: str = 'chrome',
        headless: bool = False,
        driver_path: str = '',
        browser_path: str = '',
        user_agent: str = '',
        use_persistent_profile: bool = False,
        skip_login: bool = False,
        frontmatter_format: str = "legacy",
        fetch_comments_flag: bool = False,
        comments_sort: str = COMMENTS_SORT,
    ) -> None:
        """
        Initialize the premium scraper with browser automation.
        
        Args:
            base_substack_url: The Substack URL to scrape
            md_save_dir: Directory for markdown files
            html_save_dir: Directory for HTML files
            browser: 'chrome' or 'edge' (chrome recommended)
            headless: Run browser in headless mode
            driver_path: Explicit path to WebDriver executable
            browser_path: Explicit path to browser executable
            user_agent: Custom user agent string
            use_persistent_profile: Reuse browser profile across runs (saves login)
            skip_login: Skip login if using a pre-authenticated profile
        """
        # Store settings so the driver can be recreated with identical options after a crash.
        self._browser = browser
        self._headless = headless
        self._driver_path = driver_path
        self._browser_path = browser_path
        self._user_agent = user_agent
        self._base_substack_url = base_substack_url
        self._scrape_counter = 0

        # Initialize driver before calling super().__init__ since that fetches URLs
        self.driver = BrowserManager.create_driver(
            browser=browser,
            headless=headless,
            driver_path=driver_path,
            browser_path=browser_path,
            user_agent=user_agent,
            use_persistent_profile=use_persistent_profile,
        )
        
        self.skip_login = skip_login
        self.use_persistent_profile = use_persistent_profile
        
        if not skip_login:
            self.login()
        else:
            print("Skipping login (using existing profile authentication)")
            # Navigate to substack to verify we're logged in
            self.driver.get(base_substack_url)
            sleep(3)

        super().__init__(
            base_substack_url,
            md_save_dir,
            html_save_dir,
            download_images,
            frontmatter_format,
            fetch_comments_flag=fetch_comments_flag,
            comments_sort=comments_sort,
        )

    def login(self) -> None:
        """Log into Substack using Selenium."""
        print("Logging into Substack...")
        self.driver.get("https://substack.com/sign-in")
        sleep(3)

        signin_with_password = self.driver.find_element(
            By.XPATH, "//a[@class='login-option substack-login__login-option']"
        )
        signin_with_password.click()
        sleep(3)

        email = self.driver.find_element(By.NAME, "email")
        password = self.driver.find_element(By.NAME, "password")
        email.send_keys(EMAIL)
        password.send_keys(PASSWORD)

        submit = self.driver.find_element(By.XPATH, "//*[@id=\"substack-login\"]/div[2]/div[2]/form/button")
        submit.click()
        
        print("Waiting for login to complete (this may take up to 30 seconds)...")
        sleep(30)

        if self.is_login_failed():
            raise Exception(
                "Login unsuccessful. Please check your email and password, or your account status.\n"
                "If you're seeing a CAPTCHA, try:\n"
                "  1. Run without --headless to complete CAPTCHA manually\n"
                "  2. Use --persistent-profile to save your session\n"
                "  3. Then run with --skip-login on subsequent runs"
            )
        
        print("[OK] Login successful!")
        
        if self.use_persistent_profile:
            print("[OK] Session saved to persistent profile")

    def is_login_failed(self) -> bool:
        """Check for the presence of the 'error-container' to indicate a failed login."""
        error_container = self.driver.find_elements(By.ID, 'error-container')
        return len(error_container) > 0 and error_container[0].is_displayed()

    def _get_session(self) -> Optional[requests.Session]:
        """Build a requests.Session seeded with the logged-in browser's cookies.

        Lets the JSON comment endpoints authenticate as the current user, so paid-only
        comment threads can be fetched. Cookies are read fresh each call so they stay valid
        after a ``_recreate_driver()`` (persistent profile carries the session).
        """
        session = requests.Session()
        try:
            cookies = self.driver.get_cookies()
        except Exception as e:
            print(f"[WARN] Could not read browser cookies for comments: {e}")
            return session
        for c in cookies:
            try:
                session.cookies.set(
                    c.get("name", ""),
                    c.get("value", ""),
                    domain=c.get("domain"),
                    path=c.get("path", "/"),
                )
            except Exception:
                continue
        try:
            session.headers.update({
                "User-Agent": self.driver.execute_script("return navigator.userAgent;"),
            })
        except Exception:
            pass
        return session

    def _driver_is_dead(self) -> bool:
        """Cheap liveness probe. Returns True if the driver/session is unusable."""
        try:
            _ = self.driver.current_url
            return False
        except (WebDriverException, InvalidSessionIdException):
            return True

    def _recreate_driver(self) -> None:
        """Tear down the (possibly dead) driver and build a fresh one with the same settings.

        With a persistent profile, saved cookies mean NO re-login is required after recreation.
        """
        try:
            self.driver.quit()
        except Exception:
            pass
        self.driver = BrowserManager.create_driver(
            browser=self._browser,
            headless=self._headless,
            driver_path=self._driver_path,
            browser_path=self._browser_path,
            user_agent=self._user_agent,
            use_persistent_profile=self.use_persistent_profile,
            quiet=True,
        )
        if not self.use_persistent_profile:
            self.login()
        else:
            try:
                self.driver.get(self._base_substack_url)
                sleep(3)
            except Exception:
                pass

    def get_url_soup(self, url: str, max_attempts: int = 5) -> Optional[BeautifulSoup]:
        """Gets soup from URL using logged-in Selenium driver, with retry on rate limiting."""
        for attempt in range(1, max_attempts + 1):
            try:
                sleep(random.uniform(4.0, 9.0)) 
                self.driver.get(url)

                # Wait up to 20s for the post body (or a paywall marker) to appear, instead of a fixed sleep.
                try:
                    WebDriverWait(self.driver, 20).until(
                        lambda d: d.find_elements(By.CSS_SELECTOR, "div.available-content")
                        or d.find_elements(By.CSS_SELECTOR, "h1.post-title")
                        or d.find_elements(By.CSS_SELECTOR, "h2.paywall-title")
                        or d.find_elements(By.CSS_SELECTOR, "body > pre")
                    )
                except TimeoutException:
                    print(f"[WARN] Timeout waiting for post content to render: {url}")

                soup = BeautifulSoup(self.driver.page_source, "html.parser")

                pre = soup.select_one("body > pre")
                if pre and "too many requests" in pre.text.lower():
                    if attempt == max_attempts:
                        raise RuntimeError(f"Max attempts reached for URL: {url}. Too many requests.")
                    base = 2 ** attempt
                    delay = base + random.uniform(-0.2 * base, 0.2 * base)
                    print(f"[{attempt}/{max_attempts}] Too many requests. Retrying in {delay:.2f} seconds...")
                    sleep(delay)
                    continue

                if soup.find("h2", class_="paywall-title"):
                    print(f"Skipping premium article (no access): {url}")
                    return None

                return soup
            except RuntimeError:
                raise
            except Exception as e:
                msg = str(e).lower()
                crashed = (
                    isinstance(e, InvalidSessionIdException)
                    or any(s in msg for s in (
                        "tab crashed",
                        "chrome not reachable",
                        "no such session",
                        "session not created",
                        "unable to connect to renderer",
                        "target window already closed",
                    ))
                )
                if crashed:
                    print(f"[{attempt}/{max_attempts}] Tab/session crashed — recreating driver: {e}")
                    self._recreate_driver()
                    sleep(random.uniform(5, 10))  # cool-down after recovery
                    continue  # retry the SAME url on a fresh driver
                raise ValueError(f"Error fetching page: {url}. Error: {e}") from e

        raise RuntimeError(f"Failed to fetch page after {max_attempts} attempts: {url}")
    
    def __del__(self):
        """Clean up the driver when done."""
        if hasattr(self, 'driver') and self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass


# =============================================================================
# CLI
# =============================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape a Substack site and convert posts to Markdown/HTML.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Scrape free posts
  python substack_scraper.py --url https://example.substack.com
  
  # Scrape premium posts with Chrome (recommended)
  python substack_scraper.py --url https://example.substack.com --premium --browser chrome
  
  # First run with persistent profile (complete login/CAPTCHA manually)
  python substack_scraper.py --url https://example.substack.com --premium --persistent-profile
  
  # Subsequent runs (skip login, use saved session)
  python substack_scraper.py --url https://example.substack.com --premium --persistent-profile --skip-login
  
  # Fetch comments too (public threads; add --premium for paid-only comments)
  python substack_scraper.py --url https://example.substack.com --comments
  
  # Use manually downloaded driver
  python substack_scraper.py --url https://example.substack.com --premium --chrome-driver-path /path/to/chromedriver
        """
    )
    
    parser.add_argument(
        "-u", "--url", type=str,
        help="The base URL of the Substack site to scrape."
    )
    parser.add_argument(
        "--render-only", action="store_true",
        help="Skip scraping. Re-render existing on-disk Markdown into the Substack-styled "
             "HTML (no network). Give authors as positional args or use --all. Equivalent to "
             "running render_posts.py."
    )
    parser.add_argument(
        "--render-all", action="store_true",
        help="With --render-only, re-render every author under data/."
    )
    parser.add_argument(
        "-d", "--directory", type=str,
        help="The directory to save scraped markdown posts."
    )
    parser.add_argument(
        "--html-directory", type=str,
        help="The directory to save scraped HTML posts."
    )
    parser.add_argument(
        "-n", "--number", type=int, default=0,
        help="Number of posts to scrape (0 = all posts)."
    )
    parser.add_argument(
        "--images",
        action="store_true",
        help="Download images and update markdown to use local paths."
    )
    parser.add_argument(
        "--comments",
        action="store_true",
        help="Fetch each post's comment thread as separate .comments.md/.comments.json files. "
             "Public threads need no auth; paid-only threads require --premium."
    )
    parser.add_argument(
        "--comments-sort", type=str, default=COMMENTS_SORT,
        choices=["best", "most_recent_first"],
        help="Comment sort order (default: best)."
    )
    parser.add_argument(
        "--frontmatter", type=str, default="legacy", choices=["legacy", "mdx"],
        help="Header format for scraped markdown. 'legacy' (default) uses the original "
             "'# title / **date** / **Likes:** N' block. 'mdx' emits YAML frontmatter "
             "(title, subtitle, date, author, image) suitable for MDX sites."
    )
    
    # Premium scraping options
    premium_group = parser.add_argument_group('Premium scraping options')
    premium_group.add_argument(
        "-p", "--premium", action="store_true",
        help="Use browser automation to access premium/paid content."
    )
    premium_group.add_argument(
        "--browser", type=str, default="chrome", choices=['chrome', 'edge'],
        help="Browser to use for premium scraping (default: chrome)."
    )
    premium_group.add_argument(
        "--headless", action="store_true",
        help="Run browser in headless mode (may trigger CAPTCHA)."
    )
    premium_group.add_argument(
        "--persistent-profile", action="store_true",
        help="Use a persistent browser profile to save login state."
    )
    premium_group.add_argument(
        "--skip-login", action="store_true",
        help="Skip login (use with --persistent-profile after first login)."
    )
    
    # Driver path options
    driver_group = parser.add_argument_group('Driver options (for troubleshooting)')
    driver_group.add_argument(
        "--chrome-driver-path", type=str, default="",
        help="Path to chromedriver executable."
    )
    driver_group.add_argument(
        "--edge-driver-path", type=str, default="",
        help="Path to msedgedriver executable."
    )
    driver_group.add_argument(
        "--chrome-path", type=str, default="",
        help="Path to Chrome browser executable."
    )
    driver_group.add_argument(
        "--edge-path", type=str, default="",
        help="Path to Edge browser executable."
    )
    driver_group.add_argument(
        "--user-agent", type=str, default="",
        help="Custom user agent string."
    )

    parser.add_argument(
        "authors", nargs="*", default=[],
        help="Author name(s) for --render-only (= data/<author>.json stem).",
    )

    return parser.parse_args()


def _run_render_only(args: argparse.Namespace) -> None:
    """Delegate the --render-only path to the standalone renderer (network-free)."""
    import render_posts

    if args.render_all:
        authors = render_posts.discover_authors()
        if not authors:
            print("[SKIP] No authors found under data/.")
            return
        for author in authors:
            render_posts.render_author(author, force=True)
    elif args.authors:
        for author in args.authors:
            render_posts.render_author(author, force=True)
    else:
        print("Provide one or more authors, or use --render-only --render-all.")


def main():
    args = parse_args()

    if args.render_only:
        _run_render_only(args)
        return

    if args.directory is None:
        args.directory = BASE_MD_DIR

    if args.html_directory is None:
        args.html_directory = BASE_HTML_DIR

    # Determine driver/browser paths based on selected browser
    if args.browser == 'chrome':
        driver_path = args.chrome_driver_path
        browser_path = args.chrome_path
    else:
        driver_path = args.edge_driver_path
        browser_path = args.edge_path

    if args.url:
        if args.premium:
            scraper = PremiumSubstackScraper(
                base_substack_url=args.url,
                md_save_dir=args.directory,
                html_save_dir=args.html_directory,
                download_images=args.images,
                browser=args.browser,
                headless=args.headless,
                driver_path=driver_path,
                browser_path=browser_path,
                user_agent=args.user_agent,
                use_persistent_profile=args.persistent_profile,
                skip_login=args.skip_login,
                frontmatter_format=args.frontmatter,
                fetch_comments_flag=args.comments,
                comments_sort=args.comments_sort,
            )
        else:
            scraper = SubstackScraper(
                args.url,
                md_save_dir=args.directory,
                html_save_dir=args.html_directory,
                download_images=args.images,
                frontmatter_format=args.frontmatter,
                fetch_comments_flag=args.comments,
                comments_sort=args.comments_sort,
            )
        scraper.scrape_posts(args.number)

    else:
        # Use hardcoded values
        if USE_PREMIUM:
            scraper = PremiumSubstackScraper(
                base_substack_url=BASE_SUBSTACK_URL,
                md_save_dir=args.directory,
                html_save_dir=args.html_directory,
                download_images=args.images,
                browser=args.browser,
                headless=args.headless,
                driver_path=driver_path,
                browser_path=browser_path,
                user_agent=args.user_agent,
                use_persistent_profile=args.persistent_profile,
                skip_login=args.skip_login,
                frontmatter_format=args.frontmatter,
                fetch_comments_flag=args.comments,
                comments_sort=args.comments_sort,
            )
        else:
            scraper = SubstackScraper(
                base_substack_url=BASE_SUBSTACK_URL,
                md_save_dir=args.directory,
                html_save_dir=args.html_directory,
                download_images=args.images,
                frontmatter_format=args.frontmatter,
                fetch_comments_flag=args.comments,
                comments_sort=args.comments_sort,
            )
        scraper.scrape_posts(num_posts_to_scrape=NUM_POSTS_TO_SCRAPE)


if __name__ == "__main__":
    main()