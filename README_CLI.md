# Nanogen CLI v0.0.1 — Headless Gemini / Imagen Generator

**Author:** Igor Brzezek  
**Version:** 0.0.1 (09.07.2026)  
**Repository:** <https://github.com/igorbrzezek/nanogen>  
**Platform:** Linux (no X11 / Wayland / GUI required — works over SSH)

---

## What It Is

Nanogen CLI is a **pure-command-line tool** that automatically generates images using Google Gemini's built-in image generator (Imagen). You give it a text prompt (or a JSON file with many prompts), and it:

1. Launches a **headless Chromium** browser via Playwright.
2. Connects to **gemini.google.com** and uses the web interface exactly as a human would.
3. Types each prompt, waits for Gemini to generate the image, and downloads it to your disk.
4. Supports **batch processing** — hundreds of prompts, one after another.

No official Google API key is needed. No graphical display server is needed. It works on any Linux server, including over SSH.

---

## What It Can Do

| Capability | Details |
|------------|---------|
| **Single prompt** | Generate one image from a command-line `--prompt "..."` argument |
| **Batch from JSON** | Generate many images from a JSON file (`--in prompts.json`) |
| **Model selection** | Choose Gemini model: `fast`, `think`, `pro`, `flash`, `flash-lite` |
| **Thinking mode** | Toggle Gemini thinking: `basic` or `extended` |
| **Aspect ratio** | Set 4:3, 16:9, or 1:1 via `--fmt` |
| **Exact resolution** | Specify pixel dimensions via `--res W,H`, `--resx W`, or `--resy H` |
| **Prompt modifiers** | Prepend (`--insprompt`) or append (`--addprompt`) text to every prompt |
| **Chat mode** | `tmp` — fresh temporary chat per prompt (default, recommended), `chat` — regular new chat, `native` — reuse current tab as-is |
| **Skip existing** | `--skip` — skip files that already exist (safe for resume) |
| **Overwrite control** | `--overwrite` — auto-overwrite without asking |
| **Download retries** | `--dlret N` — retry download up to N times (default 3) |
| **Rate-limit handling** | Detects Gemini "limit reached" messages, waits (default 300s), retries up to 3 times |
| **Three download methods** | 1) Official download button, 2) Canvas extraction, 3) JavaScript fetch — automatic fallback |
| **Per-image statistics** | `--stat` — shows generation time, resolution, file size for each image |
| **Debug output** | `--debug` — verbose logging of every browser action |
| **Execution log** | `--log [FILE]` — saves a list of all generated files with timing |
| **Screen capture** | `--savescr [FILE]` — saves all terminal output to a file |
| **Session persistence** | Login is stored in `~/.config/nanogen/chrome_profile/` and reused across runs |
| **Auto directory** | `--outauto` — creates output dir named after the JSON input file |
| **Random delays** | `--promptrnd MIN,MAX` — random delay between prompts to avoid rate limits |

---

## Requirements

### System prerequisites
- **Python 3.8 or newer**
- **Linux** (any distribution with glibc; also works on macOS and WSL2)
- **No graphical environment needed** — the tool runs in headless mode by default

### Python packages
```bash
pip3 install playwright colorama Pillow
```

| Package | Purpose |
|---------|---------|
| `playwright` | Browser automation — launches and controls Chromium |
| `colorama` | Colored terminal output (optional, degrades gracefully) |
| `Pillow` | Image dimension checking for `--stat` (optional, degrades gracefully) |

---

## Installation (Step by Step)

### 1. Install Python dependencies
```bash
pip3 install playwright colorama Pillow
```

### 2. Install Chromium browser
Run the built-in installer (recommended — handles everything):
```bash
python3 nanogen_cli.py --install-browser
```

If that fails with a `sudo` password prompt (common on servers), install system dependencies first:

**Debian / Ubuntu / WSL:**
```bash
sudo apt update
sudo apt install -y libnss3 libnspr4 libatk-bridge2.0-0 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 libatspi2.0-0
```

**Fedora / RHEL / CentOS:**
```bash
sudo dnf install -y nss nspr atk at-spi2-atk libdrm libxkbcommon libXcomposite libXdamage libXrandr mesa-libgbm pango cairo alsa-lib
```

