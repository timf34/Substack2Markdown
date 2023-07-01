from bs4 import BeautifulSoup
import html2text
import os

f_path: str = "data/ava/taste.html"

with open(f_path, "r", encoding="utf-8") as f:
    html = f.read()

soup = BeautifulSoup(html, "html.parser")
div_content = str(soup.find('div', {'class': 'available-content'}))


h = html2text.HTML2Text()
h.ignore_links = False
h.body_width = 0  # Disable line wrapping
md = h.handle(div_content)

print(md)

with open("test.md", 'w', encoding='utf-8') as f:
    f.write(md)
