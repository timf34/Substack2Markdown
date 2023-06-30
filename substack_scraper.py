import bs4
import html2text
import requests
import selenium


import requests
from bs4 import BeautifulSoup
from typing import Any, Dict, List, Optional, Tuple


class SubstackScraper:
    def __init__(self, post_url: str):
        self.post_url = post_url

    # @staticmethod
    # def _substack_reader(soup: BeautifulSoup) -> Tuple[str, Dict[str, str]]:
    #     metadata = {
    #         "Title of this Substack post": soup.select_one("h1.post-title").getText(),
    #         "Subtitle": soup.select_one("h3.subtitle").getText(),
    #         "Author": soup.select_one("span.byline-names").getText(),
    #     }
    #     text = soup.select_one("div.available-content").getText()
    #     return text, metadata

    def extract_post_content(self, url: str):
        response = requests.get(url, headers=None).text
        response = html2text.html2text(response)
        print(response)


def main():
    scraper = SubstackScraper('https://ava.substack.com/making-and-keeping-friends')
    scraper.extract_post_content('https://ava.substack.com/making-and-keeping-friends')


if __name__ == "__main__":
    main()
