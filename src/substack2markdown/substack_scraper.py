import argparse
import json
import os
import io
import re
import base64
import hashlib
import mimetypes
from pathlib import Path
from urllib.parse import urlparse, unquote
from abc import ABC, abstractmethod
from typing import List, Optional, Tuple
from time import sleep
import asyncio
import atexit
import signal
import string

import html2text
import markdown
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from tqdm import tqdm
from xml.etree import ElementTree as ET

from selenium_driverless import webdriver
from selenium_driverless.types.by import By

USE_PREMIUM: bool = True  # Set to True if you want to login to Substack and convert paid for posts
BASE_SUBSTACK_URL: str = "https://www.thefitzwilliam.com/"  # Substack you want to convert to markdown
BASE_MD_DIR: str = "substack_md_files"  # Name of the directory we'll save the .md essay files
BASE_HTML_DIR: str = "substack_html_pages"  # Name of the directory we'll save the .html essay files
BASE_IMAGE_DIR: str = "substack_images"
BASE_JSON_DIR: str = "substack_json"
ASSETS_DIR: str = os.path.dirname(__file__) + "/assets"
HTML_TEMPLATE: str = "author_template.html"  # HTML template to use for the author page
NUM_POSTS_TO_SCRAPE: int = 3  # Set to 0 if you want all posts
DEFAULT_OUTPUT_DIRECTORY_FORMAT = "$publication_domain"
DEFAULT_IMAGE_PATH_FORMAT = "p/$post_slug/images/$image_filename"
DEFAULT_MD_PATH_FORMAT = "p/$post_slug/readme.md"
DEFAULT_HTML_PATH_FORMAT = "p/$post_slug/index.html"
DEFAULT_POSTS_MD_PATH_FORMAT = "readme.md"
DEFAULT_POSTS_HTML_PATH_FORMAT = "index.html"
DEFAULT_POSTS_JSON_PATH_FORMAT = "posts.json"
DEFAULT_POST_JSON_PATH_FORMAT = "p/$post_slug/post.json"
DEFAULT_COMMENTS_JSON_PATH_FORMAT = "p/$post_slug/comments.json"

json_dump_kwargs = dict(
    ensure_ascii=False,
    indent=0,
    separators=(',', ':'),
)

def count_images_in_markdown(md_content: str) -> int:
    """Count number of Substack CDN image URLs in markdown content."""
    # [![](https://substackcdn.com/image/fetch/x.png)](https://substackcdn.com/image/fetch/x.png)
    # regex lookahead: match "...)" but not "...)]" suffix
    pattern = re.compile(r'\(https://substackcdn\.com/image/fetch/[^\s\)]+\)(?=[^\]]|$)')
    matches = re.findall(pattern, md_content)
    return len(matches)


def sanitize_image_filename(url: str) -> str:
    """Create a safe filename from URL or content."""
    # Extract original filename from CDN URL
    if "substackcdn.com" in url:
        # Get the actual image URL after the CDN parameters
        original_url = unquote(url.split("/https%3A%2F%2F")[1])
        filename = original_url.split("/")[-1]
    else:
        filename = url.split("/")[-1]

    # Remove invalid characters
    filename = re.sub(r'[<>:"/\\|?*]', '', filename)

    # If filename is too long or empty, create hash-based name
    if len(filename) > 100 or not filename:
        hash_object = hashlib.md5(url.encode())
        ext = mimetypes.guess_extension(requests.head(url).headers.get('content-type', '')) or '.jpg'
        filename = f"{hash_object.hexdigest()}{ext}"

    return filename


def resolve_image_url(url: str) -> str:
    """Get the original image URL."""
    # https://substackcdn.com/image/fetch/xxx/https%3A%2F%2Fsubstack-post-media.s3.amazonaws.com%2Fpublic%2Fimages%2Fxxx
    if url.startswith("https://substackcdn.com/image/fetch/"):
        # substackcdn.com returns a compressed version of the original image
        url = "https://" + unquote(url.split("/https%3A%2F%2F")[1])
    return url


def get_post_slug(url: str) -> str:
    match = re.search(r'/p/([^/]+)', url)
    return match.group(1) if match else 'unknown_post'


def extract_main_part(url: str) -> str:
    parts = urlparse(url).netloc.split('.')  # Parse the URL to get the netloc, and split on '.'
    return parts[1] if parts[0] == 'www' else parts[0]  # Return the main part of the domain, while ignoring 'www' if
    # present


