#!/usr/bin/env python3
"""
Nanogen CLI — Headless Chrome Image Generator for Gemini Imagen
================================================================
A Python automation tool that launches Chromium in headless mode,
connects to Google Gemini, and automates image generation using
Imagen (Gemini's built-in image generation model).

No display server (X11/Wayland) required — works purely in CLI/SSH.

Author:   Igor Brzezek (original), CLI port
Version:  0.0.1
License:  MIT
Repo:     https://github.com/igorbrzezek/nanogen
"""

import argparse
import atexit
import base64
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime

os.environ["NODE_OPTIONS"] = "--no-warnings"

try:
    from colorama import Fore, Back, Style, init
    init(autoreset=True)
    HAS_COLORAMA = True
except ImportError:
    HAS_COLORAMA = False
    # Dummy colorama stubs
    class Fore:
        RED = YELLOW = GREEN = CYAN = MAGENTA = LIGHTBLUE_EX = WHITE = ""
        BLUE = ""
    class Style:
        RESET_ALL = ""
    class Back:
        pass

# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------
__CODEAUTH__ = "Igor Brzezek"
__CODEVER__ = "0.0.20"
__CODEDATE__ = "09.07.2026"
__CODEGIT__ = "https://github.com/igorbrzezek/nanogen"

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
USE_COLOR = False
DEBUG_MODE = False
OVERWRITE_MODE = False
SKIP_MODE = False
STAT_MODE = False
HEADLESS_MODE = True
CHAT_MODE = "tmp"
NOEX_MODE = False

ERROR_LOG = []
TOTAL_IMAGES = 0
SUCCESS_COUNT = 0
FAIL_COUNT = 0
START_TIME = None
LOG_FILE = None
LOG_LINES = []
RATIO_1TO1_COUNT = 0
RETRY_DOWNLOAD_COUNT = 0
LIMIT_WAIT_TIME = 300

DEFAULT_USER_DATA_DIR = os.path.expanduser("~/.config/nanogen/chrome_profile")


# ---------------------------------------------------------------------------
# Logger — capture stdout/stderr (for --savescr)
# ---------------------------------------------------------------------------
class Logger:
    def __init__(self, filename):
        self.terminal = sys.stdout
        self.log = open(filename, "a", encoding="utf-8")
        self.ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

    def write(self, message):
        self.terminal.write(message)
        clean = self.ansi_escape.sub("", message)
        self.log.write(clean)
        self.log.flush()

    def flush(self):
        self.terminal.flush()
        self.log.flush()


# ---------------------------------------------------------------------------
# Print helpers
# ---------------------------------------------------------------------------
def _c(color, msg):
    if USE_COLOR and color:
        return f"{color}{msg}{Style.RESET_ALL}"
    return msg


def print_error(msg):
    print(_c(Fore.RED, msg))


def print_warning(msg):
    print(_c(Fore.YELLOW, msg))


def print_success(msg):
    print(_c(Fore.GREEN, msg))


def print_info(msg):
    print(msg)


def print_debug(msg):
    if DEBUG_MODE:
        print(_c(Fore.CYAN, msg))


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------
def get_final_filename(filename_base):
    _, ext = os.path.splitext(filename_base)
    if ext.lower() in [".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"]:
        return filename_base
    if NOEX_MODE:
        return filename_base
    return f"{filename_base}.png"


# ---------------------------------------------------------------------------
# Login helpers
# ---------------------------------------------------------------------------
def auto_login_to_google(page, email, password):
    """Automate Google sign-in with provided credentials."""
    print_info("Attempting automated Google login...")
    try:
        page.goto("https://accounts.google.com/signin", timeout=60000, wait_until="domcontentloaded")
        time.sleep(3)

        # If already redirected away from signin (already logged in), success
        if "accounts.google.com" not in page.url.lower():
            print_debug("Already logged into Google account, navigating to Gemini...")
            page.goto("https://gemini.google.com/app", timeout=60000, wait_until="domcontentloaded")
            time.sleep(3)
            return check_gemini_logged_in(page)

        # Check if there is an account picker (list of saved accounts) — click the first one
        for sel in ['[data-identifier]', 'div[data-email]', 'div[role="button"]:has-text(email)',
                    '[data-email="{email}"]'.format(email=email)]:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=2000):
                    btn.click()
                    print_debug(f"Clicked saved account: {sel}")
                    time.sleep(3)
                    break
            except Exception:
                continue

        # Wait briefly and see what we got
        time.sleep(1)
        has_password_field = False
        try:
            has_password_field = page.locator('input[type="password"]').first.is_visible(timeout=2000)
        except Exception:
            pass

        if has_password_field:
            # Account was pre-selected, only password needed
            print_debug("Password field visible directly — account was pre-selected.")
            password_field = page.locator('input[type="password"]').first
        else:
            # Need to enter email first
            email_input = None
            for sel in ['input[type="email"]', 'input[name="identifier"]',
                        '#identifierId', 'input[aria-label*="Email"]', 'input[aria-label*="email"]',
                        'input[aria-label*="Adres"]']:
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=3000):
                        email_input = el
                        print_debug(f"Email field found: {sel}")
                        break
                except Exception:
                    continue

            if not email_input:
                print_error("Email input field not found on sign-in page")
                return False

            email_input.click(force=True)
            time.sleep(0.3)
            email_input.fill(email)
            time.sleep(0.5)

            # Click Next
            next_btn = None
            for sel in ['button:has-text("Next")', 'button:has-text("Dalej")',
                        'span:has-text("Next")', 'span:has-text("Dalej")',
                        '#identifierNext button', '[aria-label*="Next"]']:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(timeout=3000):
                        next_btn = btn
                        break
                except Exception:
                    continue

            if next_btn:
                next_btn.click(force=True)
                print_debug("Clicked Next after email")
            else:
                page.keyboard.press("Enter")
            time.sleep(3)

            # Wait for password field — up to 15s
            password_field = None
            # DEBUG: print what Google shows after email
            print_debug(f"Page title after email: {page.title()}")
            try:
                bt_after_email = page.evaluate("() => document.body.innerText")
                print_debug(f"body.innerText (first 2000 chars): {bt_after_email[:2000]}")
            except Exception as e:
                print_debug(f"Could not read body text: {e}")
            print_debug(f"Current URL after email: {page.url}")

            for attempt in range(15):
                time.sleep(1)
                for sel in ['input[type="password"]', 'input[name="Passwd"]',
                            'input[aria-label*="Password"]', 'input[aria-label*="hasło"]']:
                    try:
                        el = page.locator(sel).first
                        if el.is_visible(timeout=500):
                            password_field = el
                            print_debug(f"Password field found: {sel}")
                            break
                    except Exception:
                        continue
                if password_field:
                    break
                # Early exit if error appeared before password field
                try:
                    body_text = page.evaluate("() => document.body.innerText")
                    if body_text:
                        bt = body_text.lower()
                        for err_ind in ["couldn't find your account", "nie znaleziono konta",
                                        "couldn't sign you in", "incorrect email",
                                        "nieprawidłowy adres email", "nie znaleziono"]:
                            if err_ind in bt:
                                print_error(f"Login failed: Google says '{err_ind}'.")
                                return False
                except Exception:
                    pass

            if not password_field:
                print_error("Password field not found. Check email or 2FA requirement.")
                return False

        # Now we have password_field — fill and submit
        password_field.click(force=True)
        time.sleep(0.3)
        password_field.fill(password)
        time.sleep(0.5)

        # Click Next / Sign in
        pwd_next = False
        for sel in ['button:has-text("Next")', 'button:has-text("Dalej")',
                    'button:has-text("Sign in")', 'button:has-text("Zaloguj się")',
                    'button:has-text("Zaloguj")',
                    'span:has-text("Next")', 'span:has-text("Dalej")',
                    '#passwordNext button', '[aria-label*="Next"]']:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=3000):
                    btn.click(force=True)
                    pwd_next = True
                    print_debug(f"Clicked password submit: {sel}")
                    time.sleep(3)
                    break
            except Exception:
                continue

        if not pwd_next:
            page.keyboard.press("Enter")
            time.sleep(3)

        # Poll for 20 seconds: check for error OR url change
        poll_start = time.time()
        while time.time() - poll_start < 20:
            time.sleep(1)
            try:
                cur = page.url.lower()

                # URL changed away from accounts → success
                if "accounts.google.com" not in cur and "signin" not in cur:
                    print_debug(f"Login URL changed to: {cur[:80]}")
                    page.goto("https://gemini.google.com/app", timeout=60000, wait_until="domcontentloaded")
                    time.sleep(3)
                    if check_gemini_logged_in(page):
                        return True
                    else:
                        print_error("Login verification failed after URL change.")
                        return False

                # Check error text
                body_text = page.evaluate("() => document.body.innerText")
                if body_text:
                    bt = body_text.lower()
                    for err_ind in ["wrong password", "incorrect password", "nieprawidłowe hasło",
                                    "couldn't find your account", "nie znaleziono konta",
                                    "invalid password", "bad password",
                                    "couldn't sign you in",
                                    "hasło jest nieprawidłowe", "złe hasło"]:
                        if err_ind in bt:
                            print_error(f"Login failed: Google says '{err_ind}'.")
                            return False

                if "challenge" in cur:
                    print_error("Google requires additional verification (2FA/phone prompt). Cannot automate.")
                    return False
            except Exception:
                continue

        # Timeout — still on sign-in
        print_error("Login failed: still on Google sign-in page after 20s.")
        return False

    except Exception as e:
        print_error(f"Login automation error: {e}")
        return False


