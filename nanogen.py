import argparse
import atexit
import subprocess
import sys
import time
import re
import os
os.environ["NODE_OPTIONS"] = "--no-warnings"
import json
import random
from datetime import datetime
from colorama import Fore, Back, Style, init

# Initialize colorama for cross-platform color support
init(autoreset=True)


# ---=== Code info ===---
__CODEAUTH__ = "Igor Brzezek"
__CODEVER__ = "0.0.18"
__CODEDATE__ = "09.07.2026"
__CODEGIT__ = "https://github.com/igorbrzezek/nanogen"

class Logger(object):
    def __init__(self, filename):
        self.terminal = sys.stdout
        self.log = open(filename, "a", encoding="utf-8")
        import re
        self.ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

    def write(self, message):
        self.terminal.write(message)
        clean_message = self.ansi_escape.sub('', message)
        self.log.write(clean_message)
        self.log.flush() # Ensure content is written immediately

    def flush(self):
        self.terminal.flush()
        self.log.flush()

# Global variables for output control
USE_COLOR = False
DEBUG_MODE = False
OVERWRITE_MODE = False
SKIP_MODE = False
STAT_MODE = False
RETRY_COUNT = 0
MIN_GEN_TIME = 30
CHAT_MODE = 'tmp'
SIMUL_MODE = False
CDP_HOST = 'localhost'
CDP_PORT = 9222
ERROR_LOG = []
TOTAL_IMAGES = 0
SUCCESS_COUNT = 0
FAIL_COUNT = 0
START_TIME = None
NOEX_MODE = False
LOG_FILE = None
LOG_LINES = []
RATIO_1TO1_COUNT = 0
RETRY_DOWNLOAD_COUNT = 0
DL_RESTART_MODE = False
DL_RESTART_COUNT = 0
LIMIT_WAIT_TIME = 300
LOCK_FILE = "nanogen.lock"

def release_lock():
    """Remove the lock file on exit."""
    if os.path.exists(LOCK_FILE):
        try:
            os.remove(LOCK_FILE)
        except:
            pass

def acquire_lock():
    """Ensure only one instance of nanogen is running at a time to avoid browser conflicts."""
    import os
    import time
    
    first_wait = True
    while True:
        try:
            # Atomic creation of lock file
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            atexit.register(release_lock)
            if not first_wait:
                print() # New line after "Waiting..." message
            return
        except FileExistsError:
            # Check if the process in the lock file is still running (Windows specific)
            is_stale = False
            try:
                with open(LOCK_FILE, "r") as f:
                    pid = int(f.read().strip())
                # On Windows, tasklist is a reliable way to check if PID is running
                check = subprocess.check_output(f'tasklist /FI "PID eq {pid}" /NH', shell=True).decode()
                if "No tasks are running" in check or str(pid) not in check:
                    is_stale = True
            except:
                pass # If check fails, don't assume stale
                
            if is_stale:
                try:
                    os.remove(LOCK_FILE)
                    continue # Try acquiring again
                except:
                    pass

            if first_wait:
                print_warning("\n*** Another instance of nanogen is currently running. ***")
                print_info("To avoid browser conflicts, this process will wait for the other instance to finish.")
                first_wait = False
            
            # Use carriage return to stay on same line while waiting
            dots = "." * (int(time.time()) % 4)
            print_info(f"\rWaiting for lock to be released{dots:<3} ", end="", flush=True)
            time.sleep(2)

def print_color(msg, color_code=""):
    """Print message with optional color."""
    if USE_COLOR and color_code:
        print(f"{color_code}{msg}{Style.RESET_ALL}")
    else:
        print(msg)

def print_error(msg):
    """Print error message in red."""
    print_color(msg, Fore.RED)

def print_warning(msg):
    """Print warning message in yellow."""
    print_color(msg, Fore.YELLOW)

def print_success(msg):
    """Print success message in green."""
    print_color(msg, Fore.GREEN)

def print_info(msg):
    """Print info message in white (default)."""
    print_color(msg, "")

def print_debug(msg):
    """Print debug message only if DEBUG_MODE is enabled."""
    if DEBUG_MODE:
        print_color(msg, Fore.CYAN)

def get_final_filename(filename_base):
    """Determine the final filename, appending .png if no supported extension is present."""
    global NOEX_MODE
    
    # Check if it already has a supported extension - if so, ALWAYS keep it
    supported_exts = ['.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp']
    _, ext = os.path.splitext(filename_base)
    if ext.lower() in supported_exts:
        return filename_base
        
    if NOEX_MODE:
        return filename_base
    
    res = f"{filename_base}.png"
    return res

def download_image_from_url(url, filepath):
    """Download image from URL."""
    global SUCCESS_COUNT, FAIL_COUNT, ERROR_LOG
    start_time = time.time()
    try:
        import requests
        res = requests.get(url)
        if res.status_code == 200:
            with open(filepath, "wb") as f:
                f.write(res.content)
            
            # Get file statistics if requested
            download_time = time.time() - start_time
            stats = None
            if STAT_MODE:
                try:
                    from PIL import Image
                    file_size = os.path.getsize(filepath) / 1024  # Size in KiB
                    img = Image.open(filepath)
                    width, height = img.size
                    stats = {
                        'time': download_time,
                        'resolution': (width, height),
                        'size': file_size
                    }
                except Exception as e:
                    print_debug(f"Could not get image stats: {e}")
            
            SUCCESS_COUNT += 1
            return True, stats
        else:
            error_msg = f"Download failed with status code {res.status_code}"
            print_error(error_msg)
            ERROR_LOG.append((filepath, error_msg))
            FAIL_COUNT += 1
            return False, None
    except Exception as e:
        error_msg = f"Download failed: {e}"
        print_error(error_msg)
        ERROR_LOG.append((filepath, str(e)))
        FAIL_COUNT += 1
        return False, None

