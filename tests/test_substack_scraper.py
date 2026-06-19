import os
import sys
import json
import shutil

import pytest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import substack_scraper as ss


class DummyScraper(ss.BaseSubstackScraper):
    def get_url_soup(self, url: str):
        return None


# ---------------------------------------------------------------------------
# Existing tests (preserved)
# ---------------------------------------------------------------------------


def test_resolve_image_url_extracts_original_url():
    cdn_url = (
        "https://substackcdn.com/image/fetch/w_1456,c_limit,f_webp,q_auto:good,"
        "fl_progressive:steep/https%3A%2F%2Fbucket.s3.us-west-2.amazonaws.com%2Fimage.jpg"
    )

    assert ss.resolve_image_url(cdn_url) == "https://bucket.s3.us-west-2.amazonaws.com/image.jpg"


def test_sanitize_image_filename_uses_resolved_url_name():
    cdn_url = (
        "https://substackcdn.com/image/fetch/w_1456,c_limit,f_webp,q_auto:good,"
        "fl_progressive:steep/https%3A%2F%2Fbucket.s3.us-west-2.amazonaws.com%2Fimage.jpg%3Fv%3D1"
    )

    assert ss.sanitize_image_filename(cdn_url) == "image.jpg"


def test_count_images_in_markdown_counts_cleaned_linked_images():
    markdown = "[![alt](https://cdn/a.png)](https://example.com)\n\n![plain](https://cdn/b.png)"

    assert ss.count_images_in_markdown(markdown) == 2


def test_single_post_url_initializes_without_fetching_all_posts(tmp_path):
    scraper = DummyScraper(
        "https://example.substack.com/p/my-post",
        str(tmp_path / "md"),
        str(tmp_path / "html"),
        download_images=True,
    )

    assert scraper.is_single_post is True
    assert scraper.post_slug == "my-post"
    assert scraper.base_substack_url == "https://example.substack.com/"
    assert scraper.post_urls == ["https://example.substack.com/p/my-post"]
    assert scraper.download_images is True


def test_parse_args_supports_images_flag(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        ["substack_scraper.py", "--url", "https://example.substack.com/p/post", "--images"],
    )

    args = ss.parse_args()

    assert args.url == "https://example.substack.com/p/post"
    assert args.images is True


# ---------------------------------------------------------------------------
# New tests
# ---------------------------------------------------------------------------


# 1. Parametrized test_clean_linked_images
@pytest.mark.parametrize(
    "input_md, expected",
    [
        pytest.param(
            "[![Image 1](/img/test/image1.png)](/img/test/image1.png)",
            "![Image 1](/img/test/image1.png)",
            id="basic_cleaning",
        ),
        pytest.param(
            "Check [this link](https://example.com) and [![photo](img.png)](img.png) and ![plain](other.png)",
            "Check [this link](https://example.com) and ![photo](img.png) and ![plain](other.png)",
            id="mixed_content",
        ),
        pytest.param(
            "[![CDN](https://substackcdn.com/image/fetch/w_1456/https%3A%2F%2Fexample.com%2Fphoto.jpg)](https://substackcdn.com/image/fetch/w_1456/https%3A%2F%2Fexample.com%2Fphoto.jpg)",
            "![CDN](https://substackcdn.com/image/fetch/w_1456/https%3A%2F%2Fexample.com%2Fphoto.jpg)",
            id="substack_cdn_urls",
        ),
        pytest.param(
            "![Already clean](https://example.com/img.png)",
            "![Already clean](https://example.com/img.png)",
            id="no_changes_needed",
        ),
        pytest.param(
            "",
            "",
            id="empty_content",
        ),
        pytest.param(
            "Line one\n\n[![img](a.png)](a.png)\n\nLine three",
            "Line one\n\n![img](a.png)\n\nLine three",
            id="preserve_newlines",
        ),
        pytest.param(
            '[![Image with "quotes" & special](https://example.com/img%20file.png)](https://example.com/img%20file.png)',
            '![Image with "quotes" & special](https://example.com/img%20file.png)',
            id="special_characters",
        ),
    ],
)
def test_clean_linked_images(input_md, expected):
    assert ss.clean_linked_images(input_md) == expected