def read_login_file(filepath):
    """Read email and password from a two-line file."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f.readlines() if l.strip()]
        if len(lines) < 2:
            print_error(f"Login file '{filepath}' must have at least two lines: email and password")
            sys.exit(1)
        return lines[0], lines[1]
    except FileNotFoundError:
        print_error(f"Login file not found: {filepath}")
        sys.exit(1)
    except Exception as e:
        print_error(f"Error reading login file: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Browser management
# ---------------------------------------------------------------------------
def ensure_playwright_browsers():
    """Check if Playwright Chromium is installed; guide user if not."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            executable_path = p.chromium.executable_path
            if not os.path.exists(executable_path):
                raise FileNotFoundError(f"Browser not found at {executable_path}")
    except (ImportError, FileNotFoundError, Exception) as e:
        print_error("Playwright Chromium browser is not installed.")
        print_info("Run the following command to install it:")
        print_info("  playwright install chromium")
        print_info("Or use the --install-browser flag:")
        print_info(f"  python {os.path.basename(sys.argv[0])} --install-browser")
        sys.exit(1)


def install_browsers():
    """Install Playwright system dependencies and Chromium."""
    print_info("Installing Playwright system dependencies...")
    try:
        subprocess.check_call(
            [sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"]
        )
        print_success("Playwright Chromium installed successfully!")
    except subprocess.CalledProcessError as e:
        print_error(f"Installation failed: {e}")
        sys.exit(1)


def launch_browser(user_data_dir=None, headless=True):
    """Launch Chromium via Playwright and return (playwright, context, page)."""
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()

    # Default launch args for headless Chrome
    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--window-size=1920,1080",
        "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    ]

    if user_data_dir:
        os.makedirs(user_data_dir, exist_ok=True)
        print_debug(f"Using persistent profile: {user_data_dir}")
        context = pw.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=headless,
            args=launch_args,
            ignore_default_args=["--enable-automation"],
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="UTC",
        )
        page = context.pages[0] if context.pages else context.new_page()
    else:
        browser = pw.chromium.launch(
            headless=headless,
            args=launch_args,
            ignore_default_args=["--enable-automation"],
        )
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="UTC",
        )
        page = context.new_page()

    # Stealth: override webdriver detection
    page.add_init_script("""
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    """)

    return pw, context, page


def check_gemini_logged_in(page):
    """Check if we are logged into Gemini. Returns True if ready."""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        time.sleep(2)
        current_url = page.url.lower()
        print_debug(f"[check_login] initial URL: {current_url}")
        if "accounts.google.com" in current_url or "signin" in current_url:
            print_debug("[check_login] redirected to accounts/signin — not logged in")
            return False
        if "gemini.google.com" not in current_url:
            print_debug(f"[check_login] not on gemini ({current_url}), navigating...")
            page.goto("https://gemini.google.com/app", timeout=60000, wait_until="domcontentloaded")
            time.sleep(3)

        page.wait_for_load_state("domcontentloaded", timeout=30000)
        time.sleep(2)
        current_url = page.url.lower()
        print_debug(f"[check_login] after navigation URL: {current_url}")
        if "accounts.google.com" in current_url:
            print_debug("[check_login] redirected to accounts.google.com — not logged in")
            return False

        # Try to detect the prompt input as proof of login
        try:
            page.locator('[contenteditable="true"]').first.wait_for(timeout=10000)
            print_debug("[check_login] found contenteditable — logged in")
            return True
        except Exception:
            print_debug("[check_login] contenteditable not found, checking fallback...")
            pass

        # Fallback: check URL and page title
        page_title = page.title()
        print_debug(f"[check_login] fallback — URL: {current_url}, title: {page_title}")
        try:
            body_text = page.evaluate("() => document.body.innerText")
            print_debug(f"[check_login] body.innerText (first 500): {body_text[:500]}")
        except Exception:
            pass

        if "gemini.google.com" in current_url:
            print_debug("[check_login] on gemini URL — assuming logged in")
            return True
        print_debug("[check_login] not on gemini URL — not logged in")
        return False
    except Exception as e:
        print_debug(f"[check_login] exception: {e}")
        return False


def guide_login(page, headless):
    """Print guidance for logging into Gemini."""
    if headless:
        print_error("Not logged into Google Gemini.")
        print_info("")
        print_info("You need to log in first. Options:")
        print_info("  1. Run once with --no-headless to log in:")
        print_info(f"     python {os.path.basename(sys.argv[0])} --prompt test --no-headless")
        print_info("")
        print_info("  2. Copy an existing Chrome profile to:")
        print_info(f"     {DEFAULT_USER_DATA_DIR}")
        print_info("")
        print_info("  3. Use --user-data-dir to point to an existing Chrome profile")
        print_info("     (e.g., ~/.config/google-chrome/Default or similar)")
        sys.exit(1)
    else:
        print_warning("Please log into your Google account in the opened browser.")
        print_info("Navigate to https://gemini.google.com and sign in.")
        print_info("Waiting for login (checking every 5 seconds)...")
        for _ in range(120):  # 10 minute timeout
            time.sleep(5)
            if check_gemini_logged_in(page):
                print_success("Login detected! Proceeding...")
                return
        print_error("Login timeout. Please try again.")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------
def download_image_from_url(url, filepath):
    """Download image from URL using requests or urllib."""
    global SUCCESS_COUNT, FAIL_COUNT, ERROR_LOG
    start_time = time.time()
    try:
        try:
            import requests
            res = requests.get(url, timeout=30)
            if res.status_code == 200:
                data = res.content
            else:
                raise Exception(f"HTTP {res.status_code}")
        except ImportError:
            from urllib.request import urlopen
            with urlopen(url, timeout=30) as resp:
                data = resp.read()

        with open(filepath, "wb") as f:
            f.write(data)

        stats = None
        if STAT_MODE:
            try:
                from PIL import Image
                file_size = os.path.getsize(filepath) / 1024
                img = Image.open(filepath)
                stats = {"time": time.time() - start_time, "resolution": img.size, "size": file_size}
            except Exception:
                pass

        SUCCESS_COUNT += 1
        return True, stats
    except Exception as e:
        error_msg = f"Download failed: {e}"
        print_error(error_msg)
        ERROR_LOG.append((filepath, str(e)))
        FAIL_COUNT += 1
        return False, None


