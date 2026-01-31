import warnings
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

import sys
import time
import os
import argparse
import getpass
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, parse_qs

import logging

import browser_utils
import duo_auth
import clock_actions
import selector_defs as selectors
from browser_utils import AppContext
from notifications import notify_user_with_ack

from selenium import webdriver
import chromedriver_autoinstaller
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

from dotenv import load_dotenv
import pyotp

logger = logging.getLogger("clock_manager")


def get_est_time_str() -> str:
    """Return current time in EST as a formatted string."""
    est = timezone(timedelta(hours=-5))
    now_est = datetime.now(est)
    return now_est.strftime("%I:%M %p EST")


USERNAME = None
PASSWORD = None
MINUTES = None
DUO_TIMEOUT_SECONDS = 120
RESTART_REQUESTED = False


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


#=================================================================================================#
# Functions, each step gets its own function:

# Selecting GT:
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
        ctx.driver.quit()
        return False

    return browser_utils.check_existence(ctx, element_to_find="username", method_to_find="name")


# This function logs us in once we are at the GT login Page:
def loginGT(ctx: AppContext):
    global RESTART_REQUESTED
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

    print("...")
    print("...")
    print("Script will wait for you to authenticate on duo")
    print("If you run out of time, just run the script again")
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
                        print("...")
                        print("Detected idpproxy HTTP 400. Restarting from the beginning.")
                        global RESTART_REQUESTED
                        RESTART_REQUESTED = True
                        return False
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
        ctx.driver.quit()
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
    RESTART_REQUESTED = True
    return False


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


