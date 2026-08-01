"""
================================================================================
Browser Pool - browser_pool.py
================================================================================
Description :
Manages a pool of reusable Chrome browser instances for concurrent scraping.
Provides thread-safe browser lifecycle management, CAPTCHA detection with
automatic cooldown, and URL requeueing for robust parallel scraping.

Key features:
- N reusable undetected-chromedriver instances created at startup
- Thread-safe status tracking per browser (AVAILABLE, SCRAPING, BLOCKED)
- Automatic CAPTCHA detection and browser blocking with configurable cooldown
- URL requeueing when a browser encounters CAPTCHA
- Clear logging with browser ID, URL, status transitions, and progress
- Clean shutdown of all browser instances

Usage:
    pool = BrowserPool(num_browsers=3, cooldown=180)
    pool.start()
    pool.submit_url("https://...")
    # ... submit more URLs ...
    pool.shutdown()

Dependencies:
- undetected-chromedriver
- threading, queue
"""
import os
import queue
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from colorama import Style

try:
    import undetected_chromedriver as uc
except ImportError:
    uc = None
if TYPE_CHECKING:
    from Shein import Shein

# Import Shein at module level for efficient reuse
from Shein import Shein


# Macros:
class BackgroundColors:
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BOLD = "\033[1m"
    MAGENTA = "\033[95m"
    BLUE = "\033[94m"


# Browser Status Constants:
STATUS_AVAILABLE = "AVAILABLE"
STATUS_SCRAPING = "SCRAPING"
STATUS_BLOCKED = "BLOCKED"
STATUS_INITIALIZING = "INITIALIZING"
STATUS_STOPPED = "STOPPED"

# Default Configuration:
DEFAULT_NUM_BROWSERS = 3
DEFAULT_COOLDOWN_SECONDS = 180
POLL_INTERVAL = 0.1  # Seconds between each queue poll in worker loop
MAX_RETRIES_PER_URL = 2  # Maximum times a URL can be requeued before being discarded