# ---------------------------------------------------------------------------
# Core: process a single prompt
# ---------------------------------------------------------------------------
def process_single_prompt(page, prompt, filename_base, output_dir,
                          add_prompt=None, insp_prompt=None,
                          fmt_arg=None, res_arg=None, resx_arg=None, resy_arg=None,
                          type_arg=None, think_mode=None,
                          min_gen_time=30, dl_timeout_sec=45, download_retries=0,
                          progress_prefix="", limit_attempt=0):
    """Full Gemini interaction for one prompt: navigate, configure, submit, wait, download."""
    global SUCCESS_COUNT, FAIL_COUNT, ERROR_LOG, CHAT_MODE, SKIP_MODE, NOEX_MODE
    global RETRY_DOWNLOAD_COUNT, RATIO_1TO1_COUNT, LOG_LINES, LIMIT_WAIT_TIME

    # --skip check
    if SKIP_MODE:
        final_filename = get_final_filename(filename_base)
        filepath = os.path.join(output_dir, final_filename)
        if os.path.exists(filepath):
            file_size = os.path.getsize(filepath)
            _, ext = os.path.splitext(final_filename)
            min_size = 0
            if ext.lower() in [".jpg", ".jpeg"]:
                min_size = 1024
            elif ext.lower() == ".png":
                min_size = 256
            if file_size >= min_size:
                print_info(f"[SKIP] {final_filename}")
                LOG_LINES.append(f"{progress_prefix} {final_filename} | SKIPPED")
                SUCCESS_COUNT += 1
                return True
            else:
                print_warning(f"[RE-GEN] {final_filename} exists but too small ({file_size}B)")

    # Rate limit retry
    limit_retries = 3
    limit_wait_time = LIMIT_WAIT_TIME

    if limit_attempt > 0:
        print_info(f"\n[RETRY] Attempt {limit_attempt}/{limit_retries} after rate-limit wait...")
        if USE_COLOR:
            print(f"{Fore.WHITE}[{Style.RESET_ALL}{Fore.GREEN}{progress_prefix[1:8]}{Style.RESET_ALL}{Fore.WHITE}]{Style.RESET_ALL} {Fore.CYAN}{filename_base}{Style.RESET_ALL} | {Fore.YELLOW}Processing (Retry {limit_attempt})...{Style.RESET_ALL}",
                  end="", flush=True)
        else:
            print(f"{progress_prefix} {filename_base} | Processing (Retry {limit_attempt})...",
                  end="", flush=True)

    # -----------------------------------------------------------------------
    # Navigate + chat mode setup
    # -----------------------------------------------------------------------
    print_debug(f">>> CHAT_MODE = '{CHAT_MODE}' <<<")

    if CHAT_MODE in ("tmp", "chat"):
        print_debug(f"*** {'TEMPORARY' if CHAT_MODE == 'tmp' else 'REGULAR'} CHAT MODE ***")
        try:
            max_nav_retries = 3
            for nav_attempt in range(max_nav_retries):
                try:
                    if nav_attempt > 0:
                        time.sleep(random.uniform(1.0, 3.0))
                    is_gemini = "gemini.google.com" in page.url
                    is_saved = is_gemini and any(p in page.url for p in ["/app/", "/chats/"]) and not page.url.endswith("/app")

                    if not is_gemini or page.url == "about:blank":
                        page.goto("https://gemini.google.com/app", timeout=60000, wait_until="domcontentloaded")
                    elif is_saved:
                        try:
                            new_btn = page.locator('[data-test-id="new-chat-button"]').first
                            if new_btn.is_visible(timeout=2000):
                                new_btn.click()
                                print_debug("Clicked new-chat-button")
                            else:
                                page.goto("https://gemini.google.com/app", timeout=60000, wait_until="domcontentloaded")
                        except Exception:
                            page.goto("https://gemini.google.com/app", timeout=60000, wait_until="domcontentloaded")
                    else:
                        page.reload(timeout=60000, wait_until="domcontentloaded")
                    page.wait_for_load_state("domcontentloaded", timeout=60000)
                    time.sleep(2)
                    break
                except Exception as goto_err:
                    err_str = str(goto_err)
                    if "ERR_ABORTED" in err_str and "gemini.google.com" in page.url:
                        print_debug("(nav aborted but on Gemini, continuing)")
                        break
                    if nav_attempt == max_nav_retries - 1:
                        raise
                    print_debug(f"(nav error, retry {nav_attempt + 1}...)")

            # --- Step 0: dismiss cookie / overlay ---
            try:
                page.evaluate("""() => {
                    const overlay = document.querySelector('.cdk-overlay-container');
                    if (overlay) {
                        const btns = overlay.querySelectorAll('button');
                        for (const btn of btns) {
                            const txt = btn.innerText.toLowerCase();
                            if (txt.includes('accept') || txt.includes('accept all') || txt.includes('zaakceptuj') || txt.includes('zgadzam') || txt.includes('got it') || txt.includes('ok') || txt.includes('continue')) {
                                btn.click();
                                return true;
                            }
                        }
                        overlay.remove();
                        return true;
                    }
                    const cookie = document.querySelector('[class*="cookie"], [id*="cookie"], [class*="consent"], [id*="consent"]');
                    if (cookie) { cookie.remove(); return true; }
                    return false;
                }""")
                time.sleep(1)
                print_debug("Cookie/overlay dismissal attempted")
            except Exception:
                pass

            # --- Step 1: expand sidebar ---
            print_debug("Step 1: expand sidebar...")
            try:
                page.keyboard.press("[")
                time.sleep(1)
                print_debug("Pressed '[' to open sidebar")
            except Exception:
                pass

            menu_visible = False
            for sel in ['text="New chat"', 'text="Nowy czat"', '[role="navigation"]']:
                try:
                    if page.locator(sel).first.is_visible(timeout=1000):
                        menu_visible = True
                        break
                except Exception:
                    continue

            if not menu_visible:
                for sel in ['button[aria-label="Main menu"]',
                            'button[aria-label="Menu główne"]',
                            'button[aria-label*="Menu"]',
                            'button:has(svg)']:
                    try:
                        btn = page.locator(sel).first
                        if btn.is_visible(timeout=1000):
                            btn.click()
                            menu_visible = True
                            time.sleep(2)
                            print_debug("Clicked menu button")
                            break
                    except Exception:
                        continue

            # --- Step 2: activate temp/regular chat ---
            button_found = False
            if CHAT_MODE == "tmp":
                print_debug("Looking for 'Czat tymczasowy' / 'Temporary chat'...")
                tc = page.locator("temp-chat-button").first
                if tc.count() > 0:
                    inner = tc.locator("button, gem-icon-button").first
                    if inner.count() > 0:
                        cls = (inner.get_attribute("class") or "")
                        if "temp-chat-on" in cls:
                            print_debug("Temporary chat already active")
                            button_found = True
                        else:
                            inner.click()
                            time.sleep(3)
                            cls2 = (inner.get_attribute("class") or "")
                            if "temp-chat-on" in cls2:
                                print_debug("Temporary chat activated!")
                                button_found = True

                if not button_found:
                    for sel in ['text="Czat tymczasowy"', 'text="Temporary chat"',
                                'button:has-text("Czat tymczasowy")', 'button:has-text("Temporary chat")',
                                '[aria-label="Czat tymczasowy"]', '[aria-label="Temporary chat"]']:
                        try:
                            btn = page.locator(sel).first
                            if btn.is_visible(timeout=3000):
                                active = False
                                try:
                                    html = btn.evaluate("node => node.outerHTML").lower()
                                    if "temp-chat-on" in html:
                                        active = True
                                except Exception:
                                    pass
                                if active:
                                    button_found = True
                                    break
                                btn.scroll_into_view_if_needed()
                                time.sleep(0.5)
                                try:
                                    btn.click(timeout=5000)
                                except Exception:
                                    btn.click(force=True)
                                button_found = True
                                time.sleep(3)
                                print_debug("Clicked Temporary chat!")
                                break
                        except Exception:
                            continue

                if button_found:
                    print_debug("*** TEMPORARY CHAT ACTIVATED ***")
                else:
                    print_warning("Temporary chat button not found")

            # Fallback: New chat
            if not button_found:
                target_text = "Nowy czat" if CHAT_MODE == "chat" else ("Nowy czat" if not button_found else "")
                for sel in ['text="Nowy czat"', 'text="New chat"',
                            'button:has-text("Nowy czat")', 'button:has-text("New chat")']:
                    try:
                        btn = page.locator(sel).first
                        if btn.is_visible(timeout=2000):
                            btn.scroll_into_view_if_needed()
                            time.sleep(0.5)
                            try:
                                btn.click(timeout=5000)
                            except Exception:
                                btn.click(force=True)
                            time.sleep(3)
                            print_debug(f"Clicked New chat via: {sel}")
                            break
                    except Exception:
                        continue

        except Exception as e:
            print_warning(f"Chat setup error: {e}")
    else:
        # native mode
        print_debug("*** NATIVE MODE ***")
        if "gemini.google.com" not in page.url:
            page.goto("https://gemini.google.com/", timeout=60000, wait_until="domcontentloaded")
            time.sleep(2)

    print_debug(">>> CHAT SETUP DONE <<<")

    # -----------------------------------------------------------------------
    # Model type selection
    # -----------------------------------------------------------------------
    if type_arg:
        print_debug(f"Selecting model: {type_arg}")
        try:
            selector_opened = False
            for sel in ['[data-test-id="bard-mode-menu-button"]',
                        'button[aria-label="Otwórz selektor trybu"]',
                        'button[aria-label="Open model selector"]']:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(timeout=1000):
                        btn.click()
                        selector_opened = True
                        time.sleep(1.5)
                        print_debug(f"Opened model selector via: {sel}")
                        break
                except Exception:
                    continue

            if not selector_opened:
                for name in ["Szybki", "Szybkie", "Myślący", "Flash", "Thinking", "Pro", "Gemini"]:
                    try:
                        btn = page.locator(f'button:has-text("{name}")').first
                        if btn.is_visible(timeout=800):
                            btn.click()
                            selector_opened = True
                            time.sleep(1.5)
                            print_debug(f"Opened model selector via button: {name}")
                            break
                    except Exception:
                        continue

            type_map = {
                "fast": ["3.5 Flash", "Gemini 3.5 Flash", "1.5 Flash", "Gemini 1.5 Flash", "Szybki", "Szybkie", "Flash 2.0", "Flash", "Fast"],
                "think": ["Myślący", "Myślenie", "Thinking", "Deep Thinking"],
                "pro": ["3.1 Pro", "Gemini 3.1 Pro", "1.5 Pro", "Gemini 1.5 Pro", "Pro 2.0", "Gemini 2.0 Pro", "Pro", "Zaawansowany", "Advanced"],
                "flash": ["3.5 Flash", "Gemini 3.5 Flash", "1.5 Flash", "Flash 2.0", "Gemini Flash", "Flash"],
                "flash-lite": ["3.1 Flash-Lite", "Flash-Lite 3.1", "1.5 Flash-8B", "Flash-Lite", "Flash Lite"],
            }
            terms = type_map.get(type_arg.lower(), [])
            clicked = False
            for _ in range(2):
                if clicked:
                    break
                for term in terms:
                    if clicked:
                        break
                    for sel in [f'[role="menu"] :text-is("{term}")',
                                f'[role="menu"] :has-text("{term}")',
                                f'[role="listbox"] :text-is("{term}")',
                                f'[role="listbox"] :has-text("{term}")']:
                        try:
                            el = page.locator(sel).first
                            if el.is_visible(timeout=1500):
                                el.scroll_into_view_if_needed()
                                el.click(timeout=3000)
                                clicked = True
                                print_debug(f"Model '{term}' selected via: {sel}")
                                time.sleep(1)
                                break
                        except Exception:
                            continue
                    if not clicked:
                        try:
                            ok = page.evaluate(f"""(target) => {{
                                const menus = document.querySelectorAll('[role="menu"], [role="listbox"]');
                                for (const m of menus) {{
                                    const walker = document.createTreeWalker(m, NodeFilter.SHOW_ELEMENT);
                                    let node;
                                    while (node = walker.nextNode()) {{
                                        let t = node.innerText?.trim();
                                        if (t === target || (t && t.includes(target) && t.length <= target.length + 30)) {{
                                            if (target === "Flash" && (t.includes("Lite") || (node.parentNode?.innerText || "").includes("Lite"))) continue;
                                            node.click();
                                            return true;
                                        }}
                                    }}
                                }}
                                return false;
                            }}""", term)
                            if ok:
                                clicked = True
                                print_debug(f"Model '{term}' via JS walker")
                                time.sleep(1)
                        except Exception:
                            continue
            if not clicked:
                print_warning(f"Model type '{type_arg}' not found")
        except Exception as e:
            print_warning(f"Model selection error: {e}")

    # -----------------------------------------------------------------------
    # Thinking mode
    # -----------------------------------------------------------------------
    if think_mode:
        print_debug(f"Setting thinking mode: {think_mode}")
        try:
            page.keyboard.press("Escape")
            time.sleep(0.3)

            # Try to open the thinking section by clicking the heading
            heading_clicked = False
            for head_sel in ['button:has-text("Poziom myślenia")', 'span:has-text("Poziom myślenia")',
                             'button:has-text("Level of thinking")',
                             'button:has-text("Thinking")', 'button:has-text("Myślenie")',
                             '[aria-label*="thinking"]', '[aria-label*="myślen"]']:
                try:
                    el = page.locator(head_sel).first
                    if el.is_visible(timeout=1000):
                        el.click()
                        heading_clicked = True
                        print_debug(f"Clicked thinking section heading: {head_sel}")
                        time.sleep(0.5)
                        break
                except Exception:
                    continue

            if not heading_clicked:
                # Try clicking model selector first, then heading
                try:
                    btn = page.locator('[data-test-id="bard-mode-menu-button"]').first
                    if btn.is_visible(timeout=1000):
                        btn.click()
                        time.sleep(1)
                        for head_sel in ['button:has-text("Poziom myślenia")', 'span:has-text("Poziom myślenia")',
                                         'button:has-text("Thinking")', 'button:has-text("Myślenie")',
                                         '[aria-label*="thinking"]']:
                            try:
                                el = page.locator(head_sel).first
                                if el.is_visible(timeout=1000):
                                    el.click()
                                    heading_clicked = True
                                    time.sleep(0.5)
                                    break
                            except Exception:
                                continue
                except Exception:
                    pass

            targets = [
                "Myślenie standardowe", "Standardowe myślenie", "Standardowy", "Podstawowy",
                "Basic thinking", "Standard thinking", "Basic",
            ] if think_mode == "basic" else [
                "Myślenie rozszerzone", "Rozszerzone myślenie", "Rozszerzony",
                "Extended thinking", "Deep thinking", "Advanced thinking",
                "Extended", "Zaawansowany", "Deep",
            ]

            found = False
            for tgt in targets:
                for sel in [
                    f'[role="menu"] [role="radio"]:text-is("{tgt}")',
                    f'[role="menu"] [role="menuitemradio"]:text-is("{tgt}")',
                    f'[role="menu"] button:text-is("{tgt}")',
                    f'[role="listbox"] [role="radio"]:text-is("{tgt}")',
                    f'[role="listbox"] [role="option"]:text-is("{tgt}")',
                    f'[role="radio"]:text-is("{tgt}")',
                    f'[role="menuitemradio"]:text-is("{tgt}")',
                    f'button:has-text("{tgt}")',
                    f'[role="option"]:has-text("{tgt}")',
                    f'*:text-is("{tgt}")',
                ]:
                    try:
                        el = page.locator(sel).first
                        if el.is_visible(timeout=1000):
                            el.click()
                            found = True
                            print_debug(f"Thinking mode '{tgt}' selected via: {sel}")
                            time.sleep(0.5)
                            break
                    except Exception:
                        continue
                if found:
                    break

            # JS fallback: walk entire DOM for matching text
            if not found:
                for tgt in targets:
                    try:
                        ok = page.evaluate(f"""(target) => {{
                            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
                            let node;
                            while (node = walker.nextNode()) {{
                                let t = node.innerText?.trim();
                                if (t === target || (t && t.includes(target) && t.length <= target.length + 25)) {{
                                    if (target.toLowerCase().includes("flash") && (t.includes("Lite") || (node.parentNode?.innerText || "").includes("Lite"))) continue;
                                    node.click();
                                    return true;
                                }}
                            }}
                            return false;
                        }}""", tgt)
                        if ok:
                            found = True
                            print_debug(f"Thinking mode '{tgt}' via JS document walker")
                            time.sleep(0.5)
                            break
                    except Exception:
                        continue

            if not found:
                print_warning(f"Thinking mode '{think_mode}' not found")

            # Close any open menus
            try:
                page.keyboard.press("Escape")
                time.sleep(0.3)
            except Exception:
                pass

        except Exception as e:
            print_warning(f"Thinking mode error: {e}")

    # -----------------------------------------------------------------------
    # Tools → Create images
    # -----------------------------------------------------------------------
    try:
        # Dismiss any overlay before tools
        try:
            page.evaluate("""() => {
                const c = document.querySelector('.cdk-overlay-container');
                if (c) c.remove();
            }""")
        except Exception:
            pass

        # Strategy 1: Look for a visible "Create images" / "Twórz obrazy" button directly
        # (Gemini may have it as a toggle on the toolbar, no Tools menu needed)
        print_debug("Looking for Create images button directly...")
        direct_found = False
        for sel in ['button:has-text("Twórz obrazy")', 'button:has-text("Create images")',
                    'button:has-text("Utwórz obraz")', 'button:has-text("Generuj obrazy")',
                    'span:has-text("Twórz obrazy")', 'span:has-text("Create images")',
                    '[aria-label*="Twórz obrazy"]', '[aria-label*="Create images"]',
                    '[aria-label*="obraz"]', '[aria-label*="image"]',
                    'button:has-text("Obrazy")', 'button:has-text("Images")']:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=2000) and not btn.is_disabled():
                    btn.click()
                    direct_found = True
                    time.sleep(1.5)
                    print_debug(f"Clicked Create images directly: {sel}")
                    break
            except Exception:
                continue

        if direct_found:
            pass
        elif not direct_found:
            # Strategy 1b: look for toolbar icon buttons (image icon / camera icon)
            print_debug("Looking for image icon in toolbar...")
            for sel in ['button[aria-label*="obraz"]', 'button[aria-label*="image"]',
                        'button[aria-label*="zdjęcie"]', 'button[aria-label*="photo"]',
                        'button[aria-label*="generuj"]', 'button[aria-label*="generate"]',
                        'mat-icon:has-text("image")', 'mat-icon:has-text("photo")',
                        '[data-test-id*="image"] button', '[data-test-id*="image-tool"]']:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(timeout=2000) and not btn.is_disabled():
                        btn.click()
                        direct_found = True
                        time.sleep(1.5)
                        print_debug(f"Clicked image icon via: {sel}")
                        break
                except Exception:
                    continue

        if direct_found:
            pass
        else:
            print_debug("No direct Create images button, trying Tools menu...")
            tools_clicked = False
            for sel in ['button:has-text("Narzędzia")', 'button:has-text("Tools")',
                        'button[aria-label*="narzędzi"]', 'button[aria-label*="tool"]']:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(timeout=3000) and not btn.is_disabled():
                        btn.click()
                        tools_clicked = True
                        time.sleep(1.5)
                        print_debug(f"Clicked Tools: {sel}")
                        break
                except Exception:
                    continue

            if tools_clicked:
                # DEBUG: dump all visible menu items
                try:
                    items = page.evaluate("""() => {
                        const menuItems = document.querySelectorAll('[role="menuitem"], [role="menuitemradio"], .menu-item, [class*="menu-item"]');
                        return Array.from(menuItems).map(el => el.innerText.trim()).filter(t => t);
                    }""")
                    print_debug(f"Tools menu items: {items}")
                except Exception as e:
                    print_debug(f"Could not dump menu items: {e}")

                for sel in ['text="Utwórz obraz"', 'text="Twórz obrazy"', 'text="Create images"',
                            'text="Generuj obrazy"', 'span:has-text("Utwórz obraz")',
                            'span:has-text("Twórz obrazy")', 'span:has-text("Create images")',
                            '[role="menuitem"]:has-text("obraz")', '[role="menuitem"]:has-text("image")',
                            'button:has-text("obraz")', 'button:has-text("image")']:
                    try:
                        el = page.locator(sel).first
                        if el.is_visible(timeout=3000) and not el.is_disabled():
                            el.click()
                            print_debug(f"Clicked Create images: {sel}")
                            time.sleep(1.5)
                            break
                    except Exception:
                        continue
            else:
                print_debug("Tools button not found either")

        # Last resort: JS fallback — find ANY button with image-related text
        if not direct_found:
            try:
                clicked = page.evaluate("""() => {
                    const keywords = ['obraz', 'image', 'zdjęcie', 'photo', 'generuj', 'generate', 'utwórz', 'create', 'twórz'];
                    const allButtons = document.querySelectorAll('button, [role="button"], [role="menuitem"]');
                    for (const btn of allButtons) {
                        const text = (btn.innerText || '').toLowerCase().trim();
                        const aria = (btn.getAttribute('aria-label') || '').toLowerCase();
                        if (!text && !aria) continue;
                        for (const kw of keywords) {
                            if (text.includes(kw) || aria.includes(kw)) {
                                if (btn.offsetParent !== null) {
                                    btn.click();
                                    return 'JS fallback clicked: ' + (text || aria).slice(0, 60);
                                }
                            }
                        }
                    }
                    return 'JS fallback: no image button found';
                }""")
                print_debug(f"JS fallback result: {clicked}")
                time.sleep(1.5)
            except Exception as e:
                print_debug(f"JS fallback error: {e}")
    except Exception as e:
        print_warning(f"Tools activation error: {e}")
    working_prompt = prompt

    if insp_prompt:
        sep = " " if insp_prompt.strip().endswith(".") else ". "
        working_prompt = f"{insp_prompt}{sep}{working_prompt}"

    if add_prompt:
        sep = " " if working_prompt.strip().endswith(".") else ". "
        working_prompt = f"{working_prompt}{sep}{add_prompt}"

    final_prompt = working_prompt

    if fmt_arg:
        ar_map = {"43": " Aspect ratio 4:3.", "169": " Aspect ratio 16:9.", "11": " Aspect ratio 1:1."}
        suffix = ar_map.get(fmt_arg, "")
        if suffix:
            if not final_prompt.rstrip().endswith("."):
                final_prompt = f"{final_prompt}. {suffix.strip()}"
            else:
                final_prompt = f"{final_prompt} {suffix.strip()}"

    width = height = None
    if res_arg:
        try:
            w, h = map(int, res_arg.split(","))
            width, height = w, h
        except ValueError:
            print_warning(f"Invalid --res format: {res_arg}")
    elif resx_arg or resy_arg:
        aw, ah = 16, 9
        if fmt_arg == "43":
            aw, ah = 4, 3
        elif fmt_arg == "11":
            aw, ah = 1, 1
        if resx_arg:
            try:
                width = int(resx_arg)
                height = int(width * ah / aw)
            except (ValueError, TypeError):
                print_warning(f"Invalid --resx: {resx_arg}")
        elif resy_arg:
            try:
                height = int(resy_arg)
                width = int(height * aw / ah)
            except (ValueError, TypeError):
                print_warning(f"Invalid --resy: {resy_arg}")

    if width and height:
        final_prompt = f"{final_prompt} --size {width}x{height}"

    print_debug(f"Final prompt ({len(final_prompt)} chars): {final_prompt[:80]}...")

    # -----------------------------------------------------------------------
    # Send prompt
    # -----------------------------------------------------------------------
    new_resp = page
    initial_count = 0
    try:
        initial_count = page.locator("model-response").count()
    except Exception:
        pass

    try:
        # Find input
        input_locator = None
        for sel in ['rich-textarea .ql-editor', 'div[contenteditable="true"]',
                    '.ql-editor[contenteditable="true"]']:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=2000):
                    input_locator = loc
                    print_debug(f"Input found: {sel}")
                    break
            except Exception:
                continue

        if not input_locator:
            print_error("Input field not found")
            return False

        # Force-dismiss any overlay before clicking input
        try:
            page.evaluate("""() => {
                const c = document.querySelector('.cdk-overlay-container');
                if (c) c.remove();
                const co = document.querySelector('[class*="cookie"], [id*="cookie"], [class*="consent"]');
                if (co) co.remove();
            }""")
        except Exception:
            pass
        time.sleep(0.3)

        input_locator.click(force=True)
        time.sleep(0.5)

        # Clear + type text in headless-compatible way
        try:
            input_locator.press("Control+A")
            time.sleep(0.2)
        except Exception:
            pass

        # Use keyboard.insert_text for headless compatibility
        print_debug("Typing prompt...")
        input_locator.fill("")
        time.sleep(0.2)
        page.keyboard.insert_text(final_prompt)
        time.sleep(1)

        # Verify
        try:
            entered = input_locator.inner_text()
            print_debug(f"Entered {len(entered)} chars, expected {len(final_prompt)}")
        except Exception:
            pass

        # Click send
        send_clicked = False
        for sel in ['button[aria-label*="Wyślij"]', 'button[aria-label*="Send"]',
                    'button[aria-label*="send"]', 'button[data-test-id*="send"]']:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=2000) and not btn.is_disabled():
                    btn.click(force=True)
                    send_clicked = True
                    print_debug(f"Clicked send: {sel}")
                    break
            except Exception:
                continue
        if not send_clicked:
            print_debug("Send button not found, pressing Enter...")
            page.keyboard.press("Enter")

        # Wait for response
        print_debug("Waiting for model-response container...")
        wait_start = time.time()
        new_response = False
        while time.time() - wait_start < 30:
            try:
                cur = page.locator("model-response").count()
                if cur > initial_count:
                    new_resp = page.locator("model-response").nth(initial_count)
                    new_response = True
                    print_debug(f"New response detected (count: {cur})")
                    break
            except Exception:
                pass
            time.sleep(0.5)

        if not new_response:
            print_warning("Timeout waiting for response, using last container")
            new_resp = page.locator("model-response").last

        print_debug("Prompt sent, waiting for image...")

    except Exception as e:
        print_error(f"Error sending prompt: {e}")
        return False

    # -----------------------------------------------------------------------
    # Wait for image + download
    # -----------------------------------------------------------------------
    try:
        max_wait = max(min_gen_time + 60, 120)
        elapsed = 0.0

        image_selectors = [
            "generated-image img", "single-image img", "div.image-container img",
            'img[src*="googleusercontent.com"]:not([src*="/a/"])', 'img[src*="imagen"]',
            'img[alt*="Generated"]', 'img[alt*="Wygenerowany"]', 'img[src^="blob:"]',
        ]

        button_selectors = [
            "download-generated-image-button",
            "download-generated-image-button gem-icon-button",
            "download-generated-image-button button",
            'button[aria-label*="pełnym rozmiarze"]',
            'button[aria-label*="full size"]',
            'button[aria-label="Pobierz"]',
            'button[aria-label="Download"]',
            'button[aria-label*="Pobierz"]',
            'button[aria-label*="Download"]',
            '[aria-label*="Pobierz"]',
            '[aria-label*="Download"]',
            'button:has-text("Pobierz")',
            'button:has-text("Download")',
            '[role="button"][aria-label*="Download"]',
            '[data-test-id*="download"]',
        ]

        error_indicators = [
            "nie mogę wygenerować", "can't generate", "cannot generate",
            "nie potrafię wygenerować", "nie jestem w stanie",
            "something went wrong", "wystąpił błąd", "błąd podczas",
            "przekroczono limit", "limit reached", "too many requests",
        ]

        download_button = None
        image_found = False
        image_elem = None

        # Minimum wait phase
        min_remaining = min_gen_time
        while min_remaining > 0:
            check = min(5, min_remaining)
            time.sleep(check)
            elapsed += check
            min_remaining -= check

            # Check for image
            if not image_found:
                for sel in image_selectors:
                    try:
                        ie = new_resp.locator(sel).last
                        if ie.is_visible(timeout=100):
                            image_found = True
                            image_elem = ie
                            print_debug(f"Image detected at {elapsed:.0f}s via: {sel}")
                            try:
                                ie.hover(timeout=1000)
                                time.sleep(0.5)
                            except Exception:
                                pass
                            break
                    except Exception:
                        continue

            # Check for download button
            if image_found and not download_button:
                for sel in button_selectors:
                    try:
                        db = new_resp.locator(sel).last
                        if db.is_visible(timeout=100):
                            download_button = db
                            print_debug(f"Download button at {elapsed:.0f}s via: {sel}")
                            break
                    except Exception:
                        continue

            if download_button and download_button.is_visible(timeout=100):
                print_debug("Ready before min wait completed")
                break

        # Main polling phase
        print_debug(f"Polling for image (max {max_wait}s)...")
        while elapsed < max_wait:
            # Check for errors/refusals
            try:
                if page.locator("model-response").count() > 0:
                    text = page.locator("model-response").last.inner_text().lower()
                else:
                    text = ""

                for ind in error_indicators:
                    if ind in text:
                        if "limit" in ind or "przekroczono" in ind:
                            if limit_attempt < limit_retries:
                                print_warning(f"\n[LIMIT] Waiting {limit_wait_time}s before retry...")
                                time.sleep(limit_wait_time)
                                return process_single_prompt(
                                    page, prompt, filename_base, output_dir,
                                    add_prompt, insp_prompt, fmt_arg, res_arg, resx_arg, resy_arg,
                                    type_arg, think_mode, min_gen_time, dl_timeout_sec,
                                    download_retries, progress_prefix, limit_attempt + 1,
                                )
                            else:
                                print_error("[LIMIT] Retries exhausted")
                        print_error(f"Gemini error: '{ind}'")
                        FAIL_COUNT += 1
                        ERROR_LOG.append((filename_base, f"Gemini error: {ind}"))
                        return False
            except Exception:
                pass

            # Check image
            if not image_found:
                for sel in image_selectors:
                    try:
                        ie = new_resp.locator(sel).last
                        if ie.is_visible(timeout=100):
                            image_found = True
                            image_elem = ie
                            print_debug(f"Image at {elapsed:.0f}s via: {sel}")
                            try:
                                ie.hover(timeout=1000)
                                time.sleep(0.5)
                            except Exception:
                                pass
                            break
                    except Exception:
                        continue

            # Check download button
            if not download_button:
                for sel in button_selectors:
                    try:
                        db = new_resp.locator(sel).last
                        if db.is_visible(timeout=100):
                            download_button = db
                            print_debug(f"Download button at {elapsed:.0f}s via: {sel}")
                            break
                    except Exception:
                        continue

            if image_found and download_button:
                try:
                    if download_button.is_visible(timeout=100):
                        print_debug(f"Ready after {elapsed:.0f}s")
                        break
                except Exception:
                    download_button = None

            time.sleep(1)
            elapsed += 1
            if elapsed % 5 == 0:
                status = f"{'image' if image_found else 'waiting for image'}, {'btn' if download_button else 'no btn'}"
                print_debug(f"... {elapsed:.0f}s ({status})")

        # -------------------------------------------------------------------
        # Download
        # -------------------------------------------------------------------
        final_filename = get_final_filename(filename_base)
        filepath = os.path.join(output_dir, final_filename)

        if os.path.exists(filepath):
            if SKIP_MODE:
                print_warning(f"Skipping existing: {final_filename}")
                return True
            elif not OVERWRITE_MODE:
                resp = input(f"Overwrite '{final_filename}'? (y/n): ").strip().lower()
                if resp != "y":
                    print_info(f"Skipped: {final_filename}")
                    return True

        download_success = False

        # Strategy 1: official download button
        if download_button:
            try:
                if download_button.is_visible(timeout=1000):
                    print_debug("Attempting download via button...")
                    time.sleep(2)
                    for attempt in range(download_retries + 1):
                        if attempt > 0:
                            RETRY_DOWNLOAD_COUNT += 1
                            print_warning(f"Download retry {attempt}/{download_retries}")
                            time.sleep(2)
                            # Re-locate
                            for sel in button_selectors:
                                try:
                                    db = new_resp.locator(sel).last
                                    if db.is_visible(timeout=500):
                                        download_button = db
                                        break
                                except Exception:
                                    continue

                        try:
                            with page.expect_download(timeout=dl_timeout_sec * 1000) as di:
                                download_button.click()
                            dl = di.value
                            dl.save_as(filepath)
                            sz = os.path.getsize(filepath)
                            if sz > 0:
                                print_debug(f"Downloaded {final_filename} ({sz}B)")
                                download_success = True
                                break
                            else:
                                print_warning(f"Zero-byte download (attempt {attempt+1})")
                                try:
                                    os.remove(filepath)
                                except Exception:
                                    pass
                        except Exception as e:
                            print_warning(f"DL attempt {attempt+1} failed: {e}")
            except Exception as e:
                print_debug(f"Download button error: {e}")

        # Strategy 2: canvas extraction
        if not download_success and image_found and image_elem:
            print_debug("Canvas extraction fallback...")
            try:
                b64 = image_elem.evaluate("""(img) => new Promise((res, rej) => {
                    const go = () => {
                        try {
                            const c = document.createElement('canvas');
                            c.width = img.naturalWidth || img.width || 1024;
                            c.height = img.naturalHeight || img.height || 1024;
                            c.getContext('2d').drawImage(img, 0, 0);
                            res(c.toDataURL('image/jpeg', 0.95));
                        } catch(e) { rej(e.toString()); }
                    };
                    if (img.complete && img.naturalWidth) go();
                    else { img.onload = go; img.onerror = () => rej('load err'); }
                })""")
                if b64.startswith("data:image/") and "," in b64:
                    _, encoded = b64.split(",", 1)
                    data = base64.b64decode(encoded)
                    with open(filepath, "wb") as f:
                        f.write(data)
                    if os.path.getsize(filepath) > 0:
                        print_debug(f"Canvas extraction saved to {final_filename}")
                        download_success = True
            except Exception as e:
                print_debug(f"Canvas extraction failed: {e}")

        # Strategy 3: ultimate JS fetch
        if not download_success and image_found and image_elem:
            print_debug("Ultimate JS fetch fallback...")
            try:
                src = image_elem.get_attribute("src", timeout=2000)
                if not src:
                    inner = image_elem.locator("img").first
                    if inner:
                        src = inner.get_attribute("src", timeout=1000)
                if src:
                    try:
                        b64 = page.evaluate("""async (url) => {
                            const r = await fetch(url);
                            const b = await r.blob();
                            return new Promise((res, rej) => {
                                const rd = new FileReader();
                                rd.onloadend = () => res(rd.result);
                                rd.onerror = rej;
                                rd.readAsDataURL(b);
                            });
                        }""", src)
                        with open(filepath, "wb") as f:
                            f.write(base64.b64decode(b64.split(",")[1]))
                        download_success = True
                        print_debug(f"JS fetch saved to {final_filename}")
                    except Exception:
                        print_debug("JS fetch failed, trying page.context.request.get...")
                        res = page.context.request.get(src)
                        if res.ok:
                            with open(filepath, "wb") as f:
                                f.write(res.body())
                            download_success = True
                            print_debug(f"Request API saved to {final_filename}")
            except Exception as e:
                print_error(f"Ultimate fallback failed: {e}")

        if not download_success:
            print_error(f"Download failed after all attempts: {final_filename}")
            FAIL_COUNT += 1
            ERROR_LOG.append((filename_base, "Download failed"))
            return False

    except Exception as e:
        print_error(f"Error during generation/download: {e}")
        FAIL_COUNT += 1
        ERROR_LOG.append((filename_base, str(e)))
        return False

    # -----------------------------------------------------------------------
    # Post-download checks
    # -----------------------------------------------------------------------
    # Rate limit re-check
    if limit_attempt < limit_retries:
        try:
            if page.locator("model-response").count() > 0:
                pt = page.locator("model-response").last.inner_text().lower()
            else:
                pt = ""
            for ind in ["limit reached", "too many requests", "przekroczono limit"]:
                if ind in pt:
                    print_warning(f"\n[LIMIT] Detected after generation. Waiting {limit_wait_time}s...")
                    time.sleep(limit_wait_time)
                    return process_single_prompt(
                        page, prompt, filename_base, output_dir,
                        add_prompt, insp_prompt, fmt_arg, res_arg, resx_arg, resy_arg,
                        type_arg, think_mode, min_gen_time, dl_timeout_sec,
                        download_retries, progress_prefix, limit_attempt + 1,
                    )
        except Exception:
            pass

    # 1:1 ratio check
    is_1to1 = False
    try:
        from PIL import Image as PILImage
        with PILImage.open(filepath) as im:
            if im.size == (1024, 1024):
                is_1to1 = True
                RATIO_1TO1_COUNT += 1
    except Exception:
        pass

    # Stats
    stats = None
    if STAT_MODE:
        try:
            from PIL import Image
            sz = os.path.getsize(filepath) / 1024
            im = Image.open(filepath)
            stats = {"time": elapsed, "resolution": im.size, "size": sz}
        except Exception:
            pass

    # Display result
    try:
        sep = " | "
        clear = "\033[K"
        current = ""
        total = ""
        if progress_prefix and "/" in progress_prefix:
            parts = progress_prefix.strip("[]").split("/")
            if len(parts) == 2:
                current, total = parts[0], parts[1]

        if USE_COLOR:
            ratio_sfx = f"{sep}{Fore.RED}1:1{Style.RESET_ALL}" if is_1to1 else ""
            c_bracket = Fore.WHITE
            c_num = Fore.GREEN
            c_fn = f"{Fore.CYAN}{final_filename}{Style.RESET_ALL}"
            c_ok = f"{Fore.GREEN}OK{Style.RESET_ALL}"
            if stats:
                t_s = f"{Fore.LIGHTBLUE_EX}{stats['time']:.2f}s{Style.RESET_ALL}"
                r_s = f"{Fore.LIGHTBLUE_EX}{stats['resolution'][0]:4d}x{stats['resolution'][1]:4d}{Style.RESET_ALL}"
                sz_s = f"{Fore.LIGHTBLUE_EX}{stats['size']:.2f} KiB{Style.RESET_ALL}"
                print(f"\r{clear}{c_bracket}[{Style.RESET_ALL}{c_num}{current}/{total}{Style.RESET_ALL}{c_bracket}]{Style.RESET_ALL} {c_fn}{sep}{c_ok}{sep}{t_s}{sep}{r_s}{sep}{sz_s}{ratio_sfx}")
            else:
                print(f"\r{clear}{c_bracket}[{Style.RESET_ALL}{c_num}{current}/{total}{Style.RESET_ALL}{c_bracket}]{Style.RESET_ALL} {c_fn}{sep}{c_ok}{ratio_sfx}")
        else:
            ratio_sfx = f"{sep}1:1" if is_1to1 else ""
            if stats:
                print(f"\r{clear}{progress_prefix} {final_filename}{sep}OK{sep}{stats['time']:.2f}s{sep}{stats['resolution'][0]:4d}x{stats['resolution'][1]:4d}{sep}{stats['size']:.2f} KiB{ratio_sfx}")
            else:
                print(f"\r{clear}{progress_prefix} {final_filename}{sep}OK{ratio_sfx}")

        # Log line
        log_sep = " | "
        ratio_log = f"{log_sep}1:1" if is_1to1 else ""
        if stats:
            LOG_LINES.append(f"{progress_prefix} {final_filename}{log_sep}OK{log_sep}{stats['time']:.2f}s{log_sep}{stats['resolution'][0]:4d}x{stats['resolution'][1]:4d}{log_sep}{stats['size']:.2f} KiB{ratio_log}")
        else:
            LOG_LINES.append(f"{progress_prefix} {final_filename}{log_sep}OK{ratio_log}")
    except Exception:
        print_success(f"{final_filename} | OK")

    SUCCESS_COUNT += 1
    return True


