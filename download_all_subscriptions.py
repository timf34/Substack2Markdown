#!/usr/bin/env python3
"""
Script to download content from all Substack subscriptions.
This script logs into Substack, fetches all subscribed publications,
and downloads all content from each one.
"""

import argparse
from time import sleep
from typing import List
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import SessionNotCreatedException
from webdriver_manager.microsoft import EdgeChromiumDriverManager
from bs4 import BeautifulSoup
import os

from substack_scraper import PremiumSubstackScraper, BASE_MD_DIR, BASE_HTML_DIR
from config import EMAIL, PASSWORD


class SubscriptionFetcher:
    """Class to fetch all Substack subscriptions for a user."""

    def __init__(self, headless: bool = False, edge_path: str = '',
                 edge_driver_path: str = '', user_agent: str = ''):
        """Initialize the subscription fetcher with browser options."""
        options = EdgeOptions()
        if headless:
            options.add_argument("--headless=new")
        if edge_path:
            options.binary_location = edge_path
        if user_agent:
            options.add_argument(f"user-agent={user_agent}")

        if isinstance(options, EdgeOptions):
            os.environ.setdefault("SE_DRIVER_MIRROR_URL", "https://msedgedriver.microsoft.com")

        self.driver = None

        # Initialize driver (same logic as PremiumSubstackScraper)
        if edge_driver_path and os.path.exists(edge_driver_path):
            service = Service(executable_path=edge_driver_path)
            self.driver = webdriver.Edge(service=service, options=options)
        else:
            try:
                service = Service(EdgeChromiumDriverManager().install())
                self.driver = webdriver.Edge(service=service, options=options)
            except Exception as e:
                print("webdriver_manager could not download msedgedriver. Falling back to Selenium Manager.")
                try:
                    self.driver = webdriver.Edge(options=options)
                except SessionNotCreatedException as se:
                    raise RuntimeError(
                        "Selenium Manager fallback failed due to driver/browser mismatch.\n"
                        "Fix by either: (a) removing stale msedgedriver in PATH, "
                        "or (b) pass --edge-driver-path to a manually downloaded driver."
                    ) from se

    def login(self) -> None:
        """Log into Substack using the credentials from config.py."""
        print("Logging into Substack...")
        self.driver.get("https://substack.com/sign-in")
        sleep(3)

        try:
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

            # Find the submit button and click it
            submit = self.driver.find_element(By.XPATH, "//*[@id=\"substack-login\"]/div[2]/div[2]/form/button")
            submit.click()
            sleep(30)  # Wait for the page to load

            if self.is_login_failed():
                raise Exception(
                    "Warning: Login unsuccessful. Please check your email and password, or your account status."
                )

            print("Successfully logged in!")
        except Exception as e:
            raise Exception(f"Login failed: {e}")

    def is_login_failed(self) -> bool:
        """Check for the presence of the 'error-container' to indicate a failed login attempt."""
        error_container = self.driver.find_elements(By.ID, 'error-container')
        return len(error_container) > 0 and error_container[0].is_displayed()

    def get_subscriptions(self) -> List[str]:
        """
        Navigate to the subscriptions page and extract all Substack URLs.
        Returns a list of subscription URLs.
        """
        print("Fetching subscriptions...")

        # Navigate to the subscriptions page
        self.driver.get("https://substack.com/subscriptions")
        sleep(5)  # Wait for the page to load

        # Get page source and parse with BeautifulSoup
        soup = BeautifulSoup(self.driver.page_source, "html.parser")

        subscription_urls = []

        # Try to find subscription links
        # Substack subscriptions page typically has links to publications
        # We'll look for various patterns that might contain the publication URLs

        # Method 1: Look for links in the subscriptions list
        links = soup.find_all('a', href=True)

        for link in links:
            href = link['href']
            # Filter for Substack URLs (substack.com domains)
            if '.substack.com' in href and href.startswith('http'):
                # Extract the base URL (e.g., https://example.substack.com)
                if '/p/' not in href and '/archive' not in href:
                    # Get base domain
                    base_url = '/'.join(href.split('/')[:3]) + '/'
                    if base_url not in subscription_urls:
                        subscription_urls.append(base_url)

        # Method 2: Try to find subscription elements more specifically
        # Look for elements that might contain publication info
        subscription_items = soup.find_all(['div', 'article'], class_=lambda x: x and 'subscription' in x.lower())

        for item in subscription_items:
            item_links = item.find_all('a', href=True)
            for link in item_links:
                href = link['href']
                if '.substack.com' in href and href.startswith('http'):
                    base_url = '/'.join(href.split('/')[:3]) + '/'
                    if base_url not in subscription_urls:
                        subscription_urls.append(base_url)

        # Remove duplicates and filter
        subscription_urls = list(set(subscription_urls))

        # Filter out the main substack.com domain
        subscription_urls = [url for url in subscription_urls if url != 'https://substack.com/']

        print(f"Found {len(subscription_urls)} subscriptions:")
        for url in subscription_urls:
            print(f"  - {url}")

        return subscription_urls

    def close(self):
        """Close the browser."""
        if self.driver:
            self.driver.quit()


