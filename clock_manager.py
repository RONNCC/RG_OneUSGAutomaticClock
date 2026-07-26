"""
OneUSG Automatic Clock-In/Out Manager
======================================

Automates the full OneUSG time-clock workflow via headless Chrome:

  1. Launch Chrome (headless by default, --ui for visible).
  2. Navigate to the OneUSG clock page URL.
  3. Select Georgia Tech as the Identity Provider (IdP).
  4. Log in with GT credentials and complete Duo MFA automatically
     (TOTP/HOTP via otpauth URI, or waits for manual Duo push).
  5. Clock in once authenticated.
  6. Wait for the requested duration, refreshing the page every 15 minutes
     to prevent OneUSG's session timeout from kicking us out.
  7. Clock out when the timer expires.

Session recovery:
  OneUSG's session timeout (~10-15 min) or Chrome crashes can kill the
  browser mid-wait. If a refresh or clock-out fails due to a dead session,
  the script automatically spins up a fresh browser, re-authenticates
  through GT login + Duo, and retries. If recovery fails, a blocking
  macOS alert tells the user to clock out manually.

  Clock-out success is defined as "ends up clocked out", not "submitted a
  new punch". If the timecard already shows clocked-out (e.g. the punch
  from before a session death actually landed server-side, and a recovery
  retry just observes that), that counts as success — exit code 0, no
  manual-clock-out alert. See clock_actions.select_punch_and_submit.

Sleep handling:
  On macOS, runs `caffeinate -i` to prevent idle sleep while clocked in.
  The countdown uses wall-clock time (not accumulated sleep time) so even
  if the system briefly sleeps, the timer stays accurate.

Notifications:
  On macOS, uses native osascript alerts (blocking) and banner
  notifications (non-blocking). Falls back to terminal print on other
  platforms.

Usage:
  uv run python clock_manager.py -m 60          # headless, 60 minutes
  uv run python clock_manager.py -m 30 --ui     # visible browser, 30 min
  uv run python clock_manager.py -m 0           # skip clock-in, clock out immediately (same as --clock-out)
  uv run python clock_manager.py -m -1          # same: 0 or negative = clock out immediately
  uv run python clock_manager.py --clock-out    # skip clock-in, just clock out (recovery mode)
  uv run python clock_manager.py -m 60 --debug  # verbose logging + artifacts
"""

import warnings
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

import sys
import time
import os
import argparse
import socket
import subprocess
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, parse_qs
from urllib.error import URLError

import logging

import browser_utils
import duo_auth
import clock_actions
import selector_defs as selectors
from browser_utils import AppContext
from notifications import notify_user_with_ack

from selenium import webdriver
import chromedriver_autoinstaller
from chromedriver_autoinstaller import utils as chromedriver_utils
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

from dotenv import load_dotenv
import pyotp

logger = logging.getLogger("clock_manager")


def ensure_chromedriver_installed() -> str:
    """Install chromedriver when needed, but tolerate offline runs with a cached driver."""
    chrome_version = chromedriver_utils.get_chrome_version()
    cached_driver_path = ""

    if chrome_version:
        chrome_major_version = chromedriver_utils.get_major_version(chrome_version)
        chromedriver_filename = chromedriver_utils.get_chromedriver_filename()
        cached_driver_path = os.path.join(
            chromedriver_utils.get_chromedriver_path(),
            chrome_major_version,
            chromedriver_filename,
        )

    had_cached_driver = bool(cached_driver_path and os.path.isfile(cached_driver_path))

    root_logger = logging.getLogger()
    previous_level = root_logger.level
    if had_cached_driver:
        root_logger.setLevel(max(previous_level, logging.WARNING))

    try:
        driver_path = chromedriver_autoinstaller.install()
    except (URLError, socket.gaierror) as exc:
        if had_cached_driver:
            logger.warning(
                "Chromedriver network lookup/install failed (%s). Using cached driver at %s.",
                exc,
                cached_driver_path,
            )
            return cached_driver_path
        raise
    finally:
        if had_cached_driver:
            root_logger.setLevel(previous_level)

    if not had_cached_driver and driver_path:
        print("Installing chromedriver for the local Chrome version...")

    return driver_path


def get_est_time_str() -> str:
    """Return current time in EST as a formatted string."""
    est = timezone(timedelta(hours=-5))
    now_est = datetime.now(est)
    return now_est.strftime("%I:%M %p EST")