def process_single_prompt(page, prompt, filename_base, output_dir, add_prompt=None, insp_prompt=None, fmt_arg=None, res_arg=None, resx_arg=None, resy_arg=None, type_arg=None, think_mode=None, min_gen_time=30, dl_timeout_sec=45, download_retries=0, progress_prefix="", limit_attempt=0):
    """Handles the full flow for a single prompt on Gemini."""
    global SUCCESS_COUNT, FAIL_COUNT, ERROR_LOG, CHAT_MODE, SKIP_MODE, SIMUL_MODE, NOEX_MODE, RETRY_DOWNLOAD_COUNT, RATIO_1TO1_COUNT, LOG_LINES, LIMIT_WAIT_TIME
    
    # Check if file already exists BEFORE doing anything (for --skip mode)
    if SKIP_MODE:
        final_filename = get_final_filename(filename_base)
        filepath = os.path.join(output_dir, final_filename)
        if os.path.exists(filepath):
            file_size = os.path.getsize(filepath)
            _, ext = os.path.splitext(final_filename)
            min_size = 0
            if ext.lower() in ['.jpg', '.jpeg']:
                min_size = 1024
            elif ext.lower() == '.png':
                min_size = 256
            if file_size >= min_size:
                print_info(f"[SKIP] Skipping existing file: {final_filename}")
                LOG_LINES.append(f"{progress_prefix} {final_filename} | SKIPPED")
                SUCCESS_COUNT += 1
                return True
            else:
                print_warning(f"[RE-GEN] {final_filename} exists but is too small ({file_size} bytes), regenerating...")
    
    # Retry logic info for rate limits
    limit_retries = 3
    limit_wait_time = LIMIT_WAIT_TIME # Time in seconds to wait when rate limited
    
    if limit_attempt > 0:
        print_info(f"\n[RETRY] Attempting prompt again after rate limit wait ({limit_attempt}/{limit_retries})...")
        # Clear line and reprint processing prefix
        if USE_COLOR:
            c_bracket = f"{Fore.WHITE}"
            c_numbers = f"{Fore.GREEN}"
            c_fname = f"{Fore.CYAN}{filename_base}{Style.RESET_ALL}"
            c_processing = f"{Fore.YELLOW}Processing (Retry {limit_attempt})...{Style.RESET_ALL}"
            print(f"{c_bracket}[{Style.RESET_ALL}{c_numbers}{progress_prefix[1:8]}{Style.RESET_ALL}{c_bracket}]{Style.RESET_ALL} {c_fname} {c_bracket}|{Style.RESET_ALL} {c_processing}", end="", flush=True)
        else:
            print(f"{progress_prefix} {filename_base} | Processing (Retry {limit_attempt})...", end="", flush=True)
    
    # Debug: Show current chat mode setting
    print_debug(f">>> CHAT_MODE value: '{CHAT_MODE}' <<<")
    
    # Start a NEW chat for each prompt based on chat mode
    if CHAT_MODE in ['tmp', 'chat']:
        if CHAT_MODE == 'tmp':
            print_debug("*** ACTIVATING TEMPORARY CHAT MODE ***")
        else:
            print_debug("*** ACTIVATING REGULAR CHAT MODE ***")
        try:
            # ALWAYS navigate to Gemini to ensure fresh state
            # Added robustness for ERR_ABORTED and multiple instances
            print_debug("Navigating to Gemini...")
            if not SIMUL_MODE:
                max_nav_retries = 3
                for nav_attempt in range(max_nav_retries):
                    try:
                        # Add a small random jitter to avoid collisions between multiple instances
                        if nav_attempt > 0:
                            time.sleep(random.uniform(1.0, 3.0))
                        
                        # Only navigate if not already there or if we want to ensure fresh state
                        is_gemini = "gemini.google.com" in page.url
                        is_saved_chat = is_gemini and any(p in page.url for p in ["/app/", "/chats/"]) and not page.url.endswith("/app")
                        
                        if not is_gemini or page.url == "about:blank":
                            page.goto("https://gemini.google.com/app", timeout=60000, wait_until="domcontentloaded")
                        elif is_saved_chat:
                            # Try clicking the new chat button first
                            new_chat_btn = page.locator('[data-test-id="new-chat-button"]').first
                            if new_chat_btn.is_visible(timeout=2000):
                                new_chat_btn.click()
                                print_debug("Clicked 'new-chat-button' using data-test-id to start fresh chat")
                            else:
                                page.goto("https://gemini.google.com/app", timeout=60000, wait_until="domcontentloaded")
                        else:
                            # If already at /app, reload to ensure fresh state
                            page.reload(timeout=60000, wait_until="domcontentloaded")
                            
                        page.wait_for_load_state("domcontentloaded", timeout=60000)
                        time.sleep(2)
                        break # Success
                    except Exception as goto_err:
                        err_str = str(goto_err)
                        if "ERR_ABORTED" in err_str:
                            if "gemini.google.com" in page.url:
                                print_debug("  (Navigation aborted but already on Gemini, continuing...)")
                                break
                            else:
                                print_debug(f"  (Navigation aborted, retry {nav_attempt + 1}/{max_nav_retries}...)")
                        elif nav_attempt == max_nav_retries - 1:
                            raise goto_err
                        else:
                            print_debug(f"  (Navigation error: {err_str[:50]}..., retrying...)")
            
            
            # STEP 1: ENSURE left menu is expanded (CRITICAL for finding temporary chat)
            print_debug("STEP 1: Ensuring left sidebar menu is expanded...")
            
            if SIMUL_MODE:
                 print_debug("  [SIMUL] Skipping menu expansion")
            else:
                # Try keyboard shortcut first (most reliable)
                try:
                    print_debug("  Trying keyboard shortcut to open menu...")
                    page.keyboard.press('[')  # [ key opens sidebar in Gemini
                    time.sleep(1)
                    print_debug("  ✓ Pressed '[' key to open menu")
                except Exception as e:
                    print_debug(f"  Keyboard shortcut failed: {str(e)[:30]}")
            
            # Verify menu is now open by looking for common menu items
            print_debug("  Verifying menu is expanded...")
            menu_items_visible = False
            menu_check_selectors = [
                # Common menu items that appear when sidebar is open
                'text="New chat"',
                'text="Nowy czat"',
                '[role="navigation"]',
                'nav[aria-label*="Main"]',
                'nav[aria-label*="Główne"]'
            ]
            
            for selector in menu_check_selectors:
                try:
                    item = page.locator(selector).first
                    if item.is_visible(timeout=1000):
                        menu_items_visible = True
                        print_debug(f"  ✓ Menu confirmed open (found: {selector})")
                        break
                except:
                    continue
            
            # If keyboard shortcut didn't work, try clicking menu button
            if not menu_items_visible:
                print_debug("  Menu may still be closed, trying to click menu button...")
                menu_button_selectors = [
                    # Hamburger menu icon
                    'button[aria-label="Main menu"]',
                    'button[aria-label="Menu główne"]',
                    'button[aria-label*="Menu"]',
                    'button[aria-label*="menu"]',
                    '[aria-label*="Open menu"]',
                    '[aria-label*="Otwórz menu"]',
                    # Navigation
                    '[aria-label*="Navigation"]',
                    '[aria-label*="nawigacja"]',
                    # Icon-based (hamburger icon)
                    'button:has(svg)',
                    'button:has([data-icon="menu"])',
                    'button:has([class*="menu"])',
                    'button:has([class*="Menu"])',
                    # Top-left corner buttons
                    'header button:first-child',
                    'nav button:first-child',
                    # Generic menu buttons
                    'button[type="button"]:first-of-type'
                ]
                
                menu_opened = False
                for selector in menu_button_selectors:
                    try:
                        print_debug(f"    Trying: {selector}")
                        menu_btn = page.locator(selector).first
                        if menu_btn.is_visible(timeout=2000):
                            print_debug(f"    ✓ Found menu button, clicking...")
                            try:
                                menu_btn.click(timeout=5000)
                            except:
                                menu_btn.click(force=True)
                            menu_opened = True
                            time.sleep(2)
                            print_debug(f"    ✓ Clicked menu button")
                            break
                    except Exception as e:
                        continue
                
                if menu_opened:
                    print_debug("  ✓ Menu should now be expanded")
                else:
                    print_debug("  ⚠ Could not click menu button, menu may already be open")
            
            
            # STEP 2: Now CLICK the appropriate button
            button_found = False
            if SIMUL_MODE:
                print_debug(f"  [SIMUL] Skipping {CHAT_MODE} activation")
            elif CHAT_MODE == 'tmp':
                print_debug("Looking for 'Czat tymczasowy' button/toggle...")
                temp_btn_container = page.locator("temp-chat-button").first
                if temp_btn_container.count() > 0:
                    inner_btn = temp_btn_container.locator("button, gem-icon-button").first
                    if inner_btn.count() > 0:
                        class_attr = inner_btn.get_attribute("class") or ""
                        if "temp-chat-on" in class_attr:
                            print_debug("✓ Temporary chat is ALREADY active (temp-chat-on class present). Skipping click.")
                            button_found = True
                        else:
                            print_debug("Temporary chat is not active. Clicking to activate...")
                            inner_btn.click()
                            time.sleep(3)
                            # Re-verify
                            class_attr_after = inner_btn.get_attribute("class") or ""
                            if "temp-chat-on" in class_attr_after:
                                print_debug("✓ Temporary chat activated successfully!")
                                button_found = True
                            else:
                                print_warning("Clicked temporary chat button but 'temp-chat-on' class is still not present.")
                
                if not button_found:
                    # Fallback selectors
                    temp_chat_selectors = [
                        # Direct text matches
                        'text="Czat tymczasowy"',
                        'text="Temporary chat"',
                        # Button with text
                        'button:has-text("Czat tymczasowy")',
                        'button:has-text("Temporary chat")',
                        # Div with role button
                        'div[role="button"]:has-text("Czat tymczasowy")',
                        'div[role="button"]:has-text("Temporary chat")',
                        # Aria labels
                        '[aria-label="Czat tymczasowy"]',
                        '[aria-label="Temporary chat"]',
                        '[aria-label*="tymczasow"]',
                        '[aria-label*="emporary"]',
                        # Spans with text
                        'span:has-text("Czat tymczasowy")',
                        'span:text-is("Czat tymczasowy")',
                        'span:has-text("Temporary chat")',
                        'span:text-is("Temporary chat")',
                        # Any clickable element
                        '*[role="button"]:has-text("Czat tymczasowy")',
                        '*[role="button"]:has-text("Temporary chat")',
                        # Data attributes
                        '[data-test-id*="temporary"]',
                        '[data-test-id*="tymczasow"]'
                    ]
                    
                    for selector in temp_chat_selectors:
                        try:
                            print_debug(f"  Trying legacy selector: {selector}")
                            temp_button = page.locator(selector).first
                            if temp_button.is_visible(timeout=3000):
                                print_debug(f"  ✓ FOUND with: {selector}")
                                # Avoid clicking if it is already active
                                is_active = False
                                try:
                                    html_content = temp_button.evaluate("node => node.outerHTML").lower()
                                    if "temp-chat-on" in html_content:
                                        is_active = True
                                except:
                                    pass
                                
                                if is_active:
                                    print_debug("  ✓ Temporary chat appears to be already active from HTML inspection. Skipping click.")
                                    button_found = True
                                    break
                                    
                                temp_button.scroll_into_view_if_needed()
                                time.sleep(0.5)
                                # Try regular click first
                                try:
                                    temp_button.click(timeout=5000)
                                except:
                                    # If fails, try force click
                                    temp_button.click(force=True)
                                print_debug(f"  ✓✓✓ CLICKED 'Czat tymczasowy' SUCCESSFULLY! ✓✓✓")
                                button_found = True
                                time.sleep(3)
                                break
                        except Exception as e:
                            continue
                
                if button_found:
                    print_debug("*** ✓✓✓ TEMPORARY CHAT ACTIVATED ✓✓✓ ***")
                else:
                    print_warning("!!! 'Czat tymczasowy' button NOT FOUND !!!")
            
            # If we need new chat (either mode 'chat' or 'tmp' fallback)
            if not SIMUL_MODE and not button_found:
                if CHAT_MODE == 'chat':
                    print_debug("Looking for 'Nowy czat' / 'New chat' button...")
                else:
                    print_debug("Looking for 'Nowy czat' / 'New chat' button as fallback...")
                    
                new_chat_selectors = [
                    'text="Nowy czat"',
                    'text="New chat"',
                    'span:has-text("Nowy czat")',
                    'span:has-text("New chat")',
                    'button:has-text("Nowy czat")',
                    'button:has-text("New chat")',
                    '[aria-label="Nowy czat"]',
                    '[aria-label="New chat"]'
                ]
                
                new_chat_found = False
                for selector in new_chat_selectors:
                    try:
                        print_debug(f"  Trying: {selector}")
                        btn = page.locator(selector).first
                        if btn.is_visible(timeout=3000):
                            btn.scroll_into_view_if_needed()
                            time.sleep(0.5)
                            try:
                                btn.click(timeout=5000)
                            except:
                                btn.click(force=True)
                            print_debug(f"  ✓✓✓ CLICKED 'Nowy czat' SUCCESSFULLY! ✓✓✓")
                            new_chat_found = True
                            time.sleep(3)
                            break
                    except Exception as e:
                        continue

                if not new_chat_found:
                    if CHAT_MODE == 'chat':
                        print_warning("Continuing without regular chat activation")
                    else:
                        print_warning("Continuing without temporary chat or new chat activation")
                
        except Exception as e:
            print_warning(f"Error during chat mode setup: {e}")
    else:
        # Native / Current context mode
        print_debug(f"*** USING NATIVE / CURRENT CONTEXT MODE ***")
        if not SIMUL_MODE and "gemini.google.com" not in page.url:
            page.goto("https://gemini.google.com/", timeout=60000, wait_until="domcontentloaded")
            page.wait_for_load_state("domcontentloaded", timeout=60000)
            time.sleep(2)
    
    print_debug(">>> CHAT MODE SETUP COMPLETED <<<")

    
    # Select model type if specified
    if type_arg:
        print_debug(f"Selecting model type: {type_arg}")
        if SIMUL_MODE:
             print_debug(f"  [SIMUL] Skipping model selection: {type_arg}")
        else:
            try:
                # Open the model selector dropdown
                print_debug("Looking for model selector button...")
                selector_opened = False
                
                # EXACT selectors from DOM inspection (data-test-id is most reliable)
                model_selector_buttons = [
                    '[data-test-id="bard-mode-menu-button"]',
                    'button[aria-label="Otwórz selektor trybu"]',
                    'button[aria-label="Open model selector"]',
                    'button[aria-label*="selektor"]',
                    'button[aria-label*="selector"]',
                    'button[aria-label*="trybu"]',
                    'button[aria-label*="mode"]',
                    'button[aria-label*="Mode"]',
                ]
                
                for selector in model_selector_buttons:
                    try:
                        selector_button = page.locator(selector).first
                        if selector_button.is_visible(timeout=1000):
                            selector_button.click()
                            print_debug(f"Opened model selector using: {selector}")
                            selector_opened = True
                            time.sleep(1.5)
                            break
                    except:
                        continue
                
                # Fallback: click any visible button that shows current model name
                if not selector_opened:
                    print_debug("Exact selectors failed, trying model name buttons as dropdown openers...")
                    all_model_names = ['Szybki', 'Szybkie', 'Myślący', 'Myślenie', 'Flash 2.0', 'Flash-Lite', 'Flash',
                                       'Thinking', 'Pro 2.0', 'Pro', 'Zaawansowany', 'Gemini']
                    for model_name in all_model_names:
                        try:
                            btn = page.locator(f'button:has-text("{model_name}")').first
                            if btn.is_visible(timeout=800):
                                btn.click()
                                print_debug(f"Opened model selector by clicking model name button: '{model_name}'")
                                selector_opened = True
                                time.sleep(1.5)
                                break
                        except:
                            continue
                
                if not selector_opened:
                    print_debug("Could not open model selector dropdown, trying direct menu item selection...")
                
                # Map type argument to search terms (English and Polish)
                type_mapping = {
                    'fast': ['1.5 Flash', 'Gemini 1.5 Flash', 'Szybki', 'Szybkie', 'Flash 2.0', 'Flash', 'Fast', 'Gemini Flash'],
                    'think': ['Myślący', 'Myślenie', 'Thinking', 'Deep Thinking', 'Gemini Thinking'],
                    'pro': ['1.5 Pro', 'Gemini 1.5 Pro', 'Pro 1.5', 'Pro 2.0', 'Gemini 2.0 Pro', 'Gemini Pro 2.0', 'Pro', 'Zaawansowany', 'Advanced', 'Gemini Advanced', 'Ultra', 'Gemini Pro Exp', 'Gemini Pro', 'Pro Exp'],
                    'flash': ['1.5 Flash', 'Gemini 1.5 Flash', 'Flash 1.5', 'Flash 2.0', 'Gemini Flash', 'Flash'],
                    'flash-lite': ['1.5 Flash-8B', 'Gemini 1.5 Flash-8B', 'Flash-Lite', 'Flash Lite', 'Gemini Flash-Lite']
                }
                
                search_terms = type_mapping.get(type_arg.lower(), [])
                if search_terms:
                    type_clicked = False
                    # Increase robustness for finding the menu items
                    for attempt in range(2):
                        if type_clicked: break
                        if attempt > 0:
                            print_debug("Retrying model selection...")
                            time.sleep(1)
                            
                        for term in search_terms:
                            if type_clicked:
                                break
                            # Menu items in Gemini are children of role="menu" without role="menuitem"
                            # Use text-is for exact match to avoid clicking large containers
                            selectors = [
                                f'[role="menu"] :text-is("{term}")',
                                f'[role="menu"] span:text-is("{term}")',
                                f'[role="menu"] div:text-is("{term}")',
                                f'[role="menu"] :has-text("{term}")',
                                f'[role="listbox"] :text-is("{term}")',
                                f'[role="listbox"] :has-text("{term}")',
                                f'[role="menuitem"]:text-is("{term}")',
                                f'[role="option"]:text-is("{term}")',
                            ]
                            
                            if term == "Flash":
                                selectors = [
                                    '[role="menu"] [role="menuitem"]:has-text("Flash"):not(:has-text("Lite")):not(:has-text("lite"))',
                                    '[role="listbox"] [role="option"]:has-text("Flash"):not(:has-text("Lite")):not(:has-text("lite"))',
                                ] + selectors
                            
                            for selector in selectors:
                                try:
                                    elem = page.locator(selector).first
                                    if elem.is_visible(timeout=1500):
                                        elem.scroll_into_view_if_needed()
                                        elem.click(timeout=3000)
                                        print_debug(f"Selected model type '{term}' using: {selector}")
                                        type_clicked = True
                                        time.sleep(1)
                                        break
                                except:
                                    continue
                            
                            # JavaScript fallback: find element with exact text inside role=menu
                            if not type_clicked:
                                try:
                                    clicked = page.evaluate(f"""
                                        (targetText) => {{
                                            const containers = document.querySelectorAll('[role="menu"], [role="listbox"], .model-selector-menu');
                                            for (const menu of containers) {{
                                                const walker = document.createTreeWalker(menu, NodeFilter.SHOW_ELEMENT);
                                                let node;
                                                while (node = walker.nextNode()) {{
                                                    let text = node.innerText?.trim();
                                                    if (text === targetText || (text && text.includes(targetText) && text.length <= targetText.length + 30)) {{
                                                        let parentText = node.parentNode ? node.parentNode.innerText : "";
                                                        if (targetText === "Flash" && (text.includes("Lite") || text.includes("lite") || text.includes("8B") || parentText.includes("Lite"))) continue;
                                                        node.click();
                                                        return true;
                                                    }}
                                                }}
                                            }}
                                            return false;
                                        }}
                                    """, term)
                                    if clicked:
                                        print_debug(f"Selected model type '{term}' via JS text walker")
                                        type_clicked = True
                                        time.sleep(1)
                                except Exception as e:
                                    print_debug(f"JS fallback failed for '{term}': {e}")
                    
                    if not type_clicked:
                        print_warning(f"Could not find model type selector for: {type_arg}")
                else:
                    print_warning(f"Unknown model type: {type_arg}")
            except Exception as e:
                print_warning(f"Error selecting model type: {e}")

    # Select thinking mode if specified
    if think_mode:
        print_debug(f"Selecting thinking mode: {think_mode}")
        if SIMUL_MODE:
            print_debug(f"  [SIMUL] Skipping thinking mode selection: {think_mode}")
        else:
            try:
                # Ensure model selector menu is closed first
                try:
                    page.keyboard.press("Escape")
                    time.sleep(0.3)
                except:
                    pass

                # Re-open the same model selector menu (same as --type)
                selector_opened = False
                for selector in [
                    '[data-test-id="bard-mode-menu-button"]',
                    'button[aria-label="Otwórz selektor trybu"]',
                    'button[aria-label="Open model selector"]',
                    'button[aria-label*="selektor"]',
                    'button[aria-label*="selector"]',
                    'button[aria-label*="trybu"]',
                    'button[aria-label*="mode"]',
                    'button[aria-label*="Mode"]',
                ]:
                    try:
                        btn = page.locator(selector).first
                        if btn.is_visible(timeout=1000):
                            btn.click()
                            print_debug(f"Opened model selector using: {selector}")
                            selector_opened = True
                            time.sleep(1)
                            break
                    except:
                        continue

                if not selector_opened:
                    # Fallback: click current model name button
                    for model_name in ['Szybki', 'Szybkie', 'Myślący', 'Myślenie', 'Flash 2.0', 'Flash-Lite', 'Flash',
                                       'Thinking', 'Pro 2.0', 'Pro', 'Zaawansowany', 'Gemini']:
                        try:
                            btn = page.locator(f'button:has-text("{model_name}")').first
                            if btn.is_visible(timeout=800):
                                btn.click()
                                print_debug(f"Opened model selector by clicking: '{model_name}'")
                                selector_opened = True
                                time.sleep(1)
                                break
                        except:
                            continue

                if selector_opened:
                    # Inside the menu, find and click thinking section to expand it
                    poziom_clicked = False
                    for sel in [
                        'button:has-text("Poziom myślenia")',
                        'span:has-text("Poziom myślenia")',
                        'div:has-text("Poziom myślenia")',
                        '[role="menu"] button:has-text("Poziom myślenia")',
                        '[role="listbox"] button:has-text("Poziom myślenia")',
                        'button:has-text("Thinking")',
                        '[role="menu"] button:has-text("Thinking")',
                        'button:has-text("Myślenie")',
                        '[role="menu"] button:has-text("Myślenie")',
                    ]:
                        try:
                            elem = page.locator(sel).first
                            if elem.is_visible(timeout=1000):
                                elem.click()
                                print_debug(f"Opened thinking section using: {sel}")
                                poziom_clicked = True
                                time.sleep(0.5)
                                break
                        except:
                            continue

                    # Select the target thinking mode
                    if think_mode == 'basic':
                        targets = ['Standardowy', 'Myślenie standardowy', 'Podstawowy', 'Basic']
                    else:
                        targets = ['Rozszerzony', 'Myślenie rozszerzony', 'Extended', 'Zaawansowany']

                    target_found = False
                    for target in targets:
                        mode_selectors = [
                            f'[role="menu"] [role="radio"]:text-is("{target}")',
                            f'[role="menu"] button:text-is("{target}")',
                            f'[role="menu"] span:text-is("{target}")',
                            f'[role="menu"] [role="radio"]:has-text("{target}")',
                            f'[role="menu"] button:has-text("{target}")',
                            f'[role="listbox"] [role="radio"]:text-is("{target}")',
                            f'[role="listbox"] button:text-is("{target}")',
                            f'[role="listbox"] [role="radio"]:has-text("{target}")',
                            f'[role="radio"]:text-is("{target}")',
                            f'button:text-is("{target}")',
                            f'[role="radio"]:has-text("{target}")',
                            f'button:has-text("{target}")',
                        ]
                        for sel in mode_selectors:
                            try:
                                elem = page.locator(sel).first
                                if elem.is_visible(timeout=1000):
                                    elem.click()
                                    print_debug(f"Selected thinking mode '{target}' using: {sel}")
                                    target_found = True
                                    time.sleep(0.5)
                                    break
                            except:
                                continue
                        if target_found:
                            break

                    if not target_found:
                        print_warning(f"Could not find thinking mode selector for: {think_mode}")

                    # Close the menu
                    try:
                        page.keyboard.press("Escape")
                        time.sleep(0.3)
                    except:
                        pass
                else:
                    print_warning(f"Could not open model selector to set thinking mode: {think_mode}")

            except Exception as e:
                print_warning(f"Error selecting thinking mode: {e}")

    if not SIMUL_MODE and CHAT_MODE != 'native':
        try:
            # Click on Tools button (Narzędzia)
            print_debug("Looking for Tools button...")
        
            # Try multiple selectors for the Tools button
            tools_selectors = [
                'button:has-text("Narzędzia")',
                'button:has-text("Tools")',
                'button[aria-label*="narzędzi"]',
                'button[aria-label*="tool"]',
                'button[aria-label*="Tool"]',
                'div[role="button"]:has-text("Narzędzia")',
                'div[role="button"]:has-text("Tools")'
            ]
            
            tools_clicked = False
            for selector in tools_selectors:
                try:
                    tools_button = page.locator(selector).first
                    if tools_button.is_visible(timeout=3000):
                        if tools_button.is_disabled():
                            print_debug(f"Tools button found but disabled, skipping: {selector}")
                            continue
                        tools_button.click()
                        print_debug(f"Clicked Tools button using: {selector}")
                        tools_clicked = True
                        time.sleep(1.5)
                        break
                except:
                    continue
            
            if not tools_clicked:
                print_debug("Could not find Tools button, continuing anyway...")
            
            # Click on "Twórz obrazy" / "Create images" (or "Utwórz obraz")
            print_debug("Looking for Create images option...")
            
            # Try multiple selectors for the Create images option
            create_selectors = [
                'text="Utwórz obraz"',
                'text="Twórz obrazy"',
                'text="Create images"',
                'text="Generuj obrazy"',
                'text="Image generation"',
                'span:has-text("Utwórz obraz")',
                'span:has-text("Twórz obrazy")',
                'span:has-text("Create images")',
                'div:has-text("Utwórz obraz")',
                'div:has-text("Twórz obrazy")',
                'div:has-text("Create images")',
                '[role="menuitem"]:has-text("obraz")',
                '[role="menuitem"]:has-text("obrazy")',
                '[role="menuitem"]:has-text("image")'
            ]
            
            create_clicked = False
            for selector in create_selectors:
                try:
                    create_images = page.locator(selector).first
                    if create_images.is_visible(timeout=3000):
                        disabled = create_images.is_disabled()
                        if disabled:
                            print_debug(f"Create images option found but disabled, skipping: {selector}")
                            continue
                        create_images.click()
                        print_debug(f"Clicked Create images using: {selector}")
                        create_clicked = True
                        time.sleep(1.5)
                        break
                except:
                    continue
            
            if not create_clicked:
                print_debug("Could not find Create images option, mode may already be active...")
        
        except Exception as e:
            print_warning(f"Error during tool activation: {e}")
            print_debug("Continuing anyway, assuming mode is already active...")
    elif SIMUL_MODE:
        print_debug("  [SIMUL] Skipping tool activation")

    # Apply --insprompt logic (Prepend to user prompt)
    working_prompt = prompt
    if insp_prompt:
        # Check if insp_prompt ends with dot
        if insp_prompt.strip().endswith('.'):
             working_prompt = f"{insp_prompt} {working_prompt}"
        else:
             working_prompt = f"{insp_prompt}. {working_prompt}"
        print_debug(f"Prepended insprompt. Working prompt: {working_prompt[:50]}...")
    
    # Apply --addprompt logic (Append to user prompt)
    if add_prompt:
        if working_prompt.strip().endswith('.'):
            working_prompt = f"{working_prompt} {add_prompt}"
        else:
            working_prompt = f"{working_prompt}. {add_prompt}"
        print_debug(f"Appended addprompt. Working prompt: {working_prompt[:50]}...")
    
    # Apply Format to Prompt logic
    final_prompt = working_prompt
    if fmt_arg:
        ar_suffix = ""
        if fmt_arg == "43":
            #ar_suffix = " --ar 4:3"
            ar_suffix = " Aspect ratio 4:3."
        elif fmt_arg == "169":
            #ar_suffix = " --ar 16:9"
            ar_suffix = " Aspect ratio 16:9."
        elif fmt_arg == "11":
            #ar_suffix = " --ar 1:1"
            ar_suffix = " Aspect ratio 1:1."
        
        if ar_suffix:
            # Check if prompt ends with period, add if missing
            if not working_prompt.rstrip().endswith('.'):
                final_prompt = f"{working_prompt}. {ar_suffix.strip()}"
            else:
                final_prompt = f"{working_prompt} {ar_suffix.strip()}"
            print_debug(f"Applied format modifier. Final prompt: '{final_prompt[:50]}...'")
    
    # Apply Resolution to Prompt logic
    width = None
    height = None
    
    if res_arg:
        try:
            width, height = map(int, res_arg.split(","))
            print_debug(f"Applied resolution modifier: {width}x{height}")
        except ValueError:
            print_warning(f"Invalid resolution format: {res_arg}. Expected format: width,height")
    elif resx_arg or resy_arg:
        # Determine aspect ratio from fmt_arg or default to 16:9
        aspect_width = 16
        aspect_height = 9
        
        if fmt_arg == "43":
            aspect_width = 4
            aspect_height = 3
        elif fmt_arg == "11":
            aspect_width = 1
            aspect_height = 1
        # Default is 16:9 for fmt_arg == "169" or None
        
        if resx_arg:
            try:
                width = int(resx_arg)
                height = int(width * aspect_height / aspect_width)
                print_debug(f"Calculated resolution from width: {width}x{height} (aspect ratio {aspect_width}:{aspect_height})")
            except (ValueError, TypeError):
                print_warning(f"Invalid --resx value: {resx_arg}")
        elif resy_arg:
            try:
                height = int(resy_arg)
                width = int(height * aspect_width / aspect_height)
                print_debug(f"Calculated resolution from height: {width}x{height} (aspect ratio {aspect_width}:{aspect_height})")
            except (ValueError, TypeError):
                print_warning(f"Invalid --resy value: {resy_arg}")
    
    # Only add size parameter if user explicitly specified resolution
    if width and height:
        res_suffix = f" --size {width}x{height}"
        final_prompt = f"{final_prompt}{res_suffix}".strip()
        print_debug(f"Added resolution to prompt: {width}x{height}")
    
    # --- SIMULATION MODE OUTPUT ---
    if SIMUL_MODE:
        print_info(f"\n{Fore.YELLOW}--- SIMULATION: {filename_base} ---{Style.RESET_ALL}")
        print_info(f"{Fore.CYAN}Final Prompt:{Style.RESET_ALL} {final_prompt}")
        
        ar_info = "Default (16:9)"
        if fmt_arg == "43": ar_info = "4:3"
        elif fmt_arg == "11": ar_info = "1:1"
        elif fmt_arg == "169": ar_info = "16:9"
        
        print_info(f"{Fore.CYAN}Aspect Ratio:{Style.RESET_ALL} {ar_info}")
        
        if width and height:
            print_info(f"{Fore.CYAN}Resolution:{Style.RESET_ALL} {width}x{height}")
        elif res_arg:
             print_info(f"{Fore.CYAN}Resolution:{Style.RESET_ALL} {res_arg} (Direct)")
        
        print_info(f"{Fore.CYAN}Model Type:{Style.RESET_ALL} {type_arg if type_arg else 'Default'}")
        if think_mode:
            print_info(f"{Fore.CYAN}Thinking Mode:{Style.RESET_ALL} {think_mode}")
        
        if insp_prompt:
             print_info(f"{Fore.CYAN}Prepend (Insprompt):{Style.RESET_ALL} {insp_prompt}")
        if add_prompt:
             print_info(f"{Fore.CYAN}Append (Addprompt):{Style.RESET_ALL} {add_prompt}")
             
        print_info("-" * 40 + "\n")
        
        # Print simulation output
        final_filename_simul = get_final_filename(filename_base)
        sep = " | "
        
        # Extract current/total from progress_prefix for display
        current = ""
        total = ""
        if progress_prefix and progress_prefix.startswith("[") and "/" in progress_prefix:
            parts = progress_prefix[1:].replace("]", "").split("/")
            if len(parts) == 2:
                current, total = parts[0], parts[1]

        if USE_COLOR:
            c_bracket = f"{Fore.WHITE}"
            c_numbers = f"{Fore.GREEN}"
            c_fname = f"{Fore.CYAN}{final_filename_simul}{Style.RESET_ALL}"
            c_ok = f"{Fore.GREEN}OK{Style.RESET_ALL}"
            print(f"{c_bracket}[{Style.RESET_ALL}{c_numbers}{current:>3}/{total:>3}{Style.RESET_ALL}{c_bracket}]{Style.RESET_ALL} {c_fname}{sep}{c_ok}")
        else:
            print(f"{progress_prefix} {final_filename_simul}{sep}OK")
        
        LOG_LINES.append(f"{progress_prefix} {final_filename_simul}{sep}OK")
        
        SUCCESS_COUNT += 1
        return True
    
    new_resp = page
    initial_count = 0
    if not SIMUL_MODE:
        try:
            initial_count = page.locator("model-response").count()
            print_debug(f"Initial model-response count: {initial_count}")
        except Exception as e:
            print_debug(f"Error counting initial model-response: {e}")

    # Send the prompt
    print_debug(f"Sending prompt: {final_prompt[:50]}...")
    
    try:
        # Find the input field and enter the prompt
        # Try different selectors for the Gemini input
        input_locator = None
        selectors = [
            'rich-textarea .ql-editor',
            'div[contenteditable="true"]',
            'textarea[aria-label*="prompt"]',
            'textarea[placeholder*="prompt"]',
            '.ql-editor[contenteditable="true"]'
        ]
        
        for selector in selectors:
            try:
                temp_locator = page.locator(selector).first
                if temp_locator.is_visible(timeout=2000):
                    input_locator = temp_locator
                    print_debug(f"Found input using selector: {selector}")
                    break
            except:
                continue
        
        if not input_locator:
            print_error("Could not find input field")
            return False
        
        input_locator.click()
        time.sleep(0.5)
        
        # Clear any existing content
        input_locator.press("Control+A")
        time.sleep(0.2)
        
        # Use clipboard to paste the full prompt (more reliable than typing)
        print_debug(f"Pasting prompt ({len(final_prompt)} characters)...")
        # Escape the prompt for JavaScript - do this outside f-string to avoid backslash issues
        escaped_prompt = final_prompt.replace('\\', '\\\\').replace('`', '\\`').replace('$', '\\$')
        page.evaluate(f"""
            navigator.clipboard.writeText(`{escaped_prompt}`);
        """)
        time.sleep(0.3)
        input_locator.press("Control+V")
        time.sleep(1)
        
        print_debug(f"Prompt pasted, verifying...")
        
        # Verify the text was entered
        try:
            entered_text = input_locator.inner_text()
            print_debug(f"Entered text length: {len(entered_text)}, expected: {len(final_prompt)}")
        except:
            print_debug("Could not verify entered text")
        # Submit the prompt by clicking the send button (arrow)
        print_debug("Clicking send button...")
        send_clicked = False
        for sel in [
            'button[aria-label*="Wyślij"]',
            'button[aria-label*="Send"]',
            'button[aria-label*="send"]',
            'button[aria-label*="wyślij"]',
            'button[data-test-id*="send"]',
            'button:has(svg[data-icon="send"])',
            'button:has([data-icon="send"])',
        ]:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=2000) and not btn.is_disabled():
                    btn.click()
                    print_debug(f"Clicked send button using: {sel}")
                    send_clicked = True
                    break
            except:
                continue
        if not send_clicked:
            print_debug("Send button not found, pressing Enter instead...")
            page.keyboard.press("Enter")
        
        if not SIMUL_MODE:
            # Wait for the new model-response container to appear
            print_debug("Waiting for new model-response container to appear...")
            new_response_appeared = False
            wait_start = time.time()
            while time.time() - wait_start < 30: # 30s timeout
                current_count = page.locator("model-response").count()
                if current_count > initial_count:
                    new_response_appeared = True
                    print_debug(f"New model-response container detected (count: {current_count})")
                    break
                time.sleep(0.5)
                
            if new_response_appeared:
                new_resp = page.locator("model-response").nth(initial_count)
            else:
                print_warning("Timeout waiting for new model-response container. Will proceed with last container.")
                new_resp = page.locator("model-response").last
        
        print_debug("Prompt sent, waiting for image generation...")
        

        
    except Exception as e:
        print_error(f"Error sending prompt: {e}")
        return False
    
    # Wait for image to be generated and download button to appear
    print_debug(f"Waiting for image generation (minimum {min_gen_time}s)...")
    
    try:
        # Poll for download button with timeout
        # Poll for download button with timeout - dynamically adjust based on min_gen_time
        max_wait_time = max(min_gen_time + 60, 120)  # At least 1 minute buffer over minimum, min 120s
        check_interval = 5  # Check every 5 seconds during minimum wait, then every 1 second
        elapsed_time = 0
        
        # During minimum generation time, actively check for image and button every 5 seconds
        print_debug(f"Waiting minimum generation time ({min_gen_time}s), checking for image every 5s...")
        min_wait_remaining = min_gen_time
        
        # Image presence selectors (to detect if image has been generated)
        image_selectors = [
            'generated-image img',
            'single-image img',
            'div.image-container img',
            'img[src*="googleusercontent.com"]:not([src*="/a/"])', # Exclude avatars
            'img[src*="imagen"]',
            'img[alt*="Generated"]',
            'img[alt*="Wygenerowany"]',
            'img[src^="blob:"]'
        ]
        
        # Multiple download button selector patterns
        button_selectors = [
            'download-generated-image-button',
            'download-generated-image-button gem-icon-button',
            'download-generated-image-button button',
             # Priority: Full size / specific download buttons
            'button[aria-label*="pełnym rozmiarze"]',
            'button[aria-label*="full size"]',
            'button[aria-label="Pobierz"]',
            'button[aria-label="Download"]',
            
            # Aria labels (Polish and English)
            'button[aria-label*="Pobierz"]',
            'button[aria-label*="Download"]',
            'button[aria-label*="download"]',
            '[aria-label*="Pobierz"]',
            '[aria-label*="Download"]',
            
            # Text content
            'button:has-text("Pobierz")',
            'button:has-text("Download")',
            
            # Icons and common patterns
            'button[data-tooltip*="Download"]',
            '[role="button"][aria-label*="download"]',
            '[role="button"][aria-label*="Download"]',
            '[data-test-id*="download"]',
            'button[title*="download"]',
            'button[title*="Download"]'
        ]
        
        download_button = None
        image_found = False
        image_elem = None
        
        # Check during minimum wait time
        while min_wait_remaining > 0:
            check_time = min(5, min_wait_remaining)
            time.sleep(check_time)
            elapsed_time += check_time
            min_wait_remaining -= check_time
            
            # Check if image has been generated
            if not image_found:
                for selector in image_selectors:
                    try:
                        image_elem = new_resp.locator(selector).last
                        if image_elem.is_visible(timeout=100):
                            image_found = True
                            print_debug(f"Generated image detected after {elapsed_time}s using: {selector}")
                            try:
                                image_elem.hover(timeout=1000)
                                time.sleep(0.5)
                            except:
                                pass
                            break
                    except:
                        continue
            
            # Check for download button
            if image_found:
                # Wait a bit after image appears for download button to become available
                if not download_button:
                     # print_debug("Image found, quickly checking for download button...")
                     pass
                
                for selector in button_selectors:
                    try:
                        download_button = new_resp.locator(selector).last
                        if download_button.is_visible(timeout=100):
                            print_debug(f"Download button found after {elapsed_time:.1f}s using: {selector}")
                            print_debug(f"Image ready before minimum wait time completed - proceeding early")
                            break
                    except:
                        continue
                
                # If both image and button found, exit early
                if download_button and download_button.is_visible(timeout=100):
                    break
            
            if min_wait_remaining > 0:
                print_debug(f"Checking... ({elapsed_time}s/{min_gen_time}s minimum wait)")
        
        # After minimum wait time, continue checking with 1 second intervals if needed
        check_interval = 1
        
        # Error/Refusal detection strings (Polish and English)
        error_indicators = [
            "nie mogę wygenerować", "can't generate", "cannot generate",
            "nie potrafię wygenerować", "nie jestem w stanie",
            "something went wrong", "wystąpił błąd", "błąd podczas",
            "przekroczono limit", "limit reached", "too many requests",
            "policy violation", "zasadami użytkowania", "naruszenie zasad"
        ]
        
        print_debug(f"Checking for generated image and download button (max wait: {max_wait_time}s)...")
        
        while elapsed_time < max_wait_time:
            # Check for common refusal/error messages
            try:
                # Look at the last few elements for error text, avoiding sidebar false positives
                if page.locator("model-response").count() > 0:
                    page_text = page.locator("model-response").last.inner_text().lower()
                else:
                    page_text = page.locator("div#chat-history").inner_text().lower()
                for indicator in error_indicators:
                    if indicator in page_text:
                        if "limit reached" in indicator or "too many requests" in indicator or "przekroczono limit" in indicator:
                            if limit_attempt < limit_retries:
                                print_warning(f"\n[LIMIT REACHED] Gemini rate limit detected. Waiting {limit_wait_time}s before retry...")
                                time.sleep(limit_wait_time)
                                # Recursive retry
                                return process_single_prompt(page, prompt, filename_base, output_dir, add_prompt, insp_prompt, fmt_arg, res_arg, resx_arg, resy_arg, type_arg, min_gen_time, dl_timeout_sec, download_retries, progress_prefix, limit_attempt + 1)
                            else:
                                print_error(f"\n[ERROR] Rate limit reached and all {limit_retries} retries exhausted.")
                        
                        print_error(f"Gemini refusal or error detected: '{indicator}'")
                        FAIL_COUNT += 1
                        ERROR_LOG.append((filename_base, f"Gemini refusal: {indicator}"))
                        return False
            except:
                pass
            if not image_found:
                for selector in image_selectors:
                    try:
                        image_elem = new_resp.locator(selector).last
                        if image_elem.is_visible(timeout=100):
                            image_found = True
                            print_debug(f"Generated image detected after {elapsed_time}s using: {selector}")
                            try:
                                image_elem.hover(timeout=1000)
                                time.sleep(0.5)
                            except:
                                pass
                            # Immediately check for button, no sleep
                            break
                    except:
                        continue
            
            # Then check for download button with comprehensive selectors
            download_button_selectors = [
                'download-generated-image-button',
                'download-generated-image-button gem-icon-button',
                'download-generated-image-button button',
                 # Priority: Full size / specific download buttons
                'button[aria-label*="pełnym rozmiarze"]',
                'button[aria-label*="full size"]',
                'button[aria-label="Pobierz"]',
                'button[aria-label="Download"]',
                
                # Aria labels (Polish and English)
                'button[aria-label*="Pobierz"]',
                'button[aria-label*="Download"]',
                'button[aria-label*="download"]',
                '[aria-label*="Pobierz"]',
                '[aria-label*="Download"]',
                
                # Text content
                'button:has-text("Pobierz")',
                'button:has-text("Download")',
                'text="Pobierz"',
                'text="Download"',
                
                # Icons and common patterns
                'button[data-tooltip*="Download"]',
                'button[data-tooltip*="Pobierz"]',
                'button:has([class*="download"])',
                'button:has([class*="Download"])',
                
                # Any download-related button
                '[role="button"][aria-label*="download"]',
                '[role="button"][aria-label*="Download"]',
                '[role="button"][aria-label*="Pobierz"]',
                '[role="button"][aria-label*="pobierz"]',
                'a[download]',
                'a[href*="download"]',
                
                # Generic fallbacks
                'button[title*="download"]',
                'button[title*="Download"]',
                'button[title*="Pobierz"]'
            ]
            
            for selector in download_button_selectors:
                try:
                    download_button = new_resp.locator(selector).last
                    if download_button.is_visible(timeout=100):
                        print_debug(f"Download button found after {elapsed_time:.1f}s using: {selector}")
                        break
                except Exception as e:
                    continue
            
            # If both image and button found, we're ready to download
            if image_found and download_button:
                try:
                    if download_button.is_visible(timeout=100):
                        print_debug(f"Image generated and download button available after {elapsed_time}s")
                        break
                except:
                    download_button = None

            
            time.sleep(check_interval)
            elapsed_time += check_interval
            
            if elapsed_time % 5 == 0:  # Log every 5 seconds
                status = "Image detected, waiting for download button" if image_found else "Waiting for image generation"
                print_debug(f"{status}... ({elapsed_time}s)")
        
        # Construct filename
        final_filename = get_final_filename(filename_base)
        filepath = os.path.join(output_dir, final_filename)
        
        # Check if file exists and handle based on mode (before attempting download)
        if os.path.exists(filepath):
            if SKIP_MODE:
                print_warning(f"Skipping existing file: {final_filename}")
                return True
            elif not OVERWRITE_MODE:
                response = input(f"File '{final_filename}' already exists. Overwrite? (y/n): ").strip().lower()
                if response != 'y':
                    print_info(f"Skipped: {final_filename}")
                    return True

        download_success = False

        # Try to download using the official download button first (gets full resolution)
        if download_button and download_button.is_visible(timeout=1000):
            # Download with retry — only the download click is retried, NOT image generation
            print_debug("Found download button, attempting to download full resolution image...")
            time.sleep(2)  # Give the UI time to attach event listeners to the download button
            dl_timeout_ms = dl_timeout_sec * 1000
            
            for dl_attempt in range(download_retries + 1):
                if dl_attempt > 0:
                    RETRY_DOWNLOAD_COUNT += 1
                    print_warning(f"Download retry {dl_attempt}/{download_retries} for: {final_filename}")
                    time.sleep(2)
                
                try:
                    print_debug(f"Expecting download with timeout: {dl_timeout_sec}s (attempt {dl_attempt + 1})")
                    with page.expect_download(timeout=dl_timeout_ms) as download_info:
                        download_button.click()
                    
                    download = download_info.value
                    download.save_as(filepath)
                    
                    # Check if downloaded file has zero bytes
                    file_size = os.path.getsize(filepath)
                    if file_size == 0:
                        print_warning(f"Downloaded file has 0 bytes (attempt {dl_attempt + 1}/{download_retries + 1}): {final_filename}")
                        try:
                            os.remove(filepath)
                            print_debug(f"Removed zero-byte file: {final_filename}")
                        except Exception as rm_err:
                            print_debug(f"Could not remove zero-byte file: {rm_err}")
                        if dl_attempt < download_retries:
                            time.sleep(2)
                            # Try to re-find the download button before next attempt
                            try:
                                for selector in download_button_selectors:
                                    try:
                                        btn = new_resp.locator(selector).last
                                        if btn.is_visible(timeout=500):
                                            download_button = btn
                                            break
                                    except:
                                        continue
                            except:
                                pass
                        continue
                    
                    print_debug(f"Image saved to {final_filename} ({file_size} bytes)")
                    download_success = True
                    break
                except Exception as e:
                    print_warning(f"Download attempt {dl_attempt + 1} failed: {e}")
                    if dl_attempt < download_retries:
                        # Try to re-find the download button before next attempt
                        try:
                            for selector in download_button_selectors:
                                try:
                                    btn = new_resp.locator(selector).last
                                    if btn.is_visible(timeout=500):
                                        download_button = btn
                                        break
                                except:
                                    continue
                        except:
                            pass
        else:
            print_debug(f"Download button not found after {elapsed_time:.1f}s.")

        # Fallback to canvas-based extraction (instant but may capture lower resolution UI preview)
        if not download_success and image_found and image_elem:
            print_debug("Attempting instant canvas-based image extraction as fallback...")
            try:
                base64_data = image_elem.evaluate("""(img) => {
                    return new Promise((resolve, reject) => {
                        const convert = () => {
                            try {
                                const canvas = document.createElement('canvas');
                                canvas.width = img.naturalWidth || img.width || 1024;
                                canvas.height = img.naturalHeight || img.height || 1024;
                                const ctx = canvas.getContext('2d');
                                ctx.drawImage(img, 0, 0);
                                resolve(canvas.toDataURL('image/jpeg', 0.95));
                            } catch (e) {
                                reject(e.toString());
                            }
                        };
                        if (img.complete && img.naturalWidth !== 0) {
                            convert();
                        } else {
                            img.onload = convert;
                            img.onerror = () => reject("Image load error");
                        }
                    });
                }""")
                if base64_data.startswith("data:image/") and "," in base64_data:
                    import base64
                    header, encoded = base64_data.split(",", 1)
                    data = base64.b64decode(encoded)
                    with open(filepath, "wb") as f:
                        f.write(data)
                    file_size = os.path.getsize(filepath)
                    if file_size > 0:
                        print_debug(f"Image successfully exported via canvas to {final_filename} ({file_size} bytes)")
                        download_success = True
            except Exception as canvas_err:
                print_debug(f"Canvas extraction failed: {canvas_err}. Falling back to ultimate standard download...")

        # THE ULTIMATE FALLBACK - Runs if standard download failed or button was missing
        if not download_success:
            if image_found and image_elem:
                print_warning(f"Attempting ultimate direct image fallback...")
                try:
                    src = image_elem.get_attribute("src", timeout=2000)
                    if not src:
                        inner_img = image_elem.locator("img").first
                        if inner_img:
                            src = inner_img.get_attribute("src", timeout=1000)
                            
                    if src:
                        try:
                            image_buffer = page.evaluate("""
                                async (url) => {
                                    const response = await fetch(url);
                                    const blob = await response.blob();
                                    return new Promise((resolve, reject) => {
                                        const reader = new FileReader();
                                        reader.onloadend = () => resolve(reader.result);
                                        reader.onerror = reject;
                                        reader.readAsDataURL(blob);
                                    });
                                }
                            """, src)
                            import base64
                            with open(filepath, "wb") as f:
                                f.write(base64.b64decode(image_buffer.split(",")[1]))
                        except Exception as js_e:
                            print_debug(f"JS fetch failed, trying page.context.request.get... ({js_e})")
                            res = page.context.request.get(src)
                            if res.ok:
                                with open(filepath, "wb") as f:
                                    f.write(res.body())
                            else:
                                raise Exception(f"Request failed: {res.status}")
                        
                        download_success = True
                        file_size = os.path.getsize(filepath)
                        print_debug(f"Image saved via ultimate fallback to {final_filename} ({file_size} bytes)")
                    else:
                        raise Exception("Image src attribute not found")
                except Exception as e:
                    print_error(f"Ultimate fallback download failed: {e}")
            else:
                print_error(f"Image not found. Generation timeout.")

        if not download_success:
            print_error(f"Download failed after {download_retries + 1} attempt(s): {final_filename}")
            FAIL_COUNT += 1
            ERROR_LOG.append((filename_base, "Download failed after all retry attempts"))
            return False
        
    except Exception as e:
        print_error(f"Error during image generation/download: {e}")
        FAIL_COUNT += 1
        ERROR_LOG.append((filename_base, str(e)))
        return False
    
    limit_hit_final = False
    try:
        if page.locator("model-response").count() > 0:
            page_text_final = page.locator("model-response").last.inner_text().lower()
        else:
            page_text_final = page.locator("div#chat-history").inner_text().lower()
        for ind in ["limit reached", "too many requests", "przekroczono limit"]:
             if ind in page_text_final:
                 limit_hit_final = True
                 break
    except: pass
    
    if limit_hit_final and limit_attempt < limit_retries:
        print_warning(f"\n[LIMIT REACHED] Gemini rate limit detected after generation loop. Waiting {limit_wait_time}s before retry...")
        time.sleep(limit_wait_time)
        return process_single_prompt(page, prompt, filename_base, output_dir, add_prompt, insp_prompt, fmt_arg, res_arg, resx_arg, resy_arg, type_arg, min_gen_time, dl_timeout_sec, download_retries, progress_prefix, limit_attempt + 1)
    elif limit_hit_final:
        print_error(f"\n[ERROR] Rate limit reached and all {limit_retries} retries exhausted.")
        return False # Exhausted retries

    
    # At this point, download was successful - any errors below are non-critical
    # Check if image is 1024x1024 (1:1 aspect ratio indicator)
    is_1to1 = False
    try:
        from PIL import Image as PILImage
        img_check = PILImage.open(filepath)
        img_w, img_h = img_check.size
        img_check.close()
        if img_w == 1024 and img_h == 1024:
            is_1to1 = True
            RATIO_1TO1_COUNT += 1
    except Exception as e:
        print_debug(f"Could not check image dimensions: {e}")

    # Get stats if requested
    stats = None
    if STAT_MODE:
        try:
            from PIL import Image
            file_size = os.path.getsize(filepath) / 1024  # Size in KiB
            img = Image.open(filepath)
            width, height = img.size
            stats = {
                'time': elapsed_time,  # Use actual measured time
                'resolution': (width, height),
                'size': file_size
            }
        except Exception as e:
            print_debug(f"Could not get image stats: {e}")
    
    # Display result
    try:
        if not DEBUG_MODE:
            sep = " | "
            clear_line = "\033[K"
            
            if USE_COLOR:
                c_bracket = f"{Fore.WHITE}"
                c_numbers = f"{Fore.GREEN}"
                c_fname = f"{Fore.CYAN}{final_filename}{Style.RESET_ALL}"
                c_ok = f"{Fore.GREEN}OK{Style.RESET_ALL}"
                
                # Extract current/total from progress_prefix
                current = ""
                total = ""
                if progress_prefix and progress_prefix.startswith("[") and "/" in progress_prefix:
                    parts = progress_prefix[1:].replace("]", "").split("/")
                    if len(parts) == 2:
                        current, total = parts[0], parts[1]
                
                ratio_suffix = f"{sep}{Fore.RED}1:1{Style.RESET_ALL}" if is_1to1 else ""
                if stats and STAT_MODE:
                    t_str = f"{Fore.LIGHTBLUE_EX}{stats['time']:.2f}s{Style.RESET_ALL}"
                    res_str = f"{Fore.LIGHTBLUE_EX}{stats['resolution'][0]:4d}x{stats['resolution'][1]:4d}{Style.RESET_ALL}"
                    size_str = f"{Fore.LIGHTBLUE_EX}{stats['size']:.2f} KiB{Style.RESET_ALL}"
                    print(f"\r{clear_line}{c_bracket}[{Style.RESET_ALL}{c_numbers}{current}/{total}{Style.RESET_ALL}{c_bracket}]{Style.RESET_ALL} {c_fname}{sep}{c_ok}{sep}{t_str}{sep}{res_str}{sep}{size_str}{ratio_suffix}")
                else:
                    print(f"\r{clear_line}{c_bracket}[{Style.RESET_ALL}{c_numbers}{current}/{total}{Style.RESET_ALL}{c_bracket}]{Style.RESET_ALL} {c_fname}{sep}{c_ok}{ratio_suffix}")
            else:
                ratio_suffix_plain = f"{sep}1:1" if is_1to1 else ""
                if stats and STAT_MODE:
                    stat_str = f"OK | {stats['time']:.2f}s | {stats['resolution'][0]:4d}x{stats['resolution'][1]:4d} | {stats['size']:.2f} KiB"
                    print(f"\r{clear_line}{progress_prefix} {final_filename}{sep}{stat_str}{ratio_suffix_plain}")
                else:
                    print(f"\r{clear_line}{progress_prefix} {final_filename}{sep}OK{ratio_suffix_plain}")
        else:
            msg = f"Success: {final_filename}"
            if stats and STAT_MODE:
                msg += f" | {stats['time']:.2f}s | {stats['resolution'][0]:4d}x{stats['resolution'][1]:4d} | {stats['size']:.2f} KiB"
            if is_1to1:
                if USE_COLOR:
                    msg += f" | {Fore.RED}1:1{Style.RESET_ALL}"
                else:
                    msg += " | 1:1"
            print_success(msg)

        # Collect plain-text log line for --log
        log_sep = " | "
        ratio_log = f"{log_sep}1:1" if is_1to1 else ""
        if stats and STAT_MODE:
            stat_log = f"OK{log_sep}{stats['time']:.2f}s{log_sep}{stats['resolution'][0]:4d}x{stats['resolution'][1]:4d}{log_sep}{stats['size']:.2f} KiB"
            LOG_LINES.append(f"{progress_prefix} {final_filename}{log_sep}{stat_log}{ratio_log}")
        else:
            LOG_LINES.append(f"{progress_prefix} {final_filename}{log_sep}OK{ratio_log}")
    except Exception as e:
        print_debug(f"Display error (non-critical): {e}")
        print_success(f"{final_filename} | OK")
    
    SUCCESS_COUNT += 1
    return True

