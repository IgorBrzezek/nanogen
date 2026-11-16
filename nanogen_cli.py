#!/usr/bin/env python3
"""
Nanogen CLI — universal Gemini Imagen image generator
=====================================================
Automates image generation on the Google Gemini web interface (Imagen).

Two browser strategies are supported:

  1. **Managed** (default) — the script launches its own Chromium through
     Playwright, using a persistent profile so the Google session survives
     between runs. Works fully headless, so it is usable over SSH on a
     server with no display server.
  2. **Attach** (``--attach``) — the script connects over the Chrome
     DevTools Protocol to an already running, already logged-in browser
     (port 9222 by default). This is what ``start_nanogen_debug.bat`` and
     ``start_nanogen_debug.sh`` prepare.

Both strategies support every feature: batch prompts from JSON, model and
thinking-level selection, aspect-ratio/resolution modifiers, temporary
chat isolation, rate-limit handling, download retries and full restarts.

The script is platform independent — Windows, Linux and macOS.

Requirements:
    pip install playwright colorama
    python nanogen_cli.py --install-browser

Author:  Igor Brzezek
Repo:    https://github.com/igorbrzezek/nanogen
"""

import argparse
import atexit
import base64
import errno
import hashlib
import json
import os
import random
import re
import signal
import subprocess
import sys
import time
from datetime import datetime

os.environ.setdefault("NODE_OPTIONS", "--no-warnings")

# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------
__CODEAUTH__ = "Igor Brzezek"
__CODEVER__ = "0.23"
__CODEDATE__ = "16.11.2026"
__CODEGIT__ = "https://github.com/igorbrzezek/nanogen"
__PROGNAME__ = "nanogen_cli.py"

# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------
IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

# ---------------------------------------------------------------------------
# Console encoding
#
# Windows consoles still default to a legacy code page (cp1250/cp1252) when
# stdout is redirected to a file or a pipe, which makes printing the Polish
# selectors and messages raise UnicodeEncodeError. Force UTF-8 with lossy
# replacement before colorama wraps the streams.
# ---------------------------------------------------------------------------
def _force_utf8_stdio():
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_force_utf8_stdio()

try:
    from colorama import Fore, Back, Style, init as _colorama_init

    _colorama_init(autoreset=True)
    HAS_COLORAMA = True
except ImportError:  # pragma: no cover - colorama is optional
    HAS_COLORAMA = False

    class Fore:
        RED = YELLOW = GREEN = CYAN = MAGENTA = LIGHTBLUE_EX = WHITE = BLUE = ""

    class Style:
        RESET_ALL = ""

    class Back:
        pass


# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
USE_COLOR = False
DEBUG_MODE = False
OVERWRITE_MODE = False
SKIP_MODE = False
STAT_MODE = False
SIMUL_MODE = False
HEADLESS_MODE = True
CHAT_MODE = "tmp"
NOEX_MODE = False
RETRY_COUNT = 0
MIN_GEN_TIME = 30
ATTACH_MODE = False
LOCK_WAIT_TIME = 0
INTERACTIVE = True

ERROR_LOG = []
TOTAL_IMAGES = 0
SUCCESS_COUNT = 0
FAIL_COUNT = 0
START_TIME = None
LOG_FILE = None
LOG_LINES = []
RATIO_1TO1_COUNT = 0
RETRY_DOWNLOAD_COUNT = 0
DL_RESTART_MODE = False
DL_RESTART_COUNT = 0
LIMIT_WAIT_TIME = 300

LOCK_PATH = None
_INTERRUPTED = [False]
_NONINTERACTIVE_WARNED = [False]


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------
def app_data_dir():
    """Per-user configuration directory, following each platform's convention."""
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
    elif IS_MAC:
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "nanogen")


def default_profile_dir():
    return os.path.join(app_data_dir(), "chrome_profile")


def lock_dir():
    return app_data_dir()


# ---------------------------------------------------------------------------
# Lock file — single instance per browser target
# ---------------------------------------------------------------------------
def _pid_alive(pid):
    """Cross-platform check whether a PID is still running."""
    if pid is None or pid <= 0:
        return False
    if IS_WINDOWS:
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
                stderr=subprocess.DEVNULL, timeout=20,
            )
            text = out.decode("utf-8", "replace")
            if "No tasks are running" in text or "Nie uruchomiono" in text:
                return False
            return str(pid) in text
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def release_lock():
    """Remove the lock file on exit."""
    global LOCK_PATH
    if not LOCK_PATH:
        return
    try:
        if os.path.exists(LOCK_PATH):
            os.remove(LOCK_PATH)
    except Exception:
        pass
    LOCK_PATH = None


def make_lock_path(target):
    """Lock file derived from the browser target, so unrelated targets coexist."""
    digest = hashlib.sha1(str(target).encode("utf-8", "replace")).hexdigest()[:10]
    directory = lock_dir()
    try:
        os.makedirs(directory, exist_ok=True)
    except Exception:
        directory = os.getcwd()
    return os.path.join(directory, "nanogen-%s.lock" % digest)


def acquire_lock(target):
    """Ensure only one nanogen instance drives a given browser at a time."""
    global LOCK_PATH
    LOCK_PATH = make_lock_path(target)
    first_wait = True
    waited = 0.0
    while True:
        try:
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            atexit.register(release_lock)
            if not first_wait:
                print()
            return
        except FileExistsError:
            stale = False
            owner = None
            try:
                with open(LOCK_PATH, "r", encoding="utf-8", errors="replace") as handle:
                    owner = int(handle.read().strip())
            except Exception:
                stale = True
            if owner is not None and not _pid_alive(owner):
                stale = True
            if stale:
                try:
                    os.remove(LOCK_PATH)
                    continue
                except Exception:
                    pass
            if first_wait:
                print_warning("\n*** Another nanogen instance is already using this browser. ***")
                print_info("To avoid profile/CDP conflicts this process will wait for it to finish.")
                print_info("Use --no-lock to bypass, or --lockwait N to give up after N seconds.")
                first_wait = False
            if LOCK_WAIT_TIME and waited >= LOCK_WAIT_TIME:
                print_warning(f"Lock wait timed out after {int(waited)}s, taking over the lock.")
                try:
                    os.remove(LOCK_PATH)
                except Exception:
                    pass
                continue
            dots = "." * (int(time.time()) % 4)
            print_info("\rWaiting for nanogen lock to be released%s" % dots.ljust(3), end="",
                      flush=True)
            waited += 2
            time.sleep(2)


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
class Logger:
    """Tees stdout/stderr to a file, stripping ANSI sequences."""

    def __init__(self, filename):
        self.terminal = sys.stdout
        self.log = open(filename, "a", encoding="utf-8")
        self.ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

    def write(self, message):
        self.terminal.write(message)
        self.log.write(self.ansi_escape.sub("", message))
        self.log.flush()

    def flush(self):
        self.terminal.flush()
        self.log.flush()

    def isatty(self):
        return False


def print_color(msg, color_code="", **kwargs):
    if USE_COLOR and color_code:
        print("%s%s%s" % (color_code, msg, Style.RESET_ALL), **kwargs)
    else:
        print(msg, **kwargs)


def print_error(msg):
    print_color(msg, Fore.RED)


def print_warning(msg):
    print_color(msg, Fore.YELLOW)


def print_success(msg):
    print_color(msg, Fore.GREEN)


def print_info(msg, **kwargs):
    print_color(msg, "", **kwargs)


def print_debug(msg):
    if DEBUG_MODE:
        print_color(msg, Fore.CYAN)


def format_elapsed(seconds):
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return "%dh %dm %ds" % (hours, minutes, secs)
    if minutes > 0:
        return "%dm %ds" % (minutes, secs)
    return "%ds" % secs


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------
SUPPORTED_EXTS = [".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"]


def get_final_filename(filename_base):
    _, ext = os.path.splitext(filename_base)
    if ext.lower() in SUPPORTED_EXTS:
        return filename_base
    if NOEX_MODE:
        return filename_base
    return "%s.png" % filename_base


def ask_overwrite(filename):
    """Ask before replacing a file, tolerating a closed/absent stdin."""
    if OVERWRITE_MODE:
        return True
    if SKIP_MODE:
        return False
    if not INTERACTIVE:
        if not _NONINTERACTIVE_WARNED[0]:
            print_warning("No interactive terminal: existing files are left untouched "
                          "(use --overwrite to replace them).")
            _NONINTERACTIVE_WARNED[0] = True
        return False
    try:
        answer = input("File '%s' already exists. Overwrite? (y/n): " % filename)
    except (EOFError, KeyboardInterrupt):
        print()
        print_info("Skipped: %s" % filename)
        return False
    return answer.strip().lower() in ("y", "yes", "t", "tak")


# ---------------------------------------------------------------------------
# Prompt composition
# ---------------------------------------------------------------------------
ASPECT_RATIOS = {"43": (4, 3), "169": (16, 9), "11": (1, 1)}


def join_prompt_part(prompt, extra):
    """Append ``extra`` to ``prompt`` with correct punctuation."""
    if prompt.rstrip().endswith("."):
        return "%s %s" % (prompt, extra)
    return "%s. %s" % (prompt, extra)


def build_prompt(prompt, add_prompt=None, insp_prompt=None, fmt_arg=None,
                 res_arg=None, resx_arg=None, resy_arg=None):
    """Compose the final prompt plus a report of every applied modifier."""
    report = {
        "insprompt": insp_prompt or None,
        "addprompt": add_prompt or None,
        "aspect_ratio": None,
        "resolution": None,
        "width": None,
        "height": None,
        "resolution_error": None,
    }

    working = prompt
    if insp_prompt:
        clean_insp = insp_prompt.strip()
        if clean_insp.lower().endswith(('of', 'for', 'with', 'about', ':', ',')):
            working = "%s %s" % (clean_insp, working)
        elif clean_insp.endswith("."):
            working = "%s %s" % (clean_insp, working)
        else:
            working = "%s. %s" % (clean_insp, working)
    else:
        # Check if the prompt already contains an image directive
        image_keywords = [
            "generate an image", "create an image", "draw an image", "make an image",
            "generate image", "create image", "paint an image", "render an image",
            "wygeneruj obraz", "utwórz obraz", "stwórz obraz", "namaluj obraz", "narysuj obraz",
            "generate a picture", "create a picture", "obraz:"
        ]
        has_image_directive = any(kw in working.lower() for kw in image_keywords)
        if not has_image_directive:
            working = "Generate an image of %s" % working
    if add_prompt:
        working = join_prompt_part(working, add_prompt)

    final = working

    if fmt_arg:
        ratio = ASPECT_RATIOS.get(fmt_arg)
        if ratio:
            label = "%d:%d" % ratio
            final = join_prompt_part(final, "Aspect ratio %s." % label)
            report["aspect_ratio"] = label
        else:
            report["aspect_ratio_error"] = fmt_arg

    width = height = None
    if res_arg:
        try:
            width, height = (int(part) for part in res_arg.split(","))
            report["resolution"] = "%dx%d" % (width, height)
        except ValueError:
            report["resolution_error"] = "Invalid --res value: %s" % res_arg
    elif resx_arg or resy_arg:
        ratio = ASPECT_RATIOS.get(fmt_arg, (16, 9))
        try:
            if resx_arg:
                width = int(resx_arg)
                height = int(round(width * ratio[1] / ratio[0]))
            else:
                height = int(resy_arg)
                width = int(round(height * ratio[0] / ratio[1]))
            report["resolution"] = "%dx%d" % (width, height)
        except (ValueError, TypeError, ZeroDivisionError):
            report["resolution_error"] = "Invalid --resx/--resy value"

    if width and height:
        final = "%s --size %dx%d" % (final, width, height)
        report["width"] = width
        report["height"] = height

    return final, report


# ---------------------------------------------------------------------------
# DOM selector catalogues (Polish + English, kept in sync with Gemini's UI)
# ---------------------------------------------------------------------------
MENU_ITEM_SELECTOR = (
    '[role="menu"] [role="menuitem"],'
    '[role="menu"] [role="menuitemradio"],'
    '[role="menu"] [role="radio"],'
    '[role="menu"] [role="option"],'
    '[role="listbox"] [role="option"],'
    '[role="listbox"] [role="menuitem"]'
)

MODEL_MENU_BUTTONS = [
    '[data-test-id="bard-mode-menu-button"]',
    'button[aria-label*="selektor trybu"]',
    'button[aria-label*="model selector"]',
    'button[aria-label*="Open mode selector"]',
    'button[aria-label*="Mode"]',
]

MODEL_BUTTON_NAMES = [
    "Hydrogen", "4 Hydrogen", "Flash-Lite", "3.5 Flash-Lite", "Argon", "4 Argon",
    "Carbon", "4 Carbon", "Szybki", "Szybkie", "Myślący", "Myślenie", "Flash",
    "Thinking", "Pro", "3.1 Pro", "Zaawansowany", "Gemini",
]

# Items that configure the *thinking level* rather than the model. They are
# skipped while resolving --type so the two options stay independent.
THINKING_LEVEL_MARKERS = (
    "myślenie rozszerzone", "rozszerzone myślenie", "extended thinking",
    "myślenie standardowe", "standardowe myślenie", "basic thinking",
    "standard thinking", "poziom myślenia", "thinking level",
    "głębokie myślenie", "deep thinking", "szybkie myślenie", "fast thinking",
    "myślenie",
)

THINKING_EXTENDED_MARKERS = (
    "rozszerzon", "extended", "głębok", "deep",
)

THINKING_BASIC_MARKERS = (
    "standardowy", "standard", "podstawow", "basic", "szybki", "fast", "off",
)

INPUT_SELECTORS = [
    "rich-textarea .ql-editor",
    "div[contenteditable=\"true\"]",
    "textarea[aria-label*=\"prompt\"]",
    "textarea[placeholder*=\"prompt\"]",
    ".ql-editor[contenteditable=\"true\"]",
]

SEND_BUTTON_SELECTORS = [
    'button[aria-label*="Wyślij"]',
    'button[aria-label*="wyślij"]',
    'button[aria-label*="Send"]',
    'button[aria-label*="send"]',
    'button[data-test-id*="send"]',
    'button:has(svg[data-icon="send"])',
    'button:has([data-icon="send"])',
]

# Ordered by reliability: the data-test-id variant addresses the inner
# gem-icon-button, which is the element that actually has a hit area. The bare
# custom element name resolves to a zero-sized host and is never visible.
IMAGE_SELECTORS = [
    "generated-image img",
    "single-image img",
    "div.image-container img",
    "img[src*=\"googleusercontent.com\"]:not([src*=\"/a/\"])",
    "img[src*=\"imagen\"]",
    "img[alt*=\"wygenerowane\"]",
    "img[alt*=\"Generated\"]",
    "img[src^=\"blob:\"]",
]

