# Substack2Markdown

Substack2Markdown is a Python tool for scraping free and premium Substack posts and saving them as Markdown 
files. It will save paid for content as long as you're subscribed to that substack. Most "save for later" apps (such 
as Pocket) don't save these posts, but with this script you can!

Once you run the script, it will create a folder named after the substack in the directory you have specified,
and then begin to scrape the substack URL, converting the blog posts into markdown files. You can either hardcode the 
substack URL and the number of posts you'd like to save into the top of the file, or specify them as command line
arguments.

## Installation

Clone the repo and install the dependencies:

```bash
git clone https://github.com/yourusername/substack_scraper.git
cd substack_scraper

# # Optinally create a virtual environment
# python -m venv venv
# # Activate the virtual environment
# .\venv\Scripts\activate  # Windows
# source venv/bin/activate  # Linux

pip install -r requirements.txt
```

For the premium scraper, update the `config.py` in the root directory with your Substack email and password:

```python
EMAIL = "your-email@domain.com"
PASSWORD = "your-password"
```

You'll also need Microsoft Edge installed for the Selenium webdriver.

## Usage

Specify the Substack URL and the directory to save the posts to:

You can hardcode your desired Substack URL and the number of posts you'd like to save into the top of the file and run:
```bash
python substack_scraper.py
```

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
