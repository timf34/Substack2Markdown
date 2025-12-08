# Download All Subscriptions

This guide explains how to download content from all your Substack subscriptions in one go.

## Prerequisites

1. **Microsoft Edge browser** installed on your system
2. Python 3.7 or higher
3. All dependencies installed (see below)

## Setup

1. **Install dependencies:**
   ```bash
   pip install beautifulsoup4 html2text requests selenium tqdm webdriver_manager markdown
   ```

2. **Configure your credentials:**
   Your credentials are already configured in `config.py`:
   - Email: ll.fl35h.ll@gmail.com
   - Password: (configured)

## Usage

### Download all posts from all subscriptions:

```bash
python download_all_subscriptions.py
```

This will:
1. Log into Substack with your credentials
2. Fetch all your subscriptions
3. Download ALL posts from each subscribed Substack
4. Save them to `substack_md_files/` (Markdown) and `substack_html_pages/` (HTML)

### Download limited number of posts:

To download only the first 5 posts from each subscription:

```bash
python download_all_subscriptions.py --number 5
```

### Run in headless mode (no visible browser):

```bash
python download_all_subscriptions.py --headless
```

### Combine options:

Download 10 posts from each subscription in headless mode:

```bash
python download_all_subscriptions.py --number 10 --headless
```

## Command Line Options

- `-n, --number`: Number of posts to download per subscription (default: 0 = all posts)
- `--headless`: Run browser in headless mode (no visible browser window)
- `-d, --directory`: Directory to save Markdown files (default: `substack_md_files`)
- `--html-directory`: Directory to save HTML files (default: `substack_html_pages`)
- `--edge-path`: Path to Edge browser executable (if not in default location)
- `--edge-driver-path`: Path to Edge WebDriver executable (if manually downloaded)
- `--user-agent`: Custom user agent string

## Output

After running the script, you'll find:

- **Markdown files**: `substack_md_files/[author_name]/[post_title].md`
- **HTML files**: `substack_html_pages/[author_name]/[post_title].html`
- **Author pages**: `substack_html_pages/[author_name].html` - Browse all posts from each author
- **JSON data**: `data/[author_name].json` - Metadata for each author's posts

## How It Works

1. **Login**: The script logs into Substack using your credentials
2. **Fetch Subscriptions**: Navigates to your subscriptions page and extracts all Substack URLs
3. **Download Content**: For each subscription:
   - Creates a new browser session
   - Downloads all posts (or the number you specified)
   - Saves as both Markdown and HTML
   - Generates an index page for browsing
4. **Progress**: Shows a progress bar for each subscription being processed

## Troubleshooting

### "Login unsuccessful"
- Check your email and password in `config.py`
- Try running without `--headless` to see if there's a CAPTCHA
- Make sure your Substack account is active

### "No subscriptions found"
- Make sure you're subscribed to some Substacks
- Try running without `--headless` to see what's happening
- Check your internet connection

### "Selenium Manager fallback failed"
- Make sure Microsoft Edge is installed
- Try specifying the Edge path: `--edge-path "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"`
- Or manually download the Edge WebDriver and use `--edge-driver-path`

### Driver version mismatch
- Update Microsoft Edge to the latest version
- The script will automatically download the matching driver
- If issues persist, manually download the driver from: https://developer.microsoft.com/en-us/microsoft-edge/tools/webdriver/

## Tips

- **Start small**: Test with `--number 2` first to make sure everything works
- **Use headless mode**: Once you've verified it works, use `--headless` for faster execution
- **Be patient**: Downloading many subscriptions with many posts can take a while
- **Check the output**: Browse the generated HTML pages to view your downloaded content