DOWNLOAD_SELECTORS = [
    '[data-test-id="download-generated-image-button"]',
    'download-generated-image-button gem-icon-button',
    'download-generated-image-button button',
    'button[aria-label*="pełnym rozmiarze"]',
    'button[aria-label*="full size"]',
    'button[aria-label="Pobierz obraz"]',
    'button[aria-label="Pobierz"]',
    'button[aria-label="Download image"]',
    'button[aria-label="Download"]',
    'button[aria-label*="Pobierz"]',
    'button[aria-label*="pobierz"]',
    'button[aria-label*="Download"]',
    'button[aria-label*="download"]',
    '[role="button"][aria-label*="download" i]',
    '[role="button"][aria-label*="pobierz" i]',
    'button:has-text("Pobierz")',
    'button:has-text("Download")',
    'button[data-tooltip*="Pobierz"]',
    'button[data-tooltip*="Download"]',
    'button:has([class*="download" i])',
    '[data-test-id*="download" i]',
    'a[download]',
    'a[href*="download"]',
    'button[title*="Pobierz"]',
    'button[title*="Download"]',
    'button[title*="pobierz"]',
    'button[title*="download"]',
]

SIDEBAR_MENU_BUTTONS = [
    'button[aria-label="Main menu"]',
    'button[aria-label="Menu główne"]',
    'button[aria-label*="Menu"]',
    'button[aria-label*="menu"]',
    'button[aria-label*="Nawigacja"]',
    'button[aria-label*="Nawig"]',
    'button[aria-label*="Navigation"]',
    'button[aria-label*="Open menu"]',
    'button[aria-label*="Otwórz menu"]',
    'button[aria-label*="Nawigacja boczna"]',
    'button:has(svg)',
    'header button:first-child',
    'nav button:first-child',
    'chat-app-side-nav-menu-button',
    'bard-sidenav button',
    'button[type="button"]:first-of-type',
]

SIDEBAR_OPEN_CHECKS = [
    'text="New chat"',
    'text="Nowy czat"',
    'text="Neuer Chat"',
    '[data-test-id="new-chat-button"]',
    '[role="navigation"]',
    'bard-sidenav',
    'nav[aria-label*="Main"]',
    'nav[aria-label*="Główne"]',
]

TEMP_CHAT_BUTTONS = [
    'button[aria-label="Czat tymczasowy"]',
    'button[aria-label="Temporary chat"]',
    'text="Czat tymczasowy"',
    'text="Temporary chat"',
    'button:has-text("Czat tymczasowy")',
    'button:has-text("Temporary chat")',
    'div[role="button"]:has-text("Czat tymczasowy")',
    'div[role="button"]:has-text("Temporary chat")',
    '[aria-label*="tymczasow"]',
    '[aria-label*="emporary"]',
    'span:has-text("Czat tymczasowy")',
    'span:text-is("Czat tymczasowy")',
    'span:has-text("Temporary chat")',
    'span:text-is("Temporary chat")',
    '*[role="button"]:has-text("Czat tymczasowy")',
    '*[role="button"]:has-text("Temporary chat")',
    '[data-test-id*="temporary" i]',
    '[data-test-id*="tymczasow" i]',
]

NEW_CHAT_BUTTONS = [
    '[data-test-id="new-chat-button"]',
    'text="Nowy czat"',
    'text="New chat"',
    'span:has-text("Nowy czat")',
    'span:has-text("New chat")',
    'button:has-text("Nowy czat")',
    'button:has-text("New chat")',
    '[aria-label="Nowy czat"]',
    '[aria-label="New chat"]',
]

# Gemini renamed this button to "Przesyłanie i narzędzia" / "Upload and tools";
# the older "Narzędzia" text match no longer matches anything.
TOOLS_BUTTONS = [
    'button[aria-label*="Przesyłanie"]',
    'button[aria-label*="Przesylanie"]',
    'button[aria-label*="narzędzia"]',
    'button[aria-label*="Narzędzia"]',
    'button[aria-label*="narzedzia"]',
    'button[aria-label*="Upload"]',
    'button[aria-label*="upload"]',
    'button[aria-label*="Narzędzia"]',
    'button[aria-label*="Tools"]',
    'button[aria-label*="tools"]',
    'button:has-text("Narzędzia")',
    'button:has-text("Tools")',
    'div[role="button"]:has-text("Narzędzia")',
    'div[role="button"]:has-text("Tools")',
    '[data-test-id="more-tools-button"]',
]

CREATE_IMAGE_SELECTORS = [
    '[role="menuitem"]:has-text("Utwórz obraz")',
    '[role="menuitem"]:has-text("Utwórz obrazy")',
    '[role="menuitem"]:has-text("Generuj obrazy")',
    '[role="menuitem"]:has-text("Create image")',
    '[role="menuitem"]:has-text("Create images")',
    '[role="menuitem"]:has-text("Generate image")',
    'button:has-text("Utwórz obraz")',
    'button:has-text("Twórz obrazy")',
    'button:has-text("Create image")',
    'button:has-text("Create images")',
    'text="Utwórz obraz"',
    'text="Twórz obrazy"',
    'text="Create image"',
    'text="Create images"',
    'text="Generuj obrazy"',
    'span:has-text("Utwórz obraz")',
    'span:has-text("Create images")',
    'gem-menu-item:has-text("Utwórz obraz")',
    'gem-menu-item:has-text("Create image")',
    '[role="menuitem"]:has-text("obraz")',
    '[role="menuitem"]:has-text("image" i)',
    'button:has-text("obraz")',
    'button:has-text("image" i)',
]

DIRECT_IMAGE_BUTTONS = [
    'button[aria-label*="Utwórz obraz"]',
    'button[aria-label*="Twórz obrazy"]',
    'button[aria-label*="Create image"]',
    'button[aria-label*="Create images"]',
    'button[aria-label*="obraz"]',
    'button[aria-label*="image" i]',
    'button[aria-label*="zdjęcie"]',
    'button[aria-label*="photo" i]',
    'button[aria-label*="generuj"]',
    'button[aria-label*="generate" i]',
    '[data-test-id*="image" i] button',
    '[data-test-id*="image-tool" i]',
]

# Gemini answers in the UI language; every one of these is matched against the
# newest model-response only, so sidebar text cannot trigger a false positive.
RATE_LIMIT_INDICATORS = [
    "przekroczono limit", "limit reached", "too many requests",
    "rate limit", "limit zapytań", "zbyt wiele źądań",
    "wyczerpał się limit dzienny", "you've hit the daily limit",
    "exceeded your", "przekroczyłeś limit", "wykroczyłeś limit",
    "spróbuj ponownie póżniej", "please try again later",
    "wypróbuj póżniej", "wypróbuj ponownie",
]

# The daily image quota is a hard stop — retrying cannot help, it only wastes
# time and makes the user look like an abuser to Google.
QUOTA_INDICATORS = [
    "będę w stanie utworzyć więcej obrazów",
    "gdy tylko twój limit zostanie zresetowany",
    "unable to create more images",
    "once your limit is reset",
    "image generation limit",
    "limit wyczerpany dla generowania obrazów",
    "limit zużycia tworzenia obrazów",
]

REFUSAL_INDICATORS = [
    "nie mogę wygenerować", "nie mogę utworzyć", "can't generate",
    "cannot generate", "can't create", "cannot create",
    "nie potrafię wygenerować", "nie jestem w stanie",
    "nie mogę spełnić", "can't fulfill", "cannot fulfill",
    "zasadami użytkowania", "usage policies", "policy violation",
    "naruszenie zasad", "safety guidelines", "wytyczne bezpieczeństwa",
    "hard time fulfilling your request", "can't help with that", "cannot help with that",
]

# Transient server-side failures. Retrying the same prompt often works, so
# the run loop treats these as retryable instead of burning the full wait.
# Every verdict kind that means "stop waiting for an image right now".
# Transient "error" verdicts are included so a Gemini server hiccup fails
# in seconds and gets retried, instead of waiting out the whole timeout.
TERMINAL_VERDICTS = ("rate_limit", "quota", "error", "refusal")


GENERIC_ERROR_INDICATORS = [
    "something went wrong", "coś poszło nie tak", "wystąpił błąd",
    "błąd podczas", "wykryto błąd", "an error occurred",
    "upstream error", "internal error", "błąd serwera",
    "encountered an error", "error doing what you asked",
    "could not process your request", "unable to process",
    "response was blocked", "i seem to be encountering an error",
    "wykryto nieoczekiwany błąd", "nie udało się przetworzyć",
]


# ---------------------------------------------------------------------------
# Small DOM utilities
# ---------------------------------------------------------------------------
def first_visible(page_or_root, selector, timeout=1000):
    """Return the last visible match of ``selector`` or ``None``."""
    try:
        locator = page_or_root.locator(selector)
        if locator.count() == 0:
            return None
        candidate = locator.last
        if candidate.is_visible(timeout=timeout):
            return candidate
    except Exception:
        pass
    return None


def click_locator(locator, timeout=5000):
    try:
        locator.scroll_into_view_if_needed(timeout=timeout)
    except Exception:
        pass
    try:
        locator.click(timeout=timeout)
        return True
    except Exception:
        try:
            locator.click(force=True, timeout=timeout)
            return True
        except Exception:
            return False


def dismiss_overlays(page):
    """Click through consent banners without destroying application menus.

    Angular Material renders every menu inside ``.cdk-overlay-container``, so
    that container must never be removed — doing so deletes an open menu along
    with any dialog. Only consent banners and leftover backdrops are touched.
    """
    try:
        page.evaluate("""() => {
            const accept = ['accept', 'accept all', 'zaakceptuj', 'zgadzam',
                            'got it', 'ok', 'continue', 'akceptuję', 'zgadzam się'];
            const label = (node) => (node.innerText || '').trim().toLowerCase();

            document.querySelectorAll('[class*="consent"], [class*="cookie"]')
                .forEach((node) => {
                    const buttons = node.querySelectorAll('button');
                    for (const button of buttons) {
                        if (accept.some((word) => label(button).includes(word))) {
                            button.click();
                            return;
                        }
                    }
                    node.remove();
                });

            document.querySelectorAll('.cdk-overlay-backdrop').forEach((node) => node.remove());
        }""")
    except Exception as exc:
        print_debug("Overlay dismissal failed: %s" % exc)


def read_menu_items(page):
    """Read the visible items of the model selector menu.

    Returns a list of dicts whose ``index`` is the position inside
    MENU_ITEM_SELECTOR, so ``page.locator(MENU_ITEM_SELECTOR).nth(index)``
    addresses exactly the same element.
    """
    try:
        raw = page.evaluate(
            """(sel) => Array.from(document.querySelectorAll(sel)).map((el, index) => {
                const rect = el.getBoundingClientRect();
                return {
                    index: index,
                    tag: el.tagName.toLowerCase(),
                    text: (el.innerText || '').trim().replace(/\\s+/g, ' '),
                    cls: (el.className && el.className.toString) ? el.className.toString() : '',
                    visible: rect.width > 0 && rect.height > 0
                };
            })""",
            MENU_ITEM_SELECTOR,
        )
    except Exception as exc:
        print_debug("Could not read menu items: %s" % exc)
        return []
    return [item for item in raw if item.get("visible")]


def open_model_menu(page, attempts=2):
    """Open the model selector dropdown. Returns True on success."""
    for _ in range(attempts):
        for selector in MODEL_MENU_BUTTONS:
            button = first_visible(page, selector, timeout=800)
            if button is None:
                continue
            try:
                button.click()
                time.sleep(1.2)
                items = read_menu_items(page)
                if items:
                    return True
                print_debug("Model selector opened via %s but menu stayed empty" % selector)
            except Exception as exc:
                print_debug("Model selector click failed (%s): %s" % (selector, exc))
        for name in MODEL_BUTTON_NAMES:
            try:
                button = page.locator('button:has-text("%s")' % name).first
                if button.is_visible(timeout=600):
                    button.click()
                    time.sleep(1.2)
                    if read_menu_items(page):
                        return True
            except Exception:
                continue
    return False


def close_open_menu(page):
    for _ in range(2):
        try:
            page.keyboard.press("Escape")
            time.sleep(0.3)
        except Exception:
            return


def model_button_label(page):
    try:
        return page.locator(MODEL_MENU_BUTTONS[0]).first.get_attribute("aria-label") or ""
    except Exception:
        return ""


def is_model_available(page, model_text):
    """Check if a model (substring) is present and not disabled in model menu."""
    try:
        if page.locator(MODEL_MENU_BUTTONS[0]).count() == 0:
            return False
        page.locator(MODEL_MENU_BUTTONS[0]).first.click()
        time.sleep(1.0)
        available = False
        disabled = False
        for item in read_menu_items(page):
            txt = (item.get("text") or "").lower()
            if model_text.lower() in txt:
                classes = (item.get("classes") or "").lower()
                if "disabled" in classes or "aria-disabled" in str(item).lower() or "unavailable" in txt:
                    disabled = True
                available = True
                break
        close_open_menu(page)
        if not available or disabled:
            return False
        return True
    except Exception as exc:
        print_debug("is_model_available failed: %s" % exc)
        try:
            close_open_menu(page)
        except Exception:
            pass
        return False


def is_thinking_extended(page):
    """Extended thinking is reflected in the model button's accessible name."""
    label = model_button_label(page).lower()
    return any(marker in label for marker in THINKING_EXTENDED_MARKERS)


# ---------------------------------------------------------------------------
# Chat mode
# ---------------------------------------------------------------------------
def open_sidebar(page):
    """Make sure the left navigation is expanded so its entries are clickable."""
    print_debug("STEP 1: expanding the left sidebar")
    if any(True for _ in [1] if first_visible(page, "[data-test-id=\"new-chat-button\"]", 500)):
        print_debug("  Sidebar already visible (new-chat-button found)")
        return
    try:
        page.keyboard.press("[")
        time.sleep(1)
    except Exception as exc:
        print_debug("  '[' shortcut failed: %s" % exc)

    for selector in SIDEBAR_OPEN_CHECKS:
        if first_visible(page, selector, 500) is not None:
            print_debug("  Sidebar confirmed open (%s)" % selector)
            return

    print_debug("  Sidebar still closed, trying menu buttons")
    for selector in SIDEBAR_MENU_BUTTONS:
        button = first_visible(page, selector, 700)
        if button is None:
            continue
        if click_locator(button):
            time.sleep(1.5)
            for check in SIDEBAR_OPEN_CHECKS:
                if first_visible(page, check, 500) is not None:
                    print_debug("  Sidebar opened by clicking %s" % selector)
                    return
    print_debug("  Could not confirm sidebar state, continuing anyway")