USERNAME = None
PASSWORD = None
DUO_TIMEOUT_SECONDS = 120


class RestartRequested(Exception):
    """Raised when the login flow needs a full browser restart (e.g. idpproxy 400)."""


def get_duo_passcode(ctx: AppContext) -> str:
    """Generate or fetch Duo passcode (HOTP/TOTP/static)."""
    otp_uri = os.environ.get("ONEUSG_DUO_OTP_URI", "")
    if otp_uri and pyotp:
        try:
            parsed = urlparse(otp_uri)
            if parsed.scheme == "otpauth":
                otp_type = parsed.netloc.lower()
                params = parse_qs(parsed.query or "")
                secret = (params.get("secret") or [""])[0]
                digits = int((params.get("digits") or ["6"])[0])
                period = int((params.get("period") or ["30"])[0])
                if otp_type == "totp" and secret:
                    totp = pyotp.TOTP(secret, digits=digits, interval=period)
                    code = totp.now()
                    logger.debug(f"Generated TOTP code from otpauth URI: {code}")
                    return code
                if otp_type == "hotp" and secret:
                    counter_file = os.environ.get(
                        "ONEUSG_DUO_HOTP_COUNTER_FILE",
                        os.path.expanduser("~/.duo_hotp_counter")
                    )
                    counter = 0
                    try:
                        if os.path.exists(counter_file):
                            with open(counter_file, "r") as f:
                                counter = int(f.read().strip())
                    except Exception:
                        counter = 0
                    hotp = pyotp.HOTP(secret, digits=digits)
                    code = hotp.at(counter)
                    with open(counter_file, "w") as f:
                        f.write(str(counter + 1))
                    logger.debug(f"Generated HOTP code (counter={counter})")
                    return code
        except Exception as e:
            logger.debug(f"OTP URI parsing failed: {e}")

    hotp_secret = os.environ.get("ONEUSG_DUO_HOTP_SECRET", "")
    if hotp_secret and pyotp:
        counter_file = os.environ.get(
            "ONEUSG_DUO_HOTP_COUNTER_FILE",
            os.path.expanduser("~/.duo_hotp_counter")
        )
        counter = 0
        try:
            if os.path.exists(counter_file):
                with open(counter_file, "r") as f:
                    counter = int(f.read().strip())
        except Exception:
            counter = 0
        try:
            hotp = pyotp.HOTP(hotp_secret)
            code = hotp.at(counter)
            with open(counter_file, "w") as f:
                f.write(str(counter + 1))
            logger.debug(f"Generated HOTP code (counter={counter})")
            return code
        except Exception as e:
            logger.debug(f"HOTP generation failed: {e}")
    return os.environ.get("ONEUSG_DUO_PASSCODE", "")


def is_duo_auto_auth_configured() -> bool:
    """Return whether Duo passcode automation is configured."""
    return any(
        os.environ.get(name, "").strip()
        for name in ("ONEUSG_DUO_OTP_URI", "ONEUSG_DUO_HOTP_SECRET", "ONEUSG_DUO_PASSCODE")
    )


def _set_input_value(ctx: AppContext, el, value: str) -> None:
    logger.debug(f"_set_input_value called with value: {value}")
    try:
        el.click()
    except Exception:
        pass

    try:
        el.send_keys(Keys.COMMAND, "a")
        el.send_keys(Keys.BACKSPACE)
    except Exception:
        try:
            el.send_keys(Keys.CONTROL, "a")
            el.send_keys(Keys.BACKSPACE)
        except Exception:
            try:
                el.clear()
            except Exception:
                pass

    try:
        el.send_keys(value)
        logger.debug("send_keys completed")
    except Exception as e:
        logger.debug(f"send_keys failed: {e}")
        try:
            from selenium.webdriver.common.action_chains import ActionChains
            ActionChains(ctx.driver).click(el).send_keys(value).perform()
        except Exception:
            pass

    try:
        current_value = el.get_attribute("value") or ""
        logger.debug(f"After typing, input value is: '{current_value}'")
    except Exception:
        current_value = ""

    if current_value.strip() != value.strip():
        logger.debug("Value didn't stick, trying JS approach")
        try:
            ctx.driver.execute_script(
                """
                const el = arguments[0];
                const val = arguments[1];
                el.focus();
                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                if (setter) setter.call(el, val);
                else el.value = val;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true }));
                el.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true }));
                el.blur();
                """,
                el,
                value,
            )
            final_value = el.get_attribute("value") or ""
            logger.debug(f"After JS, input value is: '{final_value}'")
        except Exception as e:
            logger.debug(f"JS value set failed: {e}")