**Arch Linux:**
```bash
sudo pacman -S nss nspr atk at-spi2-atk libdrm libxkbcommon libxcomposite libxdamage libxrandr mesa gbm pango cairo alsa-lib
```

Then install Chromium:
```bash
playwright install chromium
```

### 3. Verify everything works
```bash
python3 nanogen_cli.py --help
```
You should see the full help page with all options.

---

## Important: Every prompt must start with "Generate image"

Due to changes in Gemini's UI, the script can no longer click the "Create images" toolbar button automatically.  
**Every prompt MUST begin with the words "Generate image"** (or "Generate an image", "Create image", etc.) for Gemini to activate image generation mode.

Examples of correctly formatted prompts:
```bash
--prompt "Generate image of a majestic mountain landscape at sunset"
--prompt "Generate image: a cat sitting on a mat, cinematic lighting"
--prompt "Generate image of a cyberpunk city at night with neon lights"
```

If your prompt does not start with "Generate image", Gemini will treat it as a text-only prompt and no image will be produced.

**With `--insprompt`:** you can automate this — always prepend "Generate image" to every prompt:
```bash
python3 nanogen_cli.py --in prompts.json --insprompt "Generate image" --color
```
Your JSON prompts can then be plain descriptions (e.g., `"a cat on a mat"`) and the script will send `"Generate image: a cat on a mat"`.

---

## Quick Start Guide

### Step 1 — Set up the Chrome profile (first time only)

Choose one of the following methods:

**Option A — One-time manual login via browser window (recommended):**
```bash
python3 nanogen_cli.py --login your.email@gmail.com --pwdsilent --prompt "Generate image: test" --no-headless --color
```
1. A Chromium window will open. **Wait — the script will fill in your email and password automatically.**
2. **If Google says "This browser may not be secure"** (common for automated browsers): click the **"Try again"** button in the window, or select your account from the picker.
3. Complete any 2FA or phone verification if prompted.
4. Once Gemini loads, the browser will close.
5. Your session is now saved in `~/.config/nanogen/chrome_profile/`.

After this, **all subsequent runs work fully headless** with no login prompts:
```bash
python3 nanogen_cli.py --prompt "Generate image: twoje hasło" --color
```

> **Jak odświeżyć sesję po wygaśnięciu:** sesja Google trwa ~2 dni. Gdy skrypt zgłosi "Not logged into Google Gemini", uruchom z `--no-headless`:
> ```bash
> python3 nanogen_cli.py --prompt "Generate image: test" --no-headless --color
> ```
> W oknie Chromium kliknij swój profil i zaloguj się. Zamknij okno. Kolejne uruchomienia znów będą headless.

**Option B — Automated login (headless, no GUI needed, may fail on some Google accounts):**
```bash
python3 nanogen_cli.py --login your.email@gmail.com --pwdsilent --prompt "Generate image: test" --color
```
The script tries to log into Google automatically. Google may block with "This browser may not be secure" — in that case use Option A.

**Option C — Full manual setup (if everything fails):**
```bash
python3 nanogen_cli.py --prompt "Generate image: test" --no-headless
```
A Chromium window appears. Manually go to `gemini.google.com`, log in, close window.

**Resetting the profile:**
```bash
rm -rf ~/.config/nanogen/chrome_profile
```
Then repeat Step 1.

### Step 2 — Generate a single image (headless)

```bash
python3 nanogen_cli.py --prompt "Generate image: A majestic mountain landscape at sunset, hyper-realistic" --color
```

The script will:
1. Launch Chromium in headless mode.
2. Load your saved session (no login needed).
3. Open a temporary chat on Gemini.
4. Send the prompt (must start with "Generate image").
5. Wait for the image to generate.
6. Download it to the current directory as a timestamped PNG file.

### Step 3 — Generate from a JSON batch file

Create a file called `prompts.json`:
```json
{
  "mountains": "A majestic mountain landscape at sunset, hyper-realistic, 8K",
  "ocean": "A serene ocean view with gentle waves, cinematic lighting",
  "city": "A cyberpunk city at night with neon lights, rain, detailed"
}
```

Run (auto-prepend "Generate image" to every prompt):
```bash
python3 nanogen_cli.py --in prompts.json --out generated --insprompt "Generate image" --color --stat
```

The script will:
1. Load all 3 prompts from the file.
2. Prepend "Generate image" to each one.
3. Process them one by one.
4. Save each image to the `generated/` directory.
5. Show per-image statistics (time, resolution, file size).
6. Print a summary at the end.

