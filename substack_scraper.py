from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup
import html2text
import requests
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET


class SubstackScraper:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.keywords: List[str] = ["about", "archive", "podcast"]  # Keywords to filter out unwanted URLs

    def get_all_posts(self):
        """
        This method reads the sitemap.xml file and returns a list of all the URLs in the file
        """
        sitemap_url = f"{self.base_url}sitemap.xml"
        response = requests.get(sitemap_url)

        # Check if request was successful
        if response.status_code == 200:
            root = ET.fromstring(response.content)
            urls = [element.text for element in root.iter('{http://www.sitemaps.org/schemas/sitemap/0.9}loc')]
            urls = self.filter_urls(urls, self.keywords)
            return urls
        else:
            print(f'Error fetching sitemap: {response.status_code}')
            return []

    @staticmethod
    def filter_urls(urls: List[str], keywords: List[str]):
        """
        This method filters out URLs that contain certain keywords
        """
        return [url for url in urls if all(keyword not in url for keyword in keywords)]

    @staticmethod
    def _substack_reader(soup: BeautifulSoup) -> Tuple[str, Dict[str, str]]:
        metadata = {
            "Title of this Substack post": soup.select_one("h1.post-title").text,
            "Subtitle": soup.select_one("h3.subtitle").text,
            "Author": soup.select_one("a.navbar-title-link").text,
        }
        text = soup.select_one("div.available-content").getText()
        return text, metadata

    def extract_post_content(self, url: str):
        try:
            page = requests.get(url, headers=None)
            text = html2text.html2text(page.text)  # This prints it more nicely than bs4
            soup = BeautifulSoup(page.content, "html.parser")
            metadata = {"URL": url}
            _, metadata = self._substack_reader(soup)  # Note: _ here might actually be more parseable than the text above! But just leaving as is for now
            metadata.update(metadata)
            print(text)
            print(metadata)
        except Exception as e:
            raise ValueError(f"Error fetching page: {e}") from e

def main():
    scraper = SubstackScraper('https://ava.substack.com/')
    print(scraper.get_all_posts())
    # scraper.extract_post_content('https://ava.substack.com/making-and-keeping-friends')


if __name__ == "__main__":
    main()
