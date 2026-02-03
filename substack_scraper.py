import argparse
import json
import os
import sys
import random
from abc import ABC, abstractmethod
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
from selenium.common.exceptions import SessionNotCreatedException, WebDriverException
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.edge.options import Options as EdgeOptions
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.microsoft import EdgeChromiumDriverManager

from urllib.parse import urlparse
from config import EMAIL, PASSWORD

USE_PREMIUM: bool = True  # Set to True if you want to login to Substack and convert paid for posts
BASE_SUBSTACK_URL: str = "https://www.thefitzwilliam.com/"  # Substack you want to convert to markdown
BASE_MD_DIR: str = "substack_md_files"  # Name of the directory we'll save the .md essay files
BASE_HTML_DIR: str = "substack_html_pages"  # Name of the directory we'll save the .html essay files
HTML_TEMPLATE: str = "author_template.html"  # HTML template to use for the author page
JSON_DATA_DIR: str = "data"  
NUM_POSTS_TO_SCRAPE: int = 3  # Set to 0 if you want all posts


def extract_main_part(url: str) -> str:
    parts = urlparse(url).netloc.split('.')
    return parts[1] if parts[0] == 'www' else parts[0]


def generate_html_file(author_name: str) -> None:
    if not os.path.exists(BASE_HTML_DIR):
        os.makedirs(BASE_HTML_DIR)

    json_path = os.path.join(JSON_DATA_DIR, f'{author_name}.json')
    with open(json_path, 'r', encoding='utf-8') as file:
        essays_data = json.load(file)

    embedded_json_data = json.dumps(essays_data, ensure_ascii=False, indent=4)

    with open(HTML_TEMPLATE, 'r', encoding='utf-8') as file:
        html_template = file.read()

    html_with_data = html_template.replace('', author_name).replace(
        '<script type="application/json" id="essaysData"></script>',
        f'<script type="application/json" id="essaysData">{embedded_json_data}</script>'
    )
    html_with_author = html_with_data.replace('author_name', author_name)

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
        urls = self.fetch_urls_from_sitemap()
        if not urls:
            urls = self.fetch_urls_from_feed()
        return self.filter_urls(urls, self.keywords)

    def fetch_urls_from_sitemap(self) -> List[str]:
        sitemap_url = f"{self.base_substack_url}sitemap.xml"
        response = requests.get(sitemap_url)
        if not response.ok:
            print(f'Error fetching sitemap at {sitemap_url}: {response.status_code}')
            return []
        root = ET.fromstring(response.content)
        urls = [element.text for element in root.iter('{http://www.sitemaps.org/schemas/sitemap/0.9}loc')]
        return urls

    def fetch_urls_from_feed(self) -> List[str]:
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
        return [url for url in urls if all(keyword not in url for keyword in keywords)]

    @staticmethod
    def html_to_md(html_content: str) -> str:
        if not isinstance(html_content, str):
            raise ValueError("html_content must be a string")
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.body_width = 0
        return h.handle(html_content)

    @staticmethod
    def save_to_file(filepath: str, content: str) -> None:
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
        return markdown.markdown(md_content, extensions=['extra'])

    def save_to_html_file(self, filepath: str, content: str) -> None:
        html_dir = os.path.dirname(filepath)
        css_path = os.path.relpath("./assets/css/essay-styles.css", html_dir)
        css_path = css_path.replace("\\", "/")
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
        if not isinstance(url, str):
            raise ValueError("url must be a string")
        if not isinstance(filetype, str):
            raise ValueError("filetype must be a string")
        if not filetype.startswith("."):
            filetype = f".{filetype}"
        return url.split("/")[-1] + filetype

    @staticmethod
    def combine_metadata_and_content(title: str, subtitle: str, date: str, like_count: str, content) -> str:
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
        title_element = soup.select_one("h1.post-title, h2")
        title = title_element.text.strip() if title_element else "Untitled"
        subtitle_element = soup.select_one("h3.subtitle")
        subtitle = subtitle_element.text.strip() if subtitle_element else ""
        date = ""
        date_element = soup.select_one("div.pencraft.pc-reset.color-pub-secondary-text-hGQ02T")
        if date_element and date_element.text.strip():
            date = date_element.text.strip()
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
        like_count_element = soup.select_one("a.post-ufi-button .label")
        like_count = (
            like_count_element.text.strip()
            if like_count_element and like_count_element.text.strip().isdigit()
            else "0"
        )
        content_element = soup.select_one("div.available-content")
        content_html = str(content_element) if content_element else ""
        md = self.html_to_md(content_html)
        md_content = self.combine_metadata_and_content(title, subtitle, date, like_count, md)
        return title, subtitle, like_count, date, md_content

    @abstractmethod
    def get_url_soup(self, url: str) -> str:
        raise NotImplementedError

    def save_essays_data_to_json(self, essays_data: list) -> None:
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
                    # CHANGED: Use tqdm.write to print above the progress bar
                    tqdm.write(f"Downloading: {md_filename}") 
                    
                    # Sleep happens AFTER we tell the user what we are doing
                    sleep_time = random.uniform(10, 20)
                    sleep(sleep_time)

                    soup = self.get_url_soup(url)
                    if soup is None:
                        continue
                    
                    title, subtitle, like_count, date, md = self.extract_post_data(soup)
                    self.save_to_file(md_filepath, md)
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
                    # CHANGED: Use tqdm.write for skipping as well to keep the UI consistent
                    tqdm.write(f"Skipping (Exists): {md_filename}")
            
            except Exception as e:
                tqdm.write(f"Error scraping post: {e}")
            
            count += 1
            if num_posts_to_scrape != 0 and count == num_posts_to_scrape:
                break
        
        self.save_essays_data_to_json(essays_data=essays_data)
        generate_html_file(author_name=self.writer_name)