# 2. test_resolve_image_url_passthrough
def test_resolve_image_url_passthrough():
    """Non-CDN URLs should pass through unchanged."""
    urls = [
        "https://example.com/photo.jpg",
        "https://bucket.s3.amazonaws.com/image.png",
        "https://i.imgur.com/abc123.gif",
        "/relative/path/image.png",
    ]
    for url in urls:
        assert ss.resolve_image_url(url) == url


# 3. test_is_post_url
@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://example.substack.com/p/my-post", True),
        ("https://example.substack.com/p/another-post-slug", True),
        ("https://example.substack.com/", False),
        ("https://example.substack.com/archive", False),
        ("https://example.substack.com/about", False),
    ],
)
def test_is_post_url(url, expected):
    assert ss.is_post_url(url) == expected


# 4. test_get_publication_url
@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://example.substack.com/p/my-post", "https://example.substack.com/"),
        ("https://blog.example.com/p/slug", "https://blog.example.com/"),
        ("http://test.substack.com/p/post-name", "http://test.substack.com/"),
    ],
)
def test_get_publication_url(url, expected):
    assert ss.get_publication_url(url) == expected


# 5. test_get_post_slug
@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://example.substack.com/p/my-post", "my-post"),
        ("https://example.substack.com/p/another-slug", "another-slug"),
        ("https://example.substack.com/p/slug-with-123", "slug-with-123"),
        ("https://example.substack.com/archive", "unknown_post"),
    ],
)
def test_get_post_slug(url, expected):
    assert ss.get_post_slug(url) == expected


# 6. test_process_markdown_images
@patch("substack_scraper.download_image")
def test_process_markdown_images(mock_download):
    """Mock requests.get and verify image download + path rewriting."""
    mock_download.return_value = "substack_images/testauthor/test-post/photo.jpg"

    md_content = (
        "Some text\n"
        "![alt](https://substackcdn.com/image/fetch/w_1456,c_limit/https%3A%2F%2Fexample.com%2Fphoto.jpg)\n"
        "More text"
    )

    result = ss.process_markdown_images(md_content, "testauthor", "test-post")

    # download_image should have been called once
    assert mock_download.call_count == 1

    # The CDN URL should be replaced with a local relative path
    assert "substackcdn.com" not in result
    assert "Some text" in result
    assert "More text" in result


# 7. test_download_image_error_handling
@patch("substack_scraper.requests.get")
def test_download_image_error_handling(mock_get, tmp_path):
    """Mock network error, verify graceful handling (returns None)."""
    mock_get.side_effect = ConnectionError("Network unreachable")

    result = ss.download_image(
        "https://example.com/image.jpg",
        tmp_path / "image.jpg",
    )

    assert result is None


# 8. test_scraper_initialization
def test_scraper_initialization(tmp_path):
    """Verify writer_name and directories are created."""
    md_dir = str(tmp_path / "md")
    html_dir = str(tmp_path / "html")

    scraper = DummyScraper(
        "https://example.substack.com/p/test-post",
        md_dir,
        html_dir,
    )

    assert scraper.writer_name == "example"
    assert os.path.isdir(os.path.join(md_dir, "example"))
    assert os.path.isdir(os.path.join(html_dir, "example"))


# ---------------------------------------------------------------------------
# Comment helpers
# ---------------------------------------------------------------------------


# 9. test_render_comments_markdown_flat
def test_render_comments_markdown_flat():
    comments = [{
        "name": "Alice",
        "body": "Great post!",
        "date": "2026-06-01T22:54:34.749Z",
        "reactions": {"❤": 0},
        "metadata": {"is_author": False},
        "children": [],
    }]

    rendered = ss.render_comments_markdown(comments)

    assert "**Alice**" in rendered
    assert "Great post!" in rendered
    assert "Jun 01, 2026" in rendered
    # No reactions shown when count is 0
    assert "❤" not in rendered
    # No nested blockquotes at top level
    assert ">" not in rendered


