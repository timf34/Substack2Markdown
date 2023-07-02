import os
from typing import Dict, List, Tuple
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

from config import EMAIL, PASSWORD

# TODO: Create the savdir based on the name of the author unless specified otherwise


class BaseSubstackScraper:
    def __init__(self, base_substack_url: str, savdir: str):
        if not base_substack_url.endswith("/"):
            base_substack_url += "/"
        self.base_substack_url: str = base_substack_url

        # Check if the save_dir exists
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

    def get_post_content(self, url: str) -> str:
        raise NotImplementedError

    def scrape_all_posts(self, only_scrape_n_posts: int= 0) -> None:
        """
        Iterates over all posts and saves them as markdown files
        """
        count = 0
        for url in tqdm(self.post_urls):
            try:
                filename = self.get_filename_from_url(url, filetype=".md")
                filepath = os.path.join(self.save_dir, filename)
                if not os.path.exists(filepath):
                    content = self.get_post_content(url)
                    md = self.html_to_md(content)
                    self.save_to_file(filepath, md)
                else:
                    print(f"File already exists: {filepath}")
            except Exception as e:
                print(f"Error scraping post: {e}")
            count+= 1
            if only_scrape_n_posts != 0 and count == only_scrape_n_posts:
                break

class SubstackScraper(BaseSubstackScraper):
    def __init__(self, base_substack_url: str, savdir: str):
        super().__init__(base_substack_url, savdir)

    def get_post_content(self, url: str) -> str:
        """
        Gets post content using requests and soup and returns it as a string
        """
        try:
            page = requests.get(url, headers=None)
            soup = BeautifulSoup(page.content, "html.parser")
            content = str(soup.select_one("div.available-content"))
            return content
        except Exception as e:
            raise ValueError(f"Error fetching page: {e}") from e


class PremiumSubstackScraper(BaseSubstackScraper):
    def __init__(self, base_substack_url: str, savdir: str, headless: bool = False):
        super().__init__(base_substack_url, savdir)

        options = EdgeOptions()
        if headless:
            options.add_argument("--headless")

        self.driver = webdriver.Edge(EdgeChromiumDriverManager().install(), options=options)
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
        submit = self.driver.find_element(By.XPATH, "//*[@id=\"substack-login\"]/div[2]/div[2]/form/div[3]/button")
        submit.click()
        sleep(5) # Wait for the page to load

    def get_post_content(self, url: str) -> str:
        # sourcery skip: inline-immediately-returned-variable
        """
        Gets post content using requests and soup and returns it as a string
        """
        try:
            self.driver.get(url)
            soup = BeautifulSoup(self.driver.page_source, "html.parser")
            content = str(soup.select_one("div.available-content"))
            return content
        except Exception as e:
            raise ValueError(f"Error fetching page: {e}") from e


def main():
    premium_scraper = PremiumSubstackScraper(base_substack_url="https://ava.substack.com/", savdir="data/ava_test")
    premium_scraper.scrape_all_posts(only_scrape_n_posts=5)


if __name__ == "__main__":
    main()