# ---------------------------------------------------------------------------
# Error / Log helpers
# ---------------------------------------------------------------------------
def write_error_log():
    if not ERROR_LOG:
        return
    try:
        with open("nanogen.err", "a", encoding="utf-8") as f:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"\n{'='*60}\nError log - {ts}\n{'='*60}\n")
            for fp, err in ERROR_LOG:
                f.write(f"File: {fp}\nError: {err}\n{'-'*60}\n")
    except Exception as e:
        print_error(f"Error writing log: {e}")


def write_log_file():
    if not LOG_FILE:
        return
    try:
        end = time.time()
        elapsed = end - START_TIME
        h, r = divmod(int(elapsed), 3600)
        m, s = divmod(r, 60)
        time_str = f"{h}h {m}m {s}s" if h else (f"{m}m {s}s" if m else f"{s}s")

        with open(LOG_FILE, "w", encoding="utf-8") as f:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"Nanogen CLI execution log - {ts}\n{'='*60}\n")
            f.write(f"Time: {time_str}\nTotal: {SUCCESS_COUNT}\nFailed: {FAIL_COUNT}\n")
            f.write(f"DL retries: {RETRY_DOWNLOAD_COUNT}\n1:1 images: {RATIO_1TO1_COUNT}\n{'='*60}\n")
            dl = [l for l in LOG_LINES if "SKIPPED" not in l]
            sk = [l for l in LOG_LINES if "SKIPPED" in l]
            if dl:
                f.write("\nDownloaded:\n" + "-" * 40 + "\n" + "\n".join(dl) + "\n")
            if sk:
                f.write("\nSkipped:\n" + "-" * 40 + "\n" + "\n".join(sk) + "\n")
        print_info(f"Log saved: {LOG_FILE}")
    except Exception as e:
        print_error(f"Log write error: {e}")