def activate_temporary_chat(page):
    """Turn on Gemini's temporary chat so the batch leaves no history behind."""
    container = page.locator("temp-chat-button").first
    try:
        if container.count() > 0:
            inner = container.locator("button, gem-icon-button").first
            if inner.count() > 0:
                if "temp-chat-on" in (inner.get_attribute("class") or ""):
                    print_debug("Temporary chat already active")
                    return True
                inner.click()
                time.sleep(2.5)
                if "temp-chat-on" in (inner.get_attribute("class") or ""):
                    print_debug("Temporary chat activated")
                    return True
                print_debug("Clicked temporary chat but 'temp-chat-on' is still absent")
    except Exception as exc:
        print_debug("temp-chat-button handling failed: %s" % exc)

    for selector in TEMP_CHAT_BUTTONS:
        button = first_visible(page, selector, 1200)
        if button is None:
            continue
        try:
            html = button.evaluate("node => node.outerHTML").lower()
            if "temp-chat-on" in html:
                print_debug("Temporary chat already active (HTML check, %s)" % selector)
                return True
        except Exception:
            pass
        if click_locator(button):
            time.sleep(2.5)
            return True
    return False


def click_new_chat(page):
    for selector in NEW_CHAT_BUTTONS:
        button = first_visible(page, selector, 1200)
        if button is None:
            continue
        if click_locator(button):
            time.sleep(2.5)
            return True
    return False


def goto_gemini(page, retries=3):
    """Navigate to a fresh Gemini chat, tolerating aborted navigations."""
    for attempt in range(retries):
        try:
            if attempt > 0:
                time.sleep(random.uniform(1.0, 3.0))
            url = page.url
            is_gemini = "gemini.google.com" in url
            is_saved_chat = (
                is_gemini
                and any(part in url for part in ["/app/", "/chats/"])
                and not url.rstrip("/").endswith("/app")
            )
            if not is_gemini or url == "about:blank":
                page.goto("https://gemini.google.com/app", timeout=60000,
                          wait_until="domcontentloaded")
            elif is_saved_chat:
                if not click_new_chat(page):
                    page.goto("https://gemini.google.com/app", timeout=60000,
                              wait_until="domcontentloaded")
            else:
                page.reload(timeout=60000, wait_until="domcontentloaded")
            page.wait_for_load_state("domcontentloaded", timeout=60000)
            time.sleep(2)
            return True
        except Exception as exc:
            message = str(exc)
            if "ERR_ABORTED" in message and "gemini.google.com" in page.url:
                print_debug("Navigation aborted but already on Gemini, continuing")
                return True
            if attempt == retries - 1:
                print_debug("Navigation failed: %s" % message[:120])
                return False
            print_debug("Navigation error, retry %d/%d" % (attempt + 1, retries))
    return False


def setup_chat_mode(page):
    """Apply the --gen mode for the prompt that is about to be sent."""
    print_debug(">>> CHAT_MODE = '%s' <<<" % CHAT_MODE)
    dismiss_overlays(page)

    if CHAT_MODE not in ("tmp", "chat"):
        print_debug("*** NATIVE / CURRENT CONTEXT MODE — no navigation or chat setup ***")
        if "gemini.google.com" not in page.url:
            try:
                page.goto("https://gemini.google.com/", timeout=60000,
                          wait_until="domcontentloaded")
                page.wait_for_load_state("domcontentloaded", timeout=60000)
                time.sleep(2)
            except Exception as exc:
                print_warning("Could not open Gemini: %s" % exc)
        return

    if not goto_gemini(page):
        print_warning("Could not reach a fresh Gemini chat, continuing anyway")

    open_sidebar(page)
    dismiss_overlays(page)

    activated = False
    if CHAT_MODE == "tmp":
        print_debug("Looking for the 'Czat tymczasowy' / 'Temporary chat' control")
        if activate_temporary_chat(page):
            print_debug("*** TEMPORARY CHAT ACTIVE ***")
            activated = True
        else:
            print_warning("Temporary chat control not found, falling back to a normal chat")
    else:
        print_debug("Looking for the 'Nowy czat' / 'New chat' control")

    if not activated:
        if click_new_chat(page):
            print_debug("*** NEW CHAT STARTED ***")
        else:
            print_warning("Could not start a new chat, continuing in the current one")
    print_debug(">>> CHAT SETUP DONE <<<")


# ---------------------------------------------------------------------------
# Model and thinking level
# ---------------------------------------------------------------------------
def _is_thinking_level(text):
    lowered = text.lower()
    return any(marker in lowered for marker in THINKING_LEVEL_MARKERS)


def _model_score(text, type_arg):
    """Rank a model-menu item against the requested --type (lower is better)."""
    lowered = text.lower()
    is_lite = "lite" in lowered

    if type_arg == "flash-lite":
        if is_lite:
            return 0
        for word in ("najszybsze", "fastest", "szybki", "fast"):
            if word in lowered:
                return 1
        return 99

    if type_arg in ("fast", "flash"):
        if is_lite:
            return 99
        for word in ("hydrogen", "flash", "szybki", "szybkie", "fast"):
            if word in lowered:
                return 0
        return 99

    if type_arg in ("think",):
        for word in ("argon", "myślący", "myslacy", "thinking", "głębokie", "deep"):
            if word in lowered:
                return 0
        return 99

    if type_arg in ("pro",):
        for word in ("pro", "zaawansowan", "advanced"):
            if word in lowered:
                return 0
        return 99

    if type_arg in ("carbon",):
        if "carbon" in lowered:
            return 0
        return 99

    return 99


def select_model(page, type_arg):
    """Select the model requested by --type."""
    if not type_arg:
        return
    print_debug("Selecting model type: %s" % type_arg)
    if not open_model_menu(page):
        print_warning("Could not open the model selector for: %s" % type_arg)
        return

    items = read_menu_items(page)
    candidates = []
    for item in items:
        if _is_thinking_level(item["text"]):
            continue
        score = _model_score(item["text"], type_arg.lower())
        if score < 99:
            candidates.append((score, item))
    candidates.sort(key=lambda pair: pair[0])

    for _score, item in candidates:
        print_debug("  candidate: '%s' (%s)" % (item["text"][:60], item["tag"]))
        locator = page.locator(MENU_ITEM_SELECTOR).nth(item["index"])
        if click_locator(locator):
            time.sleep(1.2)
            close_open_menu(page)
            print_debug("Model type '%s' selected via '%s'" % (type_arg, item["text"][:50]))
            return

    # Fallback: a visible button carrying the model name outside the menu.
    for name in MODEL_BUTTON_NAMES:
        for selector in ('button:has-text("%s")' % name,):
            button = first_visible(page, selector, 600)
            if button is None:
                continue
            if click_locator(button):
                close_open_menu(page)
                print_debug("Model type '%s' selected by clicking button '%s'" % (type_arg, name))
                return

    close_open_menu(page)
    print_warning("Could not find a menu entry for model type: %s" % type_arg)


def select_thinking_mode(page, think_mode):
    """Set the thinking level requested by --thinking."""
    if not think_mode:
        return
    print_debug("Selecting thinking mode: %s" % think_mode)
    if not open_model_menu(page):
        print_warning("Could not open the model selector to set thinking mode: %s" % think_mode)
        return

    items = read_menu_items(page)
    selected_now = [item for item in items if "selected" in item["cls"].lower()]
    extended_active = any(_is_thinking_level(i["text"]) for i in selected_now) and is_thinking_extended(page)

    if think_mode in ("extended", "extend"):
        if extended_active:
            print_debug("Extended thinking is already active")
            close_open_menu(page)
            return
        wanted = THINKING_EXTENDED_MARKERS
    else:
        if not extended_active:
            print_debug("Basic thinking is already active")
            close_open_menu(page)
            return
        wanted = THINKING_BASIC_MARKERS

    ordered = []
    for item in items:
        if not _is_thinking_level(item["text"]):
            continue
        lowered = item["text"].lower()
        rank = min(
            (pos for pos, marker in enumerate(wanted) if marker in lowered),
            default=99,
        )
        if rank < 99:
            ordered.append((rank, item))
    ordered.sort(key=lambda pair: pair[0])

    if not ordered:
        close_open_menu(page)
        if think_mode in ("extended", "extend"):
            print_warning("Could not find the extended thinking option in the model menu")
        else:
            print_warning("This Gemini menu has no separate 'basic' thinking level; "
                          "leaving the current setting untouched")
        return

    _rank, item = ordered[0]
    locator = page.locator(MENU_ITEM_SELECTOR).nth(item["index"])
    if click_locator(locator):
        time.sleep(1.2)
        close_open_menu(page)
        if think_mode in ("extended", "extend"):
            if is_thinking_extended(page):
                print_debug("Extended thinking is now active")
                return
            print_warning("Clicked the extended thinking option but it does not appear active")
        else:
            if not is_thinking_extended(page):
                print_debug("Thinking level is back to basic")
                return
            print_warning("Clicked the basic thinking option but extended still appears active")
    else:
        close_open_menu(page)
        print_warning("Could not click the thinking level option: %s" % item["text"][:50])


# ---------------------------------------------------------------------------
# Imagen tool activation
# ---------------------------------------------------------------------------
def activate_image_tools(page):
    """Enter Gemini's "Create image" mode via the Tools menu."""
    dismiss_overlays(page)

    print_debug("Looking for a direct 'Create image' control")
    for selector in DIRECT_IMAGE_BUTTONS:
        button = first_visible(page, selector, 600)
        if button is None:
            continue
        try:
            if button.is_disabled():
                continue
        except Exception:
            pass
        if click_locator(button):
            time.sleep(1.2)
            print_debug("Activated image mode directly via %s" % selector)
            return True

    print_debug("Looking for the Tools menu")
    for selector in TOOLS_BUTTONS:
        button = first_visible(page, selector, 1200)
        if button is None:
            continue
        try:
            if button.is_disabled():
                continue
        except Exception:
            pass
        if not click_locator(button):
            continue
        time.sleep(1.5)
        print_debug("Opened the Tools menu via %s" % selector)
        # NB: never dismiss overlays here. Angular Material renders every menu
        # inside .cdk-overlay-container, so dismissing "overlays" after opening
        # the menu deletes the menu itself.
        for item_selector in CREATE_IMAGE_SELECTORS:
            item = first_visible(page, item_selector, 800)
            if item is None:
                continue
            try:
                if item.is_disabled():
                    continue
            except Exception:
                pass
            if click_locator(item):
                time.sleep(1.5)
                print_debug("Clicked 'Create image' via %s" % item_selector)
                return True
        close_open_menu(page)
        return False

    print_debug("Tools menu not found; image mode may already be active")
    return False


# ---------------------------------------------------------------------------
# Prompt entry
# ---------------------------------------------------------------------------
def normalize_ws(text):
    return re.sub(r"\s+", " ", (text or "").replace("\u00a0", " ")).strip()


def find_prompt_input(page):
    for selector in INPUT_SELECTORS:
        candidate = first_visible(page, selector, 1500)
        if candidate is not None:
            print_debug("Prompt input found via %s" % selector)
            return candidate
    return None


def _clear_input(page, locator):
    try:
        locator.click(timeout=5000)
    except Exception:
        try:
            locator.click(force=True, timeout=5000)
        except Exception:
            return False
    time.sleep(0.3)
    for combo in ("Control+A", "Meta+A"):
        try:
            locator.press(combo)
            time.sleep(0.15)
        except Exception:
            continue
    try:
        locator.fill("")
        time.sleep(0.15)
    except Exception:
        pass
    return True


def _clipboard_paste(page, context, locator, text):
    """Paste through the clipboard — the most faithful path for long prompts."""
    try:
        context.grant_permissions(["clipboard-read", "clipboard-write"])
    except Exception:
        try:
            context.grant_permissions(["clipboard-read", "clipboard-write"],
                                      origin="https://gemini.google.com")
        except Exception as exc:
            print_debug("Clipboard permission not granted: %s" % exc)
            return False
    escaped = text.replace("\\", "\\\\").replace("`", "\\`").replace("$", "\\$")
    try:
        page.evaluate("text => navigator.clipboard.writeText(text)", text)
    except Exception as exc:
        print_debug("Clipboard write failed: %s" % exc)
        return False
    time.sleep(0.3)
    for combo in ("Control+V", "Meta+V"):
        try:
            locator.press(combo)
            time.sleep(0.9)
            current = normalize_ws(locator.inner_text())
            if current:
                return normalize_ws(text) in current or current == normalize_ws(text)
        except Exception:
            continue
    return False


def enter_prompt(page, context, text):
    """Put ``text`` into Gemini's editor, verifying what actually landed there.

    Quill renders one block element per line, so ``innerText`` reports extra
    newlines for genuinely multi-line prompts. Verification therefore compares
    whitespace-normalised text, which still catches duplicated or truncated
    input (Playwright's insert_text has been observed to prepend stray
    characters).
    """
    locator = find_prompt_input(page)
    if locator is None:
        print_error("Could not find the Gemini prompt input field")
        return False

    expected = normalize_ws(text)
    dismiss_overlays(page)
    if not _clear_input(page, locator):
        print_error("Could not focus the Gemini prompt input field")
        return False

    # 1) fast path — a single synthetic text insertion
    try:
        page.keyboard.insert_text(text)
        time.sleep(0.6)
        if normalize_ws(locator.inner_text()) == expected:
            print_debug("Prompt entered via insert_text (%d chars)" % len(text))
            return True
        print_debug("insert_text produced unexpected content, trying clipboard")
    except Exception as exc:
        print_debug("insert_text failed: %s" % exc)

    # 2) clipboard paste
    if _clipboard_paste(page, context, locator, text):
        if normalize_ws(locator.inner_text()) == expected:
            print_debug("Prompt entered via clipboard paste (%d chars)" % len(text))
            return True
        print_debug("Clipboard paste produced unexpected content, trying key-by-key")

    # 3) real key events — slow, so only for reasonably sized prompts
    if len(text) <= 4000 and _clear_input(page, locator):
        try:
            locator.press_sequentially(text, delay=2)
            time.sleep(0.6)
            if normalize_ws(locator.inner_text()) == expected:
                print_debug("Prompt entered key-by-key (%d chars)" % len(text))
                return True
        except Exception as exc:
            print_debug("press_sequentially failed: %s" % exc)

    current = ""
    try:
        current = normalize_ws(locator.inner_text())
    except Exception:
        pass
    print_warning("Prompt field content differs from the intended text "
                  "(got %d of %d chars) — sending anyway"
                  % (len(current), len(expected)))
    return True


def send_prompt(page):
    """Submit the prompt using the send button, falling back to Enter."""
    for selector in SEND_BUTTON_SELECTORS:
        button = first_visible(page, selector, 1500)
        if button is None:
            continue
        try:
            if button.is_disabled():
                print_debug("Send button disabled via %s, nudging the field" % selector)
                try:
                    locator = find_prompt_input(page)
                    locator.type(" ")
                    time.sleep(0.4)
                except Exception:
                    pass
                if button.is_disabled():
                    continue
        except Exception:
            pass
        if click_locator(button, timeout=4000):
            print_debug("Prompt sent via %s" % selector)
            return True
    print_debug("Send button not found, pressing Enter")
    try:
        page.keyboard.press("Enter")
        return True
    except Exception as exc:
        print_error("Could not send the prompt: %s" % exc)
        return False


