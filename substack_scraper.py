#!/usr/bin/env python3
import argparse
import json
import os
from abc import ABC, abstractmethod
from typing import List, Optional, Tuple
from time import sleep

from bs4 import BeautifulSoup
import html2text
import markdown
import requests
from tqdm import tqdm
from xml.etree import ElementTree as ET

from urllib.parse import urlparse

# Constants
USE_PREMIUM: bool = False
BASE_SUBSTACK_URL: str = "https://www.thefitzwilliam.com/"
BASE_MD_DIR: str = "substack_md_files"
BASE_HTML_DIR: str = "substack_html_pages"
HTML_TEMPLATE: str = "author_template.html"
JSON_DATA_DIR: str = "data"
NUM_POSTS_TO_SCRAPE: int = 3


def extract_main_part(url: str) -> str:
    parts = urlparse(url).netloc.split('.')  # Parse the URL to get the netloc, and split on '.'
    return parts[1] if parts[0] == 'www' else parts[0]  # Return the main part of the domain, while ignoring 'www' if present


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
        try:
            response = requests.get(sitemap_url)

            if not response.ok:
                print(f'Error fetching sitemap at {sitemap_url}: {response.status_code}')
                return []

            root = ET.fromstring(response.content)
            urls = [element.text for element in root.iter('{http://www.sitemaps.org/schemas/sitemap/0.9}loc')]
            return urls
        except Exception as e:
            print(f"Error fetching sitemap: {e}")
            return []

    def fetch_urls_from_feed(self) -> List[str]:
        """
        Fetches URLs from feed.xml.
        """
        print('Falling back to feed.xml. This will only contain up to the 22 most recent posts.')
        feed_url = f"{self.base_substack_url}feed.xml"
        try:
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
        except Exception as e:
            print(f"Error fetching feed: {e}")
            return []

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
        with more robust selectors for different Substack layouts
        """
        # Try multiple possible selectors for title
        title_selectors = [
            "h1.post-title", 
            "h2.post-title", 
            "h1", 
            "article h1", 
            "article header h1",
            ".post-header h1"
        ]
        
        title = "Title not found"
        for selector in title_selectors:
            title_element = soup.select_one(selector)
            if title_element:
                title = title_element.text.strip()
                break
        
        # Try multiple possible selectors for subtitle
        subtitle_selectors = [
            "h3.subtitle", 
            ".post-subtitle", 
            "article header h3",
            ".post-header h2"
        ]
        
        subtitle = ""
        for selector in subtitle_selectors:
            subtitle_element = soup.select_one(selector)
            if subtitle_element:
                subtitle = subtitle_element.text.strip()
                break
        
        # Try multiple possible selectors for date
        date_selectors = [
            "div._color-pub-secondary-text_3axfk_207",
            ".post-date",
            "time",
            "article header time",
            ".post-header time",
            ".pencraft.pc-reset time",
            "div.pencraft.pc-reset ._meta_3axfk_442"
        ]
        
        date = "Date not found"
        for selector in date_selectors:
            date_element = soup.select_one(selector)
            if date_element:
                date = date_element.text.strip()
                break
        
        # Try multiple possible selectors for like count
        like_count_selectors = [
            "a.post-ufi-button .label",
            ".like-count",
            ".post-likes",
            ".like-button .count"
        ]
        
        like_count = "0"
        for selector in like_count_selectors:
            like_element = soup.select_one(selector)
            if like_element and like_element.text.strip().isdigit():
                like_count = like_element.text.strip()
                break
        
        # Try multiple possible selectors for content
        content_selectors = [
            "div.available-content",
            "article .post-content",
            "div.post-content",
            ".substack-post",
            "article.post"
        ]
        
        content = "<p>Content not found</p>"
        for selector in content_selectors:
            content_element = soup.select_one(selector)
            if content_element:
                content = str(content_element)
                break
                
        md = self.html_to_md(content)
        md_content = self.combine_metadata_and_content(title, subtitle, date, like_count, md)
        return title, subtitle, like_count, date, md_content

    @abstractmethod
    def get_url_soup(self, url: str) -> Optional[BeautifulSoup]:
        """
        Gets soup from URL (abstract method to be implemented by subclasses)
        """
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

    def scrape_posts(self, num_posts_to_scrape: int = 0) -> None:
        """
        Iterates over all posts and saves them as markdown and html files
        with improved error handling
        """
        essays_data = []
        count = 0
        total = num_posts_to_scrape if num_posts_to_scrape != 0 else len(self.post_urls)
        
        print(f"Found {len(self.post_urls)} posts to scrape")
        
        for url in tqdm(self.post_urls, total=total):
            try:
                md_filename = self.get_filename_from_url(url, filetype=".md")
                html_filename = self.get_filename_from_url(url, filetype=".html")
                md_filepath = os.path.join(self.md_save_dir, md_filename)
                html_filepath = os.path.join(self.html_save_dir, html_filename)

                if not os.path.exists(md_filepath):
                    print(f"Processing: {url}")
                    soup = self.get_url_soup(url)
                    
                    if soup is None:
                        print(f"Skipping URL (no soup returned): {url}")
                        continue
                    
                    try:
                        title, subtitle, like_count, date, md = self.extract_post_data(soup)
                        
                        # Skip if we couldn't extract essential data
                        if title == "Title not found" and "Content not found" in md:
                            print(f"Skipping URL (couldn't extract data): {url}")
                            continue
                            
                        self.save_to_file(md_filepath, md)
                        print(f"Saved markdown to: {md_filepath}")

                        # Convert markdown to HTML and save
                        html_content = self.md_to_html(md)
                        self.save_to_html_file(html_filepath, html_content)
                        print(f"Saved HTML to: {html_filepath}")

                        essays_data.append({
                            "title": title,
                            "subtitle": subtitle,
                            "like_count": like_count,
                            "date": date,
                            "file_link": md_filepath,
                            "html_link": html_filepath,
                            "source_url": url
                        })
                    except Exception as e:
                        print(f"Error extracting data from {url}: {str(e)}")
                        continue
                else:
                    print(f"File already exists: {md_filepath}")
                    
                # Add a short delay to avoid overloading the server
                sleep(1)
            except Exception as e:
                print(f"Error scraping post {url}: {str(e)}")
                continue
                
            count += 1
            if num_posts_to_scrape != 0 and count >= num_posts_to_scrape:
                break
                
        print(f"Successfully scraped {len(essays_data)} posts")
        self.save_essays_data_to_json(essays_data=essays_data)
        
        try:
            if os.path.exists(HTML_TEMPLATE):
                generate_html_file(author_name=self.writer_name)
                print(f"Generated HTML file for {self.writer_name}")
            else:
                print(f"HTML template not found: {HTML_TEMPLATE}")
        except Exception as e:
            print(f"Error generating HTML file: {str(e)}")


class SubstackScraper(BaseSubstackScraper):
    def __init__(self, base_substack_url: str, md_save_dir: str, html_save_dir: str):
        super().__init__(base_substack_url, md_save_dir, html_save_dir)

    def get_url_soup(self, url: str) -> Optional[BeautifulSoup]:
        """
        Gets soup from URL using requests with better error handling
        """
        try:
            page = requests.get(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            })
            
            if page.status_code != 200:
                print(f"Error: Got status code {page.status_code} for URL: {url}")
                return None
                
            soup = BeautifulSoup(page.content, "html.parser")
            
            # Check for paywall
            if soup.find("h2", class_="paywall-title") or soup.find("div", class_="paywall-overlay"):
                print(f"Skipping premium article: {url}")
                return None
                
            # Check if we actually got content
            if soup.select_one("div.available-content, article.post, div.post-content") is None:
                print(f"Warning: No content found for URL: {url}")
                # Print part of the HTML to debug
                print(f"HTML preview: {str(soup)[:500]}...")
                return None
                
            return soup
        except Exception as e:
            print(f"Error fetching page {url}: {str(e)}")
            return None


class PremiumSubstackScraper(BaseSubstackScraper):
    def __init__(
            self,
            base_substack_url: str,
            md_save_dir: str,
            html_save_dir: str,
            **kwargs
    ) -> None:
        super().__init__(base_substack_url, md_save_dir, html_save_dir)
        print("PremiumSubstackScraper is not available in this version")

    def get_url_soup(self, url: str) -> Optional[BeautifulSoup]:
        """
        Placeholder method for compatibility
        """
        print("PremiumSubstackScraper is not available in this version")
        return None


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
                html_save_dir=args.html_directory
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