def write_error_log():
    """Write errors to nanogen.err file."""
    if not ERROR_LOG:
        return
    
    try:
        with open("nanogen.err", "a", encoding="utf-8") as f:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"\n{'='*60}\n")
            f.write(f"Error log - {timestamp}\n")
            f.write(f"{'='*60}\n")
            for filepath, error in ERROR_LOG:
                f.write(f"File: {filepath}\n")
                f.write(f"Error: {error}\n")
                f.write("-" * 60 + "\n")
    except Exception as e:
        print_error(f"Failed to write error log: {e}")

def write_log_file():
    """Write execution log to file specified by --log option."""
    if not LOG_FILE:
        return
    
    try:
        end_time = time.time()
        elapsed = end_time - START_TIME
        
        # Format elapsed time
        hours, remainder = divmod(int(elapsed), 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            time_str = f"{hours}h {minutes}m {seconds}s"
        elif minutes > 0:
            time_str = f"{minutes}m {seconds}s"
        else:
            time_str = f"{seconds}s"
        
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"Nanogen execution log - {timestamp}\n")
            f.write("=" * 60 + "\n")
            f.write(f"Execution time: {time_str}\n")
            f.write(f"Total images generated: {SUCCESS_COUNT}\n")
            f.write(f"Failed downloads: {FAIL_COUNT}\n")
            f.write(f"Download retries: {RETRY_DOWNLOAD_COUNT}\n")
            f.write(f"Images with 1:1 resolution (1024x1024): {RATIO_1TO1_COUNT}\n")
            f.write("=" * 60 + "\n")
            
            skipped_lines = [line for line in LOG_LINES if "SKIPPED" in line]
            downloaded_lines = [line for line in LOG_LINES if "SKIPPED" not in line]
            
            if downloaded_lines:
                f.write("\nDownloaded images:\n")
                f.write("-" * 60 + "\n")
                for line in downloaded_lines:
                    f.write(f"{line}\n")
            
            if skipped_lines:
                f.write("\nSkipped files:\n")
                f.write("-" * 60 + "\n")
                for line in skipped_lines:
                    f.write(f"{line}\n")
        
        print_info(f"Log saved to: {LOG_FILE}")
    except Exception as e:
        print_error(f"Failed to write log file: {e}")