def selectGT(ctx: AppContext):
    # If we're already on the GT login page, don't try to select an IdP.
    if browser_utils.check_existence(ctx, element_to_find="username", method_to_find="name"):
        return True

    # OneUSG has changed this IdP selection page multiple times; try a few robust patterns.
    try:
        WebDriverWait(ctx.driver, 5).until(EC.presence_of_element_located((By.CSS_SELECTOR, "a")))
    except Exception:
        pass

    try:
        gt_option = browser_utils.find_first(ctx, selectors.GT_IDP_SELECTORS, timeout=5, clickable=True)
        # If we matched the img inside a link, click the parent anchor.
        try:
            if gt_option.tag_name.lower() == "img":
                gt_option = gt_option.find_element(By.XPATH, "./ancestor::a[1]")
        except Exception:
            pass

        if not browser_utils.safe_click(ctx, gt_option):
            raise TimeoutException("Unable to click GT IdP option")
    except TimeoutException:
        # Fallback: try a JS lookup by link text or image alt.
        try:
            gt_js = ctx.driver.execute_script(
                """
                const links = Array.from(document.querySelectorAll('a'));
                return links.find(a => /Georgia Tech/i.test(a.textContent || '')
                    || /Georgia Tech/i.test(a.getAttribute('title') || '')
                    || (a.querySelector('img') && /Georgia Tech/i.test(a.querySelector('img').alt || '')));
                """
            )
            if gt_js is not None and browser_utils.safe_click(ctx, gt_js):
                return browser_utils.check_existence(ctx, element_to_find="username", method_to_find="name")
        except Exception:
            pass

        browser_utils.dump_artifacts(ctx, "select_gt_not_found")
        print("...")
        print("Unable to find the Georgia Tech IdP selector on the OneUSG page.")
        print("This usually means the IdP selection DOM changed.")
        print("If you re-run with --debug, the script will save a screenshot + HTML for updating selectors.")
        _quiet_quit(ctx.driver)
        return False

    return browser_utils.check_existence(ctx, element_to_find="username", method_to_find="name")