---

## JSON File Format

### Dictionary format (recommended — simple)
Keys become filenames, values are prompts:
```json
{
  "mountains": "A majestic mountain landscape at sunset",
  "ocean": "A serene ocean view with gentle waves",
  "city": "A cyberpunk city at night with neon lights"
}
```

### List format (when you also need other metadata)
```json
[
  {
    "filename": "mountains",
    "prompt": "A majestic mountain landscape at sunset"
  },
  {
    "filename": "ocean",
    "prompt": "A serene ocean view with gentle waves"
  }
]
```

---

## Complete CLI Option Reference

### Input / Output

| Option | Description |
|--------|-------------|
| `--prompt TEXT` | Single text prompt. Example: `--prompt "A cat sitting on a mat"` |
| `--in FILE` | JSON file with prompts. Supports both dict and list formats (see above) |
| `--out DIR` | Output directory. Default: current directory. Created automatically if missing |
| `--outauto` | Automatically name the output directory after the input JSON file (without `.json`). Requires `--in` |

### Prompt Modifiers

| Option | Description |
|--------|-------------|
| `--insprompt TEXT` | Text **prepended** to every prompt. Use this to always start with "Generate image". Example: `--insprompt "Generate image"` → final prompt: `"Generate image: A cat..."` |
| `--addprompt TEXT` | Text **appended** to every prompt. Example: `--addprompt "Cinematic, 8K"` → final: `"A cat.... Cinematic, 8K"` |

**Note:** Because Gemini's "Create images" toolbar button is no longer clickable by this script, every prompt must result in text starting with "Generate image" (or equivalent). Either:
- Start every prompt in your JSON/CLI with `"Generate image: ..."`, or
- Use `--insprompt "Generate image"` to auto-prepend.

### Generation Control

| Option | Description |
|--------|-------------|
| `--fmt FMT` | Aspect ratio inserted into the prompt as a natural-language instruction. Values: `43` (4:3), `169` (16:9), `11` (1:1) |
| `--res W,H` | Exact pixel resolution added as `--size WxH` to the prompt. Example: `--res 1920,1080`. Mutually exclusive with `--resx` and `--resy` |
| `--resx W` | Width in pixels. Height calculated from `--fmt` (default 16:9). Example: `--resx 1024 --fmt 43` → `1024x768` |
| `--resy H` | Height in pixels. Width calculated from `--fmt` (default 16:9). Example: `--resy 720` → `1280x720` |
| `--type TYPE` | Gemini model to use. Choices: `fast`, `think`, `pro`, `flash`, `flash-lite`. The script opens the model selector dropdown and clicks the matching entry |
| `--thinking MODE` | Thinking depth. Choices: `basic` (standard), `extended` (deep thinking). Only effective on models that support it |
| `--gen MODE` | Chat startup mode. `tmp` (default) — creates a temporary chat per prompt; `chat` — creates a regular new chat; `native` — does not navigate, uses whatever tab is open |
| `--promptint MS` | Fixed delay between prompts in milliseconds. Default: 1000 (1 second) |
| `--promptrnd MIN,MAX` | Random delay in milliseconds. Example: `--promptrnd 2000,5000` → waits between 2 and 5 seconds. Overrides `--promptint` |
| `--overwrite` | If the output file already exists, overwrite it without asking |
| `--skip` | If the output file already exists (and is larger than 256 bytes for PNG or 1024 bytes for JPG), skip it. Useful for resuming interrupted batches |
| `--noex` | Do not automatically append `.png` extension to filenames |
| `--mingentime N` | Minimum wait for image generation in seconds before checking for the download button. Default: 30. Increase for complex prompts. |
| `--dltime N` | Download timeout in seconds. Default: 45. If the download takes longer, it fails and retries |
| `--dlret N` | Number of times to retry the download click if it fails or produces a zero-byte file. Default: 3 |
| `--limitwait N` | Seconds to wait when Gemini returns a rate-limit error. Default: 300 (5 minutes). After the wait, retries up to 3 times total |

### Browser Options

