import argparse
import hashlib
import json
import mimetypes
import os
import random
import re
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from urllib.parse import unquote, urlparse
from typing import List, Optional, Tuple
from time import sleep

import html2text
import markdown
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from tqdm import tqdm
from xml.etree import ElementTree as ET

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import SessionNotCreatedException, TimeoutException, WebDriverException

from config import EMAIL, PASSWORD

USE_PREMIUM: bool = True
BASE_SUBSTACK_URL: str = "https://niallferguson.substack.com/"
BASE_MD_DIR: str = "substack_md_files"
BASE_HTML_DIR: str = "substack_html_pages"
BASE_IMAGE_DIR: str = "substack_images"
HTML_TEMPLATE: str = "author_template.html"
JSON_DATA_DIR: str = "data"
NUM_POSTS_TO_SCRAPE: int = 0


def resolve_image_url(url: str) -> str:
    """Get the original image URL from a Substack CDN URL."""
    if url.startswith("https://substackcdn.com/image/fetch/"):
        parts = url.split("/https%3A%2F%2F")
        if len(parts) > 1:
            url = "https://" + unquote(parts[1])
    return url


def clean_linked_images(md_content: str) -> str:
    """Converts markdown linked images [![alt](img)](link) to ![alt](img)."""
    pattern = r'\[!\[(.*?)\]\((.*?)\)\]\(.*?\)'
    return re.sub(pattern, r'![\1](\2)', md_content)


def count_images_in_markdown(md_content: str) -> int:
    """Count number of image references in markdown content."""
    cleaned_content = clean_linked_images(md_content)
    pattern = r'!\[.*?\]\((.*?)\)'
    matches = re.findall(pattern, cleaned_content)
    return len(matches)


def is_post_url(url: str) -> bool:
    """Check if URL points to a specific post (contains /p/)."""
    return "/p/" in url


