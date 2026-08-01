"""
================================================================================
Configuration - config.py
================================================================================
Description :
Centralized configuration for verification challenge handling and other
scraper parameters. All retry counts, polling intervals, and thresholds
are configurable here.

Usage:
    from config import (
        MAX_VERIFICATION_ATTEMPTS,
        VERIFICATION_POLL_INTERVAL,
        ...
    )
"""

# ------------------------------------------------------------------
# Verification/Challenge Handling Configuration
# ------------------------------------------------------------------

# Maximum number of verification attempts before restarting the browser session
MAX_VERIFICATION_ATTEMPTS = 7

# Interval (seconds) between DOM polls when checking if verification passed
VERIFICATION_POLL_INTERVAL = 1.5

# Maximum total time (seconds) to poll for verification success after an action
VERIFICATION_POLL_TIMEOUT = 25

# After this many failed verification attempts, restart the browser session
# instead of refreshing the page or retrying with the same session
BROWSER_RESTART_THRESHOLD = 3

# Maximum consecutive browser restarts per URL before giving up entirely
MAX_CONSECUTIVE_RESTARTS = 2

# Path where verification screenshots are saved for Gemini analysis
CAPTCHA_SCREENSHOT_PATH = "captcha_screenshot.png"

# Gemini model used for vision-based verification analysis
GEMINI_VERIFICATION_MODEL = "gemini-3.1-flash-lite"

# ------------------------------------------------------------------
# Verification detection keywords (page source scan)
# ------------------------------------------------------------------

# Keywords that indicate a verification challenge may be present
VERIFICATION_KEYWORDS = [
    "verify you are human",
    "verify your identity",
    "security check",
    "slide to complete",
    "drag the puzzle",
    "i am human",
    "i'm not a robot",
    "are you human",
    "robot verification",
    "please verify",
    "security verification",
    "turnstile",

]

# Keywords in page source that indicate we are on a real product page
PRODUCT_PAGE_MARKERS = [
    '"product"',
    "productintro",
    "product-intro",
    "add to bag",
    "add to cart",
    "sku",
    "price",
    "product-detail",
    "productMainPriceId",
    "fsp-element",
    "productPrice",
]

# URL substrings that indicate a verification/block page
VERIFICATION_URL_PATTERNS = [
    "captcha",
    "challenge",
    "verify",
    "security-check",
    "cf-challenge",
    "turnstile",
]

