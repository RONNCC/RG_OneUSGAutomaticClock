# ⏰ OneUSGAutomaticClock ⏰
## CURRENT STATUS: WORKING ✅
This script automates clock in/out for OneUSG (Georgia Tech) and has been updated to the current UI.

## What is this?
This is a little script for Georgia Tech students to be able to automatically clock hours without worrying about forgetting to turn it off.

It can be easily modified to work for any university in the USG system 
(If you have any questions on how to do this, feel free to reach out, I can make a fork that works for any of the universities). 

## Requirements
- Python 3.9+ [Download](https://www.python.org)
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- Chrome (Chromedriver is auto-installed by the script)

## Set-Up Instructions

1. Open a terminal window.
1. Type the following to clone the repo and switch to the project directory
    * `git clone https://github.com/Shaun-Regenbaum/OneUSGAutomaticClock.git` then `cd OneUSGAutomaticClock` 
    * For self-help on how to clone a repo click [here](https://www.howtogeek.com/451360/how-to-clone-a-github-repository/), otherwise reach out and I will help you (seriously I will sit down with you and guide you through it)!
1. Download and install Python from [here](https://www.python.org/downloads/).
    * **Make sure to install Python 3.9 or later**
1. Install dependencies (recommended):
    * `uv pip install -r requirements.txt`
1. Run the script from the command line:
    * `uv run python clock_manager.py -m <minutes> -u <gt_username>`

**Notes**
- You will be prompted for your GT password if not set via environment variables.
- You will need to confirm Duo 2FA.

## Common Usage
Clock for 30 minutes:
- `uv run python clock_manager.py -m 30 -u <gt_username>`

Run headless:
- `uv run python clock_manager.py -m 30 -u <gt_username> --headless`

Enable debug + artifact dumps (screenshots/HTML):
- `uv run python clock_manager.py -m 30 -u <gt_username> --debug --dump-dir ./debug_dumps`

## Environment Variables
- `ONEUSG_USERNAME` / `ONEUSG_PASSWORD`
- `ONEUSG_DUO_OTP_URI` (recommended) or `ONEUSG_DUO_HOTP_SECRET`
- `ONEUSG_DUO_PASSCODE` (fallback)
- `ONEUSG_DUO_TIMEOUT` (seconds)

## Notifications
- The script shows persistent macOS alerts for errors (requires acknowledgment).
- Standard notifications are also supported via `plyer`.
## FAQ

### Chromedriver

**Installation**
If Chromedriver auto-install fails, you can download the appropriate driver for your OS from [here](https://sites.google.com/a/chromium.org/chromedriver/home). You will need to manually add this driver to your PATH and update the script if you choose to manage it manually.

**Security**
- **Q: I'm getting "cannot be opened because the developer cannot be verified"**
  
Hit cancel and then go to System Settings -> Privacy & Security -> Scroll down to "Allow applications downloaded from..."  and you should see a "<application> was blocked from use" with a button "Allow Anyway" to click.
![image](https://github.com/RONNCC/OneUSGAutomaticClock/assets/1313566/d0be177c-1985-4d8e-977f-e89c0e6adc5c)