class BaseSubstackScraper(ABC):
    def __await__(self):
        return self._async_init().__await__()

    async def __aenter__(self):
        return await self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def close(self):
        pass

    def __init__(self, args):
        self.args = args
        if not self.args.url.endswith("/"):
            self.args.url += "/"

        self.publication_handle: str = extract_main_part(self.args.url)

        self.output_directory_template = string.Template(self.args.output_directory_format)

        # all these paths are relative to output_directory
        self.md_path_template = string.Template(self.args.md_path_format)
        self.html_path_template = string.Template(self.args.html_path_format)
        self.image_path_template = string.Template(self.args.image_path_format)
        self.posts_md_path_template = string.Template(self.args.posts_md_path_format)
        self.posts_html_path_template = string.Template(self.args.posts_html_path_format)
        self.posts_json_path_template = string.Template(self.args.posts_json_path_format)
        self.post_json_path_template = string.Template(self.args.post_json_path_format)
        self.comments_json_path_template = string.Template(self.args.comments_json_path_format)

        self.format_vars = {
            "publication_handle": self.publication_handle,
            "publication_domain": f"{self.publication_handle}.substack.com",
        }

        self.keywords: List[str] = ["about", "archive", "podcast"]
        self.post_urls: List[str] = self.get_all_post_urls()

    async def _async_init(self):
        self._loop = asyncio.get_running_loop()
        return self

    def get_all_post_urls(self) -> List[str]:
        """
        Attempts to fetch URLs from sitemap.xml, falling back to feed.xml if necessary.
        """
        if self.args.offline:
            return self.get_all_post_urls_offline()
        urls = self.fetch_urls_from_sitemap()
        if not urls:
            urls = self.fetch_urls_from_feed()
        return self.filter_urls(urls, self.keywords)

    def get_all_post_urls_offline(self) -> List[str]:
        # Read JSON data
        output_directory = self.output_directory_template.substitute(self.format_vars)
        self.format_vars["output_directory"] = output_directory
        posts_json_path = os.path.join(
            # self.format_vars["output_directory"] = 
            self.format_vars["output_directory"],
            self.posts_json_path_template.substitute(self.format_vars)
        )
        with open(posts_json_path, 'r', encoding='utf-8') as file:
            posts_data = json.load(file)
        urls = []
        for post in posts_data:
            post["slug"] = post["html_link"].split("/")[-2] # FIXME remove
            urls.append(self.args.url + "p/" + post["slug"])
        return urls

    def fetch_urls_from_sitemap(self) -> List[str]:
        """
        Fetches URLs from sitemap.xml.
        """
        sitemap_url = f"{self.args.url}sitemap.xml"
        response = requests.get(sitemap_url)

        if not response.ok:
            print(f'Error fetching sitemap at {sitemap_url}: {response.status_code}')
            return []

        root = ET.fromstring(response.content)
        urls = [element.text for element in root.iter('{http://www.sitemaps.org/schemas/sitemap/0.9}loc')]
        return urls

    def fetch_urls_from_feed(self) -> List[str]:
        """
        Fetches URLs from feed.xml.
        """
        print('Falling back to feed.xml. This will only contain up to the 22 most recent posts.')
        feed_url = f"{self.args.url}feed.xml"
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
        """
        This method filters out URLs that contain certain keywords
        """
        return [url for url in urls if all(keyword not in url for keyword in keywords)]

    @staticmethod
    def html_to_md(html_content: str) -> str:
        """
        This method converts HTML to Markdown
        """
        if not isinstance(html_content, str):
            raise ValueError("html_content must be a string")
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.body_width = 0
        return h.handle(html_content)

    @staticmethod
    def save_to_file(filepath: str, content: str) -> None:
        """
        This method saves content to a file. Can be used to save HTML or Markdown
        """
        if not isinstance(filepath, str):
            raise ValueError("filepath must be a string")

        if not isinstance(content, str):
            raise ValueError("content must be a string")

        # if os.path.exists(filepath):
        if False:
            print(f"File already exists: {filepath}")
            return

        with open(filepath, 'w', encoding='utf-8') as file:
            file.write(content)

    @staticmethod
    def md_to_html(md_content: str) -> str:
        """
        This method converts Markdown to HTML
        """
        return markdown.markdown(md_content, extensions=['extra'])


    def save_to_html_file(self, filepath: str, content: str) -> None:
        """
        This method saves HTML content to a file with a link to an external CSS file.
        """
        if not isinstance(filepath, str):
            raise ValueError("filepath must be a string")

        if not isinstance(content, str):
            raise ValueError("content must be a string")

        # Calculate the relative path from the HTML file to the CSS file
        html_dir = os.path.dirname(filepath)
        css_path = self.args.assets_dir + "/css/essay-styles.css"
        if not os.path.isabs(css_path):
            css_path = os.path.relpath(css_path, html_dir)
        css_path = css_path.replace("\\", "/")  # Ensure forward slashes for web paths

        html_content = f"""
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Markdown Content</title>
                <link rel="stylesheet" href="{css_path}">
            </head>
            <body>
                <main class="markdown-content">
                {content}
                </main>
            </body>
            </html>
        """

        with open(filepath, 'w', encoding='utf-8') as file:
            file.write(html_content)

    @staticmethod
    def get_filename_from_url(url: str, filetype: str = ".md") -> str:
        """
        Gets the filename from the URL (the ending)
        """
        if not isinstance(url, str):
            raise ValueError("url must be a string")

        if not isinstance(filetype, str):
            raise ValueError("filetype must be a string")

        if not filetype.startswith("."):
            filetype = f".{filetype}"

        return url.split("/")[-1] + filetype

    @staticmethod
    def combine_metadata_and_content(title: str, subtitle: str, date: str, like_count: str, content) -> str:
        """
        Combines the title, subtitle, and content into a single string with Markdown format
        """
        if not isinstance(title, str):
            raise ValueError("title must be a string")

        if not isinstance(content, str):
            raise ValueError("content must be a string")

        metadata = f"# {title}\n\n"
        if subtitle:
            metadata += f"## {subtitle}\n\n"
        metadata += f"**{date}**\n\n"
        metadata += f"**Likes:** {like_count}\n\n"

        return metadata + content

    def extract_post_data(self, soup: BeautifulSoup) -> Tuple[str, str, str, str, str]:
        """
        Converts a Substack post soup to markdown, returning metadata and content.
        Returns (title, subtitle, like_count, date, md_content).
        """
        # Title (sometimes h2 if video present)
        title_element = soup.select_one("h1.post-title, h2")
        title = title_element.text.strip() if title_element else "Untitled"

        # Subtitle
        subtitle_element = soup.select_one("h3.subtitle")
        subtitle = subtitle_element.text.strip() if subtitle_element else ""

        # Date — try CSS selector first
        date = ""
        date_element = soup.select_one("div.pencraft.pc-reset.color-pub-secondary-text-hGQ02T")
        if date_element and date_element.text.strip():
            date = date_element.text.strip()

        # Fallback: JSON-LD metadata
        if not date:
            script_tag = soup.find("script", {"type": "application/ld+json"})
            if script_tag and script_tag.string:
                try:
                    metadata = json.loads(script_tag.string)
                    if "datePublished" in metadata:
                        date_str = metadata["datePublished"]
                        date_obj = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                        date = date_obj.strftime("%b %d, %Y")
                except (json.JSONDecodeError, ValueError, KeyError):
                    pass

        if not date:
            date = "Date not found"

        # Like count
        like_count_element = soup.select_one("a.post-ufi-button .label")
        like_count = (
            like_count_element.text.strip()
            if like_count_element and like_count_element.text.strip().isdigit()
            else "0"
        )
        like_count = int(like_count)

        # Post content
        content_element = soup.select_one("div.available-content")
        content_html = str(content_element) if content_element else ""
        md = self.html_to_md(content_html)

        # Combine metadata + content
        md_content = self.combine_metadata_and_content(title, subtitle, date, like_count, md)

        return title, subtitle, like_count, date, md_content

    def extract_post_data_from_preloads(self, post_preloads):

        title = post_preloads["post"]["title"]

        subtitle = post_preloads["post"]["description"]

        like_count = post_preloads["post"]["reactions"]["❤"]

        date = post_preloads["post"]["post_date"] # date in ISO format: "2025-10-01T14:43:48.389Z"

        # datetime_format = "%b %d, %Y" # "Oct 01, 2025"
        # date = datetime.strptime(date, "%Y-%m-%dT%H:%M:%S.%fZ").strftime(datetime_format)

        content_html = post_preloads["post"]["body_html"]
        md = self.html_to_md(content_html)
        # Combine metadata + content
        md_content = self.combine_metadata_and_content(title, subtitle, date, like_count, md)

        return title, subtitle, like_count, date, md_content

    async def get_window_preloads(self, soup):
        # all comments are stored in javascript
        # <script>window._preloads = JSON.parse("{\"isEU\":true,\"language\":\"en\",...}")</script>
        # only some comments are rendered in html
        # with buttons to "Expand full comment" and "Load More"
        # see also
        # https://www.selfpublife.com/p/automatically-expand-all-substack-comments
        window_preloads = None
        for script_element in soup.select("script"):
            script_text = script_element.text.strip()
            if not script_text.startswith("window._preloads"):
                continue
            # pos1 = re.search(r'window._preloads\s*=\s*JSON\.parse\(', script_text).span()[1]
            pos1 = script_text.find("(") + 1
            pos2 = script_text.rfind(")")
            window_preloads = json.loads(json.loads(script_text[pos1:pos2]))
            break
        assert window_preloads, f"not found <script>window._preloads...</script> at {url!r}"
        return window_preloads

    def count_comments(self, comments_preloads):

        def count_comments_inner(comment):
            res = 1
            for child_comment in comment["children"]:
                res += count_comments_inner(child_comment)
            return res

        res = 0
        for comment in comments_preloads["initialComments"]:
            res += count_comments_inner(comment)
        return res

    def render_comments_html(self, comments_preloads):

        def render_comment_body(body):
            body = body.strip()
            body = "<p>" + body + "</p>"
            body = body.replace("\n", "</p>\n<p>")
            # TODO more?
            return body

        def render_comments_html_inner(comment, buf):
            assert comment["type"] == "comment", f'unexpected comment type: {comment["type"]!r}'
            buf.write(f'<details class="comment" id="{comment["id"]}" open>\n')
            buf.write(f'<summary>\n')

            # NOTE user IDs are constant, user handles are variable
            # when i change my user handle
            # then other users can use my old user handle
            if not comment["user_id"] is None:
                buf.write(f'<a class="user" href="https://substack.com/profile/{comment["user_id"]}">')

            if not comment["name"] is None:
                buf.write(comment["name"]) # human-readable username
            else:
                # Comment removed
                buf.write("null")

            if not comment["user_id"] is None:
               buf.write('</a>\n')
            else:
               buf.write('\n')

            other_pub = comment["metadata"].get("author_on_other_pub")
            if other_pub:
                # NOTE publication handles are quasi-constant:
                # when i change my publication handle
                # then other users cannot use my old publication handle
                # NOTE "Changing your publication's subdomain
                # does not automatically set up a redirect from the old subdomain to the new one."
                buf.write(f'(<a class="pub" pub-id="{other_pub["id"]}" href="{other_pub["base_url"]}">')
                buf.write(other_pub["name"])
                buf.write('</a>)\n')

            buf.write(comment["date"] + '\n') # "2025-05-17T06:51:39.485Z"

            for reaction, reaction_count in comment["reactions"].items():
                if reaction_count == 0: continue
                buf.write(reaction + str(reaction_count) + '\n') # "❤123"
                # buf.write(str(reaction_count) + reaction + '\n') # "123❤"

            buf.write('</summary>\n')

            buf.write('<blockquote>\n')
            buf.write('\n')

            if comment["body"] is None:
                # Comment removed
                status = comment.get("status")
                if status is None:
                    buf.write('(Comment removed)\n')
                else:
                    # "moderator_removed", ...
                    buf.write('(status:' + status + ')\n')
                # TODO comment["bans"]
                # TODO comment["suppressed"]
                # TODO comment["user_banned"]
                # TODO comment["user_banned_for_comment"]
            else:
                buf.write(render_comment_body(comment["body"]) + '\n')

            for child_comment in comment["children"]:
                buf.write('\n')
                render_comments_html_inner(child_comment, buf)
            buf.write('</blockquote>\n')

            buf.write('</details>\n')
            buf.write('\n')

        buf = io.StringIO()
        # NOTE the name "initial" is misleading. all comments are stored in this array
        # NOTE comments are sorted by likes
        for comment in comments_preloads["initialComments"]:
            render_comments_html_inner(comment, buf)
        return buf.getvalue()

    @abstractmethod
    async def get_url_soup(self, url: str) -> str:
        raise NotImplementedError

    def save_posts_data_json(self, posts_data: list) -> None:
        """
        Saves essays data to a JSON file for a specific author.
        """
        posts_json_path = os.path.join(
            self.format_vars["output_directory"],
            self.posts_json_path_template.substitute(self.format_vars)
        )
        os.makedirs(os.path.dirname(posts_json_path), exist_ok=True)
        if os.path.exists(posts_json_path):
            with open(posts_json_path, 'r', encoding='utf-8') as file:
                existing_data = json.load(file)
            # remove duplicates from existing_data
            new_post_ids = set(map(lambda p: p["id"], posts_data))
            existing_data = [p for p in posts_data if p["id"] not in new_post_ids]
            posts_data = existing_data + posts_data
        # sort by post_id, descending
        posts_data.sort(key=lambda p: -1*p["id"])
        with open(posts_json_path, 'w', encoding='utf-8') as f:
            json.dump(posts_data, f, **json_dump_kwargs)

    async def scrape_posts(self, num_posts_to_scrape: int = 0) -> None:
        """
        Iterates over all posts and saves them as markdown and html files
        """
        output_directory = self.output_directory_template.substitute(self.format_vars)
        self.format_vars["output_directory"] = output_directory

        posts_json_path = os.path.join(
            self.format_vars["output_directory"],
            self.posts_json_path_template.substitute(self.format_vars)
        )
        posts_json_dir = os.path.dirname(posts_json_path)

        posts_data = []
        post_urls_slice = self.post_urls if num_posts_to_scrape == 0 else self.post_urls[:num_posts_to_scrape]
        for url in tqdm(post_urls_slice):
            try:
                post_slug = url.split("/")[-1]
                self.format_vars["post_slug"] = post_slug

                md_filepath = os.path.join(
                    output_directory,
                    self.md_path_template.substitute(self.format_vars)
                )
                self.format_vars["md_filepath"] = md_filepath
                self.format_vars["md_directory"] = os.path.dirname(md_filepath)

                html_filepath = os.path.join(
                    output_directory,
                    self.html_path_template.substitute(self.format_vars)
                )
                self.format_vars["html_filepath"] = html_filepath
                self.format_vars["html_directory"] = os.path.dirname(html_filepath)

                post_json_filepath = None
                comments_json_filepath = None
                if not self.args.no_json:
                    post_json_filepath = os.path.join(
                        output_directory,
                        self.post_json_path_template.substitute(self.format_vars)
                    )
                    comments_json_filepath = os.path.join(
                        output_directory,
                        self.comments_json_path_template.substitute(self.format_vars)
                    )

                # if not os.path.exists(md_filepath):
                if self.args.offline:
                    json_filepath = os.path.join(
                        output_directory,
                        self.post_json_path_template.substitute(self.format_vars)
                    )
                    with open(json_filepath) as f:
                        post_preloads = json.load(f)
                    title, subtitle, like_count, date, md = self.extract_post_data_from_preloads(post_preloads)
                else:
                    soup = await self.get_url_soup(url)
                    if soup is None:
                        continue
                    title, subtitle, like_count, date, md = self.extract_post_data(soup)
                    post_preloads = await self.get_window_preloads(soup)
                    date = post_preloads["post"]["post_date"] # date in ISO format: "2025-10-01T14:43:48.389Z"

                if True:
                    post_id = post_preloads["post"]["id"]

                if True:
                    if not self.args.no_images:
                        total_images = count_images_in_markdown(md)
                        with tqdm(total=total_images, desc=f"Downloading images for {post_slug}", leave=False) as img_pbar:
                            md = await self.process_markdown_images(md, img_pbar)

                md = self.process_markdown_links(md)

                if True:
                    comments_html = None
                    comments_num = None
                    if not self.args.no_comments:
                        comments_url = url + "/comments"
                        # comments_url = "https://willstorr.substack.com/p/scamming-substack/comments" # test
                        if self.args.offline:
                            json_filepath = os.path.join(
                                output_directory,
                                self.comments_json_path_template.substitute(self.format_vars)
                            )
                            with open(json_filepath) as f:
                                comments_preloads = json.load(f)
                        else:
                            comments_soup = await self.get_url_soup(comments_url)
                            comments_preloads = await self.get_window_preloads(comments_soup)
                        if not self.args.no_json:
                            json_filepath = os.path.join(
                                output_directory,
                                self.comments_json_path_template.substitute(self.format_vars)
                            )
                            _json = json.dumps(comments_preloads, **json_dump_kwargs)
                            self.save_to_file(json_filepath, _json)
                        comments_num = self.count_comments(comments_preloads)
                        if comments_num > 0:
                            comments_html = self.render_comments_html(comments_preloads)
                            comments_html = (
                                '\n\n' +
                                '<hr>\n' +
                                # this can collide with other elements with id="comments"
                                # '<section id="comments">\n' +
                                '<section class="comments">\n' +
                                '<h2>Comments</h2>\n' +
                                '<details open>\n' +
                                f'<summary>{comments_num} comments</summary>\n' +
                                comments_html + '\n' +
                                '</details>'
                                '</section>'
                            )
                            md += comments_html

                    self.save_to_file(md_filepath, md)

                    if not self.args.no_json:
                        json_filepath = os.path.join(
                            output_directory,
                            self.post_json_path_template.substitute(self.format_vars)
                        )
                        _json = json.dumps(post_preloads, **json_dump_kwargs)
                        self.save_to_file(json_filepath, _json)

                    # Convert markdown to HTML and save
                    html_content = self.md_to_html(md)
                    # if self.args.offline:
                    #     html_content = post_preloads["post"]["body_html"]
                    # else:
                    #     html_content = self.md_to_html(md)
                    self.save_to_html_file(html_filepath, html_content)

                    post = {
                        "id": post_id,
                        "slug": post_preloads["post"]["slug"],
                        "title": title,
                        "subtitle": subtitle,
                        "like_count": like_count,
                        "comment_count": comments_num,
                        "repost_count": post_preloads["post"]["restacks"],
                        "date": date,
                        "file_link": os.path.relpath(md_filepath, posts_json_dir),
                        "html_link": os.path.relpath(html_filepath, posts_json_dir),
                    }

                    if not self.args.no_json:
                        post["post_json"] = os.path.relpath(post_json_filepath, posts_json_dir)
                        post["comments_json"] = os.path.relpath(comments_json_filepath, posts_json_dir)

                    posts_data.append(post)
                else:
                    print(f"File already exists: {md_filepath}")
            except Exception as e:
                print(f"Error scraping post: {e}")
                # raise e # debug
        self.save_posts_data_json(posts_data)
        self.generate_main_md_file()
        self.generate_main_html_file()

    def generate_main_md_file(self) -> None:
        """
        Generates a Markdown file for the given author.
        """
        # Read JSON data
        posts_json_path = os.path.join(
            self.format_vars["output_directory"],
            self.posts_json_path_template.substitute(self.format_vars)
        )
        with open(posts_json_path, 'r', encoding='utf-8') as file:
            posts_data = json.load(file)

        # sort by post_id, descending
        posts_data.sort(key=lambda p: -1*p["id"])

        last_post = posts_data[0]
        last_post_json_path = last_post["post_json"]
        last_post_json_path = os.path.join(
            os.path.dirname(posts_json_path),
            last_post_json_path
        )

        with open(last_post_json_path, 'r', encoding='utf-8') as file:
            last_post = json.load(file)

        publication = last_post["pub"]

        md_output_path = os.path.join(
            self.format_vars["output_directory"],
            self.posts_md_path_template.substitute(self.format_vars)
        )

        with open(md_output_path, 'w', encoding='utf-8') as file:
            file.write(f'# {publication["name"]}\n')
            file.write('\n')
            # author_url = f'https://substack.com/@{publication["author_handle"]}' # variable
            author_url = f'https://substack.com/profile/{publication["author_id"]}' # constant
            file.write(f'by [{publication["author_name"]}]({author_url})\n')
            file.write('\n')
            author_bio = publication["author_bio"].replace("\n", "\n\n")
            file.write(f'{author_bio}\n')
            file.write('\n')
            file.write('\n')
            file.write('\n')
            file.write('## Posts\n')
            file.write('\n')
            for post in posts_data:
                # TODO use args.datetime_format
                post_date = post["date"]
                post_link = (
                    '<a id="post' +
                    str(post["id"]) +
                    '" href="' +
                    post["file_link"] +
                    '" title="' +
                    post["subtitle"].replace('"', '&quot;') +
                    '">' +
                    post["title"].replace('<', '&lt;') +
                    '</a>'
                )
                if post["like_count"] > 0:
                    post_link += f" ❤" + str(post["like_count"]) # "❤123"
                if post["comment_count"] > 0:
                    post_link += f" 🗨" + str(post["comment_count"]) # "🗨123"
                if post["repost_count"] > 0:
                    post_link += f" ↻" + str(post["repost_count"]) # "↻123"
                file.write(f'- {post_date} - {post_link}\n')

    def generate_main_html_file(self) -> None:
        """
        Generates a HTML file for the given author.
        """
        # Read JSON data
        posts_json_path = os.path.join(
            self.format_vars["output_directory"],
            self.posts_json_path_template.substitute(self.format_vars)
        )
        with open(posts_json_path, 'r', encoding='utf-8') as file:
            posts_data = json.load(file)

        # Convert JSON data to a JSON string for embedding
        embedded_json_data = json.dumps(posts_data, **json_dump_kwargs)

        md_output_path = os.path.join(
            self.format_vars["output_directory"],
            self.posts_md_path_template.substitute(self.format_vars)
        )

        html_output_path = os.path.join(
            self.format_vars["output_directory"],
            self.posts_html_path_template.substitute(self.format_vars)
        )

        with open(self.args.author_template, 'r', encoding='utf-8') as file:
            html_template = file.read()

        html_with_data = html_template

        # patch assets path
        assets_path = self.args.assets_dir
        if not os.path.isabs(assets_path):
            assets_path = os.path.relpath(assets_path, os.path.dirname(html_output_path))
        html_with_data = html_with_data.replace('"../assets', f'"{assets_path}')

        html_with_data = html_with_data.replace('<!-- AUTHOR_NAME -->', self.publication_handle)

        # Insert the JSON string into the script tag in the HTML template
        html_with_data = html_with_data.replace(
            '<script type="application/json" id="essaysData"></script>',
            f'<script type="application/json" id="essaysData">{embedded_json_data}</script>'
        )

        # Write the modified HTML to a new file
        with open(html_output_path, 'w', encoding='utf-8') as file:
            file.write(html_with_data)

    async def download_image(
            self,
            url: str,
            save_path: Path,
            pbar: Optional[tqdm] = None
        ) -> Optional[str]:
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
        except Exception as exc:
            if pbar:
                pbar.write(f"Error downloading image {url}: {str(exc)}")
            # raise exc # debug
        return None

    async def process_markdown_images(
            self,
            md_content: str,
            pbar=None
        ) -> str:
        """Process markdown content to download images and update references."""
        output_directory = self.format_vars["output_directory"]
        # [![](https://substackcdn.com/image/fetch/x.png)](https://substackcdn.com/image/fetch/x.png)
        pattern = re.compile(r'\((https://substackcdn\.com/image/fetch/[^\s\)]+)\)')
        buf = io.StringIO()
        last_end = 0
        for match in pattern.finditer(md_content):
            buf.write(md_content[last_end:match.start()])
            url = match.group(1)
            url = resolve_image_url(url)
            filename = sanitize_image_filename(url)
            format_vars = {
                **self.format_vars,
                "image_filename": filename,
            }
            save_path = Path(os.path.join(
                output_directory,
                self.image_path_template.substitute(format_vars)
            ))
            if not save_path.exists() and not self.args.offline:
                await self.download_image(url, save_path, pbar)
            md_directory = self.format_vars["md_directory"]
            rel_path = save_path
            if not os.path.isabs(rel_path):
                rel_path = os.path.relpath(save_path, md_directory)
            buf.write(f"({rel_path})")
            last_end = match.end()
        buf.write(md_content[last_end:])
        return buf.getvalue()

    def process_markdown_links(self, md_content):
        # patch links to other posts of this publication
        pattern = re.compile(r'\]\(https://' + self.publication_handle + r'\.substack\.com/p/([^\s\)]+)\)')
        md_directory = self.format_vars["md_directory"]
        output_directory = self.format_vars["output_directory"]
        def get_replacement(match):
            post_slug = match.group(1)
            md_filepath = os.path.join(
                output_directory,
                self.md_path_template.substitute({
                    **self.format_vars,
                    "post_slug": post_slug,
                })
            )
            md_filepath_rel = os.path.relpath(md_filepath, md_directory)
            return '](' + md_filepath_rel + ')'
        md_content = re.sub(pattern, get_replacement, md_content)
        return md_content