def loginGT(ctx: AppContext):
    gatech_login_username = browser_utils.find_first(ctx, [(By.NAME, "username"), (By.ID, "username")], timeout=25)
    gatech_login_password = browser_utils.find_first(ctx, [(By.NAME, "password"), (By.ID, "password")], timeout=25)

    gatech_login_username.clear()
    gatech_login_username.send_keys(USERNAME)
    gatech_login_password.clear()
    gatech_login_password.send_keys(PASSWORD)

    submit_button = browser_utils.find_first(
        ctx,
        [(By.NAME, "submit"), (By.CSS_SELECTOR, "button[type='submit']"), (By.CSS_SELECTOR, "input[type='submit']")],
        timeout=10,
        clickable=True,
    )
    submit_button.click()

    # Fail fast if GT rejects the credentials instead of falling through into Duo/OneUSG checks.
    try:
        WebDriverWait(ctx.driver, 5).until(
            lambda driver: (
                clock_actions.is_on_clock_page(ctx)
                or "sso.gatech.edu/cas/login" not in (driver.current_url or "")
                or "invalid credentials" in (driver.page_source or "").lower()
            )
        )
    except TimeoutException:
        pass

    try:
        current_url = ctx.driver.current_url or ""
        page_source = (ctx.driver.page_source or "").lower()
        if "sso.gatech.edu/cas/login" in current_url and browser_utils.check_existence(ctx, element_to_find="username", method_to_find="name") and (
            "invalid credentials" in page_source
            or "enter your gt account and password" in page_source
        ):
            browser_utils.dump_artifacts(ctx, "gatech_login_failed")
            print("...")
            print("Georgia Tech login failed before Duo started.")
            print("The GT login page reported invalid credentials or returned to the login form.")
            print("Check ONEUSG_USERNAME / ONEUSG_PASSWORD in your .env, then retry.")
            _quiet_quit(ctx.driver)
            return False
    except Exception as e:
        logger.debug(f"GT login failure detection skipped: {e}")

    print("...")
    print("...")
    if is_duo_auto_auth_configured():
        print("Script will try to complete Duo automatically using the configured passcode/TOTP.")
        print("If Duo still prompts for approval, complete it manually before the timeout.")
    else:
        print("Script is waiting for you to complete Duo authentication.")
        print("If you run out of time, just run the script again.")
    print("...")

    # Duo is often an iframe / Universal Prompt; keep nudging toward "Other options" and Duo Push.
    # Track state to fail fast if we're stuck
    last_url = ""
    stuck_count = 0
    MAX_STUCK_ITERATIONS = 10  # If URL doesn't change for 10 iterations (20s), fail fast
    
    try:
        start = time.time()
        iteration = 0
        while time.time() - start < DUO_TIMEOUT_SECONDS:
            iteration += 1

            # Detect idpproxy HTTP 400 and force a full restart
            try:
                current_url = ctx.driver.current_url or ""
                if "idpproxy.usg.edu/asimba/profiles/saml2" in current_url:
                    page = ctx.driver.page_source or ""
                    if "HTTP ERROR 400" in page or "Bad Request" in page:
                        browser_utils.dump_artifacts(ctx, "idpproxy_400")
                        print("Detected idpproxy HTTP 400. Restarting from the beginning.")
                        raise RestartRequested()
            except Exception:
                pass
            
            # Check if we've successfully logged in (supports old and new UI)
            if clock_actions.is_on_clock_page(ctx):
                logger.debug("Successfully found clock page element!")
                break
            
            # Try Duo automation
            duo_auth.try_duo_other_options(
                ctx,
                lambda: get_duo_passcode(ctx),
                lambda el, value: _set_input_value(ctx, el, value),
            )
            
            # Fail-fast: detect if we're stuck on the same page
            current_url = ctx.driver.current_url or ""
            if iteration % 5 == 0:
                logger.debug(f"Iteration {iteration}, URL: {current_url[:80]}...")
            
            if current_url == last_url:
                stuck_count += 1
                if stuck_count >= MAX_STUCK_ITERATIONS:
                    browser_utils.dump_artifacts(ctx, "stuck_state")
                    print(f"\n[FAIL-FAST] Stuck on same URL for {stuck_count} iterations. Current URL:")
                    print(f"  {current_url}")
                    print("Dumping page state and exiting to allow faster debugging.")
                    raise TimeoutException("Stuck in unexpected state - no progress detected")
            else:
                stuck_count = 0
                last_url = current_url
            
            time.sleep(2)
        else:
            raise TimeoutException("Timed out waiting for Duo / OneUSG to finish login.")
    except TimeoutException:
        browser_utils.dump_artifacts(ctx, "duo_timeout")
        print("...")
        print("Timed out waiting for Duo / OneUSG to finish login.")
        print("If Duo prompts are taking longer, re-run with a higher timeout: --duo-timeout 300")
        _quiet_quit(ctx.driver)
        return False

    # Handle window switching - OneUSG sometimes opens new windows or the original closes
    try:
        _switch_to_valid_window(ctx)
    except Exception as e:
        logger.debug(f"Window switch error: {e}")

    # Sometimes the PeopleSoft frame doesn't fully render after auth; wait and refresh
    logger.debug(f"Post-auth URL: {ctx.driver.current_url}")
    
    # Give the page extra time after Duo - OneUSG backend auth can be slow
    print("Waiting for OneUSG authentication to complete...")
    time.sleep(6)
    
    # Check if we need to refresh to get to the clock page
    for attempt in range(3):
        try:
            if clock_actions.is_on_clock_page(ctx):
                logger.debug("Clock page element found after login")
                return True
        except Exception:
            pass
        
        logger.debug(f"Clock element not found, attempt {attempt + 1}/3, refreshing...")
        
        try:
            # Try switching windows again in case a new one opened
            _switch_to_valid_window(ctx)
            ctx.driver.refresh()
            time.sleep(3)
        except Exception as e:
            logger.debug(f"Refresh attempt {attempt + 1} error: {e}")
    
    # Fallback: OneUSG auth sometimes gets stuck after Duo redirect.
    # Try opening the clock page directly in a new tab as a workaround.
    logger.debug("Attempting direct clock page navigation in new tab...")
    print("OneUSG redirect seems stuck, trying direct navigation...")
    
    if _try_direct_clock_page_navigation(ctx):
        return True
    
    browser_utils.dump_artifacts(ctx, "post_auth_no_clock")
    
    # If direct navigation also failed, request a full restart
    print("Authentication redirect failed. Will restart from the beginning...")
    raise RestartRequested()


