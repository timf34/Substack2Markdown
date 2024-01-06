import argparse
import os
from abc import ABC, abstractmethod
from typing import List
from time import sleep

from bs4 import BeautifulSoup
import html2text
import requests
from xml.etree import ElementTree as ET
from tqdm import tqdm

from selenium import webdriver
from selenium.webdriver.common.by import By
from webdriver_manager.microsoft import EdgeChromiumDriverManager
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.chrome.service import Service
from urllib.parse import urlparse

from config import EMAIL, PASSWORD

BASE_SUBSTACK_URL: str = "https://www.henrikkarlsson.xyz/"  # Substack you want to convert to markdown
BASE_DIR_NAME: str = "substack_md_files"  # Name of the directory we'll save the files to
NUM_POSTS_TO_SCRAPE: int = 10


def extract_main_part(url: str) -> str:
    parts = urlparse(url).netloc.split('.')  # Parse the URL to get the netloc, and split on '.'
    return parts[1] if parts[0] == 'www' else parts[0]  # Return the main part of the domain, while ignoring 'www' if
    # present


class BaseSubstackScraper(ABC):
    def __init__(self, base_substack_url: str):
        if not base_substack_url.endswith("/"):
            base_substack_url += "/"
        self.base_substack_url: str = base_substack_url

        url_base: str = extract_main_part(base_substack_url)
        savdir: str = f"{BASE_DIR_NAME}/{url_base}"

        if not os.path.exists(savdir):
            os.makedirs(savdir)
            print(f"Created directory {savdir}")
        self.save_dir: str = savdir
        self.keywords: List[str] = ["about", "archive", "podcast"]
        self.post_urls: List[str] = self.get_all_post_urls()

    def get_all_post_urls(self) -> List[str]:
        """
        This method reads the sitemap.xml file and returns a list of all the URLs in the file
        """
        sitemap_url = f"{self.base_substack_url}sitemap.xml"
        response = requests.get(sitemap_url)

        if response.ok:
            root = ET.fromstring(response.content)
            urls = [element.text for element in root.iter('{http://www.sitemaps.org/schemas/sitemap/0.9}loc')]
            urls = self.filter_urls(urls, self.keywords)
            return urls
        else:
            print(f'Error fetching sitemap: {response.status_code}')
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
    def combine_metadata_and_content(title: str, subtitle: str, content) -> str:
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

        return metadata + content

    def soup_to_md(self, soup: BeautifulSoup) -> str:
        """
        Converts substack post soup to markdown
        """
        title = soup.select_one("h1.post-title").text.strip()
        subtitle_element = soup.select_one("h3.subtitle")
        subtitle = subtitle_element.text.strip() if subtitle_element else ""
        content = str(soup.select_one("div.available-content"))
        content = self.html_to_md(content)
        return self.combine_metadata_and_content(title, subtitle, content)

    @abstractmethod
    def get_url_soup(self, url: str) -> str:
        raise NotImplementedError

    def scrape_posts(self, num_posts_to_scrape: int = 0) -> None:
        """
        Iterates over all posts and saves them as markdown files
        """
        count = 0
        total = num_posts_to_scrape if num_posts_to_scrape != 0 else len(self.post_urls)
        for url in tqdm(self.post_urls, total=total):
            try:
                filename = self.get_filename_from_url(url, filetype=".md")
                filepath = os.path.join(self.save_dir, filename)
                if not os.path.exists(filepath):
                    soup = self.get_url_soup(url)
                    if soup is None:
                        total += 1
                        continue
                    md = self.soup_to_md(soup)
                    self.save_to_file(filepath, md)
                else:
                    print(f"File already exists: {filepath}")
            except Exception as e:
                print(f"Error scraping post: {e}")
            count+= 1
            if num_posts_to_scrape != 0 and count == num_posts_to_scrape:
                break


class SubstackScraper(BaseSubstackScraper):
    def __init__(self, base_substack_url: str):
        super().__init__(base_substack_url)

    def get_url_soup(self, url: str) -> BeautifulSoup:
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
    def __init__(self, base_substack_url: str, headless: bool = False):
        super().__init__(base_substack_url)

        options = EdgeOptions()
        if headless:
            options.add_argument("--headless")

        service = Service(EdgeChromiumDriverManager().install())
        self.driver = webdriver.Edge(service=service, options=options)
        self.login()

    def login(self) -> None:   # sourcery skip: extract-duplicate-method
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
        sleep(5)  # Wait for the page to load

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
    parser = argparse.ArgumentParser(description='Scrape a Substack site.')
    parser.add_argument('-u', '--url', type=str,
                        help='The base URL of the Substack site to scrape.')
    parser.add_argument('-d', '--directory', type=str,
                        help='The directory to save scraped posts.')
    parser.add_argument('-n', '--number', type=int, default=0,
                        help='The number of posts to scrape. If 0 or not provided, all posts will be scraped.')
    parser.add_argument('-p', '--premium', action='store_true',
                        help='Use the Premium Substack Scraper with selenium.')
    parser.add_argument('--headless', action='store_true',
                        help='Run browser in headless mode when using the Premium Substack Scraper.')

    return parser.parse_args()


def main():
    args = parse_args()

    if args.url:
        if args.premium:
            scraper = PremiumSubstackScraper(args.url, headless=args.headless)
        else:
            scraper = SubstackScraper(args.url)
        scraper.scrape_posts(args.number)
    else:
        scraper = SubstackScraper(base_substack_url=BASE_SUBSTACK_URL)
        scraper.scrape_posts(num_posts_to_scrape=NUM_POSTS_TO_SCRAPE)


if __name__ == "__main__":
    main()