class SubstackScraper(BaseSubstackScraper):
    def __init__(self, base_substack_url: str, md_save_dir: str, html_save_dir: str):
        super().__init__(base_substack_url, md_save_dir, html_save_dir)

    def get_url_soup(self, url: str) -> Optional[BeautifulSoup]:
        try:
            # CHANGED: Added basic retry for requests-based scraper too
            max_retries = 3
            for attempt in range(max_retries):
                page = requests.get(url, headers=None)
                if page.status_code == 429:
                    print(f"Rate limited (429). Waiting 60s... (Attempt {attempt+1}/{max_retries})")
                    sleep(60)
                    continue
                elif page.status_code != 200:
                    print(f"Error fetching page: Status {page.status_code}")
                    return None
                
                soup = BeautifulSoup(page.content, "html.parser")
                if soup.find("h2", class_="paywall-title"):
                    print(f"Skipping premium article: {url}")
                    return None
                return soup
            return None
        except Exception as e:
            raise ValueError(f"Error fetching page: {e}") from e


class PremiumSubstackScraper(BaseSubstackScraper):
    def __init__(
        self,
        base_substack_url: str,
        md_save_dir: str,
        html_save_dir: str,
        headless: bool = False,
        browser: str = 'auto',
        browser_path: str = '',
        driver_path: str = '',
        user_agent: str = ''
    ) -> None:
        super().__init__(base_substack_url, md_save_dir, html_save_dir)

        self.headless = headless
        self.user_agent = user_agent
        self.browser_path = browser_path
        self.driver_path = driver_path
        self.driver = None
        
        os.environ.setdefault("SE_DRIVER_MIRROR_URL", "https://chromedriver.storage.googleapis.com")

        if browser.lower() == 'chrome':
            self._init_chrome()
        elif browser.lower() == 'edge':
            self._init_edge()
        else:
            try:
                print("Attempting to initialize Chrome...")
                self._init_chrome()
            except (SessionNotCreatedException, WebDriverException, Exception) as e:
                print(f"Chrome initialization failed: {e}")
                print("Falling back to Edge...")
                self._init_edge()

        if self.driver is None:
             raise RuntimeError("Failed to initialize both Chrome and Edge drivers.")

        self.login()

    def _init_chrome(self):
        options = ChromeOptions()
        if self.headless:
            options.add_argument("--headless=new")
        if self.browser_path and "chrome" in self.browser_path.lower():
            options.binary_location = self.browser_path
        if self.user_agent:
            options.add_argument(f"user-agent={self.user_agent}")

        driver_path = self.driver_path
        if not driver_path:
            try:
                driver_path = ChromeDriverManager().install()
                if "THIRD_PARTY_NOTICES" in driver_path:
                    base_dir = os.path.dirname(driver_path)
                    candidate = os.path.join(base_dir, "chromedriver")
                    if os.path.exists(candidate):
                        driver_path = candidate
            except Exception as e:
                print(f"Error installing Chromedriver via manager: {e}")
                raise

        if driver_path and os.path.exists(driver_path) and os.name == 'posix':
            try:
                os.chmod(driver_path, 0o755)
            except Exception:
                pass

        if driver_path:
            service = ChromeService(executable_path=driver_path)
            self.driver = webdriver.Chrome(service=service, options=options)
        else:
            self.driver = webdriver.Chrome(options=options)

    def _init_edge(self):
        options = EdgeOptions()
        if self.headless:
            options.add_argument("--headless=new")
        if self.browser_path and "msedge" in self.browser_path.lower():
            options.binary_location = self.browser_path
        if self.user_agent:
            options.add_argument(f"user-agent={self.user_agent}")

        if self.driver_path and os.path.exists(self.driver_path) and "msedge" in self.driver_path.lower():
            service = EdgeService(executable_path=self.driver_path)
            self.driver = webdriver.Edge(service=service, options=options)
        else:
            service = EdgeService(EdgeChromiumDriverManager().install())
            self.driver = webdriver.Edge(service=service, options=options)

    def login(self) -> None:
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
        sleep(30)
        if self.is_login_failed():
            raise Exception("Warning: Login unsuccessful.")

    def is_login_failed(self) -> bool:
        error_container = self.driver.find_elements(By.ID, 'error-container')
        return len(error_container) > 0 and error_container[0].is_displayed()

    def get_url_soup(self, url: str) -> Optional[BeautifulSoup]:
        """
        Gets soup from URL using logged in selenium driver with RETRY LOGIC for Rate Limits
        """
        max_retries = 3
        for attempt in range(max_retries):
            try:
                self.driver.get(url)
                
                # CHANGED: Randomized wait for JS to render
                sleep(random.uniform(5, 8))
                
                page_source = self.driver.page_source
                soup = BeautifulSoup(page_source, "html.parser")
                
                # Check for rate limit indicators in the page content
                text_content = soup.get_text().lower()
                if "too many requests" in text_content:
                    wait_time = 60 + (attempt * 30) # Increase wait on subsequent failures
                    print(f"Rate limited detected (Too Many Requests). Waiting {wait_time}s before retry {attempt + 1}/{max_retries}...")
                    sleep(wait_time)
                    continue

                return soup

            except Exception as e:
                print(f"Error fetching page (attempt {attempt+1}): {e}")
                if attempt == max_retries - 1:
                    # On final failure, raise the error so it can be caught by the outer loop
                    raise ValueError(f"Failed to fetch {url} after {max_retries} attempts") from e
                sleep(30)
        
        return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape a Substack site.")
    parser.add_argument("-u", "--url", type=str, help="The base URL of the Substack site to scrape.")
    parser.add_argument("-d", "--directory", type=str, help="The directory to save scraped posts.")
    parser.add_argument("-n", "--number", type=int, default=0, help="The number of posts to scrape.")
    parser.add_argument("-p", "--premium", action="store_true", help="Use the Premium Substack Scraper.")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode.")
    parser.add_argument("--browser", type=str, default="auto", choices=["chrome", "edge", "auto"], help="The browser to use.")
    parser.add_argument("--browser-path", type=str, default="", help='Optional: Path to browser executable.')
    parser.add_argument("--driver-path", type=str, default="", help='Optional: Path to WebDriver executable.')
    parser.add_argument("--user-agent", type=str, default="", help="Optional: Specify a custom user agent.")
    parser.add_argument("--html-directory", type=str, help="The directory to save scraped posts as HTML files.")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.directory is None: args.directory = BASE_MD_DIR
    if args.html_directory is None: args.html_directory = BASE_HTML_DIR

    if args.url:
        if args.premium:
            scraper = PremiumSubstackScraper(
                args.url, headless=args.headless, md_save_dir=args.directory, 
                html_save_dir=args.html_directory, browser=args.browser, 
                browser_path=args.browser_path, driver_path=args.driver_path, 
                user_agent=args.user_agent
            )
        else:
            scraper = SubstackScraper(args.url, md_save_dir=args.directory, html_save_dir=args.html_directory)
        scraper.scrape_posts(args.number)
    else:
        if USE_PREMIUM:
            scraper = PremiumSubstackScraper(
                base_substack_url=BASE_SUBSTACK_URL, md_save_dir=args.directory, 
                html_save_dir=args.html_directory, browser="auto"
            )
        else:
            scraper = SubstackScraper(base_substack_url=BASE_SUBSTACK_URL, md_save_dir=args.directory, html_save_dir=args.html_directory)
        scraper.scrape_posts(num_posts_to_scrape=NUM_POSTS_TO_SCRAPE)


if __name__ == "__main__":
    main()
