# Nanogen — Gemini Imagen Generator Automator

**Author:** Igor Brzezek  
**Version:** 0.0.17 (2026-06-04)  
**Repository:** [github.com/igorbrzezek/nanogen](https://github.com/igorbrzezek/nanogen)

---

## Overview

Nanogen is a Python automation tool that connects to **Google Gemini** via the **Chrome DevTools Protocol (CDP)** and automates image generation using **Imagen** (Gemini's built-in image generation model). It reads prompts from the command line or a JSON file, sends them one by one to Gemini's web interface, waits for the generated image to appear, and downloads it — all without requiring any official API key.

The tool is designed for batch image generation workflows where you have a large number of prompts and want to automate the repetitive process of typing each prompt into Gemini and manually saving the output.

---

## Files in this project

Project files:
- nanogen.py
- README_nanogen.md
- start_chrome_debug.bat

Help & Example Files
- example.json
- example_image_prompt.md
- img_prompt_example.txt
- page.cmd
- page.html
- page.json
- style.css

## How It Works

1. **Chrome** is launched in remote debugging mode on port `9222` (via `start_chrome_debug.bat`), already pointed at `https://gemini.google.com/`.
2. **Nanogen** connects to that Chrome instance over CDP using **Playwright**.
3. For each prompt, the script:
   - Navigates to Google Gemini (or uses the existing tab).
   - Expands the left sidebar menu if needed.
   - Optionally enables **Temporary Chat** mode (`--gen tmp`) so that each prompt starts in a fresh temporary conversation, keeping the chat history clean.
   - Optionally selects a specific **Gemini model** (`--type`) and **thinking mode** (`--thinking`).
   - Opens the **Tools** menu and clicks "Create images" to enter Imagen mode.
   - Appends or prepends modifier text to the prompt (`--addprompt` / `--insprompt`).
   - Appends aspect ratio and resolution instructions (`--fmt`, `--res`, `--resx`, `--resy`).
   - Pastes the final prompt into Gemini's input field and sends it.
   - Waits for the generated image and the download button to appear.
   - Downloads the image at full resolution via the browser's built-in download mechanism.
   - Falls back to **canvas extraction** or **JavaScript fetch** if the download button is unavailable.
4. A detailed execution log can be saved (`--log`), and full console output can be captured (`--savescr`).
5. A lock file (`nanogen.lock`) prevents multiple instances from running simultaneously, avoiding browser conflicts.

---

## Key Features

- **Batch processing** from JSON prompt files or single prompts.
- **Model selection** — pick between Flash, Thinking, Pro, or Flash-Lite variants.
- **Thinking mode** — toggle basic or extended thinking on supported models.
- **Aspect ratio & resolution control** — set `--fmt` (4:3, 16:9, 1:1) or exact pixel dimensions with `--res`, `--resx`, `--resy`.
- **Prompt modifiers** — prepend (`--insprompt`) or append (`--addprompt`) text to every prompt.
- **Temporary chat mode** (`--gen tmp`) — each prompt starts in a fresh temporary conversation, preventing context pollution.
- **Native mode** (`--gen native`) — operate in the current Gemini context without any navigation or chat setup.
- **Simulation mode** (`--simul`) — preview what prompts would be sent without connecting to a browser.
- **Download retry** (`--dlret`) and **full restart on failure** (`--dlrestart`) for resilience.
- **Gitignore-style skip** (`--skip`) — automatically skip prompts whose output filename already exists.
- **Overwrite confirmation** (`--overwrite`) or auto-overwrite for repeat runs.
- **Statistics** (`--stat`) — per-image timing, resolution, and file size.
- **Screen logging** and **execution logging** to files.
- **Single-instance lock** prevents concurrent runs.

---

## Requirements

- **Python 3.8+**
- **Google Chrome** or **Chromium** (must be installed on the system)
- Python packages (install via `pip install -r requirements.txt`):
  - `playwright` — browser automation
  - `colorama` — colored terminal output
  - `Pillow (PIL)` — image dimension checking (for `--stat`)
  - `requests` — fallback image download

---

## Usage

### 1. Start Chrome in Debug Mode

Before running Nanogen, Chrome must be started with remote debugging enabled. Use the provided batch script:

```
start_chrome_debug.bat
```

This script:
- **Kills ALL running Chrome instances** — any open Chrome windows (including those with important tabs) will be forcefully closed without warning. Save your work first.
- Starts Chrome on port **9222** with a dedicated user data directory (`C:\chrome_dev_profile`).
- Opens `https://gemini.google.com/` so you can log in to your Google account.

> **⚠ CRITICAL:** The batch script terminates **every** `chrome.exe` process on your machine. All your Chrome windows with unsaved work, forms, or important tabs will be **lost**. Close other browsers or save everything before running.
>
> After the script starts Chrome, **do not open any additional Chrome windows or other browsers** — they may interfere with the CDP connection or occupy port 9222. Keep this single Chrome window open and logged into Gemini throughout the entire Nanogen session.
>
> On the first run, you must log into your Google account in the opened Chrome window and verify that Gemini is accessible. Keep the Chrome window open while Nanogen is running.

### 2. Basic Commands

**Single prompt:**
```
python nanogen.py --prompt "A majestic mountain landscape at sunset" --color
```

**Batch from JSON file:**
```
python nanogen.py --in prompts.json --out generated_images --color
```

**JSON format (dict style):**
```json
{
  "mountains": "A majestic mountain landscape at sunset, hyper-realistic",
  "ocean": "A serene ocean view with gentle waves, cinematic lighting"
}
```

**JSON format (list style):**
```json
[
  {
    "filename": "mountains",
    "prompt": "A majestic mountain landscape at sunset, hyper-realistic"
  },
  {
    "filename": "ocean",
    "prompt": "A serene ocean view with gentle waves, cinematic lighting"
  }
]
```

### 3. Command-Line Options

#### Input / Output

| Option | Description |
|---|---|
| `--prompt TEXT` | Single text prompt for image generation. |
| `--in FILE` | JSON file with prompts (filename as key or in `{ "filename", "prompt" }` list format). |
| `--out DIR` | Output directory for generated images (default: current directory). |
| `--outauto` | Automatically create output directory named after the input JSON file (without extension). Requires `--in`. |

#### Prompt Modifiers

| Option | Description |
|---|---|
| `--addprompt TEXT` | Text to append to **every** prompt (e.g., "Hyper realistic, 8K"). |
| `--insprompt TEXT` | Text to prepend to **every** prompt (e.g., "Generate an image of"). |

#### Generation Options

| Option | Description |
|---|---|
| `--fmt FMT` | Aspect ratio: `43` (4:3), `169` (16:9), or `11` (1:1). |
| `--res WIDTH,HEIGHT` | Exact pixel resolution (e.g., `1920,1080`). Mutually exclusive with `--resx` and `--resy`. |
| `--resx WIDTH` | Width in pixels; height auto-calculated from `--fmt` (default 16:9). |
| `--resy HEIGHT` | Height in pixels; width auto-calculated from `--fmt` (default 16:9). |
| `--type TYPE` | Model: `fast`, `think`, `pro`, `flash`, `flash-lite`. |
| `--thinking MODE` | Thinking mode: `basic` or `extended`. |
| `--gen MODE` | Chat mode: `chat` (standard, default), `tmp` (temporary), or `native` (use current context without changes). |

#### Execution & Timing

| Option | Description |
|---|---|
| `--promptint MS` | Fixed delay between prompts in milliseconds (default: 1000). |
| `--promptrnd MIN,MAX` | Random delay range in milliseconds (e.g., `2000,5000`). Overrides `--promptint`. |
| `--simul` | Simulation mode — show what would be generated without connecting to a browser. |
| `--overwrite` | Overwrite existing files without prompting. |
| `--skip` | Automatically skip prompts whose output file already exists. |
| `--noex` | Do not append `.png` extension to output filenames. |
| `--retry N` | Number of retry attempts on generation failure (default: 0). |
| `--mingentime N` | Minimum wait time in seconds for image generation (default: 30). |
| `--dltime N` | Timeout in seconds for the download action (default: 45). |
| `--dlret N` | Number of download retry attempts (default: 3). |
| `--dlrestart N` | Restart the entire generation process on failure (default: 0, requires `--skip`). |
| `--limitwait N` | Wait time in seconds when Gemini rate limit is hit (default: 300). |

#### Connection

| Option | Description |
|---|---|
| `--host HOST` | Chrome DevTools Protocol host (default: `localhost`). |
| `--port PORT` | Chrome DevTools Protocol port (default: `9222`). |

#### Display

| Option | Description |
|---|---|
| `--color` | Enable colorized terminal output. |
| `--debug` | Enable verbose debug output. |
| `--stat` | Show per-image statistics (generation time, resolution, file size in KiB). |
| `--savescr [FILE]` | Save all console output to a file. If filename is omitted, derives from `--in` (`.scr`) or uses `nanogen.scr`. |
| `--log [FILE]` | Save an execution log with image list and summary statistics. If filename is omitted, derives from `--in` (`.log`) or uses `nanogen.log`. |

### 4. Examples

```bash
# Basic single image with color output
python nanogen.py --prompt "A cyberpunk city at night" --color

# Batch processing with aspect ratio and statistics
python nanogen.py --in prompts.json --type think --fmt 169 --res 1920,1080 --stat --color

# Temporary chat mode with Thinking model
python nanogen.py --prompt "Fantasy dragon" --gen tmp --type think --thinking extended --color

# Width-defined resolution with 4:3 aspect ratio
python nanogen.py --prompt "Portrait of a robot" --type fast --fmt 43 --resx 1024 --color

# Simulation mode to preview prompts
python nanogen.py --in prompts.json --type pro --fmt 11 --simul

# With random delays and debug output
python nanogen.py --in prompts.json --promptrnd 2000,5000 --debug --color

# Production batch: skip existing, restart on failure, log everything
python nanogen.py --in big_batch.json --out output --skip --dlrestart 3 --log --savescr --stat --color
```

---

## File: `start_chrome_debug.bat`

### Purpose

This batch script prepares the environment for Nanogen by launching Google Chrome with **remote debugging enabled**.

### What It Does

1. **Locates Chrome** — checks both standard installation paths:
   - `C:\Program Files\Google\Chrome\Application\chrome.exe`
   - `C:\Program Files (x86)\Google\Chrome\Application\chrome.exe`

2. **Terminates ALL Chrome processes** — runs `taskkill /F /IM chrome.exe /T`, which forcefully closes **every** `chrome.exe` process on the system, including all windows with any open tabs. Any unsaved work in other Chrome windows will be lost.

3. **Launches Chrome with remote debugging flags:**
   - `--remote-debugging-port=9222` — enables the Chrome DevTools Protocol on port 9222, allowing Nanogen (via Playwright) to connect and control the browser.
   - `--user-data-dir="C:\chrome_dev_profile"` — uses a dedicated profile directory so that the automation session is isolated from your regular browsing profile.
   - Opens `https://gemini.google.com/` as the initial page.

4. **Pauses** with a prompt so you can confirm Chrome started successfully before running the Python script.

### Why This Is Needed

Google Gemini's image generation (Imagen) is only accessible through the web interface, not via a public API. Nanogen controls the browser directly through CDP — the same protocol Chrome DevTools uses. The `--remote-debugging-port=9222` flag is what makes this possible.

Without this script you would need to start Chrome manually with the same flags. The script automates this to ensure a clean, repeatable environment every time.

### Important Notes

- **Run `start_chrome_debug.bat` first, then `python nanogen.py`.** The batch script must finish launching Chrome before you run the Python script — do not run them simultaneously.
- **Do not open any other Chrome windows or browsers** while Nanogen is running. A second Chrome instance could conflict with the CDP connection or occupy port 9222.
- You need to **log into your Google account** in the Chrome window that opens, and verify that Gemini is accessible, before running Nanogen.
- Keep the Chrome window **open** and **visible** while Nanogen is running. Do not close or minimize it to a state that might throttle it.
- The dedicated user data directory (`C:\chrome_dev_profile`) is created automatically on first run and reused on subsequent runs (so your login persists across sessions).

---

## Output Files

Generated images are saved to the specified output directory (or the current directory by default). The filename is either:
- The key from the JSON file (if using `--in` with dict format).
- The `filename` field (if using `--in` with list format).
- A truncated prompt + timestamp (if using `--prompt` directly).

By default, the `.png` extension is appended unless `--noex` is used or the filename already has a supported image extension (`.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`, `.bmp`).

## Example 'simple' prompt for generate .json file
- Example file: img_prompt_example.txt
- Put in directory, run Your AI
- Write in Your AI: "Execute the commands written in the file: img_prompt_example.txt"

### Auxiliary Files

| File | Description |
|---|---|
| `nanogen.lock` | Lock file preventing multiple concurrent instances. Automatically removed on exit. |
| `nanogen.err` | Error log — written when any download fails, appended on each run. |
| `<name>.log` | Execution log — list of all generated/skipped files with timing and statistics (when `--log` is used). |
| `<name>.scr` | Full console output capture (when `--savescr` is used). |

---

## Error Handling & Resilience

- **Rate limiting** — when Gemini returns a rate-limit error, Nanogen waits (`--limitwait`, default 300s) and retries up to 3 times.
- **Download retries** — if the download button click fails or produces a zero-byte file, the script retries the download (configurable via `--dlret`).
- **Full restart** — with `--dlrestart`, if a download fails after all retries, the entire script is re-executed with `--skip` so already-successful images are not regenerated.
- **Graceful fallbacks** — if the download button is not found, the script falls back to canvas-based image extraction, then to direct image URL fetch.
- **Navigation errors** — the script handles `ERR_ABORTED` and other navigation issues gracefully with retries.
- **Lock file** — prevents concurrent runs that would interfere with each other's browser session.

---

## Code Structure

- **`nanogen.py`** — the main script (single file, ~2355 lines).
  - `Logger` class — captures terminal output and strips ANSI codes for clean file logging.
  - `acquire_lock()` / `release_lock()` — single-instance enforcement.
  - `print_*()` — colored output helpers.
  - `process_single_prompt()` — the core function handling the entire Gemini interaction for one prompt: navigation, model selection, prompt entry, image waiting, and download.
  - `run_gemini_session()` — orchestrator that loads prompts, connects to Chrome via Playwright CDP, and loops through the queue.
  - CLI argument parsing and validation (block at bottom).

- **`start_chrome_debug.bat`** — Chrome launcher for CDP remote debugging.

---

## Multi-generator for images

- In file 'page.cmd' You have an example of script to generate multipple images for multiple json files

---

## License

This project is open source. See the repository at [github.com/igorbrzezek/nanogen](https://github.com/igorbrzezek/nanogen) for license information.