class BrowserPool:
    """
    Manages a pool of reusable Chrome browser instances for concurrent scraping.

    Each browser runs in its own worker thread, pulling URLs from a shared
    thread-safe queue. If a browser encounters a CAPTCHA, it is marked as BLOCKED
    and the URL is requeued for another browser. After a configurable cooldown,
    the blocked browser becomes AVAILABLE again.

    :param num_browsers: Number of browser instances in the pool (default 3)
    :param cooldown: Seconds to wait before reusing a CAPTCHA-blocked browser (default 180)
    :param api_keys: Optional dict of Gemini API keys for CAPTCHA solving
    """

    def __init__(self, num_browsers: int = DEFAULT_NUM_BROWSERS,
                 cooldown: int = DEFAULT_COOLDOWN_SECONDS,
                 api_keys: Optional[Dict[str, str]] = None,
                 save_callback: Optional[callable] = None):
        self.num_browsers = num_browsers
        self.cooldown = cooldown
        self.api_keys = api_keys or {}

        # Thread-safe URL queue
        self.url_queue: queue.Queue = queue.Queue()

        # Track retries per URL to avoid infinite requeueing
        self.url_retries: Dict[str, int] = {}
        self.retries_lock = threading.Lock()

        # Browser state management
        self.browsers: Dict[int, Any] = {} # browser_id -> driver
        self.browser_status: Dict[int, str] = {}  # browser_id -> status string
        self.browser_blocked_until: Dict[int, Optional[datetime]] = {}  # browser_id -> cooldown end time
        self.browser_current_url: Dict[int, Optional[str]] = {}  # browser_id -> current URL being processed
        self.browser_lock = threading.Lock()  # Lock for browser state mutations

        self.executor: Optional[ThreadPoolExecutor] = None
        self._shutdown_event = threading.Event()
        self._total_urls_submitted = 0
        self._total_urls_processed = 0
        self._processed_lock = threading.Lock()
        self._save_callback = save_callback  # Callback to save scraped data

    # ------------------------------------------------------------------ #
    #  Public API
    # ------------------------------------------------------------------ #

    def start(self):
        """
        Initialize all browser instances and start worker threads.
        """
        self._log("BROWSER_POOL", f"Starting pool with {self.num_browsers} browsers (cooldown={self.cooldown}s)...")

        for browser_id in range(1, self.num_browsers + 1):
            self.browser_status[browser_id] = STATUS_INITIALIZING
            self.browser_blocked_until[browser_id] = None
            self.browser_current_url[browser_id] = None

        # Create all browser instances sequentially (uc.Chrome is not thread-safe for creation)
        for browser_id in range(1, self.num_browsers + 1):
            try:
                self._create_browser(browser_id)
                self.browser_status[browser_id] = STATUS_AVAILABLE
                self._log(browser_id, "Browser initialized and available.")
            except Exception as e:
                self._log(browser_id, f"Failed to initialize: {e}", level="ERROR")
                self.browser_status[browser_id] = STATUS_STOPPED

        # Start worker threads via ThreadPoolExecutor
        self.executor = ThreadPoolExecutor(max_workers=self.num_browsers,
                                           thread_name_prefix="BrowserWorker")
        for browser_id in range(1, self.num_browsers + 1):
            if self.browser_status[browser_id] != STATUS_STOPPED:
                self.executor.submit(self._worker_loop, browser_id)

        self._log("BROWSER_POOL", f"Pool started with {self._count_available()} available browsers.")

    def submit_url(self, url: str):
        """
        Submit a product URL to the shared processing queue.

        :param url: The product URL to scrape.
        """
        self._total_urls_submitted += 1
        self.url_queue.put(url)
        self._log("BROWSER_POOL", f"URL submitted to queue (total queued: {self.url_queue.qsize()}).")

    def submit_urls(self, urls: List[str]):
        """
        Submit multiple URLs to the shared processing queue.

        :param urls: List of product URLs to scrape.
        """
        for url in urls:
            self.submit_url(url)

    def shutdown(self, wait: bool = True):
        """
        Shut down the browser pool gracefully.

        :param wait: If True, wait for all queued URLs to be processed before quitting.
        """
        self._log("BROWSER_POOL", "Shutting down browser pool...")

        if wait:
            # Wait for queue to drain and all workers to finish
            remaining = self.url_queue.qsize()
            if remaining > 0:
                self._log("BROWSER_POOL", f"Waiting for {remaining} URLs to finish processing...")
            # Block until all items in the queue have been processed (task_done called)
            self.url_queue.join()

        self._shutdown_event.set()

        if self.executor:
            self.executor.shutdown(wait=True)

        # Quit all browser instances
        for browser_id in list(self.browsers.keys()):
            self._destroy_browser(browser_id)

        self._log("BROWSER_POOL", "Browser pool shut down complete.")

    def get_status(self) -> Dict[str, object]:
        """
        Return a snapshot of the current pool status.

        :return: Dictionary with pool-wide and per-browser status.
        """
        with self.browser_lock:
            browsers_status = {}
            for bid in range(1, self.num_browsers + 1):
                status = self.browser_status.get(bid, STATUS_STOPPED)
                url = self.browser_current_url.get(bid)
                blocked_until = self.browser_blocked_until.get(bid)
                remaining_cooldown = 0
                if blocked_until and status == STATUS_BLOCKED:
                    remaining = (blocked_until - datetime.now()).total_seconds()
                    remaining_cooldown = max(0, int(remaining))

                browsers_status[str(bid)] = {
                    "id": bid,
                    "status": status,
                    "current_url": url,
                    "cooldown_remaining_s": remaining_cooldown,
                }

        return {
            "num_browsers": self.num_browsers,
            "cooldown": self.cooldown,
            "queue_size": self.url_queue.qsize(),
            "total_submitted": self._total_urls_submitted,
            "total_processed": self._total_urls_processed,
            "browsers": browsers_status,
        }

    def get_available_browsers(self) -> List[int]:
        """
        Return list of browser IDs that are currently AVAILABLE.

        :return: List of available browser IDs.
        """
        available = []
        with self.browser_lock:
            for bid in range(1, self.num_browsers + 1):
                if self.browser_status.get(bid) == STATUS_AVAILABLE:
                    available.append(bid)
        return available

    # ------------------------------------------------------------------ #
    #  Browser Lifecycle
    # ------------------------------------------------------------------ #

    def _create_browser(self, browser_id: int):
        """
        Create a new undetected-chromedriver instance for the given browser ID.

        :param browser_id: Numeric browser identifier (1-based).
        """
        if uc is None:
            raise ImportError("undetected_chromedriver is not installed. "
                              "Run: pip install undetected-chromedriver")

        profile_path = os.path.abspath(
            os.path.join(os.getcwd(), f"ChromeProfile_{browser_id}")
        )

        os.makedirs(profile_path, exist_ok=True)

        options = uc.ChromeOptions()
        options.add_argument(f"--user-data-dir={profile_path}")
        options.add_argument("--start-minimized")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        #options.add_argument("--disable-gpu")
        #options.add_argument("--disable-software-rasterizer")
        #options.add_argument("--single-process")
        #options.add_argument("--disable-features=VizDisplayCompositor")
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

        # Proxy support (read from environment variable)
        proxy = os.getenv("BROWSER_PROXY", "")
        if proxy:
            options.add_argument(f"--proxy-server={proxy}")

        chrome_path = os.getenv("CHROME_EXECUTABLE_PATH", "")
        if chrome_path:
            options.binary_location = chrome_path
            print(f"Using Chrome executable: {chrome_path}")
            
        self._log(browser_id, "About to create Chrome...")    

        driver = uc.Chrome(
            options=options,
            browser_executable_path=chrome_path if chrome_path else None,
        )

        self._log(browser_id, f"[Browser-{browser_id}] Chrome created successfully")
        self._log(browser_id, "Chrome object created. About to navigate to SHEIN...")
        try:
            driver.get("https://us.shein.com")
            self._log(browser_id, f"Current URL: {driver.current_url}")
        except Exception as e:
            self._log(browser_id, f"Initial navigation failed: {e}", level="WARNING")

        driver.set_page_load_timeout(120)

        with self.browser_lock:
            self.browsers[browser_id] = driver

    def _destroy_browser(self, browser_id: int):
        """
        Safely quit and remove a browser instance.

        :param browser_id: Numeric browser identifier (1-based).
        """
        with self.browser_lock:
            driver = self.browsers.pop(browser_id, None)
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    def _is_browser_alive(self, browser_id: int) -> bool:
        """
        Check if a browser instance is still alive/usable.

        :param browser_id: Numeric browser identifier (1-based).
        :return: True if the browser is still alive.
        """
        with self.browser_lock:
            driver = self.browsers.get(browser_id)
        if driver is None:
            return False
        try:
            _ = driver.current_url
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    #  Status Management
    # ------------------------------------------------------------------ #

    def _set_status(self, browser_id: int, status: str, url: Optional[str] = None):
        """
        Set the status of a browser and log the transition.

        :param browser_id: Numeric browser identifier (1-based).
        :param status: New status string.
        :param url: Optional URL being processed.
        """
        with self.browser_lock:
            old_status = self.browser_status.get(browser_id, "?")
            self.browser_status[browser_id] = status
            self.browser_current_url[browser_id] = url
            if status == STATUS_BLOCKED:
                self.browser_blocked_until[browser_id] = datetime.now() + timedelta(seconds=self.cooldown)

        if url:
            self._log(browser_id, f"[{old_status}→{status}] {url}")
        else:
            self._log(browser_id, f"[{old_status}→{status}]")

    def _mark_browser_available(self, browser_id: int):
        """
        Mark a browser as AVAILABLE after cooldown expires.
        Called internally by the cooldown checker.
        """
        with self.browser_lock:
            if self.browser_status.get(browser_id) == STATUS_BLOCKED:
                self.browser_status[browser_id] = STATUS_AVAILABLE
                self.browser_blocked_until[browser_id] = None
                self.browser_current_url[browser_id] = None
                self._log(browser_id, "[BLOCKED→AVAILABLE] Cooldown expired, browser ready.")

    def _count_available(self) -> int:
        """Return the count of currently AVAILABLE browsers."""
        count = 0
        with self.browser_lock:
            for bid in range(1, self.num_browsers + 1):
                if self.browser_status.get(bid) == STATUS_AVAILABLE:
                    count += 1
        return count

    # ------------------------------------------------------------------ #
    #  CAPTCHA Detection
    # ------------------------------------------------------------------ #

    def _is_captcha_on_page(self, driver) -> bool:
        """
        Quick check if the current page contains CAPTCHA keywords.

        :param driver: Selenium WebDriver instance.
        :return: True if CAPTCHA keywords are found.
        """
        try:
            page_source = driver.page_source.lower()
        except Exception:
            return False

        captcha_keywords = [
            "captcha",
            "verify you are human",
            "security check",
            "slide to complete",
            "drag the puzzle",
            "i am human",
            "robot",
            "access denied",
            "please verify",
        ]

        return any(keyword in page_source for keyword in captcha_keywords)

    # ------------------------------------------------------------------ #
    #  Worker Loop
    # ------------------------------------------------------------------ #

    def _check_and_release_blocked(self, browser_id: int):
        """
        Check if a blocked browser's cooldown has expired and mark it available.

        :param browser_id: Numeric browser identifier.
        """
        should_release = False
        with self.browser_lock:
            if self.browser_status.get(browser_id) != STATUS_BLOCKED:
                return
            blocked_until = self.browser_blocked_until.get(browser_id)
            if blocked_until and datetime.now() >= blocked_until:
                self.browser_status[browser_id] = STATUS_AVAILABLE
                self.browser_blocked_until[browser_id] = None
                self.browser_current_url[browser_id] = None
                should_release = True
        if should_release:
            self._log(browser_id, "[BLOCKED→AVAILABLE] Cooldown expired, browser ready.")

    def _worker_loop(self, browser_id: int):
        """
        Worker thread main loop: polls queue, processes URLs, handles blocking.

        :param browser_id: Numeric browser identifier (1-based).
        """
        self._log(browser_id, "Worker thread started.")

        while not self._shutdown_event.is_set():
            # 1. Check if blocked browser cooldown expired
            self._check_and_release_blocked(browser_id)

            # 2. If browser is blocked, sleep and retry
            with self.browser_lock:
                status = self.browser_status.get(browser_id, STATUS_STOPPED)
            if status == STATUS_BLOCKED:
                time.sleep(POLL_INTERVAL)
                continue

            # 3. Ensure browser is alive (recreate if dead)
            if not self._is_browser_alive(browser_id):
                self._log(browser_id, "Browser died, recreating...", level="WARNING")
                self._destroy_browser(browser_id)
                try:
                    self._create_browser(browser_id)
                    self._set_status(browser_id, STATUS_AVAILABLE)
                except Exception as e:
                    self._log(browser_id, f"Failed to recreate browser: {e}", level="ERROR")
                    time.sleep(5)
                    continue

            # 4. Try to get a URL from the queue
            try:
                url = self.url_queue.get(timeout=POLL_INTERVAL)
            except queue.Empty:
                continue

            # 5. Process the URL
            driver = self.browsers.get(browser_id)
            if driver is None:
                self._log(browser_id, "Driver unavailable, marking available for recreation.", level="WARNING")
                self._set_status(browser_id, STATUS_AVAILABLE)
                self.url_queue.task_done()
                continue

            self._set_status(browser_id, STATUS_SCRAPING, url)

            was_requeued = False  # Track if URL was requeued (don't count as processed)

            try:
                # Navigate to URL
                self._log(browser_id, f"Navigating to {url}")
                driver.get(url)

                # Proceed with scraping via Shein (imported at module level)
                scraper = Shein(
                    url,
                    local_html_path=None,
                    prefix="",
                    output_directory="./Outputs/",
                    api_keys=self.api_keys
                )

                product_data = self._scrape_with_driver(scraper, driver, browser_id, url)

                if product_data:
                    self._log(browser_id, f"Successfully scraped: {product_data.get('name', 'Unknown')}")
                    # Save product data via callback (thread-safe)
                    if self._save_callback:
                        try:
                            self._save_callback(product_data, url)
                        except Exception as save_e:
                            self._log(browser_id, f"Save callback error: {save_e}", level="WARNING")
                else:
                    self._log(browser_id, f"Failed to scrape {url}", level="WARNING")

                # Set browser back to available
                self._set_status(browser_id, STATUS_AVAILABLE)

            except Exception as e:
                self._log(browser_id, f"Unexpected error processing {url}: {e}", level="ERROR")
                self._set_status(browser_id, STATUS_AVAILABLE)

            finally:
                self.url_queue.task_done()
                # Only count as processed if the URL was NOT requeued (avoids overcounting)
                if not was_requeued and url not in self.url_retries:
                    with self._processed_lock:
                        self._total_urls_processed += 1

    def _scrape_with_driver(self, scraper: "Shein", driver, browser_id: int, url: str):
        """
        Perform scraping using the provided external driver.

        This mirrors the key scraping logic from Shein.scrape() but uses the
        externally provided driver instead of creating a new one.

        :param scraper: Shein scraper instance.
        :param driver: External uc.Chrome WebDriver instance.
        :param browser_id: Browser ID for logging.
        :param url: Product URL being scraped.
        :return: Product data dict or None on failure.
        """
        try:
            self._log(browser_id, f"Starting scrape for {url}")

            # 1. Dismiss cookie popup
            try:
                scraper.dismiss_cookie_popup(driver)
            except Exception:
                pass

            # 2. Solve CAPTCHA if present (use existing Gemini logic)
            try:
                captcha_ok = scraper.detect_and_solve_captcha(driver)
                if not captcha_ok:
                    self._log(browser_id, f"CAPTCHA could not be solved for {url}", level="WARNING")
                    # Mark browser blocked and requeue
                    self._set_status(browser_id, STATUS_BLOCKED, url)
                    self._requeue_url(url)
                    return None
            except Exception as cap_e:
                self._log(browser_id, f"CAPTCHA solver error: {cap_e}", level="WARNING")

            # 3. Scroll page to trigger lazy loading
            try:
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight/2);")
                time.sleep(6)
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(6)
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight/3);")
                time.sleep(3)
            except Exception as scroll_e:
                self._log(browser_id, f"Scroll error: {scroll_e}", level="WARNING")

            # 4. Click 'Description' dropdown if present
            try:
                driver.execute_script("""
                    var elements = document.querySelectorAll('div, span, button, a');
                    for (var i = 0; i < elements.length; i++) {
                        var text = elements[i].innerText || elements[i].textContent;
                        if (text && text.trim().toLowerCase() === 'description') {
                            elements[i].click();
                            break;
                        }
                    }
                """)
                time.sleep(2)
            except Exception:
                pass

            # 5. Get page source and parse product info
            html_content = driver.page_source

            # Save to scraper and parse
            scraper.html_content = html_content
            product_info = scraper.scrape_product_info(html_content)

            if not product_info:
                self._log(browser_id, f"No product info extracted from {url}", level="WARNING")
                return None

            # Validate product data
            name = str(product_info.get("name", "")).strip()
            price = str(product_info.get("current_price_integer", "")).strip()
            if name in ("", "Unknown Product", "None", "none", "null", "N/A"):
                self._log(browser_id, f"Invalid product name for {url}", level="WARNING")
                return None
            if price in ("", "0", "None", "none"):
                self._log(browser_id, f"Invalid product price for {url}", level="WARNING")
                return None

            return product_info

        except Exception as e:
            self._log(browser_id, f"Scraping error: {e}", level="ERROR")
            return None

    def _requeue_url(self, url: str):
        """
        Requeue a URL for processing by another browser, tracking retry count.

        :param url: The URL to requeue.
        """
        with self.retries_lock:
            retries = self.url_retries.get(url, 0)
            if retries < MAX_RETRIES_PER_URL:
                self.url_retries[url] = retries + 1
                self.url_queue.put(url)
                self._log("BROWSER_POOL",
                          f"Requeued {url} (retry {retries + 1}/{MAX_RETRIES_PER_URL})",
                          level="WARNING")
            else:
                self._log("BROWSER_POOL",
                          f"Discarding {url} after {MAX_RETRIES_PER_URL} failed attempts",
                          level="ERROR")

    # ------------------------------------------------------------------ #
    #  Logging
    # ------------------------------------------------------------------ #

    def _log(self, browser_id, message, level="INFO"):
        """
        Log a message with browser ID prefix and color based on level.

        Format: [BROWSER-{id}] [{STATUS}] {message}

        :param browser_id: Browser ID (int) or "BROWSER_POOL" string.
        :param message: The log message.
        :param level: Log level (INFO, WARNING, ERROR).
        """
        timestamp = datetime.now().strftime("%H:%M:%S")

        if isinstance(browser_id, int):
            prefix = f"Browser-{browser_id}"
        else:
            prefix = str(browser_id)

        if level == "ERROR":
            color = BackgroundColors.RED
        elif level == "WARNING":
            color = BackgroundColors.YELLOW
        else:
            color = BackgroundColors.GREEN

        status = self.browser_status.get(browser_id, "") if isinstance(browser_id, int) else ""
        status_str = f" [{status}]" if status else ""

        print(f"{color}{timestamp} [{prefix}]{status_str} {message}{Style.RESET_ALL}")


# ------------------------------------------------------------------ #
#  Convenience function for quick testing
# ------------------------------------------------------------------ #

def create_pool(num_browsers: int = DEFAULT_NUM_BROWSERS,
                cooldown: int = DEFAULT_COOLDOWN_SECONDS,
                api_keys: Optional[Dict[str, str]] = None) -> BrowserPool:
    """
    Convenience factory function to create and start a BrowserPool.

    :param num_browsers: Number of browser instances.
    :param cooldown: CAPTCHA cooldown in seconds.
    :param api_keys: Optional Gemini API keys dict.
    :return: Started BrowserPool instance.
    """
    pool = BrowserPool(num_browsers=num_browsers, cooldown=cooldown, api_keys=api_keys or {})
    pool.start()
    return pool