def print_summary():
    """Print execution summary."""
    global START_TIME, SUCCESS_COUNT, FAIL_COUNT, TOTAL_IMAGES
    
    end_time = time.time()
    elapsed = end_time - START_TIME
    
    print_info("\n" + "=" * 60)
    print_color("EXECUTION SUMMARY", Fore.MAGENTA if USE_COLOR else "")
    print_info("=" * 60)
    
    print_info(f"Total images to generate: {TOTAL_IMAGES}")
    print_success(f"Successfully downloaded: {SUCCESS_COUNT}")
    
    if FAIL_COUNT > 0:
        print_error(f"Failed downloads: {FAIL_COUNT}")
    else:
        print_info(f"Failed downloads: {FAIL_COUNT}")
    
    # Format elapsed time
    hours, remainder = divmod(int(elapsed), 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours > 0:
        time_str = f"{hours}h {minutes}m {seconds}s"
    elif minutes > 0:
        time_str = f"{minutes}m {seconds}s"
    else:
        time_str = f"{seconds}s"
    
    print_color(f"Execution time: {time_str}", Fore.CYAN if USE_COLOR else "")
    print_info("=" * 60)
    
    if ERROR_LOG:
        write_error_log()
        print_warning(f"Errors logged to nanogen.err")
    
    write_log_file()

def run_gemini_session(args):
    global START_TIME, TOTAL_IMAGES, SUCCESS_COUNT
    
    # Acquire lock before starting session
    acquire_lock()
    
    START_TIME = time.time()
    
    if not SIMUL_MODE:
        print_debug("Connecting to Chrome (ensure started with start_chrome_debug.bat)...")
    else:
        print_info(f"{Fore.YELLOW}*** SIMULATION MODE ACTIVE ***{Style.RESET_ALL}")
        print_info("No browser connection required.\n")
    
    queue = []
    existing = set()
    if SKIP_MODE and os.path.isdir(args.out):
        try:
            existing = set(os.listdir(args.out))
        except Exception:
            existing = set()
    
    if args.input_file:
        try:
            with open(args.input_file, 'r', encoding='utf-8') as f:
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
            print_error(f"Error loading JSON: {e}")
            return
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_prompt = re.sub(r'[\\/*?:"<>|]', "", args.prompt)[:20].strip()
        fname = f"{safe_prompt}_{timestamp}"
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
    skip_count = SUCCESS_COUNT  # SUCCESS_COUNT was incremented for each skipped file
    
    if skip_count > 0:
        print_info(f"Skipped {skip_count} existing file(s)")
    
    # Show initial info in non-debug mode
    if not DEBUG_MODE:
        if TOTAL_IMAGES > 0:
            print_color(f"Total images to generate: {TOTAL_IMAGES}", Fore.CYAN if USE_COLOR else "")
            print_info("")
    else:
        if TOTAL_IMAGES > 0:
            print_debug(f"Loaded {len(queue)} tasks.")

    # In SIMULATION MODE, bypass Playwright entirely
    if SIMUL_MODE:
        for idx, (prompt, fname) in enumerate(queue):
            current = idx + 1
            total = len(queue)
            progress_prefix = f"[{current:03d}/{total:03d}]"
            
            # Print "Processing..." line at start with proper colors
            if USE_COLOR:
                c_bracket = f"{Fore.WHITE}"
                c_numbers = f"{Fore.GREEN}"
                c_fname = f"{Fore.CYAN}{fname}{Style.RESET_ALL}"
                c_processing = f"{Fore.YELLOW}Processing...{Style.RESET_ALL}"
                print(f"{c_bracket}[{Style.RESET_ALL}{c_numbers}{current:03d}/{total:03d}{Style.RESET_ALL}{c_bracket}]{Style.RESET_ALL} {c_fname} {c_bracket}|{Style.RESET_ALL} {c_processing}", end="", flush=True)
            else:
                print(f"[{current:03d}/{total:03d}] {fname} | Processing...", end="", flush=True)
            
            if DEBUG_MODE:
                print()  # New line before debug output
                print_debug(f"--- Processing {progress_prefix}: {fname} ---")
            
            process_single_prompt(
                None, # No page object needed
                prompt,
                fname,
                args.out,
                args.add_prompt,
                args.insp_prompt,
                args.fmt_arg,
                args.res_arg,
                args.resx_arg,
                args.resy_arg,
                args.type_arg,
                args.think_arg,
                args.mingentime,
                args.dltime,
                0, # No retries in simul
                progress_prefix
            )
        
        print_summary()
        return

    # If all files were skipped, exit immediately without connecting to browser
    if not queue:
        print_info("All files already exist. Nothing to generate.")
        print_summary()
        return

    # Normal execution with Playwright
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        try:
            cdp_url = f"http://{CDP_HOST}:{CDP_PORT}"
            print_debug(f"Connecting to browser at {cdp_url}...")
            browser = p.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0]
            
            page = None
            for p_page in context.pages:
                if "gemini.google.com" in p_page.url:
                    page = p_page
                    break
            
            if not page:
                page = context.new_page()
                page.goto("https://gemini.google.com/")
            
            page.bring_to_front()

            restart_count = args._restart_idx
            
            for idx, (prompt, fname) in enumerate(queue):
                    current = idx + 1
                    total = len(queue)
                    progress_prefix = f"[{current:03d}/{total:03d}]"
                    
                    # Print "Processing..." line at start with proper colors
                    if USE_COLOR:
                        c_bracket = f"{Fore.WHITE}"
                        c_numbers = f"{Fore.GREEN}"
                        c_fname = f"{Fore.CYAN}{fname}{Style.RESET_ALL}"
                        c_processing = f"{Fore.YELLOW}Processing...{Style.RESET_ALL}"
                        print(f"{c_bracket}[{Style.RESET_ALL}{c_numbers}{current:03d}/{total:03d}{Style.RESET_ALL}{c_bracket}]{Style.RESET_ALL} {c_fname} {c_bracket}|{Style.RESET_ALL} {c_processing}", end="", flush=True)
                    else:
                        print(f"[{current:03d}/{total:03d}] {fname} | Processing...", end="", flush=True)
                    
                    if DEBUG_MODE:
                        print()  # New line before debug output
                        print_debug(f"--- Processing {progress_prefix}: {fname} ---")
                    
                    success = process_single_prompt(
                            page,
                            prompt,
                            fname,
                            args.out,
                            args.add_prompt,
                            args.insp_prompt,
                            args.fmt_arg,
                            args.res_arg,
                            args.resx_arg,
                            args.resy_arg,
                            args.type_arg,
                            args.think_arg,
                            args.mingentime,
                            args.dltime,
                            args.dlret,
                            progress_prefix
                        )
                    
                    if not success:
                        clear_line = "\033[K"
                        if USE_COLOR:
                            c_bracket = f"{Fore.WHITE}"
                            c_numbers = f"{Fore.GREEN}"
                            c_fname = f"{Fore.CYAN}{fname}{Style.RESET_ALL}"
                            # Only mention retries if they were actually possible/attempted
                            retry_info = f" (including {args.dlret} download retry attempt(s))" if args.dlret > 0 else ""
                            c_error = f"{Fore.RED}ERROR: Failed{retry_info}{Style.RESET_ALL}"
                            print(f"\r{clear_line}{c_bracket}[{Style.RESET_ALL}{c_numbers}{current:03d}/{total:03d}{Style.RESET_ALL}{c_bracket}]{Style.RESET_ALL} {c_fname} {c_bracket}|{Style.RESET_ALL} {c_error}")
                        else:
                            retry_info = f" (including {args.dlret} download retry attempt(s))" if args.dlret > 0 else ""
                            print(f"\r{clear_line}[{current:03d}/{total:03d}] {fname} | ERROR: Failed{retry_info}")
                        
                        # Handle restart logic - restart entire script
                        if DL_RESTART_MODE and restart_count < DL_RESTART_COUNT:
                            print_warning(f"Download failed after {args.dlret} attempts. Restarting entire script ({restart_count + 1}/{DL_RESTART_COUNT}) with --skip...")
                            
                            # Build command with original args + --skip + incremented restart idx
                            # Use absolute path for the script to ensure it's found after restart
                            script_path = os.path.abspath(sys.argv[0])
                            restart_cmd = [sys.executable, script_path]
                            
                            # Skip internal flags and flags we are re-adding
                            skip_flags = ['--skip', '--overwrite', '--_restart_idx']
                            
                            i = 1
                            while i < len(sys.argv):
                                arg = sys.argv[i]
                                if arg == '--_restart_idx':
                                    i += 2
                                    continue
                                    
                                if arg in skip_flags:
                                    i += 1
                                    continue
                                    
                                restart_cmd.append(arg)
                                i += 1
                                
                            if '--skip' not in restart_cmd:
                                restart_cmd.append('--skip')
                            
                            # Add incremented restart index
                            restart_cmd.append('--_restart_idx')
                            restart_cmd.append(str(restart_count + 1))
                            
                            print_info(f"Restarting: {' '.join(restart_cmd)}")
                            # Wait longer before restart to avoid hitting rate limits immediately
                            time.sleep(10)
                            
                            if os.name == 'nt':
                                # On Windows, subprocess.Popen is more reliable than os.execv for paths with spaces
                                subprocess.Popen(restart_cmd)
                                sys.exit(0)
                            else:
                                # On Unix, os.execv is standard
                                os.execv(sys.executable, restart_cmd)
                        elif DL_RESTART_MODE and restart_count >= DL_RESTART_COUNT:
                            print_error(f"Restart limit ({DL_RESTART_COUNT}) reached. Exiting.")
                    
                    if idx < len(queue) - 1:
                        delay_ms = 0
                        if args.promptrnd:
                            try:
                                min_d, max_d = map(int, args.promptrnd.split(","))
                                delay_ms = random.randint(min_d, max_d)
                            except:
                                delay_ms = 1000
                        else:
                            delay_ms = args.promptint
                        
                        print_debug(f"Waiting {delay_ms}ms...")
                        time.sleep(delay_ms / 1000.0)
            
            # Print summary at the end
            print_summary()

        except Exception as e:
            print_error(f"An error occurred: {e}")
            print_summary()