| Option | Description |
|--------|-------------|
| `--no-headless` | Show the Chromium GUI window. Use this for: (a) first-time login, (b) debugging what the browser sees |
| `--user-data-dir DIR` | Path to a Chrome profile directory. Default: `~/.config/nanogen/chrome_profile`. Use this to share an existing Chrome profile |
| `--install-browser` | Install Playwright's Chromium browser and all system dependencies, then exit. Does nothing else |

### Session persistence (recommended workflow)

Once you have logged in manually at least once (Step 1 above), the session cookies are stored in the Chrome profile. All future runs **skip login entirely** — the script opens Gemini directly with your existing session. No credentials needed on the command line:

```bash
python3 nanogen_cli.py --prompt "your prompt" --color
```

**Session validity:** Google sessions typically last a few days. When the session expires, the script will redirect to the Google login page. Run with `--no-headless` again to re-authenticate manually (no credentials needed — just click your account and enter 2FA if required):

```bash
python3 nanogen_cli.py --prompt "your prompt" --no-headless --color
```

If you need to force a fresh login (e.g., different account), delete the profile:
```bash
rm -rf ~/.config/nanogen/chrome_profile
```

### Automated Login Options

These options let the script **try to log in to Google automatically** in headless mode. Note: Google may block headless Chromium with "This browser may not be secure". If automation fails, use the manual method above.

| Option | Description |
|--------|-------------|
| `--login EMAIL` | Your full Google account email address (e.g., `user@gmail.com`) |
| `--pwd PASSWORD` | Your Google account password. **Warning:** the password is visible in the process list and shell history |
| `--pwdsilent` | Prompts for the password interactively (no echo, secure). Requires `--login` |
| `--loginfile FILE` | Path to a text file containing the email on line 1 and the password on line 2. Safer than `--pwd` |

**Mutual exclusivity rules:**
- `--loginfile` cannot be combined with `--login`, `--pwd`, or `--pwdsilent`
- `--pwdsilent` requires `--login` (you must specify the email)
- `--pwd` requires `--login`

**How it works:** The script navigates to `accounts.google.com/signin`, fills in the email field, clicks "Next", waits for the password field, fills it in, and clicks "Next" again. It then navigates to Gemini and verifies the login succeeded. If Google prompts for 2FA or additional verification, the login fails with a clear message.

**Limitations:**
- Does **not** handle 2FA (two-factor authentication), phone prompts, or CAPTCHA challenges
- May fail if Google shows an unusual verification screen
- For first-time setup on a new device, Google may still require additional verification even with correct credentials

**Example:**
```bash
# Interactive password prompt (secure)
python3 nanogen_cli.py --login user@gmail.com --pwdsilent --prompt "A cat" --color

# Password from file
python3 nanogen_cli.py --loginfile secret.txt --in prompts.json --out output --color --stat

# Password on command line (less secure)
python3 nanogen_cli.py --login user@gmail.com --pwd "MyPass123" --in prompts.json --color
```

### Display & Logging

| Option | Description |
|--------|-------------|
| `--color` | Enable colored output: errors in red, warnings in yellow, success in green, section headers in magenta |
| `--mono` | Force monochrome output. Overrides `--color` — useful when redirecting to a file or piping |
| `--debug` | Enable verbose debug output. Shows every step the script takes: selectors tried, buttons clicked, timing, errors |
| `--stat` | After each image, show: generation time (seconds), resolution (width × height), file size (KiB) |
| `--savescr [FILE]` | Redirect all console output to this file (ANSI color codes are stripped). If used without a filename and `--in` is provided, filename derives from the JSON file (e.g., `prompts.scr`). Without `--in`, defaults to `nanogen.scr` |
| `--log [FILE]` | Save a structured execution log to this file. Contains: timestamp, total/success/fail counts, list of all generated files with stats. Without a filename, derives from the JSON file (e.g., `prompts.log`) |

---

## Complete Examples

### Single prompt with default settings
```bash
python3 nanogen_cli.py --prompt "A futuristic cityscape with flying cars"
```
Generates one image, saves as `A_futuristic_cityscape__20260709_120000.png` in the current directory.

### Single prompt with all modifiers
```bash
python3 nanogen_cli.py --prompt "dragon" --insprompt "Generate a detailed image of" --addprompt "Fantasy art style, epic lighting, 8K" --gen tmp --type think --thinking extended --fmt 169 --resx 1920 --color --stat
```
Final prompt that Gemini receives: `"Generate a detailed image of. dragon. Fantasy art style, epic lighting, 8K Aspect ratio 16:9. --size 1920x1080"`

