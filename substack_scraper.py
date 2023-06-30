from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup
import html2text
import requests
from typing import Any, Dict, List, Optional, Tuple


class SubstackScraper:
    def __init__(self, post_url: str):
        self.post_url = post_url

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
    scraper = SubstackScraper('https://ava.substack.com/making-and-keeping-friends')
    scraper.extract_post_content('https://ava.substack.com/making-and-keeping-friends')


if __name__ == "__main__":
    main()