# 10. test_render_comments_markdown_nested
def test_render_comments_markdown_nested():
    comments = [{
        "name": "Alice",
        "body": "Parent comment",
        "date": "2026-06-01T22:54:34.749Z",
        "reactions": {},
        "metadata": {"is_author": False},
        "children": [{
            "name": "Bob",
            "body": "Reply one\n\nReply two",
            "date": "2026-06-02T15:36:32.599Z",
            "reactions": {},
            "metadata": {"is_author": False},
            "children": [],
        }],
    }]

    rendered = ss.render_comments_markdown(comments)

    assert "Parent comment" in rendered
    assert "**Bob**" in rendered
    # Child is rendered as a blockquote under the parent
    assert "> **Bob**" in rendered
    # Multi-paragraph child body: every line prefixed with ">"
    assert "> Reply one" in rendered
    assert "> Reply two" in rendered
    # Blank line inside the blockquote is rendered as ">"
    assert "\n>\n>" in rendered


# 11. test_render_comments_markdown_author_flag_and_reactions
def test_render_comments_markdown_author_flag_and_reactions():
    comments = [{
        "name": "Janey Park",
        "body": "Author reply",
        "date": "2026-06-02T15:36:32.599Z",
        "reactions": {"❤": 5},
        "metadata": {"is_author": True},
        "children": [],
    }]

    rendered = ss.render_comments_markdown(comments)

    assert "**Janey Park** (author)" in rendered
    assert "❤ 5" in rendered


# 12. test_count_all_comments_recursive
def test_count_all_comments_recursive():
    comments = [
        {"children": [{"children": [{"children": []}]}]},
        {"children": [{"children": []}]},
        {"children": []},
    ]

    assert ss.count_all_comments(comments) == 6

    assert ss.count_all_comments([]) == 0


# 13. test_get_post_id_from_slug
@patch("substack_scraper._request_json_with_rate_limit_retry")
def test_get_post_id_from_slug(mock_request):
    mock_request.return_value = {
        "id": 201818433,
        "comment_count": 2,
        "write_comment_permissions": "everyone",
    }

    result = ss.get_post_id_from_slug("https://example.substack.com/", "my-post")

    assert result == (201818433, 2, "everyone")
    mock_request.assert_called_once_with(
        "https://example.substack.com/api/v1/posts/my-post", session=None
    )


@patch("substack_scraper._request_json_with_rate_limit_retry")
def test_get_post_id_from_slug_returns_none_on_failure(mock_request):
    mock_request.return_value = None

    assert ss.get_post_id_from_slug("https://example.substack.com/", "missing") is None


# 14. test_fetch_comments_returns_list
@patch("substack_scraper._request_json_with_rate_limit_retry")
def test_fetch_comments_returns_list(mock_request):
    mock_request.return_value = {"comments": [{"id": 1}, {"id": 2}], "automod_hidden_comments": []}

    result = ss.fetch_comments("https://example.substack.com/", 201818433)

    assert result == [{"id": 1}, {"id": 2}]
    mock_request.assert_called_once_with(
        "https://example.substack.com/api/v1/post/201818433/comments?all_comments=true&sort=best",
        session=None,
    )


@patch("substack_scraper._request_json_with_rate_limit_retry")
def test_fetch_comments_empty_payload(mock_request):
    mock_request.return_value = {"comments": [], "automod_hidden_comments": []}

    assert ss.fetch_comments("https://example.substack.com/", 123) == []


# 15. test_parse_args_supports_comments_flag
def test_parse_args_supports_comments_flag(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        ["substack_scraper.py", "--url", "https://example.substack.com/p/post", "--comments"],
    )

    args = ss.parse_args()

    assert args.comments is True
    assert args.comments_sort == "best"


# 16. test_scrape_comments_loads_from_cache
def test_scrape_comments_loads_from_cache(tmp_path):
    """Cached comments JSON is loaded from disk WITHOUT any network calls."""
    scraper = DummyScraper(
        "https://example.substack.com/p/my-post",
        str(tmp_path / "md"),
        str(tmp_path / "html"),
        fetch_comments_flag=True,
    )

    # Pre-create the comments JSON cache with a real comment list.
    os.makedirs(scraper.comments_save_dir, exist_ok=True)
    slug = "my-post"
    json_path = os.path.join(scraper.comments_save_dir, f"{slug}.comments.json")
    cached = [{"id": 1, "name": "Alice", "body": "Hi", "children": []}]
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(cached, f)

    # Cache hit → no network (get_post_id_from_slug must NOT be called).
    with patch("substack_scraper.get_post_id_from_slug") as mock_meta:
        result = scraper.scrape_comments_for_post("https://example.substack.com/p/my-post")

    assert result is not None
    assert result["comments"] == cached
    assert result["total_comments"] == 1
    assert result["json_path"] == json_path
    mock_meta.assert_not_called()


