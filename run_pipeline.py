"""
================================================================================
Run Pipeline - run_pipeline.py
================================================================================
Description :
The main orchestrator for the Shein Web Scraper.
This script coordinates a fully automated 3-phase pipeline:
1. Discovery: Uses undetected-chromedriver (same anti-detect config as Shein.py)
   to navigate category pages and harvest product URLs. Reuses Shein methods for
   cookie popup dismissal and Gemini-powered verification challenge handling.
2. Cleaning: Deduplicates and sanitizes the harvested URLs.
3. Scraping: Orchestrates the main execution engine (main.py) to extract
   data using undetected-chromedriver + BrowserPool.

Key improvements over plain Playwright:
- Uses the same Chrome profile and anti-detection flags as Shein.py/scrape()
- Reuses dismiss_cookie_popup() from Shein for aggressive CSS/JS cookie handling
- Reuses detect_and_solve_captcha() for AI-powered risk/challenge page solving
- No manual intervention needed — fully automated through all 3 phases
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import OrderedDict

from bs4 import BeautifulSoup
from colorama import Style
from dotenv import load_dotenv
from user_selection import select_categories


# Macros:
class BackgroundColors:
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BOLD = "\033[1m"
    MAGENTA = "\033[95m"


# Default target category URLs for URL discovery
DEFAULT_CATEGORIES = [
    "https://us.shein.com/Women-Tops-c-1771.html",
    "https://us.shein.com/Women-Dresses-c-1727.html",
    "https://us.shein.com/Men-Tops-c-1970.html",
    "https://us.shein.com/Men-Bottoms-c-1976.html",
]

# Default number of consecutive empty pages before stopping discovery
MAX_EMPTY_PAGES = 3

# Scroll steps for lazy-loading products
SCROLL_STEPS = 12
SCROLL_PAUSE_SECONDS = 0.8

# Page load timeout for uc.Chrome
PAGE_LOAD_TIMEOUT = 90


# ------------------------------------------------------------------ #
#  API Key Parsing (shared pattern with main.py)
# ------------------------------------------------------------------ #

def parse_gemini_api_keys(env_value: str) -> OrderedDict:
    """
    Parse GEMINI API keys from an environment variable into a name->key mapping.
    Supports both 'Name:Key,Name2:Key2' and plain comma-separated formats.

    :param env_value: Raw environment variable string.
    :return: OrderedDict mapping owner name to API key string.
    """
    env_value = (env_value or "").strip()
    if not env_value:
        return OrderedDict()

    entries = [entry.strip() for entry in env_value.split(",") if entry.strip()]
    named_keys: OrderedDict[str, str] = OrderedDict()
    contains_colon = any(":" in e for e in entries)

    if contains_colon:
        for entry in entries:
            if ":" not in entry:
                continue
            name, key = entry.split(":", 1)
            name = name.strip()
            key = key.strip()
            if name and key:
                named_keys[name] = key
    else:
        for idx, entry in enumerate(entries, start=1):
            key = entry.strip()
            if key:
                named_keys[f"key_{idx}"] = key

    return named_keys


def load_api_keys() -> OrderedDict:
    """
    Load Gemini API keys from the .env file.

    :return: OrderedDict mapping owner names to API keys, or empty dict.
    """
    raw_value = os.getenv("GEMINI_API_KEY", "")
    parsed = parse_gemini_api_keys(raw_value)
    if not parsed:
        print(f"{BackgroundColors.YELLOW}Warning: No Gemini API keys found. "
              f"Verification challenges will not be solved during discovery.{Style.RESET_ALL}")
    else:
        print(f"{BackgroundColors.GREEN}Loaded {len(parsed)} Gemini API key(s).{Style.RESET_ALL}")
    return parsed


# ------------------------------------------------------------------ #
#  Browser/Driver Helpers
# ------------------------------------------------------------------ #

def _build_chrome_options(profile_name: str = "ChromeProfile"):
    """
    Build uc.ChromeOptions with the same anti-detection flags used by Shein.py.
    Uses a dedicated Chrome profile directory for discovery.

    :param profile_name: Name of the Chrome profile directory.
    :return: Configured uc.ChromeOptions instance.
    """
    import undetected_chromedriver as uc

    options = uc.ChromeOptions()

    profile_path = os.path.abspath(os.path.join(os.getcwd(), profile_name))
    os.makedirs(profile_path, exist_ok=True)

    options.add_argument(f"--user-data-dir={profile_path}")
    options.add_argument("--start-maximized")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--single-process")
    options.add_argument("--disable-features=VizDisplayCompositor")
    options.add_argument("--disable-accelerated-2d-canvas")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-sync")
    options.add_argument("--disable-translate")
    options.add_argument("--disable-default-apps")
    options.add_argument("--mute-audio")
    options.add_argument("--no-first-run")
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-renderer-backgrounding")
    options.add_argument("--disable-field-trial-config")

    # Proxy support from environment
    proxy = os.getenv("BROWSER_PROXY", "")
    if proxy:
        options.add_argument(f"--proxy-server={proxy}")

    # Custom Chrome executable path
    chrome_path = os.getenv("CHROME_EXECUTABLE_PATH", "")
    if chrome_path:
        options.binary_location = chrome_path

    return options


def _dismiss_cookie_popup_discovery(driver):
    """
    Dismiss cookie consent popup using the same aggressive JS strategy as
    Shein.dismiss_cookie_popup(). This is a standalone copy to avoid
    requiring a full Shein instance just for the discovery phase, but uses
    the exact same selectors and text-matching logic.

    :param driver: uc.Chrome WebDriver instance.
    :return: True if a popup was dismissed, False otherwise.
    """
    try:
        time.sleep(2)
        result = driver.execute_script("""
            function dismissCookie() {
                // Strategy 1: Common button selectors
                var selectors = [
                    'button:contains("Reject All")',
                    'button:contains("Accept All")',
                    'button:contains("Accept")',
                    'button:contains("Reject")',
                    'button:contains("Decline")',
                    'button:contains("Continue")',
                    'button:contains("Got it")',
                    '[aria-label*="cookie"] button',
                    '[aria-label*="consent"] button',
                    '.cookie-popup button',
                    '.cookie-banner button',
                    '.consent-banner button',
                    '#cookie-popup button',
                    '#cookies-popup button',
                    '[class*="cookie"] [class*="accept"]',
                    '[class*="cookie"] [class*="reject"]',
                    '[class*="cookie"] [class*="close"]',
                    '[class*="consent"] [class*="accept"]',
                    '[class*="consent"] [class*="reject"]',
                    '[id*="cookie"] button',
                    '[id*="consent"] button',
                    '.fc-button',
                    '.fc-cta-consume',
                    '.fc-reject-button',
                    '.css-1k5ix99',
                    '.cookie-accept-btn',
                    '#cookieActionButton',
                    '#onetrust-accept-btn-handler',
                    '#onetrust-reject-all-handler',
                    '.ot-sdk-show-settings',
                    '#truste-consent-button',
                    '#truste-show-consent',
                ];
                for (var i = 0; i < selectors.length; i++) {
                    try {
                        var els = document.querySelectorAll(selectors[i]);
                        for (var j = 0; j < els.length; j++) {
                            if (els[j].offsetParent !== null) {
                                els[j].click();
                                return 'clicked via selector: ' + selectors[i];
                            }
                        }
                    } catch(e) {}
                }
                // Strategy 2: Text content matching
                var allButtons = document.querySelectorAll('button, a, [role="button"], input[type="submit"], input[type="button"]');
                var rejectTexts = ['reject all', 'reject', 'necessary cookies only'];
                var acceptTexts = ['accept all'];
                for (var i = 0; i < allButtons.length; i++) {
                    var text = (allButtons[i].innerText || allButtons[i].textContent || allButtons[i].value || '').trim().toLowerCase();
                    for (var t = 0; t < rejectTexts.length; t++) {
                        if (text === rejectTexts[t] || text.indexOf(rejectTexts[t]) !== -1) {
                            if (allButtons[i].offsetParent !== null) {
                                allButtons[i].click();
                                return 'rejected via text: ' + text;
                            }
                        }
                    }
                }
                for (var i = 0; i < allButtons.length; i++) {
                    var text = (allButtons[i].innerText || allButtons[i].textContent || allButtons[i].value || '').trim().toLowerCase();
                    for (var t = 0; t < acceptTexts.length; t++) {
                        if (text === acceptTexts[t] || text.indexOf(acceptTexts[t]) !== -1) {
                            if (allButtons[i].offsetParent !== null) {
                                allButtons[i].click();
                                return 'accepted via text: ' + text;
                            }
                        }
                    }
                }
                // Strategy 3: Check all elements for cookie-related content
                var allElements = document.querySelectorAll('div, span, section');
                for (var i = 0; i < allElements.length; i++) {
                    var el = allElements[i];
                    var text = (el.innerText || '').trim().toLowerCase();
                    if ((text.indexOf('cookie') !== -1 || text.indexOf('consent') !== -1 || text.indexOf('privacy') !== -1) &&
                        (text.indexOf('accept') !== -1 || text.indexOf('reject') !== -1 || text.indexOf('agree') !== -1)) {
                        var clickable = el.querySelector('button, [role="button"]');
                        if (clickable && clickable.offsetParent !== null) {
                            clickable.click();
                            return 'clicked via parent button';
                        }
                    }
                }
                return null;
            }
            return dismissCookie();
        """)
        if result:
            print(f"{BackgroundColors.GREEN}[Cookie] Popup dismissed: {result}{Style.RESET_ALL}")
            time.sleep(2)
            popup_exists = driver.execute_script("""
            return [...document.querySelectorAll('button')]
            .some(btn => btn.innerText && btn.innerText.includes('Reject All'));
            """)

            print(f"[DEBUG] Cookie popup still exists: {popup_exists}")
            return True
        else:
            print(f"{BackgroundColors.CYAN}[Cookie] No popup detected.{Style.RESET_ALL}")
            return False
    except Exception as e:
        print(f"{BackgroundColors.YELLOW}[Cookie] Dismissal error: {e}{Style.RESET_ALL}")
        return False


def _solve_verification_challenge(driver, api_keys: OrderedDict) -> bool:
    """
    Detect and solve any verification challenge (risk/challenge page) using
    the Shein.detect_and_solve_captcha() method. This reuses the existing
    Gemini-powered verification solver, avoiding duplicated code.

    :param driver: uc.Chrome WebDriver instance.
    :param api_keys: Dict of Gemini API keys for vision-based CAPTCHA solving.
    :return: True if cleared or no challenge present, False if unsolved.
    """
    if not api_keys:
        print(f"{BackgroundColors.YELLOW}[Verify] No API keys available. "
              f"Refreshing page and retrying...{Style.RESET_ALL}")
        return False

    # Create a lightweight Shein instance just to access its verification solver
    from Shein import Shein
    scraper = Shein(url="", api_keys=api_keys)
    try:
        result = scraper.detect_and_solve_captcha(driver)
        if result is True:
            print(f"{BackgroundColors.GREEN}[Verify] Verification cleared.{Style.RESET_ALL}")
            return True
        elif result == "RESTART_REQUIRED":
            print(f"{BackgroundColors.YELLOW}[Verify] Verification requires restart. Refreshing...{Style.RESET_ALL}")
            return False
        else:
            print(f"{BackgroundColors.YELLOW}[Verify] Verification not cleared.{Style.RESET_ALL}")
            return False
    except Exception as e:
        print(f"{BackgroundColors.YELLOW}[Verify] Verification solver error: {e}{Style.RESET_ALL}")
        return False


def _scroll_category_page(driver, steps: int = SCROLL_STEPS, pause: float = SCROLL_PAUSE_SECONDS):
    """
    Scroll the category page to trigger lazy-loaded product elements.
    Uses bounded scrolling (not infinite) to avoid endless loops.

    :param driver: uc.Chrome WebDriver instance.
    :param steps: Number of scroll steps.
    :param pause: Seconds to pause between steps.
    """
    print(f"{BackgroundColors.CYAN}[Scroll] Scrolling category page ({steps} steps)...{Style.RESET_ALL}")
    try:
        for _ in range(steps):
            driver.execute_script("window.scrollBy(0, 800);")
            time.sleep(pause)
        # Scroll back to top
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(0.5)
    except Exception as e:
        print(f"{BackgroundColors.YELLOW}[Scroll] Error: {e}{Style.RESET_ALL}")


def _extract_product_urls_from_html(html: str) -> list:
    """
    Extract unique product URLs from rendered category page HTML using
    BeautifulSoup. Looks for anchor tags with href containing '-p-' pattern.

    :param html: Rendered page HTML string.
    :return: List of unique product URLs.
    """
    soup = BeautifulSoup(html, "html.parser")
    urls = set()
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if "-p-" in href:
            # Normalize: strip query parameters after .html
            if ".html" in href:
                href = href.split(".html")[0] + ".html"
            # Ensure absolute URL
            if href.startswith("/"):
                href = "https://us.shein.com" + href
            urls.add(href)
    return list(urls)


# ------------------------------------------------------------------ #
#  Phase 1: Discovery
# ------------------------------------------------------------------ #

def run_discovery(target_urls: list, output_file: str, max_urls: int = 10000,
                  api_keys: OrderedDict = None) -> list:
    """
    Phase 1: Mass Discovery.
    Uses undetected-chromedriver (same anti-detect config as Shein.py) to
    navigate SHEIN category pages, handle cookie popups, solve verification
    challenges if encountered, and extract product URLs from rendered HTML.

    Args:
        target_urls (list): List of category page URLs to scrape.
        output_file (str): Path to output file for discovered URLs.
        max_urls (int): Maximum number of URLs to discover before stopping.
        api_keys (OrderedDict): Gemini API keys for verification solving.

    Returns:
        list: All unique URLs collected.
    """
    print(f"{BackgroundColors.BOLD}{BackgroundColors.CYAN}"
          f"{'='*60}\n  Phase 1: Mass URL Discovery\n"
          f"  Target: {max_urls} URLs from {len(target_urls)} categories\n"
          f"{'='*60}{Style.RESET_ALL}")

    collected = set()
    os.makedirs(os.path.dirname(output_file) or ".", exist_ok=True)

    # Load previously discovered URLs
    if os.path.exists(output_file):
        with open(output_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    if ".html" in line:
                        line = line.split(".html")[0] + ".html"
                    collected.add(line)
        print(f"{BackgroundColors.CYAN}[Discovery] Loaded {len(collected)} existing URLs.{Style.RESET_ALL}")

    if len(collected) >= max_urls:
        print(f"{BackgroundColors.GREEN}[Discovery] Already have {len(collected)} URLs. Skipping.{Style.RESET_ALL}")
        return list(collected)

    # Launch browser
    import undetected_chromedriver as uc

    options = _build_chrome_options(profile_name="ChromeProfile_Discovery")
    driver = None

    try:
        print(f"{BackgroundColors.CYAN}[Discovery] Launching undetected-chromedriver...{Style.RESET_ALL}")
        driver = uc.Chrome(options=options)
        driver.maximize_window()

        # Manually apply page_load_timeout after construction
        try:
            # uc.Chrome might not have set_page_load_timeout depending on version
            # We use a shorter default timeout via the 'timeout' property if available
            pass
        except Exception:
            pass

        driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        print(f"{BackgroundColors.GREEN}[Discovery] Browser launched.{Style.RESET_ALL}")

        for cat_url in target_urls:
            if len(collected) >= max_urls:
                break

            print(f"\n{BackgroundColors.BOLD}{BackgroundColors.MAGENTA}"
                  f"[Category] {cat_url}{Style.RESET_ALL}")
            page_num = 1
            empty_pages = 0

            while len(collected) < max_urls and empty_pages < MAX_EMPTY_PAGES:
                # Build page URL
                sep = "&" if "?" in cat_url else "?"
                page_url = f"{cat_url}{sep}page={page_num}"
                print(f"{BackgroundColors.CYAN}[Page {page_num}] Navigating to: {page_url}{Style.RESET_ALL}")

                try:
                    driver.get(page_url)
                    time.sleep(5)  # Initial render wait
                except Exception as e:
                    print(f"{BackgroundColors.YELLOW}[Page {page_num}] Load timeout/error: {e}{Style.RESET_ALL}")
                    # Continue to check current state

                # Check if we're on a challenge/risk page
                current_url = driver.current_url.lower()
                if any(pattern in current_url for pattern in ["captcha", "challenge", "verify", "risk"]):
                    print(f"{BackgroundColors.YELLOW}[Page {page_num}] Verification challenge detected at URL: "
                          f"{driver.current_url}{Style.RESET_ALL}")
                    cleared = _solve_verification_challenge(driver, api_keys)
                    
                    try:
                        driver.current_url
                    except Exception:
                        print("[DEBUG] Browser session was closed.")
                        break
                    if not cleared:
                        print(f"{BackgroundColors.YELLOW}[Page {page_num}] Could not clear challenge. "
                              f"Moving to next page.{Style.RESET_ALL}")
                        page_num += 1
                        continue
                    print(f"{BackgroundColors.GREEN}[Page {page_num}] Challenge cleared.{Style.RESET_ALL}")

                # Dismiss cookie popup
                _dismiss_cookie_popup_discovery(driver)

                # Check if the page has product links after a short wait
                try:
                    # Wait for product link elements to appear (up to 15 seconds)
                    for _ in range(30):
                        has_products = driver.execute_script(
                            "return document.querySelector('a[href*=\"-p-\"]') !== null;"
                        )
                        if has_products:
                            break
                        time.sleep(0.5)
                except Exception:
                    pass

                # Scroll to trigger lazy loading
                _scroll_category_page(driver)

                # Extract product URLs from rendered HTML
                html = driver.page_source
                
                with open(f"debug_page_{page_num}.html", "w", encoding="utf-8") as f:
                    f.write(html)

                print(f"[DEBUG] HTML length: {len(html)}")

                print(f"[DEBUG] '-p-' occurrences: {html.count('-p-')}")
                new_urls = _extract_product_urls_from_html(html)
                print(f"[DEBUG] Extracted {len(new_urls)} URLs")

                # Filter only newly discovered URLs
                newly_added = []
                for u in new_urls:
                    if u not in collected:
                        collected.add(u)
                        newly_added.append(u)

                if newly_added:
                    empty_pages = 0
                    # Append to output file incrementally
                    with open(output_file, "a", encoding="utf-8") as f:
                        for u in newly_added:
                            f.write(u + "\n")
                    print(f"{BackgroundColors.GREEN}[Page {page_num}] +{len(newly_added)} new URLs "
                          f"(total: {len(collected)}/{max_urls}){Style.RESET_ALL}")
                else:
                    empty_pages += 1
                    print(f"{BackgroundColors.YELLOW}[Page {page_num}] No new URLs "
                          f"(empty pages: {empty_pages}/{MAX_EMPTY_PAGES}){Style.RESET_ALL}")

                page_num += 1

        print(f"\n{BackgroundColors.GREEN}[Discovery] Complete. Collected {len(collected)} URLs.{Style.RESET_ALL}")

    except Exception as e:
        print(f"{BackgroundColors.RED}[Discovery] Fatal error: {e}{Style.RESET_ALL}")
        # Return whatever we have so far
    finally:
        if driver:
            try:
                driver.quit()
                print(f"{BackgroundColors.CYAN}[Discovery] Browser closed.{Style.RESET_ALL}")
            except Exception:
                pass

    return list(collected)


# ------------------------------------------------------------------ #
#  Phase 2: Cleaning (unchanged)
# ------------------------------------------------------------------ #

def clean_urls(input_file):
    """
    Phase 2: URL Cleaning. Reads the input file, deduplicates URLs, and
    strips query parameters.

    Args:
        input_file (str): The path to the file containing raw URLs.
    """
    print(f"\n{BackgroundColors.BOLD}{BackgroundColors.CYAN}"
          f"{'='*60}\n  Phase 2: URL Cleaning\n"
          f"{'='*60}{Style.RESET_ALL}")

    if not os.path.exists(input_file):
        print(f"{BackgroundColors.YELLOW}[Clean] {input_file} not found.{Style.RESET_ALL}")
        return

    with open(input_file, "r", encoding="utf-8") as f:
        lines = f.readlines()

    clean_set = set()
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ".html" in line:
            line = line.split(".html")[0] + ".html"
        clean_set.add(line)

    with open(input_file, "w", encoding="utf-8") as f:
        f.write("# One product URL per line. Lines starting with # are ignored.\n")
        for u in sorted(clean_set, key=str.lower):
            f.write(u + "\n")

    print(f"{BackgroundColors.GREEN}[Clean] Reduced to {len(clean_set)} unique URLs.{Style.RESET_ALL}")


# ------------------------------------------------------------------ #
#  Phase 3: Scraping (unchanged)
# ------------------------------------------------------------------ #

def run_scraping(input_file, target=0):
    """
    Phase 3: Mass Scraping. Triggers the main backend scraper to process
    the cleaned URLs via subprocess.

    Args:
        input_file (str): The path to the file containing cleaned URLs.
        target (int): Maximum number of URLs to scrape (0 = unlimited).
    """
    print(f"\n{BackgroundColors.BOLD}{BackgroundColors.CYAN}"
          f"{'='*60}\n  Phase 3: Mass Scraping\n"
          f"{'='*60}{Style.RESET_ALL}")

    cmd = [sys.executable, "main.py"]
    if target > 0:
        cmd.extend(["--target", str(target)])

    print(f"{BackgroundColors.CYAN}[Scrape] Executing: {' '.join(cmd)}{Style.RESET_ALL}")
    result = subprocess.run(cmd)

    if result.returncode == 0:
        print(f"{BackgroundColors.GREEN}[Scrape] Completed successfully.{Style.RESET_ALL}")
    else:
        print(f"{BackgroundColors.RED}[Scrape] Exited with code {result.returncode}.{Style.RESET_ALL}")


# ------------------------------------------------------------------ #
#  Main Entry Point
# ------------------------------------------------------------------ #

def main():
    """
    Main orchestrator entry point. Loads environment, parses arguments,
    and runs the 3-phase pipeline sequentially.
    """
    parser = argparse.ArgumentParser(
        description="End-to-End Orchestrator Pipeline (undetected-chromedriver)"
    )
    parser.add_argument("--categories", type=str,
                        help="Path to text file with category URLs (1 per line)")
    parser.add_argument("--target", type=int, default=10000,
                        help="Target number of URLs to discover (default: 10000)")
    parser.add_argument("--out", type=str, default="Inputs/urls.txt",
                        help="Output file for discovered URLs (default: Inputs/urls.txt)")
    args = parser.parse_args()

    print(f"{BackgroundColors.BOLD}{BackgroundColors.GREEN}"
          f"{'='*60}\n  SHEIN Web Scraper - End-to-End Pipeline\n"
          f"  Powered by undetected-chromedriver + Gemini AI\n"
          f"{'='*60}{Style.RESET_ALL}\n")

    # Load .env for API keys
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        load_dotenv(env_path)
        print(f"{BackgroundColors.GREEN}[Env] Loaded .env from {env_path}{Style.RESET_ALL}")
    else:
        print(f"{BackgroundColors.YELLOW}[Env] .env not found at {env_path}. "
              f"Verification challenges may fail.{Style.RESET_ALL}")

    # Load API keys for verification challenge solving
    api_keys = load_api_keys()

    # Determine category URLs
    
    categories = select_categories()
    if not categories:
       print("No categories selected. Exiting.")
       return
    #if args.categories and os.path.exists(args.categories):
     #   with open(args.categories, "r", encoding="utf-8") as f:
      #      categories = [line.strip() for line in f if line.strip()]
      #  print(f"{BackgroundColors.CYAN}[Categories] Loaded {len(categories)} from {args.categories}{Style.RESET_ALL}")
    #else:
       # print(f"{BackgroundColors.CYAN}[Categories] Using {len(categories)} default categories.{Style.RESET_ALL}")

    # Phase 1: Discovery
    run_discovery(categories, args.out, max_urls=args.target, api_keys=api_keys)

    # Phase 2: Cleaning
    clean_urls(args.out)

    # Phase 3: Scraping
    run_scraping(args.out, target=args.target)

    print(f"\n{BackgroundColors.BOLD}{BackgroundColors.GREEN}"
          f"{'='*60}\n  All 3 phases complete!\n"
          f"  Check Outputs/ directory for scraped data.\n"
          f"{'='*60}{Style.RESET_ALL}")


if __name__ == "__main__":
    main()