def get_publication_url(url: str) -> str:
    """Extract the base publication URL from a post URL."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/"


def get_post_slug(url: str) -> str:
    """Extract the post slug from a Substack post URL."""
    match = re.search(r'/p/([^/]+)', url)
    return match.group(1) if match else 'unknown_post'


def sanitize_image_filename(url: str) -> str:
    """Create a safe filename from an image URL."""
    url = resolve_image_url(url)
    filename = url.split("/")[-1]
    filename = filename.split("?")[0]
    filename = re.sub(r'[<>:"/\\|?*]', '', filename)

    if len(filename) > 100 or not filename:
        hash_object = hashlib.md5(url.encode())
        ext = mimetypes.guess_extension(
            requests.head(url).headers.get('content-type', '')
        ) or '.jpg'
        filename = f"{hash_object.hexdigest()}{ext}"

    return filename


def download_image(url: str, save_path: Path, pbar=None) -> Optional[str]:
    """Download image from URL and save to path."""
    try:
        response = requests.get(url, stream=True)
        if response.status_code == 200:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            with open(save_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            if pbar:
                pbar.update(1)
            return str(save_path)
    except Exception as e:
        msg = f"Error downloading image {url}: {str(e)}"
        if pbar:
            pbar.write(msg)
        else:
            print(msg)
    return None


def process_markdown_images(md_content: str, author: str, post_slug: str, pbar=None) -> str:
    """Process markdown content to download images and update references."""
    image_dir = Path(BASE_IMAGE_DIR) / author / post_slug
    md_content = clean_linked_images(md_content)

    def replace_image(match):
        url = match.group(0).strip('()')
        resolved_url = resolve_image_url(url)
        filename = sanitize_image_filename(url)
        save_path = image_dir / filename
        if not save_path.exists():
            download_image(resolved_url, save_path, pbar)

        rel_path = os.path.relpath(save_path, Path(BASE_MD_DIR) / author)
        return f"({rel_path})"

    pattern = r'\(https://substackcdn\.com/image/fetch/[^\s\)]+\)'
    return re.sub(pattern, replace_image, md_content)


def extract_main_part(url: str) -> str:
    parts = urlparse(url).netloc.split('.')
    return parts[1] if parts[0] == 'www' else parts[0]


def generate_html_file(author_name: str) -> None:
    """Generates a HTML file for the given author."""
    if not os.path.exists(BASE_HTML_DIR):
        os.makedirs(BASE_HTML_DIR)

    json_path = os.path.join(JSON_DATA_DIR, f'{author_name}.json')
    with open(json_path, 'r', encoding='utf-8') as file:
        essays_data = json.load(file)

    embedded_json_data = json.dumps(essays_data, ensure_ascii=False, indent=4)

    with open(HTML_TEMPLATE, 'r', encoding='utf-8') as file:
        html_template = file.read()

    html_with_data = html_template.replace('<!-- AUTHOR_NAME -->', author_name).replace(
        '<script type="application/json" id="essaysData"></script>',
        f'<script type="application/json" id="essaysData">{embedded_json_data}</script>'
    )
    html_with_author = html_with_data.replace('author_name', author_name)

    html_output_path = os.path.join(BASE_HTML_DIR, f'{author_name}.html')
    with open(html_output_path, 'w', encoding='utf-8') as file:
        file.write(html_with_author)


# =============================================================================
# BROWSER/DRIVER UTILITIES
# =============================================================================

class BrowserManager:
    """
    Handles browser detection, driver management, and initialization.
    Supports Chrome (preferred) and Edge with robust fallback logic.
    
    Key insight: Instead of trying to move/delete system drivers (requires admin),
    we download to a local cache and use explicit paths, bypassing PATH entirely.
    """
    
    SUPPORTED_BROWSERS = ['chrome', 'edge']
    CACHE_DIR = os.path.join(os.path.expanduser('~'), '.substack_scraper', 'drivers')
    
    @classmethod
    def get_cache_dir(cls) -> str:
        """Get or create the local driver cache directory."""
        if not os.path.exists(cls.CACHE_DIR):
            os.makedirs(cls.CACHE_DIR)
        return cls.CACHE_DIR
    
    @staticmethod
    def get_browser_version(browser: str) -> Optional[str]:
        """
        Attempts to detect the installed browser version.
        Returns version string or None if not found.
        """
        version = None
        
        if browser == 'chrome':
            if os.name == 'nt':  # Windows
                paths = [
                    r'C:\Program Files\Google\Chrome\Application\chrome.exe',
                    r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
                    os.path.expandvars(r'%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe'),
                ]
                for path in paths:
                    if os.path.exists(path):
                        try:
                            result = subprocess.run(
                                ['powershell', '-Command', f'(Get-Item "{path}").VersionInfo.FileVersion'],
                                capture_output=True, text=True, timeout=10
                            )
                            if result.returncode == 0:
                                version = result.stdout.strip()
                                break
                        except Exception:
                            pass
            else:  # macOS/Linux
                try:
                    result = subprocess.run(
                        ['google-chrome', '--version'], 
                        capture_output=True, text=True, timeout=10
                    )
                    if result.returncode == 0:
                        match = re.search(r'(\d+\.\d+\.\d+\.\d+)', result.stdout)
                        if match:
                            version = match.group(1)
                except Exception:
                    pass
                    
        elif browser == 'edge':
            if os.name == 'nt':  # Windows
                paths = [
                    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
                    r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
                ]
                for path in paths:
                    if os.path.exists(path):
                        try:
                            result = subprocess.run(
                                ['powershell', '-Command', f'(Get-Item "{path}").VersionInfo.FileVersion'],
                                capture_output=True, text=True, timeout=10
                            )
                            if result.returncode == 0:
                                version = result.stdout.strip()
                                break
                        except Exception:
                            pass
            else:  # macOS/Linux
                try:
                    result = subprocess.run(
                        ['microsoft-edge', '--version'], 
                        capture_output=True, text=True, timeout=10
                    )
                    if result.returncode == 0:
                        match = re.search(r'(\d+\.\d+\.\d+\.\d+)', result.stdout)
                        if match:
                            version = match.group(1)
                except Exception:
                    pass
        
        return version
    
    @staticmethod
    def get_driver_version(driver_path: str) -> Optional[str]:
        """Get the version of a webdriver executable."""
        if not os.path.exists(driver_path):
            return None
        try:
            result = subprocess.run(
                [driver_path, '--version'],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                match = re.search(r'(\d+\.\d+\.\d+\.\d+)', result.stdout)
                if match:
                    return match.group(1)
        except Exception:
            pass
        return None
    
    @staticmethod
    def versions_compatible(browser_version: str, driver_version: str) -> bool:
        """Check if browser and driver major versions match."""
        if not browser_version or not driver_version:
            return False
        try:
            browser_major = int(browser_version.split('.')[0])
            driver_major = int(driver_version.split('.')[0])
            return browser_major == driver_major
        except (ValueError, IndexError):
            return False
    
    @staticmethod
    def find_stale_drivers() -> List[str]:
        """Find potentially stale driver executables in common PATH locations."""
        stale_paths = []
        common_locations = [
            r'C:\Windows\msedgedriver.exe',
            r'C:\Windows\chromedriver.exe',
            r'C:\Windows\System32\msedgedriver.exe',
            r'C:\Windows\System32\chromedriver.exe',
        ]
        for path in common_locations:
            if os.path.exists(path):
                stale_paths.append(path)
        return stale_paths
    
    @staticmethod
    def get_user_data_dir(browser: str) -> str:
        """Returns a custom user data directory for browser session persistence."""
        base_dir = os.path.join(os.path.expanduser('~'), '.substack_scraper')
        if not os.path.exists(base_dir):
            os.makedirs(base_dir)
        return os.path.join(base_dir, f'{browser}_profile')
    
    @classmethod
    def download_driver_with_requests(cls, browser: str, browser_version: str) -> Optional[str]:
        """
        Download the correct driver directly using requests.
        This bypasses webdriver_manager issues and gives us full control.
        Returns the path to the downloaded driver, or None if failed.
        """
        import zipfile
        import io
        
        major_version = browser_version.split('.')[0]
        cache_dir = cls.get_cache_dir()
        
        if browser == 'chrome':
            # Chrome for Testing JSON endpoint
            driver_name = 'chromedriver.exe' if os.name == 'nt' else 'chromedriver'
            driver_path = os.path.join(cache_dir, f'chromedriver-{major_version}', driver_name)
            
            # Check if we already have a compatible driver cached
            if os.path.exists(driver_path):
                cached_version = cls.get_driver_version(driver_path)
                if cached_version and cls.versions_compatible(browser_version, cached_version):
                    print(f"Using cached chromedriver {cached_version}")
                    return driver_path
            
            try:
                # Get the latest driver version for this Chrome version
                print(f"Fetching Chrome driver info for version {major_version}...")
                
                # Try the Chrome for Testing endpoints
                endpoints = [
                    f"https://googlechromelabs.github.io/chrome-for-testing/LATEST_RELEASE_{major_version}",
                    "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json",
                ]
                
                driver_version = None
                download_url = None
                
                # Try LATEST_RELEASE endpoint first
                try:
                    resp = requests.get(endpoints[0], timeout=30)
                    if resp.ok:
                        driver_version = resp.text.strip()
                        # Construct download URL
                        platform = 'win64' if os.name == 'nt' else ('mac-x64' if sys.platform == 'darwin' else 'linux64')
                        download_url = f"https://storage.googleapis.com/chrome-for-testing-public/{driver_version}/{platform}/chromedriver-{platform}.zip"
                except Exception:
                    pass
                
                # Fallback to JSON endpoint
                if not download_url:
                    resp = requests.get(endpoints[1], timeout=30)
                    if resp.ok:
                        data = resp.json()
                        channels = data.get('channels', {})
                        stable = channels.get('Stable', {})
                        driver_version = stable.get('version', '')
                        
                        if driver_version.startswith(major_version):
                            downloads = stable.get('downloads', {}).get('chromedriver', [])
                            platform = 'win64' if os.name == 'nt' else ('mac-x64' if sys.platform == 'darwin' else 'linux64')
                            for d in downloads:
                                if d.get('platform') == platform:
                                    download_url = d.get('url')
                                    break
                
                if not download_url:
                    print(f"Could not find chromedriver download URL for Chrome {major_version}")
                    return None
                
                print(f"Downloading chromedriver {driver_version}...")
                resp = requests.get(download_url, timeout=120)
                if not resp.ok:
                    print(f"Download failed: HTTP {resp.status_code}")
                    return None
                
                # Extract the driver
                extract_dir = os.path.join(cache_dir, f'chromedriver-{major_version}')
                if os.path.exists(extract_dir):
                    shutil.rmtree(extract_dir)
                os.makedirs(extract_dir)
                
                with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                    # Find the chromedriver executable in the zip
                    for name in zf.namelist():
                        if name.endswith(driver_name):
                            # Extract to our directory
                            source = zf.open(name)
                            target_path = os.path.join(extract_dir, driver_name)
                            with open(target_path, 'wb') as target:
                                target.write(source.read())
                            # Make executable on Unix
                            if os.name != 'nt':
                                os.chmod(target_path, 0o755)
                            print(f"[OK] Chromedriver downloaded to: {target_path}")
                            return target_path
                
                print("Could not find chromedriver in downloaded archive")
                return None
                
            except Exception as e:
                print(f"Failed to download chromedriver: {e}")
                return None
                
        elif browser == 'edge':
            driver_name = 'msedgedriver.exe' if os.name == 'nt' else 'msedgedriver'
            driver_path = os.path.join(cache_dir, f'msedgedriver-{major_version}', driver_name)
            
            # Check cache
            if os.path.exists(driver_path):
                cached_version = cls.get_driver_version(driver_path)
                if cached_version and cls.versions_compatible(browser_version, cached_version):
                    print(f"Using cached msedgedriver {cached_version}")
                    return driver_path
            
            try:
                # Get latest Edge driver version
                print(f"Fetching Edge driver info for version {major_version}...")
                
                # Edge driver download URL pattern
                platform = 'win64' if os.name == 'nt' else ('mac64' if sys.platform == 'darwin' else 'linux64')
                
                # Try to get the exact version
                version_url = f"https://msedgedriver.azureedge.net/LATEST_RELEASE_{major_version}"
                try:
                    resp = requests.get(version_url, timeout=30)
                    if resp.ok:
                        driver_version = resp.text.strip()
                    else:
                        driver_version = browser_version  # Fall back to browser version
                except Exception:
                    driver_version = browser_version
                
                download_url = f"https://msedgedriver.azureedge.net/{driver_version}/edgedriver_{platform}.zip"
                
                print(f"Downloading msedgedriver {driver_version}...")
                resp = requests.get(download_url, timeout=120)
                if not resp.ok:
                    print(f"Download failed: HTTP {resp.status_code}")
                    return None
                
                # Extract
                extract_dir = os.path.join(cache_dir, f'msedgedriver-{major_version}')
                if os.path.exists(extract_dir):
                    shutil.rmtree(extract_dir)
                os.makedirs(extract_dir)
                
                with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                    for name in zf.namelist():
                        if name.endswith(driver_name):
                            source = zf.open(name)
                            target_path = os.path.join(extract_dir, driver_name)
                            with open(target_path, 'wb') as target:
                                target.write(source.read())
                            if os.name != 'nt':
                                os.chmod(target_path, 0o755)
                            print(f"[OK] msedgedriver downloaded to: {target_path}")
                            return target_path
                
                print("Could not find msedgedriver in downloaded archive")
                return None
                
            except Exception as e:
                print(f"Failed to download msedgedriver: {e}")
                return None
        
        return None

    @classmethod
    def create_driver(
        cls,
        browser: str = 'chrome',
        headless: bool = False,
        driver_path: Optional[str] = None,
        browser_path: Optional[str] = None,
        user_agent: Optional[str] = None,
        use_persistent_profile: bool = False,
    ) -> webdriver.Remote:
        """
        Creates a WebDriver instance with smart fallback logic.
        
        Strategy:
        1. Use explicit driver path if provided
        2. Check our local cache for a compatible driver
        3. Download driver directly to our cache (bypasses PATH issues)
        4. Fall back to webdriver_manager
        5. Fall back to Selenium Manager
        """
        browser = browser.lower()
        if browser not in cls.SUPPORTED_BROWSERS:
            raise ValueError(f"Unsupported browser: {browser}. Use one of: {cls.SUPPORTED_BROWSERS}")
        
        # Check for stale drivers (for warning purposes only)
        stale_drivers = cls.find_stale_drivers()
        if stale_drivers:
            print(f"WARNING: Found old drivers in system PATH that may cause issues if other methods fail:")
            for p in stale_drivers:
                v = cls.get_driver_version(p) or "unknown"
                print(f"   - {p} (version: {v})")
            print("   We'll try to bypass these by using our own driver cache.\n")
        
        # Detect browser version
        browser_version = cls.get_browser_version(browser)
        print(f"Detected {browser.title()} version: {browser_version or 'unknown'}")
        
        if not browser_version:
            print(f"WARNING: Could not detect {browser.title()} version. Make sure it's installed.")
        
        # Build options
        if browser == 'chrome':
            options = ChromeOptions()
        else:
            options = EdgeOptions()
            
        if headless:
            options.add_argument("--headless=new")
        
        if browser_path:
            options.binary_location = browser_path
            
        if user_agent:
            options.add_argument(f"user-agent={user_agent}")
        
        if use_persistent_profile:
            profile_dir = cls.get_user_data_dir(browser)
            options.add_argument(f"user-data-dir={profile_dir}")
            print(f"Using persistent profile at: {profile_dir}")
        
        # Common options for stability
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        
        errors = []
        
        # Strategy 1: Explicit driver path
        if driver_path and os.path.exists(driver_path):
            try:
                print(f"Using explicit driver path: {driver_path}")
                driver_version = cls.get_driver_version(driver_path)
                if driver_version:
                    print(f"Driver version: {driver_version}")
                    if browser_version and not cls.versions_compatible(browser_version, driver_version):
                        print(f"WARNING: Driver version may not match browser version")
                
                if browser == 'chrome':
                    service = ChromeService(executable_path=driver_path)
                    return webdriver.Chrome(service=service, options=options)
                else:
                    service = EdgeService(executable_path=driver_path)
                    return webdriver.Edge(service=service, options=options)
            except Exception as e:
                errors.append(f"Explicit driver path failed: {e}")
                print(f"[FAIL] Explicit driver path failed: {e}")
        
        # Strategy 2: Download to our cache (primary method - bypasses PATH issues)
        if browser_version:
            print(f"\nDownloading driver to local cache (bypasses system PATH)...")
            try:
                downloaded_path = cls.download_driver_with_requests(browser, browser_version)
                if downloaded_path and os.path.exists(downloaded_path):
                    print(f"Using downloaded driver: {downloaded_path}")
                    if browser == 'chrome':
                        service = ChromeService(executable_path=downloaded_path)
                        return webdriver.Chrome(service=service, options=options)
                    else:
                        service = EdgeService(executable_path=downloaded_path)
                        return webdriver.Edge(service=service, options=options)
            except Exception as e:
                errors.append(f"Direct download failed: {e}")
                print(f"[FAIL] Direct download failed: {e}")
        
        # Strategy 3: webdriver_manager with explicit path
        print("\nTrying webdriver_manager...")
        try:
            if browser == 'chrome':
                from webdriver_manager.chrome import ChromeDriverManager
                from webdriver_manager.core.os_manager import ChromeType
                mgr = ChromeDriverManager()
                driver_path_wdm = mgr.install()
                print(f"webdriver_manager installed driver to: {driver_path_wdm}")
                service = ChromeService(executable_path=driver_path_wdm)
                return webdriver.Chrome(service=service, options=options)
            else:
                from webdriver_manager.microsoft import EdgeChromiumDriverManager
                mgr = EdgeChromiumDriverManager()
                driver_path_wdm = mgr.install()
                print(f"webdriver_manager installed driver to: {driver_path_wdm}")
                service = EdgeService(executable_path=driver_path_wdm)
                return webdriver.Edge(service=service, options=options)
        except Exception as e:
            errors.append(f"webdriver_manager failed: {e}")
            print(f"[FAIL] webdriver_manager failed: {e}")
        
        # Strategy 4: Let Selenium Manager try (last resort)
        print("\nTrying Selenium Manager (last resort)...")
        try:
            if browser == 'chrome':
                return webdriver.Chrome(options=options)
            else:
                return webdriver.Edge(options=options)
        except Exception as e:
            errors.append(f"Selenium Manager failed: {e}")
            print(f"[FAIL] Selenium Manager failed: {e}")
        
        # All strategies failed
        error_msg = cls._build_error_message(browser, browser_version, stale_drivers, errors)
        raise RuntimeError(error_msg)
    
    @classmethod
    def _build_error_message(
        cls, 
        browser: str, 
        browser_version: Optional[str],
        stale_drivers: List[str],
        errors: List[str]
    ) -> str:
        """Build a helpful error message when driver creation fails."""
        
        lines = [
            "",
            "=" * 70,
            "BROWSER DRIVER SETUP FAILED",
            "=" * 70,
            "",
            f"Could not start {browser.title()} WebDriver.",
            "",
        ]
        
        if browser_version:
            lines.append(f"Your {browser.title()} version: {browser_version}")
            major_version = browser_version.split('.')[0]
        else:
            lines.append(f"Could not detect your {browser.title()} version.")
            major_version = "XXX"
        
        lines.append("")
        
        if stale_drivers:
            lines.extend([
                "STALE DRIVERS IN SYSTEM PATH:",
                "These old drivers may have interfered with automatic setup:",
            ])
            for path in stale_drivers:
                driver_ver = cls.get_driver_version(path) or "unknown version"
                lines.append(f"   - {path} (version: {driver_ver})")
            lines.extend([
                "",
                "To fix: Open an Administrator command prompt and delete these files,",
                "or rename them (e.g., chromedriver.exe.bak)",
                "",
            ])
        
        lines.extend([
            "HOW TO FIX:",
            "",
            "Option 1: Download the correct driver manually",
        ])
        
        if browser == 'chrome':
            lines.extend([
                f"   1. Go to: https://googlechromelabs.github.io/chrome-for-testing/",
                f"   2. Download chromedriver for version {major_version} (win64)",
                f"   3. Extract chromedriver.exe somewhere (e.g., C:\\tools\\chromedriver.exe)",
                f"   4. Run with: --chrome-driver-path C:\\tools\\chromedriver.exe",
            ])
        else:
            lines.extend([
                f"   1. Go to: https://developer.microsoft.com/en-us/microsoft-edge/tools/webdriver/",
                f"   2. Download msedgedriver for version {major_version}",
                f"   3. Extract msedgedriver.exe somewhere (e.g., C:\\tools\\msedgedriver.exe)",
                f"   4. Run with: --edge-driver-path C:\\tools\\msedgedriver.exe",
            ])
        
        lines.extend([
            "",
            "Option 2: Try a different browser",
            f"   python substack_scraper.py --premium --browser {'edge' if browser == 'chrome' else 'chrome'}",
            "",
            "Option 3: Delete stale drivers (requires Administrator)",
            "   Open cmd as Administrator and run:",
        ])
        for path in stale_drivers:
            lines.append(f"   del \"{path}\"")
        
        lines.extend([
            "",
            "-" * 70,
            "Debug info (errors encountered):",
        ])
        
        for i, error in enumerate(errors, 1):
            error_short = str(error)[:300] + "..." if len(str(error)) > 300 else str(error)
            lines.append(f"   {i}. {error_short}")
        
        lines.extend(["", "=" * 70])
        
        return "\n".join(lines)


# =============================================================================
# BASE SCRAPER CLASS
# =============================================================================

class BaseSubstackScraper(ABC):
    def __init__(
        self,
        base_substack_url: str,
        md_save_dir: str,
        html_save_dir: str,
        download_images: bool = False,
    ):
        self.is_single_post: bool = is_post_url(base_substack_url)
        self.post_slug: Optional[str] = get_post_slug(base_substack_url) if self.is_single_post else None
        original_url = base_substack_url

        if self.is_single_post:
            base_substack_url = get_publication_url(base_substack_url)

        if not base_substack_url.endswith("/"):
            base_substack_url += "/"
        self.base_substack_url: str = base_substack_url

        self.writer_name: str = extract_main_part(base_substack_url)
        md_save_dir: str = f"{md_save_dir}/{self.writer_name}"

        self.md_save_dir: str = md_save_dir
        self.html_save_dir: str = f"{html_save_dir}/{self.writer_name}"

        if not os.path.exists(md_save_dir):
            os.makedirs(md_save_dir)
            print(f"Created md directory {md_save_dir}")
        if not os.path.exists(self.html_save_dir):
            os.makedirs(self.html_save_dir)
            print(f"Created html directory {self.html_save_dir}")

        self.download_images: bool = download_images
        self.image_dir = Path(BASE_IMAGE_DIR) / self.writer_name

        if self.is_single_post:
            self.post_urls: List[str] = [original_url]
        else:
            self.keywords: List[str] = ["about", "archive", "podcast"]
            self.post_urls: List[str] = self.get_all_post_urls()

    def get_all_post_urls(self) -> List[str]:
        """Attempts to fetch URLs from sitemap.xml, falling back to feed.xml if necessary."""
        urls = self.fetch_urls_from_sitemap()
        if not urls:
            urls = self.fetch_urls_from_feed()
        return self.filter_urls(urls, self.keywords)

    def fetch_urls_from_sitemap(self) -> List[str]:
        """Fetches URLs from sitemap.xml."""
        sitemap_url = f"{self.base_substack_url}sitemap.xml"
        response = requests.get(sitemap_url)

        if not response.ok:
            print(f'Error fetching sitemap at {sitemap_url}: {response.status_code}')
            return []

        root = ET.fromstring(response.content)
        urls = [element.text for element in root.iter('{http://www.sitemaps.org/schemas/sitemap/0.9}loc')]
        return urls

    def fetch_urls_from_feed(self) -> List[str]:
        """Fetches URLs from feed.xml."""
        print('Falling back to feed.xml. This will only contain up to the 22 most recent posts.')
        feed_url = f"{self.base_substack_url}feed.xml"
        response = requests.get(feed_url)

        if not response.ok:
            print(f'Error fetching feed at {feed_url}: {response.status_code}')
            return []

        root = ET.fromstring(response.content)
        urls = []
        for item in root.findall('.//item'):
            link = item.find('link')
            if link is not None and link.text:
                urls.append(link.text)

        return urls

    @staticmethod
    def filter_urls(urls: List[str], keywords: List[str]) -> List[str]:
        """Filters out URLs that contain certain keywords."""
        return [url for url in urls if all(keyword not in url for keyword in keywords)]

    @staticmethod
    def html_to_md(html_content: str) -> str:
        """Converts HTML to Markdown."""
        if not isinstance(html_content, str):
            raise ValueError("html_content must be a string")
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.body_width = 0
        return h.handle(html_content)

    @staticmethod
    def save_to_file(filepath: str, content: str) -> None:
        """Saves content to a file."""
        if not isinstance(filepath, str):
            raise ValueError("filepath must be a string")
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        if os.path.exists(filepath):
            print(f"File already exists: {filepath}")
            return
        with open(filepath, 'w', encoding='utf-8') as file:
            file.write(content)

    @staticmethod
    def md_to_html(md_content: str) -> str:
        """Converts Markdown to HTML."""
        return markdown.markdown(md_content, extensions=['extra'])

    def save_to_html_file(self, filepath: str, content: str) -> None:
        """Saves HTML content to a file with a link to an external CSS file."""
        if not isinstance(filepath, str):
            raise ValueError("filepath must be a string")
        if not isinstance(content, str):
            raise ValueError("content must be a string")

        html_dir = os.path.dirname(filepath)
        css_path = os.path.relpath("./assets/css/essay-styles.css", html_dir)
        css_path = css_path.replace("\\", "/")

        html_content = f"""
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Markdown Content</title>
                <link rel="stylesheet" href="{css_path}">
            </head>
            <body>
                <main class="markdown-content">
                {content}
                </main>
            </body>
            </html>
        """

        with open(filepath, 'w', encoding='utf-8') as file:
            file.write(html_content)

    @staticmethod
    def get_filename_from_url(url: str, filetype: str = ".md") -> str:
        """Gets the filename from the URL."""
        if not isinstance(url, str):
            raise ValueError("url must be a string")
        if not isinstance(filetype, str):
            raise ValueError("filetype must be a string")
        if not filetype.startswith("."):
            filetype = f".{filetype}"
        return url.split("/")[-1] + filetype

    @staticmethod
    def combine_metadata_and_content(title: str, subtitle: str, date: str, author: str, cover_image: str, content) -> str:
        """Combines the title, subtitle, and content into a single string with MDX frontmatter."""
        if not isinstance(title, str):
            raise ValueError("title must be a string")
        if not isinstance(content, str):
            raise ValueError("content must be a string")

        safe_title = title.replace('"', '\\"')
        safe_subtitle = subtitle.replace('"', '\\"') if subtitle else ""
        safe_author = author.replace('"', '\\"') if author else ""

        frontmatter = '---\n'
        frontmatter += f'title: "{safe_title}"\n'
        if safe_subtitle:
            frontmatter += f'subtitle: "{safe_subtitle}"\n'
        frontmatter += f'date: "{date}"\n'
        frontmatter += f'author: "{safe_author}"\n'
        if cover_image:
            frontmatter += f'image: "{cover_image}"\n'
        frontmatter += '---\n\n'

        return frontmatter + content

    def extract_post_data(self, soup: BeautifulSoup, url: str = "") -> Tuple[str, str, str, str, str, str]:
        """Converts a Substack post soup to markdown, returning (title, subtitle, author, date, cover_image, md_content)."""
        # Title
        title_element = soup.select_one("h1.post-title, h2")
        title = title_element.text.strip() if title_element else "Untitled"
        title_found = title_element is not None

        # Subtitle
        subtitle_element = soup.select_one("h3.subtitle, div.subtitle-HEEcLo")
        subtitle = subtitle_element.text.strip() if subtitle_element else ""

        # Date, Author, and Cover Image from ld+json (most reliable source)
        date = ""
        author = ""
        cover_image = ""
        script_tag = soup.find("script", {"type": "application/ld+json"})
        if script_tag and script_tag.string:
            try:
                ld_json = json.loads(script_tag.string)
                if "datePublished" in ld_json:
                    date_str = ld_json["datePublished"]
                    date_obj = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                    date = date_obj.strftime("%Y-%m-%d")
                if "author" in ld_json:
                    authors = ld_json["author"]
                    if isinstance(authors, list) and authors:
                        author = authors[0].get("name", "")
                    elif isinstance(authors, dict):
                        author = authors.get("name", "")
                if "image" in ld_json:
                    images = ld_json["image"]
                    if isinstance(images, list) and images:
                        img = images[0]
                        cover_image = img.get("url", "") if isinstance(img, dict) else str(img)
                    elif isinstance(images, dict):
                        cover_image = images.get("url", "")
            except (json.JSONDecodeError, ValueError, KeyError):
                pass

        if not date:
            date = "Date not found"

        # Content
        content_element = soup.select_one("div.available-content")
        content_html = str(content_element) if content_element else ""
        md = self.html_to_md(content_html)

        # Diagnostic: detect extraction failure (missing title or empty content) and dump page
        if not title_found or not content_element:
            paywall = soup.select_one("h2.paywall-title")
            ld_script = soup.find("script", {"type": "application/ld+json"})
            print(f"[EXTRACT FAIL] url={url}")
            print(f"  title_found={title_found} title={title!r}")
            print(f"  content_element_found={content_element is not None}")
            print(f"  paywall_present={paywall is not None}")
            print(f"  ld_json_present={ld_script is not None}")
            print(f"  date={date!r} author={author!r}")
            try:
                debug_dir = os.path.join(os.path.dirname(self.md_save_dir), "_debug", self.writer_name)
                os.makedirs(debug_dir, exist_ok=True)
                slug = (get_post_slug(url) if url and is_post_url(url) else (url.rstrip('/').split('/')[-1] or "unknown"))
                debug_path = os.path.join(debug_dir, f"{slug}.html")
                with open(debug_path, "w", encoding="utf-8") as f:
                    f.write(str(soup))
                print(f"  dumped raw HTML -> {debug_path}")
            except Exception as dump_err:
                print(f"  failed to dump debug HTML: {dump_err}")

        md_content = self.combine_metadata_and_content(title, subtitle, date, author, cover_image, md)

        return title, subtitle, author, date, cover_image, md_content

    @abstractmethod
    def get_url_soup(self, url: str) -> str:
        raise NotImplementedError

    def save_essays_data_to_json(self, essays_data: list) -> None:
        """Saves essays data to a JSON file for a specific author."""
        data_dir = os.path.join(JSON_DATA_DIR)
        if not os.path.exists(data_dir):
            os.makedirs(data_dir)

        json_path = os.path.join(data_dir, f'{self.writer_name}.json')
        if os.path.exists(json_path):
            with open(json_path, 'r', encoding='utf-8') as file:
                existing_data = json.load(file)
            essays_data = existing_data + [data for data in essays_data if data not in existing_data]
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(essays_data, f, ensure_ascii=False, indent=4)

    def scrape_posts(self, num_posts_to_scrape: int = 0) -> None:
        """Iterates over all posts and saves them as markdown and html files."""
        essays_data = []
        count = 0
        total = num_posts_to_scrape if num_posts_to_scrape != 0 else len(self.post_urls)
        with tqdm(total=total, desc="Scraping posts") as pbar:
            for url in self.post_urls:
                try:
                    md_filename = self.get_filename_from_url(url, filetype=".md")
                    html_filename = self.get_filename_from_url(url, filetype=".html")
                    md_filepath = os.path.join(self.md_save_dir, md_filename)
                    html_filepath = os.path.join(self.html_save_dir, html_filename)

                    if not os.path.exists(md_filepath):
                        soup = self.get_url_soup(url)
                        if soup is None:
                            total += 1
                            pbar.total = total
                            pbar.refresh()
                            continue

                        title, subtitle, author, date, cover_image, md = self.extract_post_data(soup, url)

                        # Skip writing if extraction clearly failed — leaves no stale file so reruns retry.
                        content_element = soup.select_one("div.available-content")
                        if title == "Untitled" or content_element is None:
                            pbar.write(f"[SKIP] Extraction failed for {url} (title={title!r}, content_present={content_element is not None}). See _debug dump.")
                            count += 1
                            pbar.update(1)
                            if num_posts_to_scrape != 0 and count == num_posts_to_scrape:
                                break
                            continue

                        if self.download_images:
                            total_images = count_images_in_markdown(md)
                            slug = get_post_slug(url) if is_post_url(url) else url.rstrip('/').split('/')[-1]
                            with tqdm(
                                total=total_images,
                                desc=f"Downloading images for {slug}",
                                leave=False,
                            ) as img_pbar:
                                md = process_markdown_images(md, self.writer_name, slug, img_pbar)

                        self.save_to_file(md_filepath, md)
                        html_content = self.md_to_html(md)
                        self.save_to_html_file(html_filepath, html_content)

                        essays_data.append({
                            "title": title,
                            "subtitle": subtitle,
                            "author": author,
                            "date": date,
                            "cover_image": cover_image,
                            "file_link": md_filepath,
                            "html_link": html_filepath
                        })
                    else:
                        pbar.write(f"File already exists: {md_filepath}")
                except Exception as e:
                    pbar.write(f"Error scraping post: {e}")

                count += 1
                pbar.update(1)
                if num_posts_to_scrape != 0 and count == num_posts_to_scrape:
                    break
        self.save_essays_data_to_json(essays_data=essays_data)
        generate_html_file(author_name=self.writer_name)


# =============================================================================
# FREE CONTENT SCRAPER
# =============================================================================

class SubstackScraper(BaseSubstackScraper):
    def __init__(
        self,
        base_substack_url: str,
        md_save_dir: str,
        html_save_dir: str,
        download_images: bool = False,
    ):
        super().__init__(base_substack_url, md_save_dir, html_save_dir, download_images)

    def get_url_soup(self, url: str, max_attempts: int = 5) -> Optional[BeautifulSoup]:
        """Gets soup from URL using requests, with retry on rate limiting."""
        for attempt in range(1, max_attempts + 1):
            try:
                page = requests.get(url, headers=None)
                soup = BeautifulSoup(page.content, "html.parser")

                if soup.find("h2", class_="paywall-title"):
                    print(f"Skipping premium article: {url}")
                    return None

                pre = soup.select_one("body > pre")
                if pre and "too many requests" in pre.text.lower():
                    if attempt == max_attempts:
                        raise RuntimeError(f"Max attempts reached for URL: {url}. Too many requests.")
                    base = 2 ** attempt
                    delay = base + random.uniform(-0.2 * base, 0.2 * base)
                    print(f"[{attempt}/{max_attempts}] Too many requests. Retrying in {delay:.2f} seconds...")
                    sleep(delay)
                    continue

                return soup
            except RuntimeError:
                raise
            except Exception as e:
                raise ValueError(f"Error fetching page: {e}") from e

        raise RuntimeError(f"Failed to fetch page after {max_attempts} attempts: {url}")


# =============================================================================
# PREMIUM CONTENT SCRAPER
# =============================================================================

class PremiumSubstackScraper(BaseSubstackScraper):
    def __init__(
        self,
        base_substack_url: str,
        md_save_dir: str,
        html_save_dir: str,
        download_images: bool = False,
        browser: str = 'chrome',
        headless: bool = False,
        driver_path: str = '',
        browser_path: str = '',
        user_agent: str = '',
        use_persistent_profile: bool = False,
        skip_login: bool = False,
    ) -> None:
        """
        Initialize the premium scraper with browser automation.
        
        Args:
            base_substack_url: The Substack URL to scrape
            md_save_dir: Directory for markdown files
            html_save_dir: Directory for HTML files
            browser: 'chrome' or 'edge' (chrome recommended)
            headless: Run browser in headless mode
            driver_path: Explicit path to WebDriver executable
            browser_path: Explicit path to browser executable
            user_agent: Custom user agent string
            use_persistent_profile: Reuse browser profile across runs (saves login)
            skip_login: Skip login if using a pre-authenticated profile
        """
        # Initialize driver before calling super().__init__ since that fetches URLs
        self.driver = BrowserManager.create_driver(
            browser=browser,
            headless=headless,
            driver_path=driver_path,
            browser_path=browser_path,
            user_agent=user_agent,
            use_persistent_profile=use_persistent_profile,
        )
        
        self.skip_login = skip_login
        self.use_persistent_profile = use_persistent_profile
        
        if not skip_login:
            self.login()
        else:
            print("Skipping login (using existing profile authentication)")
            # Navigate to substack to verify we're logged in
            self.driver.get(base_substack_url)
            sleep(3)

        super().__init__(base_substack_url, md_save_dir, html_save_dir, download_images)

    def login(self) -> None:
        """Log into Substack using Selenium."""
        print("Logging into Substack...")
        self.driver.get("https://substack.com/sign-in")
        sleep(3)

        signin_with_password = self.driver.find_element(
            By.XPATH, "//a[@class='login-option substack-login__login-option']"
        )
        signin_with_password.click()
        sleep(3)

        email = self.driver.find_element(By.NAME, "email")
        password = self.driver.find_element(By.NAME, "password")
        email.send_keys(EMAIL)
        password.send_keys(PASSWORD)

        submit = self.driver.find_element(By.XPATH, "//*[@id=\"substack-login\"]/div[2]/div[2]/form/button")
        submit.click()
        
        print("Waiting for login to complete (this may take up to 30 seconds)...")
        sleep(30)

        if self.is_login_failed():
            raise Exception(
                "Login unsuccessful. Please check your email and password, or your account status.\n"
                "If you're seeing a CAPTCHA, try:\n"
                "  1. Run without --headless to complete CAPTCHA manually\n"
                "  2. Use --persistent-profile to save your session\n"
                "  3. Then run with --skip-login on subsequent runs"
            )
        
        print("[OK] Login successful!")
        
        if self.use_persistent_profile:
            print("[OK] Session saved to persistent profile")

    def is_login_failed(self) -> bool:
        """Check for the presence of the 'error-container' to indicate a failed login."""
        error_container = self.driver.find_elements(By.ID, 'error-container')
        return len(error_container) > 0 and error_container[0].is_displayed()

    def get_url_soup(self, url: str, max_attempts: int = 5) -> Optional[BeautifulSoup]:
        """Gets soup from URL using logged-in Selenium driver, with retry on rate limiting."""
        for attempt in range(1, max_attempts + 1):
            try:
                self.driver.get(url)

                # Wait up to 20s for the post body (or a paywall marker) to appear, instead of a fixed sleep.
                try:
                    WebDriverWait(self.driver, 20).until(
                        lambda d: d.find_elements(By.CSS_SELECTOR, "div.available-content")
                        or d.find_elements(By.CSS_SELECTOR, "h1.post-title")
                        or d.find_elements(By.CSS_SELECTOR, "h2.paywall-title")
                        or d.find_elements(By.CSS_SELECTOR, "body > pre")
                    )
                except TimeoutException:
                    print(f"[WARN] Timeout waiting for post content to render: {url}")

                soup = BeautifulSoup(self.driver.page_source, "html.parser")

                pre = soup.select_one("body > pre")
                if pre and "too many requests" in pre.text.lower():
                    if attempt == max_attempts:
                        raise RuntimeError(f"Max attempts reached for URL: {url}. Too many requests.")
                    base = 2 ** attempt
                    delay = base + random.uniform(-0.2 * base, 0.2 * base)
                    print(f"[{attempt}/{max_attempts}] Too many requests. Retrying in {delay:.2f} seconds...")
                    sleep(delay)
                    continue

                if soup.find("h2", class_="paywall-title"):
                    print(f"Skipping premium article (no access): {url}")
                    return None

                return soup
            except RuntimeError:
                raise
            except Exception as e:
                raise ValueError(f"Error fetching page: {url}. Error: {e}") from e

        raise RuntimeError(f"Failed to fetch page after {max_attempts} attempts: {url}")
    
    def __del__(self):
        """Clean up the driver when done."""
        if hasattr(self, 'driver') and self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass


# =============================================================================
# CLI
# =============================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape a Substack site and convert posts to Markdown/HTML.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Scrape free posts
  python substack_scraper.py --url https://example.substack.com
  
  # Scrape premium posts with Chrome (recommended)
  python substack_scraper.py --url https://example.substack.com --premium --browser chrome
  
  # First run with persistent profile (complete login/CAPTCHA manually)
  python substack_scraper.py --url https://example.substack.com --premium --persistent-profile
  
  # Subsequent runs (skip login, use saved session)
  python substack_scraper.py --url https://example.substack.com --premium --persistent-profile --skip-login
  
  # Use manually downloaded driver
  python substack_scraper.py --url https://example.substack.com --premium --chrome-driver-path /path/to/chromedriver
        """
    )
    
    parser.add_argument(
        "-u", "--url", type=str,
        help="The base URL of the Substack site to scrape."
    )
    parser.add_argument(
        "-d", "--directory", type=str,
        help="The directory to save scraped markdown posts."
    )
    parser.add_argument(
        "--html-directory", type=str,
        help="The directory to save scraped HTML posts."
    )
    parser.add_argument(
        "-n", "--number", type=int, default=0,
        help="Number of posts to scrape (0 = all posts)."
    )
    parser.add_argument(
        "--images",
        action="store_true",
        help="Download images and update markdown to use local paths."
    )
    
    # Premium scraping options
    premium_group = parser.add_argument_group('Premium scraping options')
    premium_group.add_argument(
        "-p", "--premium", action="store_true",
        help="Use browser automation to access premium/paid content."
    )
    premium_group.add_argument(
        "--browser", type=str, default="chrome", choices=['chrome', 'edge'],
        help="Browser to use for premium scraping (default: chrome)."
    )
    premium_group.add_argument(
        "--headless", action="store_true",
        help="Run browser in headless mode (may trigger CAPTCHA)."
    )
    premium_group.add_argument(
        "--persistent-profile", action="store_true",
        help="Use a persistent browser profile to save login state."
    )
    premium_group.add_argument(
        "--skip-login", action="store_true",
        help="Skip login (use with --persistent-profile after first login)."
    )
    
    # Driver path options
    driver_group = parser.add_argument_group('Driver options (for troubleshooting)')
    driver_group.add_argument(
        "--chrome-driver-path", type=str, default="",
        help="Path to chromedriver executable."
    )
    driver_group.add_argument(
        "--edge-driver-path", type=str, default="",
        help="Path to msedgedriver executable."
    )
    driver_group.add_argument(
        "--chrome-path", type=str, default="",
        help="Path to Chrome browser executable."
    )
    driver_group.add_argument(
        "--edge-path", type=str, default="",
        help="Path to Edge browser executable."
    )
    driver_group.add_argument(
        "--user-agent", type=str, default="",
        help="Custom user agent string."
    )

    return parser.parse_args()