def _try_direct_clock_page_navigation(ctx: AppContext):
    """
    Fallback for when OneUSG authentication redirect gets stuck after Duo.
    
    Opens the clock page URL directly in a new tab, which can bypass the stuck
    redirect since the session should already be authenticated.
    
    Returns True if successfully navigated to clock page, False otherwise.
    """
    try:
        # Remember original window
        original_window = ctx.driver.current_window_handle
        original_handles = set(ctx.driver.window_handles)
        
        # Open clock page in new tab
        logger.debug(f"Opening clock page directly in new tab: {selectors.CLOCK_PAGE_URL}")
        ctx.driver.execute_script(f"window.open('{selectors.CLOCK_PAGE_URL}', '_blank');")
        time.sleep(3)
        
        # Switch to the new tab
        new_handles = set(ctx.driver.window_handles) - original_handles
        if new_handles:
            new_tab = new_handles.pop()
            ctx.driver.switch_to.window(new_tab)
            logger.debug(f"Switched to new tab, URL: {ctx.driver.current_url}")
            
            # Give it extra time to load
            time.sleep(4)
            
            # Check if we're now on the clock page
            for check_attempt in range(3):
                if clock_actions.is_on_clock_page(ctx):
                    logger.debug("Successfully reached clock page via direct navigation!")
                    print("Direct navigation successful!")
                    
                    # Close the original stuck tab
                    try:
                        ctx.driver.switch_to.window(original_window)
                        ctx.driver.close()
                        ctx.driver.switch_to.window(new_tab)
                        logger.debug("Closed original stuck tab")
                    except Exception as e:
                        logger.debug(f"Could not close original tab: {e}")
                        # Make sure we're still on the new tab
                        try:
                            ctx.driver.switch_to.window(new_tab)
                        except Exception:
                            pass
                    
                    return True
                
                logger.debug(f"Direct nav check {check_attempt + 1}/3, waiting...")
                time.sleep(2)
            
            # New tab didn't work either - close it and return to original
            logger.debug("Direct navigation did not reach clock page")
            try:
                ctx.driver.close()
                ctx.driver.switch_to.window(original_window)
            except Exception:
                pass
        else:
            logger.debug("No new tab was opened")
        
        return False
        
    except Exception as e:
        logger.debug(f"Direct clock page navigation failed: {e}")
        return False


def _switch_to_valid_window(ctx: AppContext):
    """Switch to a valid window handle if the current one is invalid."""
    try:
        # Test if current window is valid
        _ = ctx.driver.current_url
        return True
    except Exception:
        pass
    
    # Current window is invalid, find a valid one
    try:
        handles = ctx.driver.window_handles
        logger.debug(f"Available window handles: {len(handles)}")
        if handles:
            ctx.driver.switch_to.window(handles[-1])  # Switch to most recent window
            logger.debug(f"Switched to window, URL: {ctx.driver.current_url}")
            return True
    except Exception as e:
        logger.debug(f"Failed to switch windows: {e}")
    return False


def _quiet_quit(driver):
    """Quit the driver, capturing Chrome's stderr noise into the debug log."""
    try:
        stderr_fd = sys.stderr.fileno()
        saved = os.dup(stderr_fd)
        r, w = os.pipe()
        os.dup2(w, stderr_fd)
        os.close(w)
        try:
            driver.quit()
        finally:
            os.dup2(saved, stderr_fd)
            os.close(saved)
            captured = os.read(r, 8192).decode("utf-8", errors="replace").strip()
            os.close(r)
            if captured:
                logger.debug(f"[chrome] {captured}")
    except Exception:
        try:
            driver.quit()
        except Exception:
            pass