def print_summary():
    end = time.time()
    elapsed = end - START_TIME
    h, r = divmod(int(elapsed), 3600)
    m, s = divmod(r, 60)
    time_str = f"{h}h {m}m {s}s" if h else (f"{m}m {s}s" if m else f"{s}s")

    print_info("\n" + "=" * 60)
    print(_c(Fore.MAGENTA, "EXECUTION SUMMARY") if USE_COLOR else "EXECUTION SUMMARY")
    print_info("=" * 60)
    print_info(f"Total images: {TOTAL_IMAGES}")
    print_success(f"Successful:  {SUCCESS_COUNT}")
    if FAIL_COUNT > 0:
        print_error(f"Failed:      {FAIL_COUNT}")
    else:
        print_info(f"Failed:      {FAIL_COUNT}")
    print(_c(Fore.CYAN, f"Time:        {time_str}") if USE_COLOR else f"Time:        {time_str}")
    print_info("=" * 60)

    if ERROR_LOG:
        write_error_log()
        print_warning("Errors logged to nanogen.err")
    write_log_file()


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
def run_gemini_session(args):
    global START_TIME, TOTAL_IMAGES, SUCCESS_COUNT

    START_TIME = time.time()
    debug_summary = args.debug

    # Resolve login credentials
    login_email = None
    login_password = None
    if args.loginfile:
        login_email, login_password = read_login_file(args.loginfile)
    elif args.login:
        login_email = args.login
        if args.pwdsilent:
            import getpass
            login_password = getpass.getpass("Google password: ")
        elif args.pwd:
            login_password = args.pwd
        if not login_password:
            print_error("Password required. Use --pwd PASSWORD or --pwdsilent (interactive)")
            sys.exit(1)

    # Load queue
    queue = []
    existing = set()
    if SKIP_MODE and os.path.isdir(args.out):
        try:
            existing = set(os.listdir(args.out))
        except Exception:
            pass

    if args.input_file:
        try:
            with open(args.input_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for fname, prmt in data.items():
                    s = str(fname)
                    if SKIP_MODE:
                        t = get_final_filename(s)
                        if t in existing:
                            LOG_LINES.append(f"SKIPPED: {t}")
                            SUCCESS_COUNT += 1
                            continue
                    queue.append((prmt, s))
            elif isinstance(data, list):
                for item in data:
                    if "prompt" in item and "filename" in item:
                        s = str(item["filename"])
                        if SKIP_MODE:
                            t = get_final_filename(s)
                            if t in existing:
                                LOG_LINES.append(f"SKIPPED: {t}")
                                SUCCESS_COUNT += 1
                                continue
                        queue.append((item["prompt"], s))
        except Exception as e:
            print_error(f"JSON load error: {e}")
            return
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = re.sub(r'[\\/*?:"<>|]', "", args.prompt)[:20].strip()
        fname = f"{safe}_{ts}"
        if SKIP_MODE:
            t = get_final_filename(fname)
            if t in existing:
                LOG_LINES.append(f"SKIPPED: {t}")
                SUCCESS_COUNT += 1
            else:
                queue.append((args.prompt, fname))
        else:
            queue.append((args.prompt, fname))

    TOTAL_IMAGES = len(queue)
    skip_count = SUCCESS_COUNT

    if skip_count > 0:
        print_info(f"Skipped {skip_count} existing file(s)")

    if TOTAL_IMAGES > 0:
        print(_c(Fore.CYAN, f"Images to generate: {TOTAL_IMAGES}") if USE_COLOR else f"Images to generate: {TOTAL_IMAGES}")
        print_info("")

    if not queue:
        print_info("All files exist. Nothing to generate.")
        print_summary()
        return

    # Ensure Playwright browsers are available
    ensure_playwright_browsers()

    # Launch browser
    user_data = args.user_data_dir or DEFAULT_USER_DATA_DIR
    print_info("Launching Chromium...")
    pw, context, page = launch_browser(user_data_dir=user_data, headless=HEADLESS_MODE)
    atexit.register(lambda: cleanup(pw, context))
    print_success("Browser launched.")

    # Handle login
    page.goto("https://gemini.google.com/app", timeout=60000, wait_until="domcontentloaded")

    if login_email and login_password:
        # Credentials provided: login fresh regardless of existing session
        if not auto_login_to_google(page, login_email, login_password):
            print_error("Automated login failed.")
            sys.exit(1)
        print_success(f"Logged into Gemini ({login_email}). Starting image generation.\n")
    else:
        # No credentials: check if session already exists
        logged_in = check_gemini_logged_in(page)
        if not logged_in:
            guide_login(page, HEADLESS_MODE)
            logged_in = True
        email_display = "(session restored)"
        print_success(f"Logged into Gemini {email_display}. Starting image generation.\n")

    # Process queue
    try:
        for idx, (prompt, fname) in enumerate(queue):
            current = idx + 1
            total = len(queue)
            progress_prefix = f"[{current:03d}/{total:03d}]"

            if USE_COLOR:
                print(f"{Fore.WHITE}[{Style.RESET_ALL}{Fore.GREEN}{current:03d}/{total:03d}{Style.RESET_ALL}{Fore.WHITE}]{Style.RESET_ALL} {Fore.CYAN}{fname}{Style.RESET_ALL} | {Fore.YELLOW}Processing...{Style.RESET_ALL}",
                      end="", flush=True)
            else:
                print(f"[{current:03d}/{total:03d}] {fname} | Processing...", end="", flush=True)

            if debug_summary:
                print()
                print_debug(f"--- {progress_prefix}: {fname} ---")

            success = process_single_prompt(
                page, prompt, fname, args.out,
                args.add_prompt, args.insp_prompt,
                args.fmt_arg, args.res_arg, args.resx_arg, args.resy_arg,
                args.type_arg, args.think_arg,
                args.mingentime, args.dltime, args.dlret,
                progress_prefix,
            )

            if not success:
                clear = "\033[K"
                if USE_COLOR:
                    print(f"\r{clear}{Fore.WHITE}[{Style.RESET_ALL}{Fore.GREEN}{current:03d}/{total:03d}{Style.RESET_ALL}{Fore.WHITE}]{Style.RESET_ALL} {Fore.CYAN}{fname}{Style.RESET_ALL} | {Fore.RED}ERROR{Style.RESET_ALL}")
                else:
                    print(f"\r{clear}[{current:03d}/{total:03d}] {fname} | ERROR")

            if idx < len(queue) - 1:
                delay_ms = 0
                if args.promptrnd:
                    try:
                        mn, mx = map(int, args.promptrnd.split(","))
                        delay_ms = random.randint(mn, mx)
                    except Exception:
                        delay_ms = 1000
                else:
                    delay_ms = args.promptint
                print_debug(f"Waiting {delay_ms}ms...")
                time.sleep(delay_ms / 1000.0)

        print_summary()

    except Exception as e:
        print_error(f"Session error: {e}")
        print_summary()
    finally:
        cleanup(pw, context)


def cleanup(pw, context):
    try:
        context.close()
    except Exception:
        pass
    try:
        pw.stop()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def main():
    global USE_COLOR, DEBUG_MODE, OVERWRITE_MODE, SKIP_MODE, STAT_MODE
    global HEADLESS_MODE, CHAT_MODE, NOEX_MODE, LIMIT_WAIT_TIME, LOG_FILE

    # Normalize single-dash long args (e.g., -type → --type)
    normalized = []
    for arg in sys.argv[1:]:
        if arg.startswith("-") and not arg.startswith("--") and len(arg) > 2:
            normalized.append("-" + arg)
        else:
            normalized.append(arg)
    sys.argv[1:] = normalized

    parser = argparse.ArgumentParser(
        description="Nanogen CLI v0.0.20 — Headless Gemini Imagen Generator (no display/GUI required)",
        add_help=False,
    )

    io_grp = parser.add_argument_group("Input / Output")
    io_grp.add_argument("--prompt", help="Single text prompt")
    io_grp.add_argument("--addprompt", dest="add_prompt", help="Text to append to prompts")
    io_grp.add_argument("--insprompt", dest="insp_prompt", help="Text to prepend to prompts")
    io_grp.add_argument("--in", dest="input_file", help="JSON file with prompts")
    io_grp.add_argument("--out", default=".", help="Output directory")
    io_grp.add_argument("--outauto", action="store_true", help="Auto output dir from JSON filename")

    gen_grp = parser.add_argument_group("Generation")
    gen_grp.add_argument("--promptint", type=int, default=1000, help="Delay ms between prompts (default: 1000)")
    gen_grp.add_argument("--promptrnd", help="Random delay range ms: MIN,MAX")
    gen_grp.add_argument("--type", dest="type_arg", type=str.lower,
                         choices=["fast", "think", "pro", "flash", "flash-lite"],
                         help="Model: fast, think, pro, flash, flash-lite")
    gen_grp.add_argument("--thinking", dest="think_arg", type=str.lower,
                         choices=["basic", "extended"], help="Thinking mode: basic or extended")
    gen_grp.add_argument("--fmt", dest="fmt_arg", help="Aspect ratio: 43, 169, 11")
    gen_grp.add_argument("--res", dest="res_arg", help="Resolution: WIDTH,HEIGHT")
    gen_grp.add_argument("--resx", dest="resx_arg", help="Width in px, height from --fmt")
    gen_grp.add_argument("--resy", dest="resy_arg", help="Height in px, width from --fmt")
    gen_grp.add_argument("--overwrite", action="store_true", help="Overwrite without prompt")
    gen_grp.add_argument("--skip", action="store_true", help="Skip existing files")
    gen_grp.add_argument("--noex", action="store_true", help="Don't append .png extension")
    gen_grp.add_argument("--mingentime", type=int, default=30, help="Min wait for generation (s)")
    gen_grp.add_argument("--dltime", type=int, default=45, help="Download timeout (s)")
    gen_grp.add_argument("--dlret", type=int, default=3, help="Download retries")
    gen_grp.add_argument("--limitwait", type=int, default=300, help="Rate-limit wait (s)")
    gen_grp.add_argument("--gen", choices=["chat", "tmp", "native"], default="tmp",
                         help="Chat mode: chat, tmp (default), native")

    browser_grp = parser.add_argument_group("Browser")
    browser_grp.add_argument("--headless", action="store_true", default=True,
                             help=argparse.SUPPRESS)
    browser_grp.add_argument("--no-headless", dest="headless", action="store_false",
                             help="Show browser GUI (for debugging / first-time login)")
    browser_grp.add_argument("--user-data-dir", dest="user_data_dir",
                             help="Chrome profile directory for persistent login")
    browser_grp.add_argument("--install-browser", action="store_true",
                             help="Install Playwright Chromium and exit")

    login_grp = parser.add_argument_group("Login (automated Google sign-in)")
    login_grp.add_argument("--login", dest="login", help="Google account email for automated login")
    login_grp.add_argument("--pwd", dest="pwd", help="Google account password (visible on command line)")
    login_grp.add_argument("--pwdsilent", dest="pwdsilent", action="store_true",
                           help="Prompt for password interactively (secure, no echo)")
    login_grp.add_argument("--loginfile", dest="loginfile",
                           help="File with email on line 1 and password on line 2")

    display_grp = parser.add_argument_group("Display")
    display_grp.add_argument("--color", action="store_true", help="Colorized output (default if terminal supports it)")
    display_grp.add_argument("--mono", action="store_true", help="Force monochrome output (no colors), overrides --color")
    display_grp.add_argument("--debug", action="store_true", help="Verbose debug output")
    display_grp.add_argument("--stat", action="store_true", help="Show per-image stats")
    display_grp.add_argument("--savescr", dest="save_screen_file",
                             nargs="?", const="", default=None,
                             help="Save console output to file")
    display_grp.add_argument("--log", dest="log_file", nargs="?", const="", default=None,
                             help="Save execution log to file")

    parser.add_argument("-h", action="store_true", help="Short help")
    parser.add_argument("--help", action="store_true", help="Detailed help")

    args, unknown = parser.parse_known_args()

    # Handle browser install
    if args.install_browser:
        install_browsers()
        return

    # Validate unknown args
    if unknown:
        print_error(f"Unknown arguments: {', '.join(unknown)}")
        print_info("Use --help to see available options.")
        sys.exit(1)

    # Set global flags early for validation messages
    USE_COLOR = args.color and not args.mono
    if args.mono:
        USE_COLOR = False
    HEADLESS_MODE = args.headless

    # Validate resolution options
    res_opts = [args.res_arg, args.resx_arg, args.resy_arg]
    if sum(1 for o in res_opts if o) > 1:
        print_error("--res, --resx, --resy are mutually exclusive")
        sys.exit(1)

    if args.fmt_arg and args.fmt_arg not in ("43", "169", "11"):
        print_error(f"Invalid --fmt: {args.fmt_arg}. Use 43, 169, or 11")
        sys.exit(1)

    if args.res_arg:
        try:
            parts = args.res_arg.split(",")
            if len(parts) != 2:
                raise ValueError
            w, h = int(parts[0]), int(parts[1])
            if w <= 0 or h <= 0:
                raise ValueError
        except ValueError:
            print_error(f"Invalid --res: {args.res_arg}. Format: WIDTH,HEIGHT")
            sys.exit(1)

    if args.resx_arg:
        try:
            if int(args.resx_arg) <= 0:
                raise ValueError
        except ValueError:
            print_error(f"Invalid --resx: {args.resx_arg}")
            sys.exit(1)

    if args.resy_arg:
        try:
            if int(args.resy_arg) <= 0:
                raise ValueError
        except ValueError:
            print_error(f"Invalid --resy: {args.resy_arg}")
            sys.exit(1)

    if args.mingentime <= 0:
        print_error("--mingentime must be positive")
        sys.exit(1)

    if args.dltime <= 0:
        print_error("--dltime must be positive")
        sys.exit(1)

    if args.dlret < 0:
        print_error("--dlret must be non-negative")
        sys.exit(1)

    if args.promptint < 0:
        print_error("--promptint must be non-negative")
        sys.exit(1)

    if args.promptrnd:
        try:
            parts = args.promptrnd.split(",")
            if len(parts) != 2:
                raise ValueError
            mn, mx = int(parts[0]), int(parts[1])
            if mn < 0 or mx < 0 or mn > mx:
                raise ValueError
        except ValueError:
            print_error(f"Invalid --promptrnd: {args.promptrnd}. Format: MIN,MAX")
            sys.exit(1)

    # --- Help display ---
    if args.h:
        print(f"NanoGen CLI  v{__CODEVER__}")
        print(f"Author: {__CODEAUTH__}")
        print(f"Repo:   {__CODEGIT__}")
        print()
        print("Usage: python nanogen_cli.py --prompt TEXT [options]")
        print("       python nanogen_cli.py --in FILE.json [options]")
        print()
        print("Use --help for detailed options.")
        sys.exit(0)

    if args.help:
        print(f"\nNanoGen CLI  v{__CODEVER__}  ({__CODEDATE__})")
        print(f"Author: {__CODEAUTH__}  |  Repo: {__CODEGIT__}")
        print("\n" + "=" * 60)
        print("INPUT / OUTPUT")
        print("  --prompt TEXT        Single text prompt")
        print("  --in FILE            JSON file with prompts")
        print("  --out DIR            Output directory (default: .)")
        print("  --outauto            Auto output dir from JSON filename")
        print()
        print("PROMPT MODIFIERS")
        print("  --addprompt TEXT     Append to every prompt")
        print("  --insprompt TEXT     Prepend to every prompt")
        print()
        print("GENERATION")
        print("  --fmt FMT            Aspect ratio: 43, 169, 11")
        print("  --res W,H            Exact resolution (mutually exclusive with --resx/--resy)")
        print("  --resx W             Width, height from aspect ratio")
        print("  --resy H             Height, width from aspect ratio")
        print("  --type TYPE          Model: fast, think, pro, flash, flash-lite")
        print("  --thinking MODE      Thinking: basic, extended")
        print("  --gen MODE           Chat: chat, tmp (default), native")
        print("  --promptint MS       Delay between prompts (ms, default: 1000)")
        print("  --promptrnd MIN,MAX  Random delay range (ms)")
        print("  --overwrite          Overwrite without prompting")
        print("  --skip               Skip existing files")
        print("  --noex               Don't append .png extension")
        print("  --mingentime N       Min wait for generation (s, default: 30)")
        print("  --dltime N           Download timeout (s, default: 45)")
        print("  --dlret N            Download retries (default: 3)")
        print("  --limitwait N        Rate limit wait (s, default: 300)")
        print()
        print("BROWSER")
        print("  --no-headless        Show browser GUI (for first-time login)")
        print("  --user-data-dir DIR  Chrome profile directory")
        print("  --install-browser    Install Playwright Chromium and exit")
        print()
        print("LOGIN (automated Google sign-in)")
        print("  --login EMAIL        Google account email")
        print("  --pwd PASSWORD       Password (visible in process list)")
        print("  --pwdsilent          Prompt for password interactively (secure)")
        print("  --loginfile FILE     File with email (line 1) and password (line 2)")
        print()
        print("DISPLAY")
        print("  --color              Colorized output")
        print("  --mono               Force monochrome output (no colors)")
        print("  --debug              Verbose debug")
        print("  --stat               Show per-image statistics")
        print("  --savescr [FILE]     Save console output")
        print("  --log [FILE]         Save execution log")
        print()
        print("EXAMPLES")
        print('  python nanogen_cli.py --prompt "Mountain landscape" --color')
        print('  python nanogen_cli.py --in prompts.json --type think --fmt 169 --stat --color')
        print("  python nanogen_cli.py --install-browser")
        print('  python nanogen_cli.py --login user@gmail.com --pwdsilent --prompt "test"')
        sys.exit(0)

    # --- Validation ---
    if args.prompt and args.input_file:
        print_error("--prompt and --in are mutually exclusive")
        sys.exit(1)

    if args.res_arg and args.fmt_arg:
        print_error("--res and --fmt are mutually exclusive")
        sys.exit(1)

    if not args.prompt and not args.input_file:
        print_error("Specify --prompt or --in")
        print_info("Use --help for usage.")
        sys.exit(1)

    if args.input_file and not os.path.exists(args.input_file):
        print_error(f"Input file not found: {args.input_file}")
        sys.exit(1)

    if getattr(args, "outauto", False):
        if not args.input_file:
            print_error("--outauto requires --in")
            sys.exit(1)
        args.out = os.path.splitext(os.path.basename(args.input_file))[0]
        print_info(f"Auto output dir: {args.out}")

    # Validate login options
    login_opts = [args.loginfile, args.login]
    if args.pwdsilent and not args.login:
        print_error("--pwdsilent requires --login EMAIL")
        sys.exit(1)
    if args.pwd and not args.login:
        print_error("--pwd requires --login EMAIL")
        sys.exit(1)
    if args.loginfile and (args.login or args.pwd or args.pwdsilent):
        print_error("--loginfile cannot be combined with --login, --pwd, or --pwdsilent")
        sys.exit(1)
    if args.loginfile and not os.path.exists(args.loginfile):
        print_error(f"Login file not found: {args.loginfile}")
        sys.exit(1)

    if not os.path.exists(args.out):
        try:
            os.makedirs(args.out)
            print_info(f"Created output dir: {args.out}")
        except Exception as e:
            print_error(f"Cannot create {args.out}: {e}")
            sys.exit(1)

    # Screen logger
    if args.save_screen_file is not None:
        if args.save_screen_file == "":
            if args.input_file:
                base = os.path.splitext(os.path.basename(args.input_file))[0]
                args.save_screen_file = base + ".scr"
            else:
                args.save_screen_file = "nanogen.scr"
        sys.stdout = Logger(args.save_screen_file)
        sys.stderr = sys.stdout
        print_info(f"Saving output to: {args.save_screen_file}")

    # Set globals
    DEBUG_MODE = args.debug
    OVERWRITE_MODE = args.overwrite
    SKIP_MODE = args.skip
    STAT_MODE = args.stat
    NOEX_MODE = args.noex
    CHAT_MODE = args.gen
    LIMIT_WAIT_TIME = args.limitwait

    if args.log_file is not None:
        if args.log_file == "":
            if args.input_file:
                base = os.path.splitext(os.path.basename(args.input_file))[0]
                LOG_FILE = base + ".log"
            else:
                LOG_FILE = "nanogen.log"
        else:
            LOG_FILE = args.log_file

    if not HAS_COLORAMA:
        USE_COLOR = False

    run_gemini_session(args)


if __name__ == "__main__":
    main()