def main():
    args = parse_args()

    if args.directory is None:
        args.directory = BASE_MD_DIR

    if args.html_directory is None:
        args.html_directory = BASE_HTML_DIR

    # Determine driver/browser paths based on selected browser
    if args.browser == 'chrome':
        driver_path = args.chrome_driver_path
        browser_path = args.chrome_path
    else:
        driver_path = args.edge_driver_path
        browser_path = args.edge_path

    if args.url:
        if args.premium:
            scraper = PremiumSubstackScraper(
                base_substack_url=args.url,
                md_save_dir=args.directory,
                html_save_dir=args.html_directory,
                download_images=args.images,
                browser=args.browser,
                headless=args.headless,
                driver_path=driver_path,
                browser_path=browser_path,
                user_agent=args.user_agent,
                use_persistent_profile=args.persistent_profile,
                skip_login=args.skip_login,
            )
        else:
            scraper = SubstackScraper(
                args.url,
                md_save_dir=args.directory,
                html_save_dir=args.html_directory,
                download_images=args.images,
            )
        scraper.scrape_posts(args.number)

    else:
        # Use hardcoded values
        if USE_PREMIUM:
            scraper = PremiumSubstackScraper(
                base_substack_url=BASE_SUBSTACK_URL,
                md_save_dir=args.directory,
                html_save_dir=args.html_directory,
                download_images=args.images,
                browser=args.browser,
                headless=args.headless,
                driver_path=driver_path,
                browser_path=browser_path,
                user_agent=args.user_agent,
                use_persistent_profile=args.persistent_profile,
                skip_login=args.skip_login,
            )
        else:
            scraper = SubstackScraper(
                base_substack_url=BASE_SUBSTACK_URL,
                md_save_dir=args.directory,
                html_save_dir=args.html_directory,
                download_images=args.images,
            )
        scraper.scrape_posts(num_posts_to_scrape=NUM_POSTS_TO_SCRAPE)


if __name__ == "__main__":
    main()