def init_browser(headless=True, dump_dir=None):
    """Initialize a fresh Chrome browser with clean session (no cookies)."""
    chrome_options = webdriver.ChromeOptions()
    if headless:
        chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--window-size=1280,900")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-features=WebAuthentication,WebAuthenticationConditionalUI,WebAuthenticationRemoteDesktopSupport")
    chrome_options.add_experimental_option("prefs", {
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
    })
    chrome_options.add_experimental_option("excludeSwitches", ["enable-logging"])

    driver = webdriver.Chrome(options=chrome_options)
    ctx = AppContext(
        driver=driver,
        wait=WebDriverWait(driver, 25),
        mini_wait=WebDriverWait(driver, 5),
        dump_dir=dump_dir,
        logger=logger,
    )
    try:
        driver.add_virtual_authenticator(
            webdriver.common.virtual_authenticator.VirtualAuthenticatorOptions(
                protocol="ctap2", transport="internal",
                has_resident_key=True, has_user_verification=True, is_user_verified=True,
            )
        )
    except Exception:
        pass
    return ctx


def _recover_session(old_ctx, headless, dump_dir):
    """Kill old browser, start fresh one, re-authenticate. Returns new ctx or None."""
    try:
        _quiet_quit(old_ctx.driver)
    except Exception:
        pass
    new_ctx = init_browser(headless=headless, dump_dir=dump_dir)
    try:
        new_ctx.driver.get(selectors.CLOCK_PAGE_URL)
        if selectGT(new_ctx) and loginGT(new_ctx):
            print("Session recovered successfully.")
            return new_ctx
    except Exception:
        pass
    try:
        _quiet_quit(new_ctx.driver)
    except Exception:
        pass
    return None


def _clock_out_with_recovery(ctx, headless, dump_dir):
    """Attempt clock-out, recovering the session if needed. Returns (ctx, success)."""
    try:
        if clock_actions.clock_out(ctx):
            return ctx, True
    except Exception:
        logger.debug("clock_out failed, session may be dead — attempting recovery")
    new_ctx = _recover_session(ctx, headless, dump_dir)
    if new_ctx is None:
        return ctx, False
    try:
        if clock_actions.clock_out(new_ctx):
            return new_ctx, True
    except Exception:
        logger.debug("clock_out failed even after session recovery")
    return new_ctx, False


