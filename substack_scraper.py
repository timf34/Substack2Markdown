import argparse
import json
import os
from abc import ABC, abstractmethod
from typing import List, Optional, Tuple
from time import sleep
from datetime import datetime

from bs4 import BeautifulSoup
import html2text
import markdown
import requests
from tqdm import tqdm
from xml.etree import ElementTree as ET

from selenium import webdriver
from selenium.webdriver.common.by import By
from webdriver_manager.microsoft import EdgeChromiumDriverManager
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.chrome.service import Service
from urllib.parse import urlparse
from config import EMAIL, PASSWORD
from ebooklib import epub
import shutil  # For robustly creating directories

USE_PREMIUM: bool = False  # Set to True if you want to login to Substack and convert paid for posts
BASE_SUBSTACK_URL: str = "https://www.thefitzwilliam.com/"  # Substack you want to convert to markdown
BASE_MD_DIR: str = "substack_md_files"  # Name of the directory we'll save the .md essay files
BASE_HTML_DIR: str = "substack_html_pages"  # Name of the directory we'll save the .html essay files
HTML_TEMPLATE: str = "author_template.html"  # HTML template to use for the author page
JSON_DATA_DIR: str = "data"
NUM_POSTS_TO_SCRAPE: int = 3  # Set to 0 if you want all posts


def extract_main_part(url: str) -> str:
    parts = urlparse(url).netloc.split('.')  # Parse the URL to get the netloc, and split on '.'
    return parts[1] if parts[0] == 'www' else parts[0]  # Return the main part of the domain, while ignoring 'www' if
    # present


def format_substack_date(date_str: str) -> str:
    """
    Converts Substack date string (e.g., "Jan 1, 2023", "1 day ago", "1 hr ago") to YYYY-MM-DD format.
    Returns original string if parsing fails.
    """
    if date_str == "Date not found":
        return "Unknown Date"  # Or handle as an error, or return None

    # Normalize common variations like "hours" to "hr"
    date_str = date_str.replace(" hours", " hr").replace(" hour", " hr")
    date_str = date_str.replace(" days", " day").replace(" day", " day")  # "1 day ago" is fine
    date_str = date_str.replace(" minutes", " min").replace(" minute", " min")

    try:
        # Handle formats like "Jan 1, 2023"
        dt_object = datetime.strptime(date_str, "%b %d, %Y")
        return dt_object.strftime("%Y-%m-%d")
    except ValueError:
        # Handle relative dates like "1 day ago", "1 hr ago", "Mar 23" (assuming current year)
        if "ago" in date_str or "hr" in date_str or "min" in date_str:  # simple handling for recent posts
            # For "X days/hours/mins ago", approximate to today's date.
            # More precise parsing would require libraries like `dateparser`
            # or more complex logic to subtract the duration.
            # For simplicity in this script, we'll use today's date.
            return datetime.now().strftime("%Y-%m-%d")
        try:
            # Try parsing "Mar 23" (assuming current year if year is missing)
            dt_object = datetime.strptime(date_str + f", {datetime.now().year}", "%b %d, %Y")
            return dt_object.strftime("%Y-%m-%d")
        except ValueError:
            # Try parsing "YYYY-MM-DD" style dates if they already exist
            try:
                dt_object = datetime.strptime(date_str, "%Y-%m-%d")
                return dt_object.strftime("%Y-%m-%d")  # Already in correct format
            except ValueError:
                print(f"Warning: Could not parse date string: '{date_str}'. Using original string.")
                return date_str  # Fallback to original string if all parsing fails


def generate_html_file(author_name: str) -> None:
    """
    Generates a HTML file for the given author.
    """
    if not os.path.exists(BASE_HTML_DIR):
        os.makedirs(BASE_HTML_DIR)

    # Read JSON data
    json_path = os.path.join(JSON_DATA_DIR, f'{author_name}.json')
    with open(json_path, 'r', encoding='utf-8') as file:
        essays_data = json.load(file)

    # Convert JSON data to a JSON string for embedding
    embedded_json_data = json.dumps(essays_data, ensure_ascii=False, indent=4)

    with open(HTML_TEMPLATE, 'r', encoding='utf-8') as file:
        html_template = file.read()

    # Insert the JSON string into the script tag in the HTML template
    html_with_data = html_template.replace('<!-- AUTHOR_NAME -->', author_name).replace(
        '<script type="application/json" id="essaysData"></script>',
        f'<script type="application/json" id="essaysData">{embedded_json_data}</script>'
    )
    html_with_author = html_with_data.replace('author_name', author_name)

    # Write the modified HTML to a new file
    html_output_path = os.path.join(BASE_HTML_DIR, f'{author_name}.html')
    with open(html_output_path, 'w', encoding='utf-8') as file:
        file.write(html_with_author)