def main():
    global USERNAME, PASSWORD
    global MINUTES
    global DUO_TIMEOUT_SECONDS, RESTART_REQUESTED

    parser = argparse.ArgumentParser(
        description='OneUSGAutomaticClock',
        epilog='Example: uv run python clock_manager.py -m 60 --ui',
    )
    parser.add_argument('-m', '--minutes', type=float, help="Minutes to clock (required)", required=True)
    parser.add_argument('--ui', action='store_true', help='Run with visible Chrome UI (default is headless)')
    parser.add_argument('--debug', action='store_true', help='Verbose debug output and artifact dumps on failure')
    parser.add_argument('--dump-dir', default=os.environ.get('ONEUSG_DUMP_DIR', ''), help='Directory to write debug artifacts (png/html/url)')
    parser.add_argument('--duo-timeout', type=int, default=int(os.environ.get('ONEUSG_DUO_TIMEOUT', DUO_TIMEOUT_SECONDS)), help='Seconds to wait for Duo/SSO completion')
    args = vars(parser.parse_args())

    load_dotenv()

    # Set up logging level based on --debug flag
    if args['debug']:
        logging.basicConfig(level=logging.DEBUG, format='[%(levelname)s] %(message)s')
        logger.setLevel(logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO, format='%(message)s')
        logger.setLevel(logging.INFO)

    USERNAME = os.environ.get('ONEUSG_USERNAME')
    PASSWORD = os.environ.get('ONEUSG_PASSWORD')
    MINUTES = args['minutes']
    dump_dir = args.get('dump_dir') or None
    DUO_TIMEOUT_SECONDS = int(args.get('duo_timeout') or DUO_TIMEOUT_SECONDS)

    if not USERNAME:
        parser.error("ONEUSG_USERNAME must be set in .env file")
    if not PASSWORD:
        parser.error("ONEUSG_PASSWORD must be set in .env file")

    total_seconds = max(0, int(round(MINUTES * 60)))
    chromedriver_autoinstaller.install()

    def init_browser(headless=True):
        """Initialize a fresh Chrome browser with clean session (no cookies)."""
        chrome_options = webdriver.ChromeOptions()
        if headless:
            chrome_options.add_argument("--headless=new")
        chrome_options.add_argument("--window-size=1280,900")
        chrome_options.add_argument("--disable-gpu")
        # Avoid system passkey / WebAuthn prompts in Duo by disabling WebAuthn UI.
        chrome_options.add_argument("--disable-features=WebAuthentication,WebAuthenticationConditionalUI,WebAuthenticationRemoteDesktopSupport")
        # Additional prefs to disable passkey prompts
        chrome_options.add_experimental_option("prefs", {
            "credentials_enable_service": False,
            "profile.password_manager_enabled": False,
        })

        driver = webdriver.Chrome(options=chrome_options)
        wait = WebDriverWait(driver, 25)
        mini_wait = WebDriverWait(driver, 5)
        ctx = AppContext(driver=driver, wait=wait, mini_wait=mini_wait, dump_dir=dump_dir, logger=logger)

        # Set up a virtual authenticator to auto-handle WebAuthn / passkey prompts.
        try:
            driver.add_virtual_authenticator(
                webdriver.common.virtual_authenticator.VirtualAuthenticatorOptions(
                    protocol="ctap2",
                    transport="internal",
                    has_resident_key=True,
                    has_user_verification=True,
                    is_user_verified=True,
                )
            )
            logger.debug("Virtual authenticator attached")
        except Exception as e:
            logger.debug(f"Could not attach virtual authenticator: {e}")
        return ctx

    ctx = init_browser(headless=not args.get('ui'))

    print(f'\nClocking {MINUTES} minutes starting at {get_est_time_str()}...\n')
    logger.debug(f"headless={not bool(args.get('ui'))} dump_dir={dump_dir or '(disabled)'} duo_timeout={DUO_TIMEOUT_SECONDS}s")

    try:
        # Retry login flow once if idpproxy HTTP 400 occurs
        for attempt in range(2):
            RESTART_REQUESTED = False
            ctx.driver.get(selectors.CLOCK_PAGE_URL)
            if not selectGT(ctx):
                return 1
            if not loginGT(ctx):
                if RESTART_REQUESTED and attempt == 0:
                    logger.debug("Restarting login flow after idpproxy 400 - closing browser and starting fresh")
                    # Close the browser completely and start a fresh one with no cookies
                    try:
                        ctx.driver.quit()
                    except Exception:
                        pass
                    ctx = init_browser(headless=not args.get('ui'))
                    continue
                return 1
            if not clock_actions.clock_in(ctx):
                return 1
            break

        # This is a little loop to make sure we prevent timeouts and to keep track of how long its been
        # It just refreshes the page every fifteen minutes and keeps track of how much time has passed.
        if total_seconds == 0:
            clock_actions.clock_out(ctx)
            print(f'\nNow clocked out. The current time is {get_est_time_str()}.\n')
            return 0

        elapsed_seconds = 0
        refresh_interval = 15 * 60
        while elapsed_seconds < total_seconds:
            if elapsed_seconds == 0 or (elapsed_seconds % refresh_interval) == 0:
                browser_utils.prevent_timeout(ctx)

            remaining_seconds = total_seconds - elapsed_seconds
            sleep_chunk = min(60, remaining_seconds)
            time.sleep(sleep_chunk)
            elapsed_seconds += sleep_chunk

            minutes_done = int(elapsed_seconds // 60)
            minutes_left = max(0, round((total_seconds - elapsed_seconds) / 60, 2))
            print(f"{minutes_done} minutes done, roughly {minutes_left} minutes left to go.")
            print("...")

            if elapsed_seconds >= total_seconds:
                clock_actions.clock_out(ctx)
                print(f'\nNow clocked out. The current time is {get_est_time_str()}.\n')
                break

        # This is just another safety check to make sure we don't ever leave without clocking out first.
        else:
            try:
                clock_actions.clock_out(ctx)
                print(f'\nNow clocked out. The current time is {get_est_time_str()}.\n')
            except Exception:
                browser_utils.dump_artifacts(ctx, "clock_out_exception")
                print("Make sure you were clocked out please.")
        return 0
    except Exception as e:
        browser_utils.dump_artifacts(ctx, "unhandled_exception")
        if logger.level <= logging.DEBUG:
            raise
        print("...")
        print("Unexpected error. Re-run with --debug to see details and save a screenshot/HTML.")
        print(str(e))
        notify_user_with_ack(
            "Clock manager error",
            "Unexpected error occurred. Please check the terminal output and verify your timecard.",
            require_ack=True,
        )
        return 1
    finally:
        try:
            if ctx and ctx.driver is not None:
                ctx.driver.quit()
                ctx.driver = None
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
