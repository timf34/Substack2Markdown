from bs4 import BeautifulSoup
import html2text
import os
import requests
from typing import Dict, List, Tuple
from xml.etree import ElementTree as ET
from tqdm import tqdm

from selenium import webdriver
from selenium.webdriver.common.by import By
from webdriver_manager.microsoft import EdgeChromiumDriverManager
from time import sleep

class SubstackScraper:
    def __init__(self, base_url: str):
        self.base_url: str = base_url
        self.keywords: List[str] = ["about", "archive", "podcast"]  # Keywords to filter out unwanted URLs

    def get_all_post_urls(self) -> List[str]:
        """
        This method reads the sitemap.xml file and returns a list of all the URLs in the file
        """
        sitemap_url = f"{self.base_url}sitemap.xml"
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
    def _substack_reader(soup: BeautifulSoup, url: str) -> Tuple[str, Dict[str, str]]:
        """
        This method extracts the content of a Substack post and returns it as a string
        """
        metadata = {
            "Title of this Substack post": soup.select_one("h1.post-title").text,
            "Subtitle": soup.select_one("h3.subtitle").text,
            "Author": soup.select_one("a.navbar-title-link").text,
            "URL": url
        }
        text = soup.select_one("div.available-content").getText()
        return text, metadata

    def extract_post_content(self, url: str) -> None:
        """
        This method extracts the content of a Substack post and prints it to the console
        """
        try:
            page = requests.get(url, headers=None)
            soup = BeautifulSoup(page.content, "html.parser")
            text = html2text.html2text(str(soup.select_one("div.available-content")))  # This prints it more nicely than bs4
            _, metadata = self._substack_reader(soup, url)  # Note: _ here might actually be more parseable than the text above! But just leaving as is for now
            print(text)
            print(metadata)
        except Exception as e:
            raise ValueError(f"Error fetching page: {e}") from e


# This can inherit from SubstackScraper down the line, but just keep it separate for now
# Try log in using selenium and then scrape the premium content
class PremiumSubstackScraper(SubstackScraper):
    def __init__(self, base_url: str):
        super().__init__(base_url)
        self.driver = webdriver.Edge(EdgeChromiumDriverManager().install())
        self.login()
        self.post_urls: List[str] = self.get_all_post_urls()

    def login(self) -> None:  # sourcery skip: extract-duplicate-method
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
        email.send_keys("farrelti@tcd.ie")
        password.send_keys("shopkeeper2")

        # Find the submit button and click it.
        submit = self.driver.find_element(By.XPATH, "//*[@id=\"substack-login\"]/div[2]/div[2]/form/div[3]/button")
        submit.click()
        sleep(5) # Wait for the page to load


    def save_posts_as_html(self, data_dir: str) -> None:
        """
        This method loops over the post URLs and saves the content as an HTML file.
        It names the HTML file after the last part of the URL.
        """
        count = 0
        if not os.path.exists(data_dir):  # ensure the directory exists
            os.makedirs(data_dir)
        for url in tqdm(self.post_urls, desc="Saving posts", unit="post"):
            try:
                file_name = url.rsplit('/', 1)[-1]  # get the last part of the URL
                file_path = os.path.join(data_dir, f"{file_name}.html")
                if not os.path.exists(file_path):  # only proceed if the file does not already exist
                    self.driver.get(url)
                    sleep(5)  # wait for the page to load
                    page_content = self.driver.page_source
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(page_content)
                else:
                    print(f"File {file_path} already exists. Skipping.")
            except Exception as e:
                print(f"Error fetching or saving page: {e}")
            count += 1
            if count > 5:
                self.driver.quit()
                break  # Just for testing purposes


def main():
    premium_substack_scraper = PremiumSubstackScraper("https://ava.substack.com/")
    premium_substack_scraper.save_posts_as_html("data/ava")


if __name__ == "__main__":
    main()
