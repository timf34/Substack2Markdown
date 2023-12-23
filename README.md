# SubstackScraper

Substack Scraper is a Python tool for scraping free and premium Substack posts and saving them as Markdown 
files. It will save paid for content as long as you're subscribed to that substack. Most "save for later" apps (such 
as Pocket) don't save these posts, but with this script you can!

## TODO

- [ ] Write instructions for using with venv & ensure requirments.txt is good
- [ ] Build CLI interface/ make pip installable for easier usage

## Installation

Clone the repo and install the dependencies:

```bash
git clone https://github.com/yourusername/substack_scraper.git
cd substack_scraper
pip install -r requirements.txt
```

For the premium scraper, create a `config.py` in the root directory with your Substack email and password:

```python
EMAIL = "your-email@domain.com"
PASSWORD = "yourpassword"
```

You'll also need Microsoft Edge installed for the Selenium webdriver.

## Usage

Specify the Substack URL and the directory to save the posts to:


For free Substack sites:

```bash
python substack_scraper.py --url https://example.substack.com --directory /path/to/save/posts
```

For premium Substack sites:

```bash
python substack_scraper.py --url https://example.substack.com --directory /path/to/save/posts --premium
```

To scrape a specific number of posts:

```bash
python substack_scraper.py --url https://example.substack.com --directory /path/to/save/posts --number 5
```
