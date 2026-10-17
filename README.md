# ⏰ OneUSGAutomaticClock

Automatically clock in and out on OneUSG for Georgia Tech students/staff. Set a duration, run the script, and it handles the rest—including Duo 2FA.

Two commands ship with this repo:

- **`gatech-clock`** — a single clock in → wait → clock out run.
- **`gatech-worker`** — a scheduler that keeps clocking in/out with breaks across a daily window (default 9am–6pm) until you hit a weekly hour target.

> **Attribution:** This project is based on [Shaun-Regenbaum/OneUSGAutomaticClock](https://github.com/Shaun-Regenbaum/OneUSGAutomaticClock) by Shaun Regenbaum.

## Requirements

- **Python 3.9+** — [Download](https://www.python.org/downloads/)
- **uv** — [Install uv](https://docs.astral.sh/uv/getting-started/installation/)
- **Chrome** — Chromedriver is auto-installed when needed

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/RONNCC/RG_OneUSGAutomaticClock.git
cd RG_OneUSGAutomaticClock
uv sync
```

### 2. Put the launchers on your PATH

The repo ships two executable launchers at its root — `gatech-clock` and
`gatech-worker`. They `uv run --project` the surrounding repo, resolving their
own location so they work from any directory (even through a symlink). Symlink
them into `~/.local/bin`:

```bash
mkdir -p ~/.local/bin
ln -sf "$PWD/gatech-clock" ~/.local/bin/gatech-clock
ln -sf "$PWD/gatech-worker" ~/.local/bin/gatech-worker
```

Make sure `~/.local/bin` is on your `PATH`. Add this to `~/.zshrc` (or
`~/.bashrc`) if it isn't already:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Then reload your shell and verify:

```bash
source ~/.zshrc
which gatech-clock gatech-worker
gatech-clock --help
```

<details>
<summary>Prefer aliases over symlinks?</summary>

```bash
alias gatech-clock='uv run --project /path/to/RG_OneUSGAutomaticClock python /path/to/RG_OneUSGAutomaticClock/clock_manager.py'
alias gatech-worker='uv run --project /path/to/RG_OneUSGAutomaticClock python /path/to/RG_OneUSGAutomaticClock/gatech_worker.py'
```

Replace `/path/to/` with your clone location.
</details>

### 3. Create your `.env` file

Start from the template, then fill in your real values:

```bash
cp .env.example .env
```

Example values:

```dotenv
ONEUSG_USERNAME=gburdell3
ONEUSG_PASSWORD=your_password_here
ONEUSG_DUO_OTP_URI=otpauth://totp/Duo:gburdell3?secret=BASE32SECRET...&issuer=Duo
```

`.env` is gitignored — never commit it. Keep your password/secret in your
password manager so you can restore this on a new machine.

See [Getting Your Duo OTP URI](#getting-your-duo-otp-uri) to fill in
`ONEUSG_DUO_OTP_URI`.

### 4. Run it

```bash
gatech-clock --hours        # check week-to-date total
gatech-clock -m 60          # clock in, wait 60 min, clock out
gatech-worker               # saturate the week 9-6 with breaks
```

---

## Getting Your Duo OTP URI

To automate Duo 2FA (no manual push approval), extract your TOTP secret by
registering a virtual "Duo Mobile" device and reading the secret out of the
activation response. The repo ships `get-duo-secret.py` to do this.

### 1. Start a device enrollment

1. Log in to any Duo-protected page, e.g. the OneUSG clock page.
2. **Other options** → **Manage devices** → **Add another device**.
3. Choose **Tablet** → **Android** → **"I have Duo Mobile installed"**.
   (Or choose **"Email me an activation link"** / SMS.)

### 2. Grab the activation URL

Right-click the QR image → **Copy image address** (or copy the activation link).
There are two formats, and `get-duo-secret.py` handles both:

- **Android QR:** `https://api-XXXX.duosecurity.com/frame/qr?value=<CODE>-<base64host>`
- **Email/SMS link:** `https://api-XXXX.duosecurity.com/frame/qr?value=https%3A%2F%2Fm-XXXX.duosecurity.com%2Factivate%2F<CODE>`

### 3. Extract the secret

```bash
uv run --with requests --with pycryptodome python get-duo-secret.py '<paste-url-here>'
```

It prints a base32 secret plus ready-to-paste `otpauth://totp/...` and
`otpauth://hotp/...` URIs.

### 4. Put it in `.env`

- **TOTP** (usual): set `ONEUSG_DUO_OTP_URI` to the printed **TOTP URI**.
- **HOTP** (if your org issues counter-based codes): set
  `ONEUSG_DUO_HOTP_SECRET` to the base32 secret. The script tracks its counter
  in `~/.duo_hotp_counter` (override with `ONEUSG_DUO_HOTP_COUNTER_FILE`).

> The activation link is **single-use**. If extraction fails, start a fresh
> enrollment and try again.

### Notes & caveats

- `npx unduo` only understands the Android `CODE-BASE64HOST` QR format. If your
  enrollment gives the `m-.../activate/...` email/SMS format, use
  `get-duo-secret.py` (it handles both).
- The base32 secret is the same for TOTP and HOTP; only the URIs differ.
- If your org has **push-only** Duo (no "Duo Mobile passcode" option), leave the
  URI blank. The script will wait for you to approve the push manually within
  `--duo-timeout` seconds (default 120).

---

## Usage — `gatech-clock`

```bash
# Clock for 60 minutes (headless by default)
gatech-clock -m 60

# Show the browser window while running
gatech-clock -m 60 --ui

# Clock out immediately without clocking in (recovery mode)
gatech-clock --clock-out

# Show week-to-date total, no clock-in/out (single line)
gatech-clock --hours
# -> Week total so far: 12.50h (7.50h left of 20h cap)

# List every day's clock in/out punches for the week, plus total
gatech-clock --hours --detail
# -> Mon 09/08: In 8:00 AM, Out 5:00 PM (9.00h)
# -> Tue 09/09: In 8:30 AM, Out 4:30 PM (8.00h)
# -> Week total so far: 17.00h

# Clock-in refuses once the week hits the 20h cap.

# Cap this run so current week-to-date hours + requested minutes stay <= 15h
# (lowers the built-in 20h cap; trims -m automatically, refuses if already at/over cap)
gatech-clock -m 90 --max-hours 15

# 0 or negative minutes also clock out immediately
gatech-clock -m 0

# Debug mode (saves screenshots/HTML on failure)
gatech-clock -m 60 --debug --dump-dir ./dumps

# See all options
gatech-clock --help
```

---

## Usage — `gatech-worker`

Keeps calling `gatech-clock` in work chunks with breaks in between, staying
inside a daily window until week-to-date hours reach your target. Defaults:
**9am–6pm**, **19h** weekly target (just under the 20h OneUSG cap), **60–150m**
work chunks, **30–40m** breaks.

```bash
gatech-worker                       # 9-6, target 19h, 30-40m breaks
gatech-worker --dry-run             # print the plan, don't clock in/out
gatech-worker --once                # one shift, then exit
gatech-worker --start 10:00 --end 17:00
gatech-worker --target 15 --break-min 20 --break-max 30
gatech-worker --shift-min 90 --shift-max 120
gatech-worker --hours --detail      # passthrough to gatech-clock
```

| Flag | Default | Meaning |
|------|---------|---------|
| `--start` | `09:00` | Daily window start (24h `HH:MM`) |
| `--end` | `18:00` | Daily window end |
| `--target` | `19.0` | Weekly hour target `(0, 20]` |
| `--break-min` / `--break-max` | `30` / `40` | Break minutes between shifts |
| `--shift-min` / `--shift-max` | `60` / `150` | Work chunk minutes |
| `--once` | off | Do a single shift, then exit |
| `--dry-run` | off | Print plan only |
| `--seed N` | random | Reproducible breaks/shifts |
| `--ui`, `--debug`, `--duo-timeout` | | Passed through to `gatech-clock` |

Run the worker in its own terminal (or under `tmux`/`nohup`) since it loops all
day. It sleeps until the window opens, takes breaks between shifts, and exits
once the target is reached.

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ONEUSG_USERNAME` | ✅ | Your GT username |
| `ONEUSG_PASSWORD` | ✅ | Your GT password |
| `ONEUSG_DUO_OTP_URI` | Recommended | `otpauth://` TOTP/HOTP URI for automatic Duo (not a QR URL) |
| `ONEUSG_DUO_HOTP_SECRET` | Optional | Raw base32 HOTP secret (fallback when no OTP URI) |
| `ONEUSG_DUO_HOTP_COUNTER_FILE` | Optional | HOTP counter file (default `~/.duo_hotp_counter`) |
| `ONEUSG_DUO_TIMEOUT` | Optional | Seconds to wait for Duo (default: 120) |
| `ONEUSG_DUMP_DIR` | Optional | Directory for debug screenshots/HTML |

Use `.env.example` as the template any time you move this project to a new machine or folder.

---

## Troubleshooting

### "cannot be opened because the developer cannot be verified"

This is macOS blocking Chromedriver. Fix it:

1. Open **System Settings** → **Privacy & Security**
2. Scroll down to find the blocked app message
3. Click **Allow Anyway**

### Script times out during Duo

If you haven't set up `ONEUSG_DUO_OTP_URI`, you'll need to manually approve the
Duo push within 120 seconds. Increase the timeout with:

```bash
gatech-clock -m 60 --duo-timeout 300
```

Also double-check `ONEUSG_DUO_OTP_URI` is the `otpauth://...` URI, not the raw
QR/activation URL.

### Duo secret extraction fails

The activation link is single-use and expires quickly. Start a fresh enrollment
(Quick Start → [Getting Your Duo OTP URI](#getting-your-duo-otp-uri)) and run
`get-duo-secret.py` again immediately. Newer Duo enrollments need the RSA pubkey
the helper sends; older `duo-cli`-style scripts that POST an empty body will fail.

### Clock-out failed / forgot to clock out

If a previous session crashed before clocking you out, use recovery mode — it logs in fresh and clocks out immediately without clocking in again:

```bash
gatech-clock --clock-out
# or equivalently:
gatech-clock -m 0
```

Note: if you're already clocked out by the time this runs (e.g. the original
punch actually landed on the server before the session died), the script
exits successfully (exit code 0) instead of treating "nothing to clock out
from" as a failure. Clock-out success means "you end up clocked out," not
"a new punch was submitted."

### Something else broke

Run with debug mode to capture what's happening:

```bash
gatech-clock -m 60 --debug --dump-dir ./dumps --ui
```

Check the `./dumps` folder for screenshots and HTML of what the script saw.

---

## How It Works

**Clock-in/out** (`gatech-clock -m <minutes>`):
1. Opens OneUSG timecard page
2. Logs in with your GT credentials
3. Handles Duo 2FA (automatically if OTP URI is set, otherwise waits for push)
4. Selects "Clock In" and submits
5. Waits for the specified duration (refreshing periodically to prevent timeout)
6. Selects "Clock Out" and submits

**Recovery / immediate clock-out mode** (`gatech-clock --clock-out` or `-m 0` / negative):
1. Opens OneUSG timecard page
2. Logs in with your GT credentials
3. Handles Duo 2FA
4. Skips clock-in entirely
5. Selects "Clock Out" and submits (with automatic session recovery if needed)

**Worker scheduler** (`gatech-worker`):
1. Checks week-to-date hours (`gatech-clock --hours`).
2. If before the window, sleeps until `--start`; if after `--end`, sleeps until
   the next day's window.
3. Otherwise runs a work chunk of `--shift-min`–`--shift-max` minutes, capped so
   it never exceeds the day's remaining window or the weekly `--target`.
4. Re-checks hours; if the target is reached, exits. Otherwise waits a
   `--break-min`–`--break-max` minute break and repeats.
