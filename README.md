# ⏰ OneUSGAutomaticClock

Automatically clock in and out on OneUSG for Georgia Tech students. Set a duration, run the script, and it handles the rest—including Duo 2FA.

## Requirements

- **Python 3.9+** — [Download](https://www.python.org/downloads/)
- **uv** — [Install uv](https://docs.astral.sh/uv/getting-started/installation/)
- **Chrome** — Chromedriver is auto-installed

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/Shaun-Regenbaum/OneUSGAutomaticClock.git
cd OneUSGAutomaticClock
uv sync
```

### 2. Create your `.env` file

Create a file named `.env` in the project folder:

```
ONEUSG_USERNAME=gburdell3
ONEUSG_PASSWORD=your_password_here
ONEUSG_DUO_OTP_URI=otpauth://totp/Duo:gburdell3?secret=ABCD1234...&issuer=Duo
```

### 3. Run it

```bash
uv run python clock_manager.py -m 60
```

This clocks you in, waits 60 minutes, then clocks you out.

---

## Getting Your Duo OTP URI

To fully automate Duo 2FA (no manual push approval), you'll need to extract your TOTP secret. This is a bit involved—see [this guide](TODO) for detailed instructions.

Without the OTP URI, the script will wait for you to manually approve the Duo push (default 120 seconds).

---

## Shell Alias (Recommended)

Add this to your `~/.zshrc` or `~/.bashrc` to run from anywhere:

```bash
alias gatech-clock="uv run --project /path/to/OneUSGAutomaticClock python /path/to/OneUSGAutomaticClock/clock_manager.py"
```

Replace `/path/to/` with your actual path. Then:

```bash
source ~/.zshrc
gatech-clock -m 60
```

---

## Usage

```bash
# Clock for 60 minutes (runs headless by default)
gatech-clock -m 60

# Show the browser window while running
gatech-clock -m 60 --ui

# Debug mode (saves screenshots/HTML on failure)
gatech-clock -m 60 --debug --dump-dir ./dumps

# See all options
gatech-clock --help
```

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ONEUSG_USERNAME` | ✅ | Your GT username |
| `ONEUSG_PASSWORD` | ✅ | Your GT password |
| `ONEUSG_DUO_OTP_URI` | Recommended | TOTP URI for automatic Duo 2FA |
| `ONEUSG_DUO_TIMEOUT` | Optional | Seconds to wait for Duo (default: 120) |
| `ONEUSG_DUMP_DIR` | Optional | Directory for debug screenshots/HTML |

---

## Troubleshooting

### "cannot be opened because the developer cannot be verified"

This is macOS blocking Chromedriver. Fix it:

1. Open **System Settings** → **Privacy & Security**
2. Scroll down to find the blocked app message
3. Click **Allow Anyway**

### Script times out during Duo

If you haven't set up `ONEUSG_DUO_OTP_URI`, you'll need to manually approve the Duo push within 120 seconds. Increase the timeout with:

```bash
gatech-clock -m 60 --duo-timeout 300
```

### Something else broke

Run with debug mode to capture what's happening:

```bash
gatech-clock -m 60 --debug --dump-dir ./dumps --ui
```

Check the `./dumps` folder for screenshots and HTML of what the script saw.

---

## How It Works

1. Opens OneUSG timecard page
2. Logs in with your GT credentials
3. Handles Duo 2FA (automatically if OTP URI is set, otherwise waits for push)
4. Selects "Clock In" and submits
5. Waits for the specified duration (refreshing periodically to prevent timeout)
6. Selects "Clock Out" and submits

---

## Contributing

PRs welcome! This can be adapted for any USG school—just update the selectors in `selector_defs.py`.