def download_all_subscriptions(
    headless: bool = False,
    edge_path: str = '',
    edge_driver_path: str = '',
    user_agent: str = '',
    md_save_dir: str = BASE_MD_DIR,
    html_save_dir: str = BASE_HTML_DIR,
    num_posts: int = 0
):
    """
    Main function to download content from all subscriptions.

    Args:
        headless: Run browser in headless mode
        edge_path: Path to Edge browser executable
        edge_driver_path: Path to Edge WebDriver executable
        user_agent: Custom user agent string
        md_save_dir: Directory to save Markdown files
        html_save_dir: Directory to save HTML files
        num_posts: Number of posts to scrape per subscription (0 for all)
    """
    fetcher = None

    try:
        # First, log in and get all subscriptions
        fetcher = SubscriptionFetcher(
            headless=headless,
            edge_path=edge_path,
            edge_driver_path=edge_driver_path,
            user_agent=user_agent
        )

        fetcher.login()
        subscription_urls = fetcher.get_subscriptions()

        if not subscription_urls:
            print("No subscriptions found. Make sure you're subscribed to some Substacks.")
            return

        # Close the fetcher browser
        fetcher.close()

        # Now scrape each subscription
        print(f"\nStarting to download content from {len(subscription_urls)} subscriptions...")

        for idx, url in enumerate(subscription_urls, 1):
            print(f"\n{'='*80}")
            print(f"Processing {idx}/{len(subscription_urls)}: {url}")
            print(f"{'='*80}")

            try:
                scraper = PremiumSubstackScraper(
                    base_substack_url=url,
                    md_save_dir=md_save_dir,
                    html_save_dir=html_save_dir,
                    headless=headless,
                    edge_path=edge_path,
                    edge_driver_path=edge_driver_path,
                    user_agent=user_agent
                )

                scraper.scrape_posts(num_posts_to_scrape=num_posts)

                # Close the scraper's driver
                if scraper.driver:
                    scraper.driver.quit()

                print(f"✓ Successfully downloaded content from {url}")

            except Exception as e:
                print(f"✗ Error downloading from {url}: {e}")
                continue

        print(f"\n{'='*80}")
        print("All subscriptions processed!")
        print(f"{'='*80}")

    except Exception as e:
        print(f"Error: {e}")
    finally:
        if fetcher and fetcher.driver:
            fetcher.close()


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Download content from all your Substack subscriptions."
    )
    parser.add_argument(
        "-n",
        "--number",
        type=int,
        default=0,
        help="The number of posts to scrape per subscription. If 0 (default), all posts will be scraped.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode.",
    )
    parser.add_argument(
        "--edge-path",
        type=str,
        default="",
        help='Optional: The path to the Edge browser executable.',
    )
    parser.add_argument(
        "--edge-driver-path",
        type=str,
        default="",
        help='Optional: The path to the Edge WebDriver executable.',
    )
    parser.add_argument(
        "--user-agent",
        type=str,
        default="",
        help="Optional: Specify a custom user agent for browser automation.",
    )
    parser.add_argument(
        "-d",
        "--directory",
        type=str,
        default=BASE_MD_DIR,
        help="The directory to save scraped Markdown posts.",
    )
    parser.add_argument(
        "--html-directory",
        type=str,
        default=BASE_HTML_DIR,
        help="The directory to save scraped HTML posts.",
    )

    return parser.parse_args()


def main():
    """Main entry point."""
    args = parse_args()

    download_all_subscriptions(
        headless=args.headless,
        edge_path=args.edge_path,
        edge_driver_path=args.edge_driver_path,
        user_agent=args.user_agent,
        md_save_dir=args.directory,
        html_save_dir=args.html_directory,
        num_posts=args.number
    )


if __name__ == "__main__":
    main()