def wait_for_new_response(page, initial_count, timeout=30):
    """Wait for the response container created by the prompt we just sent."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if page.locator("model-response").count() > initial_count:
                print_debug("New model-response container appeared")
                return page.locator("model-response").nth(initial_count)
        except Exception:
            pass
        time.sleep(0.5)
    print_warning("Timed out waiting for a new response container; using the last one")
    return page.locator("model-response").last


# ---------------------------------------------------------------------------
# Response inspection
# ---------------------------------------------------------------------------
def last_response_text(page):
    try:
        if page.locator("model-response").count() > 0:
            return page.locator("model-response").last.inner_text().lower()
        return page.locator("div#chat-history").inner_text().lower()
    except Exception:
        return ""


def classify_response(page):
    """Return (kind, indicator) for a refusal / quota / rate limit, else None."""
    text = last_response_text(page)
    if not text:
        return None
    for indicator in QUOTA_INDICATORS:
        if indicator in text:
            return ("quota", indicator)
    for indicator in RATE_LIMIT_INDICATORS:
        if indicator in text:
            return ("rate_limit", indicator)
    for indicator in REFUSAL_INDICATORS:
        if indicator in text:
            return ("refusal", indicator)
    for indicator in GENERIC_ERROR_INDICATORS:
        if indicator in text:
            return ("error", indicator)
    return None


# ---------------------------------------------------------------------------
# Image retrieval
# ---------------------------------------------------------------------------
IMAGE_MAGIC = (
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"BM",
)


def looks_like_image(path):
    """Reject HTML error pages that browsers sometimes 'download'."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(16)
    except Exception:
        return False
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return True
    return any(head.startswith(magic) for magic in IMAGE_MAGIC)


def image_size(path):
    try:
        from PIL import Image

        with Image.open(path) as image:
            return image.size
    except Exception:
        return None


def image_file_size_kib(path):
    try:
        return os.path.getsize(path) / 1024
    except Exception:
        return 0.0


def is_generated_image(element):
    """Reject thumbnails that merely look generated.

    When Gemini answers from web search it embeds licensed search images whose
    alt text ends with "wygenerowane przez AI" and which live in
    ``div.attachment-container.search-images``. Those are small
    ``encrypted-tbn*.gstatic.com`` previews, not real generated images, so
    saving them would produce low-resolution output.
    """
    if element is None:
        return False
    try:
        src = element.get_attribute("src") or ""
        if "encrypted-tbn" in src or "/images?q=tbn:" in src:
            return False
        if element.evaluate(
                "(e) => !!(e.closest('div.search-images') || e.closest('div.image-container[class*=spark]')"
                " || e.closest('.spark-licensed-center-host'))"
        ):
            return False
    except Exception:
        return True
    return True


def hover_image(image_element):
    """Gemini only renders the download control while the image is hovered."""
    if image_element is None:
        return False
    try:
        image_element.scroll_into_view_if_needed(timeout=1500)
    except Exception:
        pass
    try:
        image_element.hover(timeout=2000)
        time.sleep(0.5)
        return True
    except Exception:
        return False


def find_image_element(container):
    for selector in IMAGE_SELECTORS:
        for element in container.locator(selector).all():
            try:
                if not element.is_visible(timeout=150):
                    continue
            except Exception:
                continue
            if not is_generated_image(element):
                print_debug("Skipping search thumbnail matching %s" % selector)
                continue
            hover_image(element)
            return element, selector
    return None, None


def find_download_button(container, image_element=None):
    """Return Gemini's own download control, hovering the image if needed.

    The button is not part of the DOM until the generated image is hovered,
    so an image-only lookup used to time out on slow responses.
    """
    def _lookup():
        for selector in DOWNLOAD_SELECTORS:
            element = first_visible(container, selector, 150)
            if element is not None:
                return element, selector
        return None, None

    element, selector = _lookup()
    if element is not None:
        return element, selector

    if hover_image(image_element):
        element, selector = _lookup()
        if element is not None:
            return element, selector
    return None, None


def download_via_button(page, container, button, filepath, timeout_sec, retries):
    """Click Gemini's own download button, retrying without regenerating."""
    time.sleep(1.5)
    last_error = None
    for attempt in range(retries + 1):
        if attempt > 0:
            global RETRY_DOWNLOAD_COUNT
            RETRY_DOWNLOAD_COUNT += 1
            print_warning("Download retry %d/%d" % (attempt, retries))
            time.sleep(2)
            refreshed, _ = find_download_button(container)
            if refreshed is not None:
                button = refreshed
        try:
            print_debug("Expecting a download with a %ds timeout (attempt %d)"
                        % (timeout_sec, attempt + 1))
            with page.expect_download(timeout=timeout_sec * 1000) as info:
                button.click(timeout=10000)
            download = info.value
            download.save_as(filepath)
            if os.path.getsize(filepath) == 0:
                print_warning("Downloaded file is 0 bytes (attempt %d/%d)"
                              % (attempt + 1, retries + 1))
                try:
                    os.remove(filepath)
                except Exception:
                    pass
                last_error = "zero byte file"
                continue
            return True, None
        except Exception as exc:
            last_error = str(exc)
            print_warning("Download attempt %d failed: %s" % (attempt + 1, exc))
    return False, last_error


def save_via_canvas(page, image_element, filepath):
    try:
        payload = image_element.evaluate("""(img) => new Promise((resolve, reject) => {
            const convert = () => {
                try {
                    const canvas = document.createElement('canvas');
                    canvas.width = img.naturalWidth || img.width || 1024;
                    canvas.height = img.naturalHeight || img.height || 1024;
                    canvas.getContext('2d').drawImage(img, 0, 0);
                    resolve(canvas.toDataURL('image/png'));
                } catch (err) { reject(err.toString()); }
            };
            if (img.complete && img.naturalWidth !== 0) { convert(); }
            else { img.onload = convert; img.onerror = () => reject('image load error'); }
        })""")
        if not payload.startswith("data:image/") or "," not in payload:
            return False
        with open(filepath, "wb") as handle:
            handle.write(base64.b64decode(payload.split(",", 1)[1]))
        return os.path.getsize(filepath) > 0
    except Exception as exc:
        print_debug("Canvas extraction failed: %s" % exc)
        return False


def save_via_fetch(page, image_element, filepath):
    """Fetch the image URL from inside the page, or through the Playwright API."""
    src = None
    try:
        src = image_element.get_attribute("src", timeout=2000)
    except Exception:
        pass
    if not src:
        try:
            inner = image_element.locator("img").first
            if inner and inner.count() > 0:
                src = inner.get_attribute("src", timeout=1000)
        except Exception:
            pass
    if not src:
        return False
    print_debug("Image source: %s" % src[:90])

    if src.startswith("http"):
        try:
            payload = page.evaluate("""async (url) => {
                const response = await fetch(url, {credentials: 'include'});
                if (!response.ok) { throw new Error('HTTP ' + response.status); }
                const blob = await response.blob();
                return new Promise((resolve, reject) => {
                    const reader = new FileReader();
                    reader.onloadend = () => resolve(reader.result);
                    reader.onerror = reject;
                    reader.readAsDataURL(blob);
                });
            }""", src)
            with open(filepath, "wb") as handle:
                handle.write(base64.b64decode(payload.split(",", 1)[1]))
            return os.path.getsize(filepath) > 0
        except Exception as exc:
            print_debug("In-page fetch failed (%s), trying the request API" % exc)
        try:
            response = page.context.request.get(src)
            if response.ok:
                with open(filepath, "wb") as handle:
                    handle.write(response.body())
                return os.path.getsize(filepath) > 0
        except Exception as exc:
            print_debug("Playwright request API failed: %s" % exc)
        ok, _ = download_image_from_url(src, filepath)
        return ok
    return save_via_canvas(page, image_element, filepath)


def download_image_from_url(url, filepath):
    """Download an image over plain HTTP (last-resort fallback)."""
    start = time.time()
    try:
        try:
            import requests

            response = requests.get(url, timeout=30)
            if response.status_code != 200:
                raise IOError("HTTP %d" % response.status_code)
            data = response.content
        except ImportError:
            from urllib.request import urlopen

            with urlopen(url, timeout=30) as response:
                data = response.read()
        with open(filepath, "wb") as handle:
            handle.write(data)
        stats = None
        if STAT_MODE:
            stats = {
                "time": time.time() - start,
                "resolution": image_size(filepath),
                "size": image_file_size_kib(filepath),
            }
        return True, stats
    except Exception as exc:
        print_debug("HTTP download failed: %s" % exc)
        return False, None


def save_image(page, container, image_element, download_button, filepath,
               timeout_sec, retries):
    """Try every retrieval strategy in order of fidelity."""
    if download_button is not None:
        ok, error = download_via_button(page, container, download_button, filepath,
                                        timeout_sec, retries)
        if ok:
            return True, "download button"
        print_debug("Download button strategy failed: %s" % error)

    if image_element is not None:
        if save_via_canvas(page, image_element, filepath):
            return True, "canvas extraction"
        if save_via_fetch(page, image_element, filepath):
            return True, "image URL fetch"

    return False, None


# ---------------------------------------------------------------------------
# Result reporting
# ---------------------------------------------------------------------------
def progress_numbers(progress_prefix):
    if progress_prefix and progress_prefix.startswith("[") and "/" in progress_prefix:
        parts = progress_prefix[1:].replace("]", "").split("/")
        if len(parts) == 2:
            return parts[0], parts[1]
    return "", ""


def report_success(final_filename, filepath, progress_prefix, stats, is_1to1, elapsed):
    sep = " | "
    clear = "\033[K"
    current, total = progress_numbers(progress_prefix)
    ratio_text = "1:1" if is_1to1 else ""

    if DEBUG_MODE:
        message = "Success: %s" % final_filename
        if stats:
            message += "%s%.2fs%s%4dx%-4d%s%.2f KiB" % (
                sep, stats["time"], sep, stats["resolution"][0],
                stats["resolution"][1], sep, stats["size"])
        if is_1to1:
            message += "%s%s1:1%s" % (sep, Fore.RED if USE_COLOR else "", Style.RESET_ALL)
        print_success(message)
    else:
        if USE_COLOR:
            bracket = Fore.WHITE
            numbers = Fore.GREEN
            name = "%s%s%s" % (Fore.CYAN, final_filename, Style.RESET_ALL)
            ok = "%sOK%s" % (Fore.GREEN, Style.RESET_ALL)
            ratio = "%s%s1:1%s" % (sep, Fore.RED, Style.RESET_ALL) if is_1to1 else ""
            if stats:
                timing = "%s%.2fs%s" % (Fore.LIGHTBLUE_EX, stats["time"], Style.RESET_ALL)
                resolution = "%s%4dx%-4d%s" % (Fore.LIGHTBLUE_EX,
                                               stats["resolution"][0],
                                               stats["resolution"][1], Style.RESET_ALL)
                size = "%s%.2f KiB%s" % (Fore.LIGHTBLUE_EX, stats["size"], Style.RESET_ALL)
                line = ("%s[%s%s%s/%s%s]%s %s%s%s%s%s%s%s%s%s%s"
                        % (bracket, Style.RESET_ALL, numbers, current, total, bracket,
                           Style.RESET_ALL, name, sep, ok, sep, timing, sep, resolution,
                           sep, size, ratio))
            else:
                line = ("%s[%s%s%s/%s%s]%s %s%s%s%s"
                        % (bracket, Style.RESET_ALL, numbers, current, total, bracket,
                           Style.RESET_ALL, name, sep, ok, ratio))
            print("\r%s%s" % (clear, line))
        else:
            if stats:
                stat_text = "OK%s%.2fs%s%4dx%-4d%s%.2f KiB" % (
                    sep, stats["time"], sep, stats["resolution"][0],
                    stats["resolution"][1], sep, stats["size"])
            else:
                stat_text = "OK"
            ratio = "%s1:1" % sep if is_1to1 else ""
            print("\r%s%s %s%s%s%s" % (clear, progress_prefix, final_filename,
                                       sep, stat_text, ratio))

    log_sep = " | "
    if stats:
        LOG_LINES.append("%s %s%sOK%s%.2fs%s%4dx%-4d%s%.2f KiB%s" % (
            progress_prefix, final_filename, log_sep, log_sep, stats["time"], log_sep,
            stats["resolution"][0], stats["resolution"][1], log_sep, stats["size"],
            log_sep + ratio_text if is_1to1 else ""))
    else:
        LOG_LINES.append("%s %s%sOK%s" % (
            progress_prefix, final_filename, log_sep,
            log_sep + ratio_text if is_1to1 else ""))


def report_failure(filename_base, reason):
    global FAIL_COUNT
    FAIL_COUNT += 1
    ERROR_LOG.append((filename_base, reason))


def write_error_log():
    if not ERROR_LOG:
        return
    try:
        with open("nanogen.err", "a", encoding="utf-8") as handle:
            handle.write("\n%s\nError log - %s\n%s\n"
                         % ("=" * 60, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "=" * 60))
            for name, error in ERROR_LOG:
                handle.write("File: %s\nError: %s\n%s\n" % (name, error, "-" * 60))
    except Exception as exc:
        print_error("Failed to write the error log: %s" % exc)