### Batch with skip and logs (production use)
```bash
python3 nanogen_cli.py --in my_project.json --out my_images --skip --dlret 5 --limitwait 600 --log --savescr --color --stat
```
- Reads prompts from `my_project.json`.
- Saves images to `my_images/`.
- Skips any image whose file already exists (safe to re-run).
- Retries downloads up to 5 times.
- Waits 10 minutes on rate limit.
- Saves a .log file and .scr file with full output.

### Automated login in headless mode
```bash
python3 nanogen_cli.py --login user@gmail.com --pwdsilent --prompt "A cat sitting on a mat" --color
```
The script prompts for the password interactively (no echo), then logs into Google automatically, navigates to Gemini, and generates the image — all in headless mode.

### Batch with credentials from file
```bash
echo "user@gmail.com" > credentials.txt
echo "MySecretPassword" >> credentials.txt
python3 nanogen_cli.py --loginfile credentials.txt --in prompts.json --out output --skip --stat --color
```
The email and password are read from a two-line text file. This is safer than passing `--pwd` on the command line.

### Debugging what the browser sees
```bash
python3 nanogen_cli.py --in prompts.json --debug --no-headless
```
Opens a visible Chromium window and prints every action the script takes — useful for troubleshooting selector issues.

### Random delays between prompts
```bash
python3 nanogen_cli.py --in prompts.json --promptrnd 3000,8000 --skip --color
```
Waits 3 to 8 seconds (random) between each prompt to avoid triggering rate limits.

---

## How It Works (Detailed)

### The Full Flow for One Prompt

1. **Browser launch** — On first run, Playwright launches Chromium headless (or visible if `--no-headless`). The profile at `~/.config/nanogen/chrome_profile/` preserves cookies and login state across runs.

2. **Login verification** — The script navigates to `https://gemini.google.com/app` and checks whether the page shows a login screen or a ready-to-use Gemini. If not logged in, the behavior depends on which flags are set:
   - **`--login` + `--pwd` / `--pwdsilent` / `--loginfile`**: The script automatically fills the Google sign-in form with the provided credentials, submits it, navigates to Gemini, and confirms the session is active. If 2FA or a challenge page appears, it fails with a clear message.
   - **No login flags**: The script guides the user (or exits with instructions in headless mode).

3. **Chat setup** — Based on `--gen` mode:
   - `tmp` (default): Opens the sidebar menu, clicks "Temporary chat" toggle button. Each prompt gets a fresh, isolated chat with no context from previous prompts.
   - `chat`: Clicks "New chat" button in the sidebar.
   - `native`: Does nothing — uses whatever page is currently open.

4. **Model selection** (if `--type` is given): Clicks the model selector dropdown button, finds the matching model by text (supports both Polish and English labels), and clicks it.

5. **Thinking mode** (if `--thinking` is given): Reopens the model selector, finds the "Thinking level" section, clicks either "Standard" / "Basic" or "Extended" / "Advanced".

6. **Tool activation**: Clicks the "Tools" button, then clicks "Create images" (or "Twórz obrazy" / "Utwórz obraz") from the menu.

7. **Prompt construction**:
   - Start with the original prompt from `--prompt` or JSON file.
   - If `--insprompt`: prepend it (adds a period if missing).
   - If `--addprompt`: append it (adds a period if missing).
   - If `--fmt`: append aspect ratio instruction (e.g., `" Aspect ratio 16:9."`).
   - If `--res`, `--resx`, or `--resy`: append `--size WxH` to the prompt.

8. **Send prompt**: Clicks the input field, clears it, types the full prompt using `keyboard.insert_text()` (headless-compatible), then clicks the send button (or presses Enter).

9. **Wait for generation**: The script polls every 1-5 seconds for:
   - An `<img>` element inside the response (Google standard `generated-image` selector, `single-image img`, or any `img[src*="googleusercontent.com"]`).
   - A download button (`download-generated-image-button`, or any button with aria label "Download" / "Pobierz").
   
   Minimum wait time is `--mingentime` (default 30s). Maximum total wait is `max(mingentime + 60, 120)` seconds.