class BaseSubstackScraper(ABC):
    def __init__(self, base_substack_url: str, md_save_dir: str, html_save_dir: str):
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

        self.keywords: List[str] = ["about", "archive", "podcast"]
        self.post_urls: List[str] = self.get_all_post_urls()

    def get_all_post_urls(self) -> List[str]:
        """
        Attempts to fetch URLs from sitemap.xml, falling back to feed.xml if necessary.
        """
        urls = self.fetch_urls_from_sitemap()
        if not urls:
            urls = self.fetch_urls_from_feed()
        return self.filter_urls(urls, self.keywords)

    def fetch_urls_from_sitemap(self) -> List[str]:
        """
        Fetches URLs from sitemap.xml.
        """
        sitemap_url = f"{self.base_substack_url}sitemap.xml"
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

        if os.path.exists(filepath):
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
        css_path = os.path.relpath("./assets/css/essay-styles.css", html_dir)
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
        Converts substack post soup to markdown, returns metadata and content
        """
        title = soup.select_one("h1.post-title, h2").text.strip()  # When a video is present, the title is demoted to h2

        subtitle_element = soup.select_one("h3.subtitle")
        subtitle = subtitle_element.text.strip() if subtitle_element else ""

        date_element = soup.find(
            "div",
            class_="pencraft pc-reset color-pub-secondary-text-hGQ02T line-height-20-t4M0El font-meta-MWBumP size-11-NuY2Zx weight-medium-fw81nC transform-uppercase-yKDgcq reset-IxiVJZ meta-EgzBVA"
        )
        raw_date = date_element.text.strip() if date_element else "Date not found"
        date = format_substack_date(raw_date)

        like_count_element = soup.select_one("a.post-ufi-button .label")
        like_count = (
            like_count_element.text.strip()
            if like_count_element and like_count_element.text.strip().isdigit()
            else "0"
        )

        content = str(soup.select_one("div.available-content"))
        md = self.html_to_md(content)
        md_content = self.combine_metadata_and_content(title, subtitle, date, like_count, md)
        return title, subtitle, like_count, date, md_content

    @abstractmethod
    def get_url_soup(self, url: str) -> str:
        raise NotImplementedError

    def save_essays_data_to_json(self, essays_data: list) -> None:
        """
        Saves essays data to a JSON file for a specific author.
        """
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

    def create_epub_from_author_markdown(self, author_name: str, base_md_dir: str, base_html_dir: str,
                                         json_data_dir: str) -> None:
        """
        Creates an EPUB file from the scraped markdown posts for a given author.

        Args:
            author_name: The name of the Substack author.
            base_md_dir: The base directory where markdown files are stored.
            base_html_dir: The base directory where HTML files are stored (unused in current EPUB logic but passed for consistency).
            json_data_dir: The directory where JSON metadata files are stored.
        """
        print(f"Starting EPUB generation for {author_name}...")

        json_path = os.path.join(json_data_dir, f'{author_name}.json')
        if not os.path.exists(json_path):
            print(f"Error: JSON data file not found for {author_name} at {json_path}. Cannot generate EPUB.")
            return

        with open(json_path, 'r', encoding='utf-8') as f:
            posts_metadata = json.load(f)

        if not posts_metadata:
            print(f"No posts found in JSON data for {author_name}. EPUB will be empty.")
            return

        # Sort posts by date. Handles "Unknown Date" by placing them at the end or beginning based on preference.
        # Here, 'Unknown Date' will cause an error if not handled before sorting.
        # We will filter out entries with "Unknown Date" or handle them by assigning a placeholder date.
        valid_posts = []
        for post in posts_metadata:
            if post.get('date') and post['date'] != "Unknown Date" and post['date'] != "Date not found":
                try:
                    # Ensure date is in a comparable format if it's already YYYY-MM-DD
                    datetime.strptime(post['date'], "%Y-%m-%d")
                    valid_posts.append(post)
                except ValueError:
                    print(
                        f"Skipping post with invalid date format: {post.get('title', 'Unknown Title')} - {post.get('date')}")
            else:
                print(f"Skipping post with missing or unknown date: {post.get('title', 'Unknown Title')}")

        # Sort posts by date, then by title as a secondary key if dates are the same
        # The primary sort key is 'date'.
        # If 'title' is missing, use a placeholder string.
        sorted_posts = sorted(valid_posts, key=lambda x: (x['date'], x.get('title', '')))

        book = epub.EpubBook()
        book.set_identifier(f"urn:uuid:{author_name}-{datetime.now().timestamp()}")
        book.set_title(f"{author_name.replace('_', ' ').title()} Substack Archive")
        book.set_language("en")
        book.add_author(author_name.replace('_', ' ').title())

        # Define TOC and chapters list
        chapters = []
        toc = []

        # Create a directory for EPUBs if it doesn't exist
        epub_dir = "substack_epubs"
        if not os.path.exists(epub_dir):
            os.makedirs(epub_dir)

        author_epub_dir = os.path.join(epub_dir, author_name)
        if not os.path.exists(author_epub_dir):
            os.makedirs(author_epub_dir)

        epub_filename = os.path.join(author_epub_dir, f"{author_name}_substack_archive.epub")

        # Default CSS for styling the EPUB content
        default_css = epub.EpubItem(
            uid="style_default",
            file_name="style/default.css",
            media_type="text/css",
            content="""
                body { font-family: serif; line-height: 1.6; }
                h1, h2, h3, h4, h5, h6 { font-family: sans-serif; }
                img { max-width: 100%; height: auto; }
                pre { white-space: pre-wrap; word-wrap: break-word; background-color: #f4f4f4; padding: 10px; border-radius: 4px; }
                code { font-family: monospace; }
            """
        )
        book.add_item(default_css)

        for i, post_meta in enumerate(sorted_posts):
            md_filepath = post_meta.get("file_link")
            if not md_filepath or not os.path.exists(md_filepath):
                print(
                    f"Warning: Markdown file not found for post: {post_meta.get('title', 'Unknown Title')}. Skipping.")
                continue

            with open(md_filepath, 'r', encoding='utf-8') as md_file:
                markdown_content = md_file.read()

            # Convert Markdown to HTML. The `markdown` library is already a dependency.
            # We strip the existing metadata from the markdown content before converting to HTML for the EPUB body
            # as EPUB will have its own metadata.
            # A simple way to strip metadata is to find the first occurrence of "\n\n**Likes:**"
            # and take content after that, or more robustly, find the end of the metadata block.
            # For now, let's assume metadata is at the start and ends before the main content.
            # A common pattern is that content starts after the second `\n\n` if there's a subtitle, or first if no subtitle.

            # Revised logic to remove metadata from the top of the .md file
            # Looks for the line containing "**Likes:**"
            metadata_marker = "**Likes:**"
            # Find the start of the metadata_marker, but be flexible with preceding newlines.
            # We search for the marker that could be at the start of a line or after some text (like a date).
            # A simple find should be okay if the marker is distinct enough.
            likes_line_start_index = markdown_content.find(metadata_marker)

            if likes_line_start_index != -1:
                # Find the end of the line containing "**Likes:**"
                # This is the position of the first newline character after the marker's occurrence.
                end_of_likes_line = markdown_content.find("\n", likes_line_start_index + len(metadata_marker))
                if end_of_likes_line != -1:
                    # Content starts after this newline.
                    content_after_likes_line = markdown_content[end_of_likes_line + 1:]  # +1 to move past the \n
                    # Strip leading whitespace (including newlines) from the extracted content
                    actual_content_markdown = content_after_likes_line.lstrip()
                else:
                    # This case means "**Likes:**" was found, but it's the very last thing in the file (no newline after it).
                    actual_content_markdown = ""
            else:
                # If "**Likes:**" is not found, assume no metadata or a different format;
                # use all content. This part of the logic remains the same.
                # This could happen if posts have no likes or the format changes.
                actual_content_markdown = markdown_content

            html_content = markdown.markdown(actual_content_markdown, extensions=['extra', 'meta'])

            chapter_title = post_meta.get("title", f"Chapter {i + 1}")
            # Sanitize filename for EPUB internal use
            chapter_filename_sanitized = "".join(c if c.isalnum() else "_" for c in chapter_title[:50])
            chapter_filename = f"chap_{i + 1}_{chapter_filename_sanitized}.xhtml"

            # Create chapter
            # Need to ensure html_content is bytes, not str.
            # Also, title should be a string.
            epub_chapter = epub.EpubHtml(title=str(chapter_title), file_name=chapter_filename, lang="en")

            # Basic HTML structure for the chapter content
            full_html_content = f"""<!DOCTYPE html>
            <html xmlns="http://www.w3.org/1999/xhtml" lang="en">
            <head>
                <meta charset="utf-8" />
                <title>{str(chapter_title)}</title>
                <link rel="stylesheet" type="text/css" href="style/default.css" />
            </head>
            <body>
                <h1>{str(chapter_title)}</h1>
                {html_content}
            </body>
            </html>"""

            epub_chapter.content = full_html_content.encode('utf-8')  # Ensure content is bytes
            epub_chapter.add_item(default_css)  # Link CSS to this chapter
            book.add_item(epub_chapter)
            chapters.append(epub_chapter)
            toc.append(epub.Link(chapter_filename, str(chapter_title), f"chap_{i + 1}"))

        # Define Table of Contents
        book.toc = tuple(toc)

        # Add default NCX and Nav file
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())

        # Set the spine (order of chapters)
        # The first item in spine is often the cover or title page, then Nav, then chapters.
        # For simplicity, we'll just list the chapters.
        # To include the Nav in the spine (as some readers prefer):
        # book.spine = ['nav'] + chapters
        # Or, if you have a cover:
        # book.spine = ['cover', 'nav'] + chapters
        # For now, just chapters:
        book.spine = ['nav'] + chapters  # Nav should usually come first for navigation structure

        try:
            epub.write_epub(epub_filename, book, {})
            print(f"Successfully generated EPUB: {epub_filename}")
        except Exception as e:
            print(f"Error writing EPUB file for {author_name}: {e}")

    def scrape_posts(self, num_posts_to_scrape: int = 0) -> None:
        """
        Iterates over all posts and saves them as markdown and html files
        """
        essays_data = []
        count = 0
        total = num_posts_to_scrape if num_posts_to_scrape != 0 else len(self.post_urls)
        for url in tqdm(self.post_urls, total=total):
            try:
                md_filename = self.get_filename_from_url(url, filetype=".md")
                html_filename = self.get_filename_from_url(url, filetype=".html")
                md_filepath = os.path.join(self.md_save_dir, md_filename)
                html_filepath = os.path.join(self.html_save_dir, html_filename)

                if not os.path.exists(md_filepath):
                    soup = self.get_url_soup(url)
                    if soup is None:
                        total += 1
                        continue
                    title, subtitle, like_count, date, md = self.extract_post_data(soup)
                    self.save_to_file(md_filepath, md)

                    # Convert markdown to HTML and save
                    html_content = self.md_to_html(md)
                    self.save_to_html_file(html_filepath, html_content)

                    essays_data.append({
                        "title": title,
                        "subtitle": subtitle,
                        "like_count": like_count,
                        "date": date,
                        "file_link": md_filepath,
                        "html_link": html_filepath
                    })
                else:
                    print(f"File already exists: {md_filepath}")
            except Exception as e:
                print(f"Error scraping post: {e}")
            count += 1
            if num_posts_to_scrape != 0 and count == num_posts_to_scrape:
                break
        self.save_essays_data_to_json(essays_data=essays_data)
        generate_html_file(author_name=self.writer_name)

        # Call EPUB generation
        # Need to pass the base directories, not the author-specific ones
        # self.md_save_dir is like "substack_md_files/author_name"
        # We need "substack_md_files"
        parent_md_dir = os.path.dirname(self.md_save_dir)
        parent_html_dir = os.path.dirname(self.html_save_dir)
        self.create_epub_from_author_markdown(
            author_name=self.writer_name,
            base_md_dir=parent_md_dir,
            base_html_dir=parent_html_dir,
            json_data_dir=JSON_DATA_DIR
        )


class SubstackScraper(BaseSubstackScraper):
    def __init__(self, base_substack_url: str, md_save_dir: str, html_save_dir: str):
        super().__init__(base_substack_url, md_save_dir, html_save_dir)

    def get_url_soup(self, url: str) -> Optional[BeautifulSoup]:
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
    def __init__(
            self,
            base_substack_url: str,
            md_save_dir: str,
            html_save_dir: str,
            headless: bool = False,
            edge_path: str = '',
            edge_driver_path: str = '',
            user_agent: str = ''
    ) -> None:
        super().__init__(base_substack_url, md_save_dir, html_save_dir)

        options = EdgeOptions()
        if headless:
            options.add_argument("--headless")
        if edge_path:
            options.binary_location = edge_path
        if user_agent:
            options.add_argument(f'user-agent={user_agent}')  # Pass this if running headless and blocked by captcha

        if edge_driver_path:
            service = Service(executable_path=edge_driver_path)
        else:
            service = Service(EdgeChromiumDriverManager().install())

        self.driver = webdriver.Edge(service=service, options=options)
        self.login()

    def login(self) -> None:
        """
        This method logs into Substack using Selenium
        """
        self.driver.get("https://substack.com/sign-in")
        sleep(3)

        signin_with_password = self.driver.find_element(
            By.XPATH, "//a[@class='login-option substack-login__login-option']"
        )
        signin_with_password.click()
        sleep(3)

        # Email and password
        email = self.driver.find_element(By.NAME, "email")
        password = self.driver.find_element(By.NAME, "password")
        email.send_keys(EMAIL)
        password.send_keys(PASSWORD)

        # Find the submit button and click it.
        submit = self.driver.find_element(By.XPATH, "//*[@id=\"substack-login\"]/div[2]/div[2]/form/button")
        submit.click()
        sleep(30)  # Wait for the page to load

        if self.is_login_failed():
            raise Exception(
                "Warning: Login unsuccessful. Please check your email and password, or your account status.\n"
                "Use the non-premium scraper for the non-paid posts. \n"
                "If running headless, run non-headlessly to see if blocked by Captcha."
            )

    def is_login_failed(self) -> bool:
        """
        Check for the presence of the 'error-container' to indicate a failed login attempt.
        """
        error_container = self.driver.find_elements(By.ID, 'error-container')
        return len(error_container) > 0 and error_container[0].is_displayed()

    def get_url_soup(self, url: str) -> BeautifulSoup:
        """
        Gets soup from URL using logged in selenium driver
        """
        try:
            self.driver.get(url)
            return BeautifulSoup(self.driver.page_source, "html.parser")
        except Exception as e:
            raise ValueError(f"Error fetching page: {e}") from e


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape a Substack site.")
    parser.add_argument(
        "-u", "--url", type=str, help="The base URL of the Substack site to scrape."
    )
    parser.add_argument(
        "-d", "--directory", type=str, help="The directory to save scraped posts."
    )
    parser.add_argument(
        "-n",
        "--number",
        type=int,
        default=0,
        help="The number of posts to scrape. If 0 or not provided, all posts will be scraped.",
    )
    parser.add_argument(
        "-p",
        "--premium",
        action="store_true",
        help="Include -p in command to use the Premium Substack Scraper with selenium.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Include -h in command to run browser in headless mode when using the Premium Substack "
             "Scraper.",
    )
    parser.add_argument(
        "--edge-path",
        type=str,
        default="",
        help='Optional: The path to the Edge browser executable (i.e. "path_to_msedge.exe").',
    )
    parser.add_argument(
        "--edge-driver-path",
        type=str,
        default="",
        help='Optional: The path to the Edge WebDriver executable (i.e. "path_to_msedgedriver.exe").',
    )
    parser.add_argument(
        "--user-agent",
        type=str,
        default="",
        help="Optional: Specify a custom user agent for selenium browser automation. Useful for "
             "passing captcha in headless mode",
    )
    parser.add_argument(
        "--html-directory",
        type=str,
        help="The directory to save scraped posts as HTML files.",
    )

    return parser.parse_args()


def main():
    args = parse_args()

    if args.directory is None:
        args.directory = BASE_MD_DIR

    if args.html_directory is None:
        args.html_directory = BASE_HTML_DIR

    if args.url:
        if args.premium:
            scraper = PremiumSubstackScraper(
                args.url,
                headless=args.headless,
                md_save_dir=args.directory,
                html_save_dir=args.html_directory
            )
        else:
            scraper = SubstackScraper(
                args.url,
                md_save_dir=args.directory,
                html_save_dir=args.html_directory
            )
        scraper.scrape_posts(args.number)

    else:  # Use the hardcoded values at the top of the file
        if USE_PREMIUM:
            scraper = PremiumSubstackScraper(
                base_substack_url=BASE_SUBSTACK_URL,
                md_save_dir=args.directory,
                html_save_dir=args.html_directory,
                edge_path=args.edge_path,
                edge_driver_path=args.edge_driver_path
            )
        else:
            scraper = SubstackScraper(
                base_substack_url=BASE_SUBSTACK_URL,
                md_save_dir=args.directory,
                html_save_dir=args.html_directory
            )
        scraper.scrape_posts(num_posts_to_scrape=NUM_POSTS_TO_SCRAPE)


if __name__ == "__main__":
    main()