# ---------------------------------------------------------------------------
# Comment HTML rendering (for the individual post page)
# ---------------------------------------------------------------------------


# 17. test_render_comments_html_flat
def test_render_comments_html_flat():
    comments = [{
        "name": "Alice",
        "body": "Great post!",
        "date": "2026-06-01T22:54:34.749Z",
        "reactions": {"❤": 0},
        "metadata": {"is_author": False},
        "children": [],
    }]

    html = ss.render_comments_html(comments)

    assert '<section class="comments">' in html
    assert "<h2>Comments (1)</h2>" in html
    assert 'class="comment-author">Alice' in html
    assert "Great post!" in html
    assert "Jun 01, 2026" in html
    # No avatar when photo_url missing; no reactions when count 0; not an author.
    assert "comment-avatar" not in html
    assert "comment-author-flag" not in html
    assert "comment-reactions" not in html


# 18. test_render_comments_html_nested
def test_render_comments_html_nested():
    comments = [{
        "name": "Alice",
        "body": "Parent",
        "date": "2026-06-01T22:54:34.749Z",
        "reactions": {},
        "metadata": {"is_author": False},
        "children": [{
            "name": "Bob",
            "body": "Reply",
            "date": "2026-06-02T15:36:32.599Z",
            "reactions": {},
            "metadata": {"is_author": False},
            "children": [],
        }],
    }]

    html = ss.render_comments_html(comments)

    assert "Parent" in html
    assert "Bob" in html
    assert "Reply" in html
    # Total includes the nested child.
    assert "<h2>Comments (2)</h2>" in html
    # Child is rendered inside a .comment-children list.
    assert '<ul class="comment-children">' in html


# 19. test_render_comments_html_author_flag_and_reactions
def test_render_comments_html_author_flag_and_reactions():
    comments = [{
        "name": "Janey Park",
        "body": "Author reply",
        "date": "2026-06-02T15:36:32.599Z",
        "photo_url": "https://example.com/avatar.png",
        "reactions": {"❤": 5},
        "metadata": {"is_author": True},
        "children": [],
    }]

    html = ss.render_comments_html(comments)

    assert '<span class="comment-author-flag">Author</span>' in html
    assert '<span class="comment-reactions">❤ 5</span>' in html
    assert '<img class="comment-avatar" src="https://example.com/avatar.png"' in html


# 20. test_render_comments_html_empty_returns_empty
def test_render_comments_html_empty_returns_empty():
    assert ss.render_comments_html([]) == ""


# 21. test_save_to_html_file_injects_comments
def test_save_to_html_file_injects_comments(tmp_path):
    scraper = DummyScraper(
        "https://example.substack.com/p/my-post",
        str(tmp_path / "md"),
        str(tmp_path / "html"),
    )
    html_path = str(tmp_path / "out.html")

    scraper.save_to_html_file(html_path, "<p>body</p>", comments_html="<section class=\"comments\">C</section>")

    content = Path(html_path).read_text(encoding="utf-8")
    assert '<section class="comments">C</section>' in content
    assert "<p>body</p>" in content


def test_save_to_html_file_without_comments(tmp_path):
    scraper = DummyScraper(
        "https://example.substack.com/p/my-post",
        str(tmp_path / "md"),
        str(tmp_path / "html"),
    )
    html_path = str(tmp_path / "out.html")

    scraper.save_to_html_file(html_path, "<p>body</p>")

    content = Path(html_path).read_text(encoding="utf-8")
    assert "<p>body</p>" in content
    assert "comments" not in content


# 22. test_write_post_html_includes_comments_section
def test_write_post_html_includes_comments_section(tmp_path):
    scraper = DummyScraper(
        "https://example.substack.com/p/my-post",
        str(tmp_path / "md"),
        str(tmp_path / "html"),
    )
    html_path = str(tmp_path / "out.html")
    comments = [{"name": "Alice", "body": "Hi", "children": []}]

    scraper._write_post_html(html_path, "# Title\n\nBody", comments)

    content = Path(html_path).read_text(encoding="utf-8")
    assert '<section class="comments">' in content
    assert "Alice" in content
    # The markdown body was converted to HTML.
    assert "<h1>Title</h1>" in content or "<h1>" in content