10. **Download**: Three strategies in order of preference:
    - **Strategy A (official button)**: Clicks the download button, waits for Chrome's download event, saves the file. Full resolution, best quality.
    - **Strategy B (canvas extraction)**: If the download button is missing, uses JavaScript to draw the image onto an HTML canvas and extract it as a base64 JPEG. Fast but may capture a lower-resolution UI preview.
    - **Strategy C (JS fetch)**: Reads the `<img>` element's `src` attribute, fetches the image data via JavaScript `fetch()`, converts to base64, and writes to disk.

11. **Retry logic**: If the download button click fails or produces a zero-byte file, retries up to `--dlret` times (default 3). Re-locates the button each time.

12. **Rate limit detection**: After each poll or after download, checks the response text for "limit reached", "too many requests", or Polish equivalents. If detected, waits `--limitwait` seconds (default 300) and retries the entire prompt up to 3 times.

13. **Post-download**: If `--stat` is enabled, opens the saved image with Pillow to read resolution and file size. Checks if image is exactly 1024x1024 (1:1 ratio indicator). Prints the result line.

### Error Handling

- **Rate limits**: Automatically detected in both Polish and English. Waits and retries 3 times.
- **Navigation errors**: `ERR_ABORTED` and similar are handled gracefully with retries.
- **Missing elements**: Every element lookup uses a wide range of selectors (by aria-label, data-test-id, text content, role) supporting both Polish and English.
- **Download failures**: Three fallback strategies, plus configurable retries per download.
- **Timeout**: If generation takes too long, the image is marked as failed.

---

## Output Files

Generated images are saved with the filename from the JSON key (or a truncated prompt + timestamp for single prompts). By default, `.png` is appended unless `--noex` is set or the filename already has an image extension.

Supporting files created during execution:

| File | When Created | Description |
|------|-------------|-------------|
| `<output>/<filename>.png` | Every successful prompt | Generated image |
| `nanogen.err` | After any failure | Error log (appended on each run) |
| `<name>.log` | With `--log` | Execution log: file list, stats, summary |
| `<name>.scr` | With `--savescr` | Full console output (ANSI-stripped) |
| `~/.config/nanogen/chrome_profile/` | First run | Persistent browser profile (session cookies) |

---

## Session and Profile Management

### How login persistence works

Playwright's `launch_persistent_context` keeps all cookies, localStorage, and session data in a dedicated directory. By default this is `~/.config/nanogen/chrome_profile/`.

When you log in with `--no-headless`, everything is saved there. Every subsequent headless run loads the same profile and you are **automatically logged in**.

### Resetting the session
If your Gemini login expires or you want to use a different account:
```bash
rm -rf ~/.config/nanogen/chrome_profile
python3 nanogen_cli.py --prompt "test" --no-headless
```

### Using your own Chrome profile
If you already have a Chrome profile with a Gemini login:
```bash
python3 nanogen_cli.py --prompt "test" --user-data-dir ~/.config/google-chrome/Default --no-headless
```

---

## Comparison: `nanogen.py` vs `nanogen_cli.py`

| Aspect | Original (`nanogen.py`) | CLI version (`nanogen_cli.py`) |
|--------|------------------------|-------------------------------|
| Version | 0.0.18 | 0.0.20 |
| Browser launch | Manual — must run `start_chrome_debug.bat` first | Automatic — Playwright launches Chromium directly |
| Display needed | Yes — requires X11 / Wayland to show Chrome | **No** — headless by default, works over SSH |
| Platform | Windows-oriented (`.bat` script) | **Linux-first** (also macOS, WSL) |
| Session profile | `C:\chrome_dev_profile` (hardcoded) | `~/.config/nanogen/chrome_profile/` (auto-managed) |
| Input method | `navigator.clipboard` + paste | `keyboard.insert_text()` (works in headless) |
| CDP connection | `connect_over_cdp()` to existing browser | `launch_persistent_context()` fresh browser instance |
| Single-instance | File-based lock | Not needed (each run gets its own browser) |
| `--install-browser` | Not available | Built-in: installs Chromium + system deps |

---

## Files

| File | Description |
|------|-------------|
| `nanogen_cli.py` | Main Python script (~1780 lines) |
| `README_CLI.md` | This documentation file |
| `~/.config/nanogen/chrome_profile/` | Persistent Chrome profile (auto-created on first run) |

---

## License

This project is open source. See the repository at <https://github.com/igorbrzezek/nanogen> for license information.