class SubstackScraper(BaseSubstackScraper):
    async def get_url_soup(self, url: str) -> Optional[BeautifulSoup]:
        """
        Gets soup from URL using requests
        """
        try:
            page = requests.get(url, headers=None)
            soup = BeautifulSoup(page.content, "html.parser")
            if soup.find("h2", class_="paywall-title"):
                print(f"Skipping premium article: {url}")
                return None
            return soup
        except Exception as e:
            raise ValueError(f"Error fetching page: {e}") from e


class PremiumSubstackScraper(BaseSubstackScraper):
    def __init__(self, args) -> None:
        super().__init__(args)

        self.driver = None

        def exit_handler(signum, frame):
            print()
            print(f"exit_handler: received signal {signum}")
            try:
                asyncio.get_event_loop().create_task(self._cleanup_sync())
            except Exception:
                pass
            raise SystemExit(0)

        signal.signal(signal.SIGINT, exit_handler)
        signal.signal(signal.SIGTERM, exit_handler)

        atexit.register(self._cleanup_sync)

        options = webdriver.ChromeOptions()
        self.chrome_options = options
        if self.args.headless:
            # modern headless flag (works better with recent Chromium)
            options.add_argument("--headless=new")
        if self.args.chromium_path:
            options.binary_location = self.args.chromium_path
        if self.args.user_agent:
            options.add_argument(f"user-agent={self.args.user_agent}")

    async def _async_init(self):
        self._loop = asyncio.get_running_loop()

        await self._start_driver()
        await self.login()
        return self

    async def _start_driver(self):
        self.driver = await webdriver.Chrome(options=self.chrome_options)

    async def close(self) -> None:
        if self.driver:
            await self.driver.quit()

    def _cleanup_sync(self):
        try:
            if not self.driver:
                return
            proc = self.driver._process
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1)
                except Exception:
                    proc.kill()
        except Exception as exc:
            print("_cleanup_sync failed:", exc)

    async def login(self):
        await self.driver.get("https://substack.com/sign-in")
        await asyncio.sleep(2)

        signin = await self.driver.find_element(
            By.XPATH, "//a[contains(@class,'login-option')]"
        )
        await signin.click()

        await asyncio.sleep(2)

        email = await self.driver.find_element(By.NAME, "email")
        password = await self.driver.find_element(By.NAME, "password")

        await email.send_keys(self.args.email)
        await password.send_keys(self.args.password)

        submit = await self.driver.find_element(
            By.XPATH, "//*[@id='substack-login']//form//button"
        )
        await submit.click()

        await asyncio.sleep(8)

        if await self.is_login_failed():
            raise RuntimeError("Substack login failed")

    async def is_login_failed(self):
        """
        Check for the presence of the 'error-container' to indicate a failed login attempt.
        """
        elements = await self.driver.find_elements(By.ID, "error-container")
        return bool(elements)

    async def get_url_soup(self, url: str):
        """
        Gets soup from URL using logged in selenium driver
        """
        await self.driver.get(url)
        html = await self.driver.page_source
        return BeautifulSoup(html, "html.parser")

    async def download_image_FIXME(
            self,
            url: str,
            save_path: Path,
            pbar: Optional[tqdm] = None
        ) -> Optional[str]:
        """Download image using selenium_driverless"""

        # NOTE for now this works with the default "def download_image"

        # WONTFIX "fetch" fails due to CORS policy

        # WONTFIX "canvas" does not return the original image bytes

        # we could fetch images with CDP Network.getResponseBody
        # but that requires lots of boilerplate code
        # fix: use https://github.com/milahu/aiohttp_chromium

        try:
            # Execute JS fetch inside browser
            result = await self.driver.execute_async_script(
                """
                const url = arguments[0];
                const callback = arguments[arguments.length - 1];

                const img = new Image();
                img.crossOrigin = 'Anonymous'; // try to avoid CORS issues
                img.onload = () => {
                    try {
                        const canvas = document.createElement('canvas');
                        canvas.width = img.width;
                        canvas.height = img.height;
                        const ctx = canvas.getContext('2d');
                        ctx.drawImage(img, 0, 0);
                        const dataUrl = canvas.toDataURL('image/png'); // returns "data:image/png;base64,..."
                        const base64 = dataUrl.split(',')[1]; // strip prefix
                        callback({data: base64});
                    } catch (err) {
                        callback({error: err.message, stack: err.stack});
                    }
                };
                img.onerror = (err) => {
                    callback({error: 'Image load error', stack: err.toString()});
                };
                img.src = url;
                """,
                url
            )

            if isinstance(result, dict) and "error" in result:
                raise RuntimeError(f"{result['error']}\nJS stack:\n{result['stack']}")

            # Decode base64 to bytes
            image_bytes = base64.b64decode(result)

            save_path.parent.mkdir(parents=True, exist_ok=True)
            with open(save_path, "wb") as f:
                f.write(image_bytes)

            if pbar:
                pbar.update(1)

            return str(save_path)

        except Exception as exc:
            if pbar:
                pbar.write(f"Error downloading image {url}: {exc}")
            # raise exc # debug
            return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape a Substack site.")
    parser.add_argument(
        "--config", type=str, help="JSON config file with email and password."
    )
    parser.add_argument(
        "--email", type=str, help="Login E-Mail."
    )
    parser.add_argument(
        "--password", type=str, help="Login password."
    )
    parser.add_argument(
        "-u",
        "--url", # args.url
        type=str,
        default=BASE_SUBSTACK_URL,
        help="The base URL of the Substack site to scrape."
    )
    parser.add_argument(
        "-n",
        "--number", # args.number
        type=int,
        default=0,
        help="The number of posts to scrape. If 0 or not provided, all posts will be scraped.",
    )
    # this was based on the wrong assumption
    # that post_preloads JSON data contains the same body_html as the HTML page, but
    # post_preloads["post"]["body_html"] contains HTML components with "data-attrs" attributes
    # str(soup.select_one("div.available-content")) is clean HTML
    # TODO convert HTML components to clean HTML
    # parser.add_argument(
    #     "--offline", # args.offline
    #     action="store_true",
    #     help="Use existing JSON files to render Markdown and HTML files.",
    # )
    parser.add_argument(
        "-p",
        "--premium",
        action="store_true",
        help="Include -p in command to use the Premium Substack Scraper with selenium.",
    )
    parser.add_argument(
        "--assets-dir", # args.assets_dir
        default=ASSETS_DIR,
        help=f"Path to assets directory. Default: {ASSETS_DIR!r}",
    )
    parser.add_argument(
        "--author-template", # args.author_template
        help=f"Path to author_template.html. Default: {repr('{assets_dir}/' + HTML_TEMPLATE)}",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Include -h in command to run browser in headless mode when using the Premium Substack "
        "Scraper.",
    )
    parser.add_argument(
        "--chromium-path", # args.chromium_path
        type=str,
        default="",
        help='Optional: The path to the Chromium browser executable (i.e. "path/to/chromium").',
    )
    parser.add_argument(
        "--user-agent",
        type=str,
        default="",
        help="Optional: Specify a custom user agent for selenium browser automation. Useful for "
        "passing captcha in headless mode",
    )
    parser.add_argument(
        "--output-directory-format", # args.output_directory_format
        type=str,
        default=DEFAULT_OUTPUT_DIRECTORY_FORMAT,
        # all relative output file paths are relative to this directory
        help=f"The file path format of the directory to save output files. Default: {DEFAULT_OUTPUT_DIRECTORY_FORMAT!r}",
    )
    parser.add_argument(
        "--md-path-format", # args.md_path_format
        type=str,
        default=DEFAULT_MD_PATH_FORMAT,
        help=f"The file path format to save scraped posts as Markdown files. Default: {DEFAULT_MD_PATH_FORMAT!r}",
    )
    parser.add_argument(
        "--html-path-format", # args.html_path_format
        type=str,
        default=DEFAULT_HTML_PATH_FORMAT,
        help=f"The file path format to save scraped posts as HTML files. Default: {DEFAULT_HTML_PATH_FORMAT!r}",
    )
    parser.add_argument(
        "--image-path-format", # args.image_path_format
        type=str,
        default=DEFAULT_IMAGE_PATH_FORMAT,
        help=f"The file path format to save scraped image files. Default: {DEFAULT_IMAGE_PATH_FORMAT!r}",
    )
    parser.add_argument(
        "--posts-md-path-format", # args.posts_md_path_format
        type=str,
        default=DEFAULT_POSTS_MD_PATH_FORMAT,
        help=f"The file path format to save an index of scraped posts as Markdown file. Default: {DEFAULT_POSTS_MD_PATH_FORMAT!r}",
    )
    parser.add_argument(
        "--posts-html-path-format", # args.posts_html_path_format
        type=str,
        default=DEFAULT_POSTS_HTML_PATH_FORMAT,
        help=f"The file path format to save an index of scraped posts as HTML file. Default: {DEFAULT_POSTS_HTML_PATH_FORMAT!r}",
    )
    parser.add_argument(
        "--posts-json-path-format", # args.posts_json_path_format
        type=str,
        default=DEFAULT_POSTS_JSON_PATH_FORMAT,
        help=f"The file path format to save metadata of scraped posts as JSON file. Default: {DEFAULT_POSTS_JSON_PATH_FORMAT!r}",
    )
    parser.add_argument(
        "--post-json-path-format", # args.post_json_path_format
        type=str,
        default=DEFAULT_POST_JSON_PATH_FORMAT,
        help=f"The file path format to save scraped posts as JSON files. Default: {DEFAULT_POST_JSON_PATH_FORMAT!r}",
    )
    parser.add_argument(
        "--comments-json-path-format", # args.comments_json_path_format
        type=str,
        default=DEFAULT_COMMENTS_JSON_PATH_FORMAT,
        help=f"The file path format to save scraped comments as JSON files. Default: {DEFAULT_COMMENTS_JSON_PATH_FORMAT!r}",
    )
    parser.add_argument(
        "--no-images", # args.no_images
        action="store_true",
        help=f"Do not download images.",
    )
    parser.add_argument(
        "--no-comments", # args.no_comments
        action="store_true",
        help=f"Do not download comments.",
    )
    parser.add_argument(
        "--no-json", # args.no_json
        action="store_true",
        help=f"Do not write JSON files.",
    )

    return parser.parse_args()


async def async_main():
    args = parse_args()

    args.offline = False

    if args.config:
        with open(args.config) as f:
            config = json.load(f)
        args.email = config["email"]
        args.password = config["password"]
        # TODO more

    assert args.email
    assert args.password

    if not args.author_template:
        args.author_template = args.assets_dir + "/" + HTML_TEMPLATE

    if True:
        if args.offline:
            scraper = await SubstackScraper(args)
        elif args.premium:
            scraper = await PremiumSubstackScraper(args)
        else:
            scraper = await SubstackScraper(args)

        await scraper.scrape_posts(args.number)
        await scraper.close()


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
