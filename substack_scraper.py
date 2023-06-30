import bs4
import requests
import selenium


import requests
from bs4 import BeautifulSoup
from typing import Any, Dict, List, Optional, Tuple


class SubstackScraper:
    def __init__(self, base_url: str):
        self.base_url = base_url

    def get_all_posts(self, max_posts: Optional[int] = None) -> List[str]:
        response = requests.get(self.base_url)
        soup = BeautifulSoup(response.text, 'html.parser')

        post_elements = soup.find_all('a', class_='post-link')

        post_urls = [self.base_url + el['href'] for el in post_elements]

        if max_posts is not None:
            return post_urls[:max_posts]
        else:
            return post_urls

    @staticmethod
    def _substack_reader(soup: BeautifulSoup) -> Tuple[str, Dict[str, str]]:
        metadata = {
            "Title of this Substack post": soup.select_one("h1.post-title").getText(),
            "Subtitle": soup.select_one("h3.subtitle").getText(),
            "Author": soup.select_one("span.byline-names").getText(),
        }
        text = soup.select_one("div.available-content").getText()
        return text, metadata

    def extract_post_content(self, post_url: str) -> Tuple[str, Dict[str, str]]:
        response = requests.get(post_url)
        soup = BeautifulSoup(response.text, 'html.parser')
        return self._substack_reader(soup)


def main():
    scraper = SubstackScraper('https://ava.substack.com/archive')

    post_urls = scraper.get_all_posts(max_posts=1)

    for url in post_urls:
        content, metadata = scraper.extract_post_content(url)
        print(content)
        print(metadata)

    print(scraper.extract_post_content("https://ava.substack.com/p/making-and-keeping-friends"))


if __name__ == "__main__":
    main()