def write_log_file():
    if not LOG_FILE:
        return
    try:
        elapsed = (time.time() - START_TIME) if START_TIME else 0
        with open(LOG_FILE, "w", encoding="utf-8") as handle:
            handle.write("Nanogen CLI execution log - %s\n"
                         % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            handle.write("%s\n" % ("=" * 60))
            handle.write("Execution time: %s\n" % format_elapsed(elapsed))
            handle.write("Total images generated: %d\n" % SUCCESS_COUNT)
            handle.write("Failed downloads: %d\n" % FAIL_COUNT)
            handle.write("Download retries: %d\n" % RETRY_DOWNLOAD_COUNT)
            handle.write("Images with 1:1 resolution (1024x1024): %d\n" % RATIO_1TO1_COUNT)
            handle.write("%s\n" % ("=" * 60))
            skipped = [line for line in LOG_LINES if "SKIPPED" in line]
            done = [line for line in LOG_LINES if "SKIPPED" not in line]
            if done:
                handle.write("\nDownloaded images:\n%s\n" % ("-" * 60))
                handle.write("\n".join(done) + "\n")
            if skipped:
                handle.write("\nSkipped files:\n%s\n" % ("-" * 60))
                handle.write("\n".join(skipped) + "\n")
        print_info("Log saved to: %s" % LOG_FILE)
    except Exception as exc:
        print_error("Failed to write the log file: %s" % exc)


def print_summary():
    elapsed = (time.time() - START_TIME) if START_TIME else 0
    print_info("\n" + "=" * 60)
    print_color("EXECUTION SUMMARY", Fore.MAGENTA if USE_COLOR else "")
    print_info("=" * 60)
    print_info("Total images to generate: %d" % TOTAL_IMAGES)
    print_success("Successfully downloaded: %d" % SUCCESS_COUNT)
    if FAIL_COUNT > 0:
        print_error("Failed downloads: %d" % FAIL_COUNT)
    else:
        print_info("Failed downloads: %d" % FAIL_COUNT)
    print_color("Execution time: %s" % format_elapsed(elapsed),
                Fore.CYAN if USE_COLOR else "")
    print_info("=" * 60)
    if ERROR_LOG:
        write_error_log()
        print_warning("Errors logged to nanogen.err")
    write_log_file()


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------
def report_simulation(filename_base, final_prompt, report, progress_prefix):
    separator = " | "
    final_filename = get_final_filename(filename_base)
    current, total = progress_numbers(progress_prefix)
    print_info("\n%s--- SIMULATION: %s ---%s" % (Fore.YELLOW, filename_base, Style.RESET_ALL))
    print_info("%sFinal prompt:%s %s" % (Fore.CYAN, Style.RESET_ALL, final_prompt))
    print_info("%sOutput file:%s %s" % (Fore.CYAN, Style.RESET_ALL, final_filename))
    print_info("%sAspect ratio:%s %s"
               % (Fore.CYAN, Style.RESET_ALL, report["aspect_ratio"] or "default (16:9)"))
    if report["width"] and report["height"]:
        print_info("%sResolution:%s %dx%d"
                   % (Fore.CYAN, Style.RESET_ALL, report["width"], report["height"]))
    if report["insprompt"]:
        print_info("%sPrepend (--insprompt):%s %s"
                   % (Fore.CYAN, Style.RESET_ALL, report["insprompt"]))
    if report["addprompt"]:
        print_info("%sAppend (--addprompt):%s %s"
                   % (Fore.CYAN, Style.RESET_ALL, report["addprompt"]))
    if report["resolution_error"]:
        print_warning(report["resolution_error"])
    print_info("-" * 40 + "\n")

    if USE_COLOR:
        line = ("%s[%s%s%s/%s%s]%s %s%s%s%s%s%s"
                % (Fore.WHITE, Style.RESET_ALL, Fore.GREEN, current, total, Fore.WHITE,
                   Style.RESET_ALL, Fore.CYAN, final_filename, separator,
                   Fore.GREEN, "OK", Style.RESET_ALL))
        print(line)
    else:
        print("%s %s%sOK" % (progress_prefix, final_filename, separator))
    LOG_LINES.append("%s %s%sOK" % (progress_prefix, final_filename, separator))
    return final_filename


# ---------------------------------------------------------------------------
# Core: one prompt end to end
# ---------------------------------------------------------------------------
def process_single_prompt(page, context, prompt, filename_base, output_dir,
                          add_prompt=None, insp_prompt=None, fmt_arg=None,
                          res_arg=None, resx_arg=None, resy_arg=None,
                          type_arg=None, think_mode=None, min_gen_time=30,
                          dl_timeout_sec=45, download_retries=0,
                          progress_prefix="", limit_attempt=0):
    """Run the full Gemini interaction for a single prompt."""
    global SUCCESS_COUNT, RATIO_1TO1_COUNT, RETRY_DOWNLOAD_COUNT

    limit_retries = 3
    limit_wait = LIMIT_WAIT_TIME

    final_prompt, report = build_prompt(prompt, add_prompt, insp_prompt, fmt_arg,
                                       res_arg, resx_arg, resy_arg)
    if report["resolution_error"]:
        print_warning(report["resolution_error"])

    final_filename = get_final_filename(filename_base)
    filepath = os.path.join(output_dir, final_filename)

    if SKIP_MODE and os.path.exists(filepath):
        size = os.path.getsize(filepath)
        _, ext = os.path.splitext(final_filename)
        minimum = 1024 if ext.lower() in (".jpg", ".jpeg") else (256 if ext.lower() == ".png" else 0)
        if size >= minimum:
            print_info("[SKIP] Skipping existing file: %s" % final_filename)
            LOG_LINES.append("%s %s | SKIPPED" % (progress_prefix, final_filename))
            SUCCESS_COUNT += 1
            return True
        print_warning("[RE-GEN] %s exists but is too small (%d bytes)"
                      % (final_filename, size))

    if limit_attempt > 0:
        print_info("\n[RETRY] Attempting the prompt again after a rate-limit wait "
                   "(%d/%d)..." % (limit_attempt, limit_retries))
        if USE_COLOR:
            print("%s[%s%s%s/%s%s]%s %s%s%s %sProcessing (Retry %d)...%s"
                  % (Fore.WHITE, Style.RESET_ALL, Fore.GREEN, progress_prefix[1:8],
                     Fore.WHITE, Style.RESET_ALL, Style.RESET_ALL, Fore.CYAN,
                     filename_base, Style.RESET_ALL, Fore.WHITE, limit_attempt,
                     Style.RESET_ALL), end="", flush=True)
        else:
            print("%s %s | Processing (Retry %d)..."
                  % (progress_prefix, filename_base, limit_attempt), end="", flush=True)

    # --- chat mode, model, thinking level, tools ---
    setup_chat_mode(page)
    select_model(page, type_arg)
    select_thinking_mode(page, think_mode)
    if CHAT_MODE != "native":
        activate_image_tools(page)
    else:
        print_debug("Native mode: leaving the image tool state untouched")

    # --- send the prompt ---
    try:
        initial_count = page.locator("model-response").count()
    except Exception:
        initial_count = 0
    print_debug("Initial model-response count: %d" % initial_count)

    print_debug("Prompt (%d chars): %s..." % (len(final_prompt), final_prompt[:60]))
    if not enter_prompt(page, context, final_prompt):
        report_failure(filename_base, "Could not enter the prompt")
        return False
    if not send_prompt(page):
        report_failure(filename_base, "Could not send the prompt")
        return False

    response = wait_for_new_response(page, initial_count, timeout=30)

    # --- wait for the image ---
    max_wait = max(min_gen_time + 60, 120)
    elapsed = 0.0
    image_element = None
    image_selector = None
    download_button = None
    download_selector = None
    refusal = None

    remaining = min_gen_time
    while remaining > 0:
        step = min(5, remaining)
        time.sleep(step)
        elapsed += step
        remaining -= step
        verdict = classify_response(page)
        if verdict and verdict[0] in TERMINAL_VERDICTS:
            refusal = verdict
            break
        if image_element is None:
            image_element, image_selector = find_image_element(response)
        if image_element is not None and download_button is None:
            download_button, download_selector = find_download_button(response, image_element)
        if image_element is not None and download_button is not None:
            print_debug("Image and download button ready after %.1fs" % elapsed)
            break
        print_debug("Waiting for the image (%.0fs/%ds minimum)" % (elapsed, min_gen_time))

    while refusal is None and elapsed < max_wait:
        verdict = classify_response(page)
        if verdict and verdict[0] in TERMINAL_VERDICTS:
            refusal = verdict
            break
        if image_element is None:
            image_element, image_selector = find_image_element(response)
        if download_button is None:
            download_button, download_selector = find_download_button(response, image_element)
        if image_element is not None and download_button is not None:
            try:
                if download_button.is_visible(timeout=150):
                    print_debug("Image and download button ready after %.1fs" % elapsed)
                    break
            except Exception:
                download_button = None
        time.sleep(1)
        elapsed += 1
        if int(elapsed) % 5 == 0:
            state = "image" if image_element is not None else "no image"
            button = "button" if download_button is not None else "no button"
            print_debug("%s, %s... (%.0fs)" % (state, button, elapsed))

    if refusal is not None:
        kind, indicator = refusal
        if kind == "rate_limit":
            if limit_attempt < limit_retries:
                print_warning("\n[LIMIT REACHED] Gemini rate limit detected ('%s'). "
                              "Waiting %ds before retrying..." % (indicator, limit_wait))
                time.sleep(limit_wait)
                return process_single_prompt(
                    page, context, prompt, filename_base, output_dir,
                    add_prompt=add_prompt, insp_prompt=insp_prompt, fmt_arg=fmt_arg,
                    res_arg=res_arg, resx_arg=resx_arg, resy_arg=resy_arg,
                    type_arg=type_arg, think_mode=think_mode, min_gen_time=min_gen_time,
                    dl_timeout_sec=dl_timeout_sec, download_retries=download_retries,
                    progress_prefix=progress_prefix, limit_attempt=limit_attempt + 1)
            print_error("\n[ERROR] Gemini rate limit persisted after %d retries." % limit_retries)
            report_failure(filename_base, "Rate limit: %s" % indicator)
            return False
        if kind == "quota":
            print_error("\n[QUOTA] Gemini's image quota is exhausted ('%s'). "
                        "This is a daily account limit, not a transient error." % indicator)
            print_info("Check your usage in Gemini settings; no retry will help.")
            report_failure(filename_base, "Image quota exhausted: %s" % indicator)
            return False
        # If Gemini fails on Pro model (frequent internal error on dense prompts), fallback to fast (Hydrogen)
        if (kind in ("error", "refusal")) and type_arg == "pro" and limit_attempt == 0:
            print_warning("\n[MODEL FALLBACK] Gemini error/refusal on Pro model ('%s'). Retrying with fast model (Hydrogen)..." % indicator)
            return process_single_prompt(
                page, context, prompt, filename_base, output_dir,
                add_prompt=add_prompt, insp_prompt=insp_prompt, fmt_arg=fmt_arg,
                res_arg=res_arg, resx_arg=resx_arg, resy_arg=resy_arg,
                type_arg="fast", think_mode=think_mode, min_gen_time=min_gen_time,
                dl_timeout_sec=dl_timeout_sec, download_retries=download_retries,
                progress_prefix=progress_prefix, limit_attempt=limit_attempt + 1)

        if kind == "error":
            print_error("\n[TRANSIENT] Gemini reported an error: '%s'" % indicator)
            report_failure(filename_base, "Gemini error: %s" % indicator)
            return False
        print_error("\n[REFUSED] Gemini declined the request: '%s'" % indicator)
        report_failure(filename_base, "Gemini refused: %s" % indicator)
        return False

    if image_element is None:
        print_error("No generated image appeared within %ds (%s)" % (max_wait, image_selector or ""))
        report_failure(filename_base, "Generation timeout, no image found")
        return False

    if os.path.exists(filepath) and not ask_overwrite(final_filename):
        print_info("Skipped: %s" % final_filename)
        return True

    print_debug("Saving the image (source: %s)" % (download_selector or image_selector))
    ok, method = save_image(page, response, image_element, download_button, filepath,
                            dl_timeout_sec, download_retries)
    if not ok:
        print_error("Download failed after %d attempt(s): %s" % (download_retries + 1, final_filename))
        report_failure(filename_base, "Download failed after all strategies")
        return False

    if not looks_like_image(filepath):
        print_error("Saved file is not a valid image: %s" % final_filename)
        try:
            os.remove(filepath)
        except Exception:
            pass
        report_failure(filename_base, "Saved data was not an image")
        return False

    print_debug("Image saved via %s (%d bytes)"
                % (method, os.path.getsize(filepath)))

    verdict = classify_response(page)
    if verdict and verdict[0] == "rate_limit" and limit_attempt < limit_retries:
        print_warning("\n[LIMIT REACHED] Rate limit text still present after the download. "
                      "Waiting %ds before retrying..." % limit_wait)
        time.sleep(limit_wait)
        return process_single_prompt(
            page, context, prompt, filename_base, output_dir,
            add_prompt=add_prompt, insp_prompt=insp_prompt, fmt_arg=fmt_arg,
            res_arg=res_arg, resx_arg=resx_arg, resy_arg=resy_arg,
            type_arg=type_arg, think_mode=think_mode, min_gen_time=min_gen_time,
            dl_timeout_sec=dl_timeout_sec, download_retries=download_retries,
            progress_prefix=progress_prefix, limit_attempt=limit_attempt + 1)

    dimensions = image_size(filepath)
    is_1to1 = dimensions == (1024, 1024)
    if is_1to1:
        RATIO_1TO1_COUNT += 1

    stats = None
    if STAT_MODE:
        stats = {"time": elapsed, "resolution": dimensions,
                 "size": image_file_size_kib(filepath)}

    report_success(final_filename, filepath, progress_prefix, stats, is_1to1, elapsed)
    SUCCESS_COUNT += 1
    return True


# ---------------------------------------------------------------------------
# Browser management
# ---------------------------------------------------------------------------
def chromium_candidates():
    """Well-known system Chrome/Chromium locations for this platform."""
    if IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA", "")
        program_files = [os.environ.get("ProgramFiles", r"C:\Program Files"),
                         os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")]
        found = []
        for base in program_files:
            found.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
            found.append(os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"))
        if local:
            found.append(os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"))
        return [path for path in found if os.path.exists(path)]
    if IS_MAC:
        return [path for path in (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ) if os.path.exists(path)]
    return [path for path in (
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/opt/google/chrome/chrome",
        "/snap/bin/chromium",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/microsoft-edge",
    ) if os.path.exists(path)]


def playwright_browser_path():
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as play:
            return play.chromium.executable_path
    except Exception:
        return None


def build_launch_args():
    args = [
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--window-size=1920,1080",
    ]
    if not IS_WINDOWS:
        # Required inside containers and when running as root on Linux.
        args += ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
    return args


def resolve_executable(explicit):
    if explicit:
        if not os.path.exists(explicit):
            print_error("Browser executable not found: %s" % explicit)
            sys.exit(1)
        return explicit
    bundled = playwright_browser_path()
    if bundled and os.path.exists(bundled):
        return None
    for candidate in chromium_candidates():
        print_debug("Using the system browser: %s" % candidate)
        return candidate
    return None


def launch_browser(user_data_dir=None, headless=True, executable_path=None,
                   channel=None, locale=None):
    """Launch Chromium through Playwright; returns (playwright, context, page)."""
    from playwright.sync_api import sync_playwright

    play = sync_playwright().start()
    launch_args = build_launch_args()
    options = {
        "headless": headless,
        "args": launch_args,
        "ignore_default_args": ["--enable-automation"],
    }
    if executable_path:
        options["executable_path"] = executable_path
    if channel:
        options["channel"] = channel
    if locale:
        options["locale"] = locale

    if not user_data_dir:
        print_error("An internal error occurred: no browser profile directory was resolved")
        raise SystemExit(1)

    os.makedirs(user_data_dir, exist_ok=True)
    print_debug("Persistent profile: %s" % user_data_dir)
    context = play.chromium.launch_persistent_context(user_data_dir=user_data_dir,
                                                      **options)
    context.set_default_timeout(30000)
    page = context.pages[0] if context.pages else context.new_page()

    page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    """)
    return play, context, page


def connect_over_cdp(host, port):
    """Attach to an already running browser; returns (playwright, context, page, owned)."""
    from playwright.sync_api import sync_playwright

    endpoint = "http://%s:%d" % (host, port)
    print_debug("Connecting to the browser at %s" % endpoint)
    play = sync_playwright().start()
    try:
        browser = play.chromium.connect_over_cdp(endpoint, timeout=30000)
    except Exception as exc:
        print_error("Could not connect to a browser at %s" % endpoint)
        print_info("Start one with start_nanogen_debug.sh (Linux) or "
                   "start_chrome_debug.bat (Windows), or drop --attach to let "
                   "nanogen launch its own browser.")
        print_debug("Connection error: %s" % exc)
        raise SystemExit(1)

    if not browser.contexts:
        print_error("The attached browser exposes no usable profile context")
        raise SystemExit(1)
    context = browser.contexts[0]

    page = None
    for candidate in context.pages:
        if "gemini.google.com" in candidate.url:
            page = candidate
            break
    if page is None:
        page = context.new_page()
        try:
            page.goto("https://gemini.google.com/app", timeout=60000,
                      wait_until="domcontentloaded")
        except Exception as exc:
            print_debug("Initial navigation to Gemini failed: %s" % exc)
    try:
        page.bring_to_front()
    except Exception:
        pass
    return play, context, page


def ensure_playwright_browsers(executable_path=None, channel=None):
    """Verify that a usable Chromium is available before starting a session."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        print_error("The 'playwright' package is not installed.")
        print_info("  pip install playwright colorama")
        print_info("  python %s --install-browser" % __PROGNAME__)
        raise SystemExit(1)

    if executable_path or channel:
        return
    bundled = playwright_browser_path()
    if bundled and os.path.exists(bundled):
        return
    system = chromium_candidates()
    if system:
        print_debug("No bundled browser, but a system browser exists at %s" % system[0])
        return
    print_error("No Chromium build was found for Playwright.")
    print_info("  python %s --install-browser" % __PROGNAME__)
    print_info("or point nanogen at a system Chrome/Chromium with --executable-path")
    raise SystemExit(1)


def install_browsers(with_deps=False):
    """Install the Playwright Chromium build (and optionally system libraries)."""
    command = [sys.executable, "-m", "playwright", "install"]
    if with_deps:
        if IS_WINDOWS:
            print_info("--with-deps has no effect on Windows, installing the browser only")
        elif not shutil_which("sudo"):
            print_warning("sudo not found; installing the browser without system libraries")
        else:
            command.append("--with-deps")
    command.append("chromium")
    print_info("Running: %s" % " ".join(command))
    print_info("System libraries are installed with sudo; expect a password prompt.")
    try:
        subprocess.check_call(command)
    except subprocess.CalledProcessError as exc:
        print_error("Browser installation failed: %s" % exc)
        print_info("Install the libraries manually, for example:")
        print_info("  sudo playwright install-deps chromium")
        raise SystemExit(1)
    print_success("Playwright Chromium installed.")


def shutil_which(name):
    from shutil import which

    return which(name)


# ---------------------------------------------------------------------------
# Google login
# ---------------------------------------------------------------------------
def read_login_file(filepath):
    """Read the email (line 1) and password (line 2) from a file."""
    accounts = read_login_accounts(filepath)
    if not accounts:
        print_error("Login file '%s' needs at least two lines: email then password"
                    % filepath)
        raise SystemExit(1)
    return accounts[0]


def read_login_accounts(path):
    """Return list of (email, password) from a file."""
    accounts = []
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
            raw = handle.readlines()
    except FileNotFoundError:
        print_error("Login file not found: %s" % path)
        raise SystemExit(1)
    except SystemExit:
        raise
    except Exception as exc:
        print_error("Error reading the login file: %s" % exc)
        raise SystemExit(1)
    i = 0
    while i < len(raw):
        sline = raw[i].strip()
        i += 1
        if not sline or sline.startswith("#"):
            continue
        parts = sline.split(None, 1)
        if len(parts) == 2 and "@" in parts[0]:
            accounts.append((parts[0], parts[1]))
            continue
        if "@" in sline:
            if i < len(raw):
                passw = raw[i].strip()
                i += 1
                if passw and not (passw.startswith("#") and "@" in passw):
                    accounts.append((sline, passw))
            continue
    if not accounts:
        print_error("Login file '%s' needs at least two lines: email then password"
                    % path)
        raise SystemExit(1)
    return accounts


def check_gemini_logged_in(page):
    """Return True once Gemini's composer is reachable."""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        time.sleep(2)
        url = page.url.lower()
        print_debug("[check_login] URL: %s" % url)
        if "accounts.google.com" in url or "signin" in url:
            print_debug("[check_login] redirected to the sign-in page")
            return False
        if "gemini.google.com" not in url:
            print_debug("[check_login] not on Gemini, navigating")
            page.goto("https://gemini.google.com/app", timeout=60000,
                      wait_until="domcontentloaded")
            time.sleep(3)
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        time.sleep(2)
        url = page.url.lower()
        if "accounts.google.com" in url:
            print_debug("[check_login] redirected to accounts.google.com")
            return False
        if find_prompt_input(page) is not None:
            print_debug("[check_login] composer found — logged in")
            return True
        print_debug("[check_login] composer missing, title: %s" % page.title())
        return "gemini.google.com" in url
    except Exception as exc:
        print_debug("[check_login] exception: %s" % exc)
        return False


def auto_login_to_google(page, email, password):
    """Drive the Google sign-in form with the supplied credentials."""
    print_info("Signing in to Google as %s..." % email)
    try:
        page.goto("https://accounts.google.com/signin", timeout=60000,
                  wait_until="domcontentloaded")
        time.sleep(3)

        if "accounts.google.com" not in page.url.lower():
            print_debug("Already signed in, opening Gemini")
            page.goto("https://gemini.google.com/app", timeout=60000,
                      wait_until="domcontentloaded")
            time.sleep(3)
            return check_gemini_logged_in(page)

        for selector in ['[data-identifier]', 'div[data-email]',
                         'div[role="button"]:has-text("%s")' % email,
                         '[data-email="%s"]' % email]:
            button = first_visible(page, selector, 1500)
            if button is not None and click_locator(button):
                print_debug("Picked the saved account via %s" % selector)
                time.sleep(3)
                break

        time.sleep(1)
        password_field = first_visible(page, 'input[type="password"]', 2000)
        if password_field is None:
            email_input = None
            for selector in ['input[type="email"]', 'input[name="identifier"]',
                             '#identifierId', 'input[aria-label*="Email"]',
                             'input[aria-label*="email"]', 'input[aria-label*="Adres"]']:
                email_input = first_visible(page, selector, 2000)
                if email_input is not None:
                    print_debug("Email field found via %s" % selector)
                    break
            if email_input is None:
                print_error("Could not find the email field on the Google sign-in page")
                return False
            email_input.click(force=True)
            time.sleep(0.3)
            email_input.fill(email)
            time.sleep(0.5)

            advanced = False
            for selector in ['button:has-text("Next")', 'button:has-text("Dalej")',
                             'span:has-text("Next")', 'span:has-text("Dalej")',
                             '#identifierNext button', '[aria-label*="Next"]']:
                button = first_visible(page, selector, 2000)
                if button is not None and click_locator(button):
                    print_debug("Submitted the email via %s" % selector)
                    advanced = True
                    time.sleep(3)
                    break
            if not advanced:
                page.keyboard.press("Enter")
                time.sleep(3)

            print_debug("URL after the email step: %s" % page.url)
            for _ in range(15):
                time.sleep(1)
                password_field = first_visible(page, 'input[type="password"]', 500)
                if password_field is None:
                    password_field = first_visible(
                        page, 'input[name="Passwd"], input[aria-label*="Password"], '
                              'input[aria-label*="hasło"]', 500)
                if password_field is not None:
                    break
                body = ""
                try:
                    body = page.evaluate("() => document.body.innerText").lower()
                except Exception:
                    pass
                for indicator in ("couldn't find your account", "nie znaleziono konta",
                                  "couldn't sign you in", "incorrect email",
                                  "nieprawidłowy adres email", "wrong password",
                                  "nieprawidłowe hasło"):
                    if indicator in body:
                        print_error("Google rejected the sign-in: '%s'" % indicator)
                        return False
            if password_field is None:
                print_error("The password field never appeared (wrong email, or 2FA required)")
                return False

        password_field.click(force=True)
        time.sleep(0.3)
        password_field.fill(password)
        time.sleep(0.5)

        submitted = False
        for selector in ['button:has-text("Next")', 'button:has-text("Dalej")',
                         'button:has-text("Sign in")', 'button:has-text("Zaloguj się")',
                         'button:has-text("Zaloguj")', 'span:has-text("Next")',
                         'span:has-text("Dalej")', '#passwordNext button',
                         '[aria-label*="Next"]']:
            button = first_visible(page, selector, 2000)
            if button is not None and click_locator(button):
                print_debug("Submitted the password via %s" % selector)
                submitted = True
                time.sleep(3)
                break
        if not submitted:
            page.keyboard.press("Enter")
            time.sleep(3)

        deadline = time.time() + 25
        while time.time() < deadline:
            time.sleep(1)
            url = page.url.lower()
            if "accounts.google.com" not in url and "signin" not in url:
                print_debug("Signed in, URL is now %s" % url[:80])
                page.goto("https://gemini.google.com/app", timeout=60000,
                          wait_until="domcontentloaded")
                time.sleep(3)
                if check_gemini_logged_in(page):
                    return True
                print_error("Signed in, but Gemini did not accept the session")
                return False
            body = ""
            try:
                body = page.evaluate("() => document.body.innerText").lower()
            except Exception:
                pass
            for indicator in ("wrong password", "incorrect password", "bad password",
                              "nieprawidłowe hasło", "złe hasło",
                              "couldn't sign you in"):
                if indicator in body:
                    print_error("Google rejected the sign-in: '%s'" % indicator)
                    return False
            if "challenge" in url:
                print_error("Google is asking for 2FA/verification, which cannot be automated")
                return False
        print_error("Still on the Google sign-in page after 25s")
        return False
    except Exception as exc:
        print_error("Login automation failed: %s" % exc)
        return False


def guide_login(page, headless):
    """Explain how to authenticate when no credentials were supplied."""
    if headless:
        print_error("No Google session found in the browser profile.")
        print_info("")
        print_info("Choose one of:")
        print_info("  1. Interactive login (needs a desktop or Xvfb):")
        print_info("       python %s --prompt test --no-headless" % __PROGNAME__)
        print_info("  2. Automated login with a credentials file:")
        print_info("       python %s --prompt test --loginfile creds.txt" % __PROGNAME__)
        print_info("       (line 1: e-mail, line 2: password)")
        print_info("  3. Reuse a profile you are already signed into:")
        print_info("       python %s --prompt test --user-data-dir /path/to/profile" % __PROGNAME__)
        print_info("  4. Attach to a signed-in browser:")
        print_info("       python %s --prompt test --attach" % __PROGNAME__)
        print_info("")
        print_info("The profile directory currently in use is: %s" % default_profile_dir())
        raise SystemExit(1)

    print_warning("Log into your Google account in the browser window that just opened.")
    print_info("Open https://gemini.google.com and sign in — nanogen will notice.")
    for _ in range(120):
        time.sleep(5)
        if check_gemini_logged_in(page):
            print_success("Login detected, continuing...")
            return
    print_error("No login detected within 10 minutes.")
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# Session orchestration
# ---------------------------------------------------------------------------
def install_signal_handlers():
    def handler(signum, _frame):
        if _INTERRUPTED[0]:
            print()
            print_error("Second interrupt — aborting immediately.")
            os._exit(130)
        _INTERRUPTED[0] = True
        print()
        print_warning("Interrupt received — finishing the current image, then stopping. "
                      "Press Ctrl+C again to abort now.")

    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            signal.signal(signum, handler)
        except (ValueError, OSError):
            pass


def load_queue(args):
    """Build the (prompt, filename) queue and pre-count already existing files."""
    queue = []
    existing = set()
    if SKIP_MODE and os.path.isdir(args.out):
        try:
            existing = set(os.listdir(args.out))
        except Exception:
            existing = set()

    def add(prompt, name):
        name = str(name)
        if SKIP_MODE:
            candidate = get_final_filename(name)
            if candidate in existing:
                LOG_LINES.append("SKIPPED: %s" % candidate)
                globals()["SUCCESS_COUNT"] += 1
                return
        queue.append((prompt, name))

    if args.input_file:
        try:
            with open(args.input_file, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except Exception as exc:
            print_error("Error loading the JSON file: %s" % exc)
            return None
        if isinstance(data, dict):
            for name, prompt in data.items():
                add(prompt, name)
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and "prompt" in item and "filename" in item:
                    add(item["prompt"], item["filename"])
                else:
                    print_warning("Skipping a JSON list entry without "
                                  "'filename' and 'prompt': %r" % (item,))
        else:
            print_error("The JSON file must contain an object or a list")
            return None
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = re.sub(r'[\\/*?:"<>|]', "", args.prompt)[:20].strip()
        add(args.prompt, "%s_%s" % (safe, timestamp))
    return queue


def print_progress_start(current, total, filename):
    if USE_COLOR:
        print("%s[%s%s%03d/%03d%s]%s %s%s%s %sProcessing...%s"
              % (Fore.WHITE, Style.RESET_ALL, Fore.GREEN, current, total, Fore.WHITE,
                 Style.RESET_ALL, Fore.CYAN, filename, Style.RESET_ALL, Fore.WHITE,
                 Style.RESET_ALL),
              end="", flush=True)
    else:
        print("[%03d/%03d] %s | Processing..." % (current, total, filename),
              end="", flush=True)


def print_progress_failed(current, total, filename, retries):
    clear = "\033[K"
    suffix = " (including %d download retr%s)" % (retries, "y" if retries == 1 else "ies") \
        if retries > 0 else ""
    if USE_COLOR:
        print("\r%s%s[%s%s%03d/%03d%s]%s %s%s%s %s%sERROR: Failed%s%s"
              % (clear, Fore.WHITE, Style.RESET_ALL, Fore.GREEN, current, total,
                 Fore.WHITE, Style.RESET_ALL, Fore.CYAN, filename, Style.RESET_ALL,
                 Fore.WHITE, Fore.RED, suffix, Style.RESET_ALL))
    else:
        print("\r%s[%03d/%03d] %s | ERROR: Failed%s"
              % (clear, current, total, filename, suffix))


def build_restart_command(args):
    """Reconstruct this invocation for --dlrestart, adding --skip."""
    script_path = os.path.abspath(sys.argv[0])
    command = [sys.executable, script_path]
    drop_value = {"--_restart_idx"}
    drop_flags = {"--skip", "--overwrite", "--_restart_idx", "--dlrestart", "--delrestart"}
    index = 1
    while index < len(sys.argv):
        argument = sys.argv[index]
        if argument in drop_value:
            index += 2
            continue
        if argument in drop_flags or any(argument.startswith(flag + "=") for flag in drop_flags):
            index += 1
            continue
        command.append(argument)
        index += 1
    if "--skip" not in command:
        command.append("--skip")
    command += ["--_restart_idx", str((args._restart_idx or 0) + 1)]
    return command


def restart_process(args):
    """Relaunch the whole run after a failed download (--dlrestart)."""
    command = build_restart_command(args)
    print_info("Restarting: %s" % " ".join(command))
    print_info("Waiting 10s before the restart to stay clear of rate limits...")
    try:
        sys.stdout.flush()
    except Exception:
        pass
    time.sleep(10)
    release_lock()
    if IS_WINDOWS:
        subprocess.Popen(command)
        raise SystemExit(0)
    os.execv(sys.executable, command)


def run_gemini_session(args):
    global START_TIME, TOTAL_IMAGES

    START_TIME = time.time()

    if SIMUL_MODE:
        print_info("%s*** SIMULATION MODE ACTIVE ***%s" % (Fore.YELLOW, Style.RESET_ALL))
        print_info("No browser is launched.\n")
    elif ATTACH_MODE:
        print_debug("Attaching to an existing browser on %s:%d" % (args.host, args.port))
    else:
        print_info("Launching Chromium (headless)..." if HEADLESS_MODE
                   else "Launching Chromium with a visible window...")

    queue = load_queue(args)
    if queue is None:
        raise SystemExit(1)

    TOTAL_IMAGES = len(queue)
    if SUCCESS_COUNT > 0:
        print_info("Skipped %d existing file(s)" % SUCCESS_COUNT)
    if TOTAL_IMAGES > 0:
        print_color("Total images to generate: %d" % TOTAL_IMAGES,
                    Fore.CYAN if USE_COLOR else "")
        print_info("")

    if not queue:
        print_info("All files already exist — nothing to generate.")
        print_summary()
        return

    if SIMUL_MODE:
        for index, (prompt, filename) in enumerate(queue):
            current, total = index + 1, len(queue)
            prefix = "[%03d/%03d]" % (current, total)
            print_progress_start(current, total, filename)
            if DEBUG_MODE:
                print()
                print_debug("--- %s: %s ---" % (prefix, filename))
            final_prompt, report = build_prompt(prompt, args.add_prompt, args.insp_prompt,
                                                args.fmt_arg, args.res_arg,
                                                args.resx_arg, args.resy_arg)
            report_simulation(filename, final_prompt, report, prefix)
            globals()["SUCCESS_COUNT"] += 1
        print_summary()
        return

    user_data_dir = args.user_data_dir or default_profile_dir()
    executable_path = resolve_executable(args.executable_path)
    ensure_playwright_browsers(executable_path, args.channel)

    if ATTACH_MODE:
        play, context, page = connect_over_cdp(args.host, args.port)
        owned_browser = False
    else:
        if not HEADLESS_MODE and IS_LINUX and not os.environ.get("DISPLAY") \
                and not os.environ.get("WAYLAND_DISPLAY"):
            print_error("--no-headless needs a display, but neither DISPLAY nor "
                        "WAYLAND_DISPLAY is set.")
            print_info("Use Xvfb (e.g. 'xvfb-run -a python %s ...') or drop "
                       "--no-headless." % __PROGNAME__)
            raise SystemExit(1)
        play, context, page = launch_browser(user_data_dir=user_data_dir,
                                             headless=HEADLESS_MODE,
                                             executable_path=executable_path,
                                             channel=args.channel,
                                             locale=args.locale)
        owned_browser = True

    try:
        page.set_default_timeout(30000)
        try:
            page.goto("https://gemini.google.com/app", timeout=60000,
                      wait_until="domcontentloaded")
        except Exception as exc:
            print_debug("Initial navigation failed: %s" % exc)

        email = password = None
        if args.loginfile:
            email, password = read_login_file(args.loginfile)
        elif args.login:
            email = args.login
            if args.pwdsilent:
                import getpass

                password = getpass.getpass("Google password: ")
            elif args.pwd:
                password = args.pwd
            if not password:
                print_error("A password is required: use --pwd or --pwdsilent")
                raise SystemExit(1)

        if email and password:
            if not auto_login_to_google(page, email, password):
                print_error("Automated login failed.")
                raise SystemExit(1)
            print_success("Signed in to Gemini as %s.\n" % email)
        else:
            if ATTACH_MODE:
                if not check_gemini_logged_in(page):
                    print_error("The attached browser is not signed in to Gemini.")
                    print_info("Sign in manually in that window, then run nanogen again.")
                    raise SystemExit(1)
                print_success("Using the already signed-in browser session.\n")
            else:
                if not check_gemini_logged_in(page):
                    guide_login(page, HEADLESS_MODE)
                print_success("Gemini session ready.\n")

        try:
            grant_clipboard(context)
        except Exception:
            pass

        for index, (prompt, filename) in enumerate(queue):
            if _INTERRUPTED[0]:
                print_warning("Stopped before '%s' because of an interrupt." % filename)
                break
            current, total = index + 1, len(queue)
            prefix = "[%03d/%03d]" % (current, total)
            print_progress_start(current, total, filename)
            if DEBUG_MODE:
                print()
                print_debug("--- %s: %s ---" % (prefix, filename))

            success = False
            attempts = 0
            failed_before = len(ERROR_LOG)
            while True:
                success = process_single_prompt(
                    page, context, prompt, filename, args.out,
                    add_prompt=args.add_prompt, insp_prompt=args.insp_prompt,
                    fmt_arg=args.fmt_arg, res_arg=args.res_arg,
                    resx_arg=args.resx_arg, resy_arg=args.resy_arg,
                    type_arg=args.type_arg, think_mode=args.think_arg,
                    min_gen_time=args.mingentime, dl_timeout_sec=args.dltime,
                    download_retries=args.dlret, progress_prefix=prefix)
                if success or attempts >= RETRY_COUNT:
                    break
                attempts += 1
                print_warning("\nRetrying '%s' (attempt %d/%d)..."
                              % (filename, attempts, RETRY_COUNT))
                time.sleep(5)

            if success and len(ERROR_LOG) > failed_before:
                # A retry recovered, so the earlier failures must not be counted.
                global FAIL_COUNT
                recovered = len(ERROR_LOG) - failed_before
                del ERROR_LOG[failed_before:]
                FAIL_COUNT = max(0, FAIL_COUNT - recovered)
                print_debug("Recovered on retry — discarded %d earlier failure(s)"
                             % recovered)

            if not success:
                print_progress_failed(current, total, filename, args.dlret)
                if DL_RESTART_MODE and (args._restart_idx or 0) < DL_RESTART_COUNT:
                    print_warning("Download failed after %d attempts — restarting the "
                                  "whole run (%d/%d) with --skip."
                                  % (args.dlret, (args._restart_idx or 0) + 1,
                                     DL_RESTART_COUNT))
                    restart_process(args)
                if DL_RESTART_MODE:
                    print_error("Restart limit (%d) reached, exiting." % DL_RESTART_COUNT)

            if index < len(queue) - 1 and not _INTERRUPTED[0]:
                if args.promptrnd:
                    try:
                        low, high = (int(part) for part in args.promptrnd.split(","))
                        delay_ms = random.randint(low, high)
                    except Exception:
                        delay_ms = 1000
                else:
                    delay_ms = args.promptint
                print_debug("Waiting %dms before the next prompt..." % delay_ms)
                time.sleep(delay_ms / 1000.0)

        print_summary()
    finally:
        close_browser(play, context, owned_browser)


def grant_clipboard(context):
    """Clipboard access needs an explicit grant, especially in headless mode."""
    for kwargs in ({}, {"origin": "https://gemini.google.com"}):
        try:
            context.grant_permissions(["clipboard-read", "clipboard-write"], **kwargs)
            return True
        except Exception:
            continue
    print_debug("Clipboard permissions unavailable, will fall back to text insertion")
    return False


def close_browser(play, context, owned):
    """Close a browser we started; leave an attached one alone."""
    try:
        if owned:
            context.close()
    except Exception:
        pass
    try:
        play.stop()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------
def nearest_writable_dir(path):
    """True when ``path`` or any of its ancestors exists and is writable.

    The profile directory usually does not exist yet, so testing only its
    immediate parent reports a perfectly usable location as unwritable.
    """
    current = os.path.abspath(path)
    while True:
        if os.path.isdir(current):
            return os.access(current, os.W_OK)
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def check_environment(args):
    """Report whether this machine can run nanogen."""
    print_info("Nanogen environment check")
    print_info("=" * 60)
    print_info("Platform        : %s (%s)" % (sys.platform, os.name))
    print_info("Python          : %s" % sys.version.split()[0])
    print_info("Script          : %s" % os.path.abspath(sys.argv[0]))

    try:
        import playwright

        print_info("playwright      : %s" % getattr(playwright, "__version__", "installed"))
    except ImportError:
        print_error("playwright      : MISSING  (pip install playwright)")
    else:
        bundled = playwright_browser_path()
        if bundled and os.path.exists(bundled):
            print_info("Browser build   : %s" % bundled)
        else:
            system = chromium_candidates()
            if system:
                print_info("Browser build   : not installed, system browser: %s" % system[0])
                print_info("                  (nanogen will use it automatically)")
            else:
                print_error("Browser build   : MISSING  (python %s --install-browser)"
                            % __PROGNAME__)

    for module, purpose in (("colorama", "colours"), ("PIL", "--stat resolution"),
                            ("requests", "HTTP download fallback")):
        try:
            __import__(module)
            print_info("%-15s : installed" % module)
        except ImportError:
            print_info("%-15s : missing (optional, needed for %s)"
                       % (module, "colours" if module == "colorama" else purpose))

    profile = args.user_data_dir or default_profile_dir()
    print_info("Profile dir     : %s%s"
               % (profile, "" if nearest_writable_dir(profile)
                  else "  [no writable parent directory]"))

    import socket

    for host, port in (("127.0.0.1", 9222), (args.host, args.port)):
        if port is None:
            continue
        probe = socket.socket()
        probe.settimeout(2)
        reachable = probe.connect_ex((host, port)) == 0
        probe.close()
        print_info("CDP %s:%-6d : %s" % (host, port,
                                         "reachable" if reachable else "closed"))

    if not IS_WINDOWS:
        print_info("Display         : DISPLAY=%s" % (os.environ.get("DISPLAY") or "(none)"))
    print_info("=" * 60)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
def build_parser():
    parser = argparse.ArgumentParser(
        description="Nanogen CLI v%s — headless Gemini Imagen generator "
                    "(works on Windows and Linux)" % __CODEVER__,
        add_help=False,
    )

    io_group = parser.add_argument_group("Input / Output")
    io_group.add_argument("--prompt", help="Single text prompt")
    io_group.add_argument("--addprompt", dest="add_prompt", help="Text appended to every prompt")
    io_group.add_argument("--insprompt", dest="insp_prompt", help="Text prepended to every prompt")
    io_group.add_argument("--in", dest="input_file", help="JSON file with prompts")
    io_group.add_argument("--out", default=".", help="Output directory (default: .)")
    io_group.add_argument("--outauto", action="store_true",
                          help="Output directory named after the input JSON file")

    gen_group = parser.add_argument_group("Generation")
    gen_group.add_argument("--promptint", type=int, default=1000,
                           help="Fixed delay between prompts in ms (default: 1000)")
    gen_group.add_argument("--promptrnd", help="Random delay range in ms: MIN,MAX")
    gen_group.add_argument("--type", dest="type_arg", type=str.lower,
                           choices=["fast", "think", "pro", "flash", "flash-lite"],
                           help="Model type: fast, think, pro, flash, flash-lite")
    gen_group.add_argument("--thinking", dest="think_arg", type=str.lower,
                           choices=["basic", "extended", "extend"],
                           help="Thinking level: basic or extended ('extend' is an alias)")
    gen_group.add_argument("--fmt", dest="fmt_arg", choices=["43", "169", "11"],
                           help="Aspect ratio: 43 (4:3), 169 (16:9), 11 (1:1)")
    gen_group.add_argument("--res", dest="res_arg",
                           help="Exact resolution WIDTH,HEIGHT (exclusive with --resx/--resy)")
    gen_group.add_argument("--resx", dest="resx_arg",
                           help="Width in px; height derived from --fmt (default 16:9)")
    gen_group.add_argument("--resy", dest="resy_arg",
                           help="Height in px; width derived from --fmt (default 16:9)")
    gen_group.add_argument("--gen", choices=["chat", "tmp", "native"], default="tmp",
                           help="Chat mode: tmp (default), chat, or native")

    run_group = parser.add_argument_group("Execution & Timing")
    run_group.add_argument("--simul", action="store_true",
                           help="Show what would be sent without launching a browser")
    run_group.add_argument("--overwrite", action="store_true",
                           help="Overwrite existing files without asking")
    run_group.add_argument("--skip", action="store_true",
                           help="Skip prompts whose output file already exists")
    run_group.add_argument("--noex", action="store_true",
                           help="Do not append .png to output filenames")
    run_group.add_argument("--retry", type=int, default=0,
                           help="Retries for a failed prompt (default: 0)")
    run_group.add_argument("--mingentime", type=int, default=30,
                           help="Minimum generation wait in seconds (default: 30)")
    run_group.add_argument("--dltime", type=int, default=45,
                           help="Download timeout in seconds (default: 45)")
    run_group.add_argument("--dlret", type=int, default=3,
                           help="Download retries (default: 3)")
    run_group.add_argument("--dlrestart", "--delrestart", dest="dlrestart", type=int,
                           default=0,
                           help="Restart the whole run on failure (default: 0, needs --skip)")
    run_group.add_argument("--limitwait", type=int, default=300,
                           help="Wait in seconds when Gemini rate-limits (default: 300)")
    run_group.add_argument("--_restart_idx", type=int, default=0, help=argparse.SUPPRESS)

    browser_group = parser.add_argument_group("Browser")
    browser_group.add_argument("--attach", action="store_true",
                               help="Attach to an already running browser over CDP")
    browser_group.add_argument("--host", default="localhost",
                               help="CDP host for --attach (default: localhost)")
    browser_group.add_argument("--port", type=int, default=9222,
                               help="CDP port for --attach (default: 9222)")
    browser_group.add_argument("--headless", action="store_true", default=True,
                               help=argparse.SUPPRESS)
    browser_group.add_argument("--no-headless", dest="headless", action="store_false",
                               help="Show the browser window (for first-time login)")
    browser_group.add_argument("--user-data-dir", dest="user_data_dir",
                               help="Persistent browser profile directory")
    browser_group.add_argument("--executable-path", dest="executable_path",
                               help="Use a specific Chrome/Chromium binary")
    browser_group.add_argument("--channel",
                               help="Playwright browser channel: chrome, msedge, chromium")
    browser_group.add_argument("--locale",
                               help="Browser locale, e.g. en-US, pl-PL (default: en-US)")
    browser_group.add_argument("--no-lock", dest="no_lock", action="store_true",
                               help="Do not take the single-instance lock")
    browser_group.add_argument("--lockwait", type=int, default=0,
                               help="Give up waiting for the lock after N seconds (0 = forever)")
    browser_group.add_argument("--install-browser", action="store_true",
                               help="Install the Playwright Chromium build and exit")
    browser_group.add_argument("--with-deps", action="store_true",
                               help="Also install system libraries (implies sudo, Linux only)")
    browser_group.add_argument("--check-env", action="store_true",
                               help="Report the environment and exit")

    login_group = parser.add_argument_group("Login (automated Google sign-in)")
    login_group.add_argument("--login", dest="login", help="Google account e-mail")
    login_group.add_argument("--logname", "--username", dest="login", help="Alias for --login")
    login_group.add_argument("--pwd", dest="pwd",
                             help="Google password (visible in the process list)")
    login_group.add_argument("--password", "--pass", dest="pwd",
                             help="Alias for --pwd")
    login_group.add_argument("--pwdsilent", action="store_true",
                             help="Prompt for the password without echoing it")
    login_group.add_argument("--loginfile", dest="loginfile",
                             help="File with login credentials (pairs: email pass or two lines)")

    display_group = parser.add_argument_group("Display")
    display_group.add_argument("--color", action="store_true", help="Colorized output")
    display_group.add_argument("--mono", action="store_true",
                               help="Force monochrome output (overrides --color)")
    display_group.add_argument("--debug", action="store_true", help="Verbose debug output")
    display_group.add_argument("--stat", action="store_true",
                               help="Show per-image timing, resolution and size")
    display_group.add_argument("--savescr", dest="save_screen_file", nargs="?", const="",
                               default=None, help="Save all console output to a file")
    display_group.add_argument("--log", dest="log_file", nargs="?", const="", default=None,
                               help="Save the execution log to a file")

    parser.add_argument("-h", action="store_true", help="Short help")
    parser.add_argument("--help", action="store_true", help="Detailed help")
    parser.add_argument("--version", action="store_true", help="Print the version and exit")
    return parser


def normalize_argv(argv):
    """Accept both -type and --type, the way nanogen.py does."""
    normalized = []
    for argument in argv:
        if (argument.startswith("-") and not argument.startswith("--")
                and len(argument) > 2 and not re.match(r"^-\d", argument)):
            normalized.append("-" + argument)
        else:
            normalized.append(argument)
    return normalized


def validate(args, parser):
    """Return an error message, or None when the arguments are consistent."""
    if sum(1 for value in (args.res_arg, args.resx_arg, args.resy_arg) if value) > 1:
        return "--res, --resx and --resy are mutually exclusive"
    if args.res_arg and args.fmt_arg:
        return "--res and --fmt are mutually exclusive (--res already sets the dimensions)"
    if args.prompt and args.input_file:
        return "--prompt and --in are mutually exclusive"
    if not args.prompt and not args.input_file:
        return "either --prompt or --in is required"
    if args.input_file and not os.path.exists(args.input_file):
        return "input file not found: %s" % args.input_file
    if args.outauto and not args.input_file:
        return "--outauto requires --in"
    if args.retry < 0:
        return "--retry must be zero or greater"
    if args.mingentime <= 0:
        return "--mingentime must be greater than zero"
    if args.dltime <= 0:
        return "--dltime must be greater than zero"
    if args.dlret < 0:
        return "--dlret must be zero or greater"
    if args.dlrestart < 0:
        return "--dlrestart must be zero or greater"
    if args.limitwait < 0:
        return "--limitwait must be zero or greater"
    if args.promptint < 0:
        return "--promptint must be zero or greater"
    if args.lockwait < 0:
        return "--lockwait must be zero or greater"
    if args.port <= 0 or args.port > 65535:
        return "--port must be between 1 and 65535"
    if args.res_arg:
        try:
            parts = args.res_arg.split(",")
            if len(parts) != 2:
                raise ValueError
            if int(parts[0]) <= 0 or int(parts[1]) <= 0:
                raise ValueError
        except ValueError:
            return "invalid --res value '%s', expected WIDTH,HEIGHT" % args.res_arg
    for name, value in (("--resx", args.resx_arg), ("--resy", args.resy_arg)):
        if value:
            try:
                if int(value) <= 0:
                    raise ValueError
            except ValueError:
                return "invalid %s value '%s', expected a positive integer" % (name, value)
    if args.promptrnd:
        try:
            parts = args.promptrnd.split(",")
            if len(parts) != 2:
                raise ValueError
            low, high = int(parts[0]), int(parts[1])
            if low < 0 or high < 0 or low > high:
                raise ValueError
        except ValueError:
            return ("invalid --promptrnd value '%s', expected MIN,MAX with MIN <= MAX"
                    % args.promptrnd)
    if args.dlrestart > 0 and not args.skip:
        return "--dlrestart requires --skip"
    if args.attach and args.user_data_dir:
        return "--attach cannot be combined with --user-data-dir (the profile belongs to the " \
               "browser you attach to)"
    if args.pwdsilent and not args.login:
        return "--pwdsilent requires --login EMAIL"
    if args.pwd and not args.login:
        return "--pwd requires --login EMAIL"
    if args.loginfile and (args.login or args.pwd or args.pwdsilent):
        return "--loginfile cannot be combined with --login, --pwd or --pwdsilent"
    if args.loginfile and not os.path.exists(args.loginfile):
        return "login file not found: %s" % args.loginfile
    return None


def print_short_help():
    print("Nanogen CLI  v%s  (%s)" % (__CODEVER__, __CODEDATE__))
    print("Author: %s" % __CODEAUTH__)
    print("Repo:   %s" % __CODEGIT__)
    print("")
    print("Usage: python %s --prompt TEXT [options]" % __PROGNAME__)
    print("       python %s --in FILE.json [options]" % __PROGNAME__)
    print("")
    print("Runs --help for the full option list, --check-env to verify this machine.")


def print_detailed_help():
    print("")
    print("Nanogen CLI  v%s  (%s)" % (__CODEVER__, __CODEDATE__))
    print("Author: %s  |  Repo: %s" % (__CODEAUTH__, __CODEGIT__))
    print("")
    print("=" * 72)
    print("INPUT / OUTPUT")
    print("  --prompt TEXT        Single text prompt")
    print("  --in FILE            JSON file with prompts")
    print("                       object form:  {\"name\": \"prompt\", ...}")
    print("                       list form:    [{\"filename\": \"n\", \"prompt\": \"p\"}, ...]")
    print("  --out DIR            Output directory (default: .)")
    print("  --outauto            Output directory named after the input JSON file")
    print("")
    print("PROMPT MODIFIERS")
    print("  --insprompt TEXT     Prepended to every prompt")
    print("  --addprompt TEXT     Appended to every prompt")
    print("")
    print("GENERATION")
    print("  --fmt FMT            Aspect ratio: 43 (4:3), 169 (16:9), 11 (1:1)")
    print("  --res W,H            Exact resolution (exclusive with --resx/--resy)")
    print("  --resx W             Width in px, height derived from --fmt")
    print("  --resy H             Height in px, width derived from --fmt")
    print("  --type TYPE          Model: fast, think, pro, flash, flash-lite")
    print("  --thinking MODE      Thinking level: basic, extended")
    print("  --gen MODE           Chat: tmp (default), chat, native")
    print("")
    print("EXECUTION & TIMING")
    print("  --promptint MS       Delay between prompts (default: 1000)")
    print("  --promptrnd MIN,MAX  Random delay range in ms")
    print("  --simul              Show the composed prompts without using a browser")
    print("  --overwrite          Overwrite existing files without asking")
    print("  --skip               Skip prompts whose output file already exists")
    print("  --noex               Do not append .png to output filenames")
    print("  --retry N            Retries for a failed prompt (default: 0)")
    print("  --mingentime N       Minimum generation wait in s (default: 30)")
    print("  --dltime N           Download timeout in s (default: 45)")
    print("  --dlret N            Download retries (default: 3)")
    print("  --dlrestart N        Restart the whole run on failure (needs --skip)")
    print("  --limitwait N        Wait in s when Gemini rate-limits (default: 300)")
    print("")
    print("BROWSER")
    print("  --attach             Attach to a running browser over CDP instead of")
    print("                       launching one (use with start_nanogen_debug.sh/.bat)")
    print("  --host HOST          CDP host for --attach (default: localhost)")
    print("  --port PORT          CDP port for --attach (default: 9222)")
    print("  --no-headless        Show the browser window (first-time login)")
    print("  --user-data-dir DIR  Persistent profile (default: %s)" % default_profile_dir())
    print("  --executable-path P  Use a specific Chrome/Chromium binary")
    print("  --channel NAME       Playwright channel: chrome, msedge, chromium")
    print("  --locale LOCALE      Browser locale, e.g. en-US or pl-PL")
    print("  --no-lock            Skip the single-instance lock")
    print("  --lockwait N         Give up waiting for the lock after N s (0 = forever)")
    print("  --install-browser    Install the Playwright Chromium build and exit")
    print("  --with-deps          Also install system libraries (Linux, needs sudo)")
    print("  --check-env          Report the environment and exit")
    print("")
    print("LOGIN (automated Google sign-in)")
    print("  --login EMAIL        Google account e-mail")
    print("  --pwd PASSWORD       Password (visible in the process list)")
    print("  --pwdsilent          Prompt for the password without echoing it")
    print("  --logname/--login     Google account e-mail")
    print("  --pwd/--password      Password")
    print("  --pwdsilent           Prompt for the password without echoing it")
    print("  --loginfile FILE      File with credentials (pairs: email + password per account)")

    print("")
    print("DISPLAY")
    print("  --color              Colorized output")
    print("  --mono               Force monochrome output")
    print("  --debug              Verbose debug output")
    print("  --stat               Show per-image timing, resolution and size")
    print("  --savescr [FILE]     Save all console output to a file")
    print("  --log [FILE]         Save the execution log to a file")
    print("")
    print("EXAMPLES")
    print('  python %s --prompt "Mountain landscape" --color' % __PROGNAME__)
    print('  python %s --in prompts.json --type pro --thinking extended --stat' % __PROGNAME__)
    print("  python %s --in prompts.json --skip --dlrestart 3 --log --color" % __PROGNAME__)
    print("  python %s --prompt test --login user@gmail.com --pwdsilent" % __PROGNAME__)
    print("  python %s --in prompts.json --attach --color" % __PROGNAME__)
    print("  python %s --install-browser" % __PROGNAME__)
    print("")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    global USE_COLOR, DEBUG_MODE, OVERWRITE_MODE, SKIP_MODE, STAT_MODE, SIMUL_MODE
    global HEADLESS_MODE, CHAT_MODE, NOEX_MODE, RETRY_COUNT, MIN_GEN_TIME
    global LIMIT_WAIT_TIME, LOG_FILE, ATTACH_MODE, DL_RESTART_MODE, DL_RESTART_COUNT
    global LOCK_WAIT_TIME, INTERACTIVE

    sys.argv[1:] = normalize_argv(sys.argv[1:])

    parser = build_parser()
    args, unknown = parser.parse_known_args()

    if args.install_browser:
        install_browsers(args.with_deps)
        return 0

    if unknown:
        print_error("Unknown arguments: %s" % ", ".join(unknown))
        print_info("Check the spelling or run --help.")
        return 1

    USE_COLOR = args.color and not args.mono
    if args.mono or not HAS_COLORAMA:
        USE_COLOR = False

    if args.version:
        print("Nanogen CLI %s (%s) — %s" % (__CODEVER__, __CODEDATE__, __CODEAUTH__))
        return 0

    if args.h:
        print_short_help()
        return 0

    if args.help:
        print_detailed_help()
        return 0

    if args.check_env:
        check_environment(args)
        return 0

    if args.h or args.help:
        return 0

    problem = validate(args, parser)
    if problem:
        print_error("Error: %s" % problem)
        print_info("Run --help for usage.")
        return 1

    DEBUG_MODE = args.debug
    OVERWRITE_MODE = args.overwrite
    SKIP_MODE = args.skip
    STAT_MODE = args.stat
    SIMUL_MODE = args.simul
    HEADLESS_MODE = args.headless
    CHAT_MODE = args.gen
    NOEX_MODE = args.noex
    RETRY_COUNT = args.retry
    MIN_GEN_TIME = args.mingentime
    LIMIT_WAIT_TIME = args.limitwait
    ATTACH_MODE = args.attach
    DL_RESTART_MODE = args.dlrestart > 0
    DL_RESTART_COUNT = args.dlrestart
    LOCK_WAIT_TIME = args.lockwait
    INTERACTIVE = sys.stdin is not None and sys.stdin.isatty()

    if args.outauto:
        args.out = os.path.splitext(os.path.basename(args.input_file))[0]
        print_info("Output directory set automatically: %s" % args.out)

    if not os.path.exists(args.out):
        try:
            os.makedirs(args.out)
            print_info("Created output directory: %s" % args.out)
        except Exception as exc:
            print_error("Cannot create the output directory '%s': %s" % (args.out, exc))
            return 1

    if args.save_screen_file is not None:
        if args.save_screen_file == "":
            if args.input_file:
                base = os.path.splitext(os.path.basename(args.input_file))[0]
                args.save_screen_file = base + ".scr"
            else:
                args.save_screen_file = "nanogen.scr"
        try:
            sys.stdout = Logger(args.save_screen_file)
            sys.stderr = sys.stdout
        except Exception as exc:
            print_error("Cannot open the screen log '%s': %s" % (args.save_screen_file, exc))
            return 1
        print_info("Saving console output to: %s" % args.save_screen_file)

    if args.log_file is not None:
        if args.log_file == "":
            if args.input_file:
                base = os.path.splitext(os.path.basename(args.input_file))[0]
                LOG_FILE = base + ".log"
            else:
                LOG_FILE = "nanogen.log"
        else:
            LOG_FILE = args.log_file

    if not args.no_lock:
        if ATTACH_MODE:
            target = "cdp://%s:%d" % (args.host, args.port)
        else:
            target = os.path.abspath(args.user_data_dir or default_profile_dir())
        acquire_lock(target)
    else:
        print_debug("Single-instance lock disabled")

    install_signal_handlers()

    try:
        run_gemini_session(args)
    except KeyboardInterrupt:
        print()
        print_warning("Interrupted by the user.")
        write_error_log()
        write_log_file()
        release_lock()
        return 130
    release_lock()
    return 0


if __name__ == "__main__":
    sys.exit(main())