def main():
    global USERNAME, PASSWORD, DUO_TIMEOUT_SECONDS

    parser = argparse.ArgumentParser(
        description='OneUSGAutomaticClock',
        epilog='Example: uv run python clock_manager.py -m 60 --ui',
    )
    parser.add_argument('-m', '--minutes', type=float, help="Minutes to clock. Use 0 or negative to clock out immediately after clocking in. Omit when using --clock-out.")
    parser.add_argument('--clock-out', action='store_true', help='Skip clock-in and clock out immediately (recovery mode for failed clock-outs)')
    parser.add_argument('--ui', action='store_true', help='Run with visible Chrome UI (default is headless)')
    parser.add_argument('--debug', action='store_true', help='Verbose debug output and artifact dumps on failure')
    parser.add_argument('--dump-dir', default=os.environ.get('ONEUSG_DUMP_DIR', ''), help='Directory to write debug artifacts (png/html/url)')
    parser.add_argument('--duo-timeout', type=int, default=int(os.environ.get('ONEUSG_DUO_TIMEOUT', DUO_TIMEOUT_SECONDS)), help='Seconds to wait for Duo/SSO completion')
    args = vars(parser.parse_args())

    if args.get('minutes') is None and not args.get('clock_out'):
        parser.error("Either -m/--minutes or --clock-out is required")

    load_dotenv()

    if args['debug']:
        import tempfile
        log_file = tempfile.NamedTemporaryFile(
            prefix="oneusg_debug_", suffix=".log", delete=False, mode="w",
        )
        file_handler = logging.FileHandler(log_file.name)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter('[%(levelname)s %(asctime)s] %(message)s', datefmt='%H:%M:%S'))
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG)
        console_handler.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
        logging.basicConfig(level=logging.DEBUG, handlers=[console_handler, file_handler])
        logger.setLevel(logging.DEBUG)
        log_file.close()
        print(f"Debug log: {log_file.name}")
    else:
        logging.basicConfig(level=logging.INFO, format='%(message)s')
        logger.setLevel(logging.INFO)

    USERNAME = os.environ.get('ONEUSG_USERNAME')
    PASSWORD = os.environ.get('ONEUSG_PASSWORD')
    DUO_TIMEOUT_SECONDS = int(args.get('duo_timeout') or DUO_TIMEOUT_SECONDS)
    if not USERNAME:
        parser.error("ONEUSG_USERNAME must be set in .env file")
    if not PASSWORD:
        parser.error("ONEUSG_PASSWORD must be set in .env file")

    headless = not args.get('ui')
    dump_dir = args.get('dump_dir') or None
    clock_out_only = args.get('clock_out') or (args.get('minutes') is not None and args['minutes'] <= 0)
    minutes = args.get('minutes') or 0
    total_seconds = max(0, int(round(minutes * 60)))

    ensure_chromedriver_installed()

    # Prevent macOS from idle-sleeping while we're clocked in.
    caffeinate_proc = None
    if sys.platform == "darwin":
        try:
            caffeinate_proc = subprocess.Popen(["caffeinate", "-i"])
            logger.debug("caffeinate started to prevent idle sleep")
        except Exception:
            pass

    ctx = init_browser(headless=headless, dump_dir=dump_dir)

    if clock_out_only:
        print(f'\nClocking out immediately at {get_est_time_str()} (recovery / immediate clock-out mode)...\n')
    else:
        print(f'\nClocking {minutes} minutes starting at {get_est_time_str()}...\n')

    try:
        # Login with one retry on RestartRequested (idpproxy 400, stuck redirect, etc.)
        for attempt in range(2):
            try:
                ctx.driver.get(selectors.CLOCK_PAGE_URL)
                if not selectGT(ctx) or not loginGT(ctx):
                    return 1
                break
            except RestartRequested:
                if attempt == 0:
                    logger.debug("RestartRequested — closing browser and retrying")
                    try:
                        _quiet_quit(ctx.driver)
                    except Exception:
                        pass
                    ctx = init_browser(headless=headless, dump_dir=dump_dir)
                    continue
                return 1

        if clock_out_only:
            # Recovery / immediate clock-out: skip clock-in entirely.
            pass
        else:
            if not clock_actions.clock_in(ctx):
                return 1

            # Keep session alive by refreshing every 15 min, then clock out.
            # Uses wall-clock time so laptop sleep doesn't cause the countdown to drift.
            start_time = time.time()
            last_refresh_at = -1
            while True:
                elapsed = time.time() - start_time
                if elapsed >= total_seconds:
                    break

                # Refresh every 15 min (by wall clock, not sleep accumulation)
                refresh_bucket = int(elapsed) // (15 * 60)
                if last_refresh_at < refresh_bucket:
                    last_refresh_at = refresh_bucket
                    if not browser_utils.prevent_timeout(ctx):
                        print("Browser session lost. Attempting to recover...")
                        new_ctx = _recover_session(ctx, headless, dump_dir)
                        if new_ctx is None:
                            notify_user_with_ack(
                                "Clock manager - session lost",
                                "Browser session died and recovery failed. Please clock out manually!",
                                require_ack=True,
                            )
                            return 1
                        ctx = new_ctx

                remaining = total_seconds - elapsed
                time.sleep(min(60, remaining))
                elapsed = time.time() - start_time
                print(f"{int(elapsed // 60)} minutes done, roughly {max(0, (total_seconds - elapsed) / 60):.1f} minutes left to go.")

        ctx, ok = _clock_out_with_recovery(ctx, headless, dump_dir)
        if ok:
            print(f'\nNow clocked out. The current time is {get_est_time_str()}.\n')
        else:
            browser_utils.dump_artifacts(ctx, "clock_out_failed")
            notify_user_with_ack("Clock-out failed", "Could not clock out. Please clock out manually!", require_ack=True)
            return 1
        return 0
    except Exception as e:
        browser_utils.dump_artifacts(ctx, "unhandled_exception")
        if logger.level <= logging.DEBUG:
            raise
        print(f"Unexpected error. Re-run with --debug to see details.\n{e}")
        notify_user_with_ack("Clock manager error", "Unexpected error occurred. Please check the terminal and verify your timecard.", require_ack=True)
        return 1
    finally:
        try:
            if ctx and ctx.driver is not None:
                _quiet_quit(ctx.driver)
        except Exception:
            pass
        if caffeinate_proc:
            try:
                caffeinate_proc.terminate()
                caffeinate_proc.wait(timeout=5)
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