if __name__ == "__main__":
    # Normalize single-dash long arguments to double-dash (e.g. -type -> --type)
    # This allows users to use either -type or --type interchangeably
    normalized_argv = []
    for arg in sys.argv[1:]:
        if arg.startswith('-') and not arg.startswith('--') and len(arg) > 2:
            normalized_argv.append('-' + arg)
        else:
            normalized_argv.append(arg)
    sys.argv[1:] = normalized_argv

    parser = argparse.ArgumentParser(description="Gemini Imagen Generator Automator", add_help=False)
    
    io_group = parser.add_argument_group("Input/Output")
    io_group.add_argument("--prompt", help="Single text prompt for image generation")
    io_group.add_argument("--addprompt", dest="add_prompt", help="Text to append to every prompt")
    io_group.add_argument("--insprompt", dest="insp_prompt", help="Text to prepend to every prompt")
    io_group.add_argument("--in", dest="input_file", help="JSON file with prompts")
    io_group.add_argument("--out", default=".", help="Output directory")
    io_group.add_argument("--outauto", action="store_true", help="Automatically create output directory based on input JSON filename")
    
    proc_group = parser.add_argument_group("Processing")
    proc_group.add_argument("--promptint", type=int, default=1000, help="Fixed delay ms")
    proc_group.add_argument("--promptrnd", help="Random delay range ms")
    proc_group.add_argument("--type", dest="type_arg", type=str.lower, choices=['fast', 'think', 'pro', 'flash', 'flash-lite'], help="Model type: fast, think, pro, flash, flash-lite")
    proc_group.add_argument("--thinking", dest="think_arg", type=str.lower, choices=['basic', 'extended'], help="Thinking mode: basic or extended (Gemini thinking toggle)")
    proc_group.add_argument("--simul", action="store_true", help="Simulate execution and show generated prompts without connecting to browser")
    proc_group.add_argument("--fmt", dest="fmt_arg", help="Aspect ratio: 43 (4:3), 169 (16:9), 11 (1:1)")
    proc_group.add_argument("--res", dest="res_arg", help="Image resolution in pixels: width,height (e.g. 1024,768)")
    proc_group.add_argument("--resx", dest="resx_arg", help="Image width in pixels, height calculated from --fmt (default 16:9)")
    proc_group.add_argument("--resy", dest="resy_arg", help="Image height in pixels, width calculated from --fmt (default 16:9)")
    proc_group.add_argument("--overwrite", action="store_true", help="Overwrite existing files without prompting")
    proc_group.add_argument("--skip", action="store_true", help="Skip existing files automatically")
    proc_group.add_argument("--noex", action="store_true", help="Do not append .png extension to output filenames")
    proc_group.add_argument("--retry", type=int, default=0, help="Number of retry attempts on failure (default: 0)")
    proc_group.add_argument("--mingentime", type=int, default=30, help="Minimum wait time in seconds for image generation before checking download button (default: 30)")
    proc_group.add_argument("--dltime", type=int, default=45, help="Minimum time in seconds to wait for image download (default: 45)")
    proc_group.add_argument("--dlret", type=int, default=3, help="Number of download retry attempts on failure (default: 3)")
    proc_group.add_argument("--dlrestart", "--delrestart", type=int, default=0, help="Restart entire generation process on failure (default: 0, requires --skip)")
    proc_group.add_argument("--limitwait", type=int, default=300, help="Wait time in seconds when Gemini rate limit is reached (default: 300)")
    proc_group.add_argument("--_restart_idx", type=int, default=0, help=argparse.SUPPRESS)
    proc_group.add_argument("--gen", choices=['chat', 'tmp', 'native'], default='chat', help="Generation mode: 'chat' (standard), 'tmp' (temporary), or 'native' (use current context). Default: chat")
    
    conn_group = parser.add_argument_group("Connection")
    conn_group.add_argument("--host", default="localhost", help="Browser host for CDP connection (default: localhost)")
    conn_group.add_argument("--port", type=int, default=9222, help="Browser port for CDP connection (default: 9222)")
    
    display_group = parser.add_argument_group("Display")
    display_group.add_argument("--color", action="store_true", help="Enable colorized output")
    display_group.add_argument("--debug", action="store_true", help="Enable debug mode (verbose output)")
    display_group.add_argument("--stat", action="store_true", help="Show statistics (time, resolution, size) for each image")
    display_group.add_argument("--savescr", dest="save_screen_file", nargs='?', const='', default=None, help="Save all console output to specified file")
    display_group.add_argument("--log", dest="log_file", nargs="?", const="", default=None, help="Save execution log to file (default: input JSON filename with .log extension)")
    
    parser.add_argument("-h", action="store_true", help="Show simple help")
    parser.add_argument("--help", action="store_true", help="Show detailed help")

    args, unknown = parser.parse_known_args()
    
    # Initialize colorama for error messages
    USE_COLOR = args.color
    
    # Validation: Check for unknown arguments
    if unknown:
        print_error(f"Error: Unknown arguments encountered: {', '.join(unknown)}")
        print_info("Check spelling or use --help to see available options.")
        exit(1)
    
    # Validation: Check for mutually exclusive resolution options
    res_options = [args.res_arg, args.resx_arg, args.resy_arg]
    res_count = sum(1 for opt in res_options if opt is not None)
    if res_count > 1:
        print_error("Error: --res, --resx, and --resy are mutually exclusive. Use only one.")
        exit(1)
    
    # Validation: Check --fmt values
    if args.fmt_arg and args.fmt_arg not in ['43', '169', '11']:
        print_error(f"Error: Invalid --fmt value '{args.fmt_arg}'. Must be one of: 43, 169, 11")
        exit(1)
    
    # Validation: Check --res format
    if args.res_arg:
        try:
            parts = args.res_arg.split(',')
            if len(parts) != 2:
                raise ValueError("Must have exactly 2 values")
            width, height = int(parts[0]), int(parts[1])
            if width <= 0 or height <= 0:
                raise ValueError("Values must be positive")
        except ValueError as e:
            print_error(f"Error: Invalid --res value '{args.res_arg}'. Expected format: WIDTH,HEIGHT (e.g. 1024,768)")
            exit(1)
    
    # Validation: Check --resx value
    if args.resx_arg:
        try:
            resx = int(args.resx_arg)
            if resx <= 0:
                raise ValueError("Must be positive")
        except ValueError:
            print_error(f"Error: Invalid --resx value '{args.resx_arg}'. Must be a positive integer")
            exit(1)
    
    # Validation: Check --resy value
    if args.resy_arg:
        try:
            resy = int(args.resy_arg)
            if resy <= 0:
                raise ValueError("Must be positive")
        except ValueError:
            print_error(f"Error: Invalid --resy value '{args.resy_arg}'. Must be a positive integer")
            exit(1)
    
    # Validation: Check --retry value
    if args.retry < 0:
        print_error(f"Error: Invalid --retry value '{args.retry}'. Must be a non-negative integer")
        exit(1)
    
    # Validation: Check --mingentime value
    if args.mingentime <= 0:
        print_error(f"Error: Invalid --mingentime value '{args.mingentime}'. Must be a positive integer")
        exit(1)

    # Validation: Check --dltime value
    if args.dltime <= 0:
        print_error(f"Error: Invalid --dltime value '{args.dltime}'. Must be a positive integer")
        exit(1)
    
    # Validation: Check --dlret value
    if args.dlret < 0:
        print_error(f"Error: Invalid --dlret value '{args.dlret}'. Must be a non-negative integer")
        exit(1)
    
    # Validation: Check --promptint value
    if args.promptint < 0:
        print_error(f"Error: Invalid --promptint value '{args.promptint}'. Must be a non-negative integer")
        exit(1)
    
    # Validation: Check --promptrnd format
    if args.promptrnd:
        try:
            parts = args.promptrnd.split(',')
            if len(parts) != 2:
                raise ValueError("Must have exactly 2 values")
            min_delay, max_delay = int(parts[0]), int(parts[1])
            if min_delay < 0 or max_delay < 0:
                raise ValueError("Values must be non-negative")
            if min_delay > max_delay:
                raise ValueError("MIN must be <= MAX")
        except ValueError as e:
            print_error(f"Error: Invalid --promptrnd value '{args.promptrnd}'. Expected format: MIN,MAX (e.g. 1000,5000)")
            exit(1)
    
    # Validation: Check that either --prompt or --in is provided (but not in help mode), and not both
    if not args.h and not args.help:
        if args.prompt and args.input_file:
             print_error("Error: --prompt and --in cannot be used together. Please specify only one prompt source.")
             exit(1)

        if args.res_arg and args.fmt_arg:
             print_error("Error: --res (custom resolution) and --fmt (aspect ratio) cannot be used together. --res implies specific dimensions.")
             exit(1)

        if not args.prompt and not args.input_file:
            print_error("Error: Either --prompt or --in must be specified")
            print_info("Use --help for usage information")
            exit(1)
        
        # Validation: Check if input file exists
        if args.input_file and not os.path.exists(args.input_file):
            print_error(f"Error: Input file '{args.input_file}' does not exist")
            exit(1)
            
        if getattr(args, 'outauto', False):
            if not args.input_file:
                print_error("Error: --outauto can only be used with --in (JSON file).")
                exit(1)
            args.out = os.path.splitext(os.path.basename(args.input_file))[0]
            print_info(f"Auto-configured output directory: {args.out}")
        
        # Validation: Check if output directory exists, create if not
        if not os.path.exists(args.out):
            try:
                os.makedirs(args.out)
                print_info(f"Created output directory: {args.out}")
            except Exception as e:
                print_error(f"Error: Cannot create output directory '{args.out}': {e}")
                exit(1)
    
    # Set SAVE_SCREEN_FILE: use provided filename, or derive from --in filename
    if args.save_screen_file is not None:
        if args.save_screen_file == "":
            # --savescr used without filename: derive from --in
            if args.input_file:
                base = os.path.splitext(os.path.basename(args.input_file))[0]
                args.save_screen_file = base + ".scr"
            else:
                args.save_screen_file = "nanogen.scr"
        
        # Redirect stdout and stderr
        sys.stdout = Logger(args.save_screen_file)
        sys.stderr = sys.stdout  # Redirect stderr to same logger
        print_info(f"Saving screen output to: {args.save_screen_file}")

    # Set global flags
    DEBUG_MODE = args.debug
    OVERWRITE_MODE = args.overwrite
    SKIP_MODE = args.skip
    STAT_MODE = args.stat
    NOEX_MODE = args.noex
    RETRY_COUNT = args.retry
    MIN_GEN_TIME = args.mingentime
    CHAT_MODE = args.gen
    SIMUL_MODE = args.simul
    CDP_HOST = args.host
    CDP_PORT = args.port
    DL_RESTART_MODE = args.dlrestart > 0
    DL_RESTART_COUNT = args.dlrestart
    LIMIT_WAIT_TIME = args.limitwait
    
    # Validation: --dlrestart requires --skip
    if DL_RESTART_MODE and not SKIP_MODE:
        print_error("Error: --dlrestart requires --skip to be enabled")
        exit(1)
    
    # Set LOG_FILE: use provided filename, or derive from --in filename
    if args.log_file is not None:
        if args.log_file == "":
            # --log used without filename: derive from --in
            if args.input_file:
                base = os.path.splitext(os.path.basename(args.input_file))[0]
                LOG_FILE = base + ".log"
            else:
                LOG_FILE = "nanogen.log"
        else:
            LOG_FILE = args.log_file
    
    if args.h:
        # Show program info first
        print(f"Gemini Imagen Generator Automator")
        print(f"Author: {__CODEAUTH__}")
        print(f"Version: {__CODEVER__}")
        print(f"Date: {__CODEDATE__}")
        print(f"Repository: {__CODEGIT__}")
        print()
        print("Usage: python nanogen.py [--prompt TEXT] [--in FILE] [--addprompt TEXT] [--insprompt TEXT]")
        print("                         [--out DIR] [--outauto]")
        print("                         [--fmt 43|169|11] [--res WIDTH,HEIGHT | --resx WIDTH | --resy HEIGHT]")
        print("                         [--type fast|think|pro|flash|flash-lite] [--thinking basic|extended] [--gen chat|tmp|native]")
        print("                         [--promptint MS] [--promptrnd MIN,MAX]")
        print("                         [--overwrite] [--skip] [--noex] [--retry N] [--dlret N] [--dlrestart N]")
        print("                         [--mingentime N] [--dltime N] [--limitwait N]")
        print("                         [--host HOST] [--port PORT]")
        print("                         [--color] [--debug] [--stat] [--simul] [--savescr [FILENAME]]")
        print("                         [--log [FILENAME]]")
        exit(0)
        
    if args.help:
        # Show program info first
        print(f"\nGemini Imagen Generator Automator")
        print(f"Author: {__CODEAUTH__}")
        print(f"Version: {__CODEVER__}")
        print(f"Date: {__CODEDATE__}")
        print(f"Repository: {__CODEGIT__}")
        print("\n" + "=" * 60 + "\n")
        
        print("Input/Output Options:")
        print("  --prompt TEXT       Single text prompt to generate.")
        print("  --in FILENAME       JSON file containing prompts (Key=Filename, Value=Prompt).")
        print("  --out DIRECTORY     Directory to save images (default: current dir).")
        print("  --outauto           Automatically create output directory based on input JSON filename.")
        
        print("\nPrompt Modifiers:")
        print("  --addprompt TEXT    Text to append to every prompt (e.g. 'Hyper realistic').")
        print("  --insprompt TEXT    Text to prepend to every prompt (e.g. 'Generate an image of').")
        print("                      Note: --addprompt appends to the END, --insprompt prepends to the START.")
        
        print("\nGeneration Options:")
        print("  --fmt FMT           Set aspect ratio: '43' (4:3), '169' (16:9), or '11' (1:1).")
        print("  --res WIDTH,HEIGHT  Image resolution in pixels (e.g. '1024,768').")
        print("  --resx WIDTH        Image width in px, height auto-calculated from --fmt (default 16:9).")
        print("  --resy HEIGHT       Image height in px, width auto-calculated from --fmt (default 16:9).")
        print("                      Note: --res, --resx, and --resy are mutually exclusive.")
        print("  --type TYPE         Model type: 'fast' (Fast/Szybki), 'think' (Thinking/Myślący), 'pro' (Pro), 'flash' (Flash), 'flash-lite' (Flash-Lite).")
        print("  --thinking MODE     Thinking mode: 'basic' or 'extended' (Gemini thinking toggle).")
        print("  --gen MODE          Generation mode: 'chat' (standard, default), 'tmp' (temporary), or")
        print("                      'native' (operate in current context without changing it).")
        
        print("\nExecution & Timing Options:")
        print("  --promptint MS      Fixed delay between prompts in milliseconds (default: 1000).")
        print("  --promptrnd MIN,MAX Random delay range in milliseconds (e.g. '2000,5000').")
        print("  --simul             Simulate execution and show generated prompts without generating images.")
        print("  --overwrite         Overwrite existing files without prompting.")
        print("  --skip              Skip existing files automatically.")
        print("  --noex              Do not append .png extension to output filenames.")
        print("  --retry N           Number of retry attempts on failure (default: 0).")
        print("  --mingentime N      Minimum wait time in seconds for image generation before checking download button (default: 30).")
        print("  --dltime N          Minimum time in seconds to wait for image download (default: 45).")
        print("  --dlret N           Number of download retry attempts on failure (default: 3).")
        print("  --dlrestart N       Restart entire generation process on failure (default: 0, requires --skip).")
        print("  --limitwait N       Wait time in seconds when Gemini rate limit is reached (default: 300).")
        
        print("\nConnection Options:")
        print("  --host HOST         Browser host for CDP connection (default: localhost).")
        print("  --port PORT         Browser port for CDP connection (default: 9222).")
        
        print("\nDisplay Options:")
        print("  --color             Enable colorized output (errors=red, warnings=yellow, success=green).")
        print("  --debug             Enable debug mode (verbose output).")
        print("  --stat              Show statistics (generation time, resolution, size in KiB) for each image.")
        print("  --savescr [FILENAME] Save exactly all console output to file.")
        print("                      If FILENAME is omitted, uses input JSON filename with .scr extension.")
        print("  --log [FILENAME]    Save execution log (statistics and image list) to file.")
        print("                      If FILENAME is omitted, uses input JSON filename with .log extension.")
        print("\nExamples:")
        print('  python nanogen.py --prompt "Mountain" --insprompt "Epic battle" --addprompt "Cinematic" --color')
        print('  python nanogen.py --in prompts.json --type think --fmt 169 --res 1920,1080 --stat --debug')
        print('  python nanogen.py --prompt "Portrait" --type fast --fmt 43 --resx 1024 --color')
        print('  python nanogen.py --prompt "Banner" --gen tmp --resy 720 --debug')
        exit(0)
    
    if not args.prompt and not args.input_file:
        print(f"Gemini Imagen Generator Automator v{__CODEVER__}")
        print("Usage: python nanogen.py [--prompt TEXT] [--in FILE] ... (use --help for more)")
    else:
        run_gemini_session(args)
