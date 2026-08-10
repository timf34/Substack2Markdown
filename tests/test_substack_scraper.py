import os
import sys
import shutil
import types

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

# 9. test_mdx_frontmatter_includes_source_url
def test_mdx_frontmatter_includes_source_url():
    """Verify the post URL is emitted as source_url in mdx frontmatter."""
    result = ss.BaseSubstackScraper.combine_metadata_and_content(
        "Title",
        "Subtitle",
        "2024-01-01",
        "Author",
        "",
        "5",
        "Body",
        frontmatter_format="mdx",
        source_url="https://example.substack.com/p/test-post",
    )

    assert 'source_url: "https://example.substack.com/p/test-post"' in result
    assert result.index('author: "Author"') < result.index("source_url:")

    # Legacy format is unchanged and never includes source_url
    legacy = ss.BaseSubstackScraper.combine_metadata_and_content(
        "Title",
        "Subtitle",
        "2024-01-01",
        "Author",
        "",
        "5",
        "Body",
        frontmatter_format="legacy",
        source_url="https://example.substack.com/p/test-post",
    )
    assert "source_url" not in legacy


# 10. get_credentials
def test_get_credentials_env_vars_take_precedence(monkeypatch):
    fake_config = types.ModuleType("config")
    fake_config.EMAIL = "file@example.com"
    fake_config.PASSWORD = "file-secret"
    monkeypatch.setitem(sys.modules, "config", fake_config)
    monkeypatch.setenv("SUBSTACK_EMAIL", "env@example.com")
    monkeypatch.setenv("SUBSTACK_PASSWORD", "env-secret")

    assert ss.get_credentials() == ("env@example.com", "env-secret")


def test_get_credentials_falls_back_to_config(monkeypatch):
    fake_config = types.ModuleType("config")
    fake_config.EMAIL = "file@example.com"
    fake_config.PASSWORD = "file-secret"
    monkeypatch.setitem(sys.modules, "config", fake_config)
    monkeypatch.delenv("SUBSTACK_EMAIL", raising=False)
    monkeypatch.delenv("SUBSTACK_PASSWORD", raising=False)

    assert ss.get_credentials() == ("file@example.com", "file-secret")


def test_get_credentials_empty_when_unconfigured(monkeypatch):
    # None in sys.modules makes `import config` raise ImportError,
    # simulating a missing config.py even if one exists locally
    monkeypatch.setitem(sys.modules, "config", None)
    monkeypatch.delenv("SUBSTACK_EMAIL", raising=False)
    monkeypatch.delenv("SUBSTACK_PASSWORD", raising=False)

    assert ss.get_credentials() == ("", "")


# 11. YouTube embeds (issue #25)
YOUTUBE_EMBED_HTML = (
    '<div class="available-content"><p>Intro</p>'
    '<div id="youtube2-9FDgRXPSv3U" data-attrs="{&quot;videoId&quot;:&quot;9FDgRXPSv3U&quot;,'
    '&quot;startTime&quot;:null,&quot;endTime&quot;:null}" data-component-name="Youtube2ToDOM" '
    'class="youtube-wrap"><div class="youtube-inner">'
    '<iframe src="https://www.youtube-nocookie.com/embed/9FDgRXPSv3U?rel=0" frameborder="0">'
    '</iframe></div></div><p>Outro</p></div>'
)


def test_youtube_embed_exported_as_linked_thumbnail():
    md = ss.BaseSubstackScraper.html_to_md(YOUTUBE_EMBED_HTML)

    assert (
        "[![YouTube video](https://img.youtube.com/vi/9FDgRXPSv3U/hqdefault.jpg)]"
        "(https://www.youtube.com/watch?v=9FDgRXPSv3U)" in md
    )
    assert "Intro" in md and "Outro" in md


def test_youtube_embed_with_malformed_attrs_is_skipped():
    html = (
        '<div class="youtube-wrap" data-attrs="not-json"><iframe src="x"></iframe></div>'
        "<p>Body</p>"
    )

    md = ss.BaseSubstackScraper.html_to_md(html)

    assert "Body" in md


def test_clean_linked_images_preserves_youtube_thumbnail_links():
    md = (
        "[![YouTube video](https://img.youtube.com/vi/abc/hqdefault.jpg)]"
        "(https://www.youtube.com/watch?v=abc)"
    )

    assert ss.clean_linked_images(md) == md
