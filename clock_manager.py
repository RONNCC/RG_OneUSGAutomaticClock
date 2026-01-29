import sys
import time
import os
import argparse
import getpass
import warnings
import subprocess
from datetime import datetime
from urllib.parse import urlparse, parse_qs

# Set up logging
import logging
logger = logging.getLogger("clock_manager")

# Suppress verbose selenium/urllib3 logging
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("selenium").setLevel(logging.WARNING)
logging.getLogger("selenium.webdriver.remote.remote_connection").setLevel(logging.WARNING)

warnings.filterwarnings(
    "ignore",
    message=r".*only supports OpenSSL.*",
    category=Warning,
    module=r"urllib3",
)

from selenium import webdriver
import chromedriver_autoinstaller

from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support.ui import Select
from selenium.webdriver.common.keys import Keys

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC

from selenium.common.exceptions import TimeoutException
from selenium.common.exceptions import NoSuchElementException
from selenium.common.exceptions import ElementNotInteractableException

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

try:
    import pyotp
except ImportError:
    pyotp = None

try:
    from plyer import notification as plyer_notification
except Exception:
    plyer_notification = None

HOTP_COUNTER_FILE = None

# (By the way for newcomers, variables with CAPITAL LETTERS imply they are a global variable.)
USERNAME = None
PASSWORD = None

MINUTES = None
TIME_BLOCKS = None
BLOCKS_DONE = 0

DRIVER = None
WAIT = None
MINI_WAIT = None

DUMP_DIR = None
DUO_TIMEOUT_SECONDS = 120
RESTART_REQUESTED = False


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _debug_write_secret(label: str, value: str) -> None:
    """Write sensitive debug info to a local file only (never print to stdout)."""
    if logger.level > logging.DEBUG:
        return
    if os.environ.get("ONEUSG_DEBUG_WRITE_SECRETS", "").strip() not in ("1", "true", "yes"):
        return
    if not DUMP_DIR:
        return
    try:
        os.makedirs(DUMP_DIR, exist_ok=True)
        path = os.path.join(DUMP_DIR, "secrets-debug.log")
        ts = _timestamp()
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{ts} {label}: {value}\n")
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
    except Exception:
        pass


def get_duo_passcode() -> str:
    """
    Get Duo passcode - either from HOTP secret (preferred) or static passcode.
    
    Environment variables:
      ONEUSG_DUO_HOTP_SECRET - Base32-encoded HOTP secret from Duo enrollment
      ONEUSG_DUO_HOTP_COUNTER_FILE - File to persist HOTP counter (default: ~/.duo_hotp_counter)
      ONEUSG_DUO_PASSCODE - Static passcode fallback (not recommended)
    
    For HOTP setup:
      1. In Duo, choose "Enter a passcode" → "Get a new passcode" 
      2. When it shows the QR code, decode it to get the secret
      3. Or use Duo's HOTP activation to get the secret directly
      4. Set ONEUSG_DUO_HOTP_SECRET=<your-base32-secret>
    """
    # Prefer otpauth URI if provided (supports HOTP or TOTP).
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
                    _debug_write_secret("TOTP", code)
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
                    _debug_write_secret("HOTP", code)
                    return code
        except Exception as e:
            logger.debug(f"OTP URI parsing failed: {e}")

    # Try HOTP secret directly
    hotp_secret = os.environ.get("ONEUSG_DUO_HOTP_SECRET", "")
    if hotp_secret and pyotp:
        counter_file = os.environ.get(
            "ONEUSG_DUO_HOTP_COUNTER_FILE",
            os.path.expanduser("~/.duo_hotp_counter")
        )

        # Read current counter
        counter = 0
        try:
            if os.path.exists(counter_file):
                with open(counter_file, "r") as f:
                    counter = int(f.read().strip())
        except Exception:
            counter = 0

        # Generate HOTP code
        try:
            hotp = pyotp.HOTP(hotp_secret)
            code = hotp.at(counter)

            # Increment and save counter
            with open(counter_file, "w") as f:
                f.write(str(counter + 1))

            logger.debug(f"Generated HOTP code (counter={counter})")
            _debug_write_secret("HOTP", code)
            return code
        except Exception as e:
            logger.debug(f"HOTP generation failed: {e}")
    
    # Fallback to static passcode
    return os.environ.get("ONEUSG_DUO_PASSCODE", "")


def _mask_code(code: str) -> str:
    if not code:
        return ""
    # At debug level, show full code for testing
    if logger.level <= logging.DEBUG:
        return code
    if len(code) <= 2:
        return "*" * len(code)
    return "*" * (len(code) - 2) + code[-2:]


def notify_user(title: str, message: str) -> None:
    notify_user_with_ack(title, message, require_ack=False)


def _escape_osascript(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\"", "\\\"")


def notify_user_with_ack(title: str, message: str, require_ack: bool = False) -> None:
    if require_ack and sys.platform == "darwin":
        try:
            title_escaped = _escape_osascript(title)
            message_escaped = _escape_osascript(message)
            script = f'display alert "{title_escaped}" message "{message_escaped}" buttons {{"OK"}} default button "OK"'
            subprocess.run(["osascript", "-e", script], check=False)
            return
        except Exception:
            pass
    if plyer_notification is not None:
        try:
            plyer_notification.notify(
                title=title,
                message=message,
                app_name="OneUSGAutomaticClock",
                timeout=10,
            )
        except Exception:
            pass
    print(message)


def get_last_action_text() -> str:
    candidates = [
        "TL_WEB_CLOCK_WK_DESCR50_1",
        "TL_RPTD_SFF_WK_DESCR50_1",
    ]
    for element_id in candidates:
        try:
            el = DRIVER.find_element(By.ID, element_id)
            if el and el.text:
                return el.text.strip()
        except Exception:
            continue
    return ""


def is_already_clocked_out() -> bool:
    last_action = get_last_action_text().lower()
    if not last_action:
        return False
    return "out" in last_action


def find_duo_passcode_input(timeout=6):
    """Find the Duo passcode input in the rendered prompt UI."""
    # From the artifact HTML: <input name="passcode-input" id="passcode-input" class="passcode-input ...">
    candidates = [
        (By.ID, "passcode-input"),
        (By.NAME, "passcode-input"),
        (By.CSS_SELECTOR, "#passcode-input"),
        (By.CSS_SELECTOR, "input[name='passcode-input']"),
        (By.CSS_SELECTOR, "input.passcode-input"),
        (By.NAME, "passcode"),
        (By.ID, "passcode"),
        (By.CSS_SELECTOR, "input[name='passcode']"),
        (By.CSS_SELECTOR, "input[aria-label='Passcode']"),
        (By.CSS_SELECTOR, "input[inputmode='numeric']"),
    ]
    try:
        el = find_first(candidates, timeout=timeout, clickable=True)
        if el:
            logger.debug(f"Found passcode input: id={el.get_attribute('id')}, name={el.get_attribute('name')}")
        return el
    except Exception:
        return None


def find_duo_verify_button(timeout=6):
    # From the artifact HTML: <button ... class="... verify-button ..." data-testid="verify-button">Verify</button>
    candidates = [
        (By.CSS_SELECTOR, "button[data-testid='verify-button']"),
        (By.CSS_SELECTOR, "button.verify-button"),
        (By.XPATH, "//button[@type='submit' and normalize-space()='Verify']"),
        (By.XPATH, "//button[contains(.,'Verify')]"),
        (By.CSS_SELECTOR, "button[type='submit']"),
        (By.XPATH, "//button[contains(.,'Log in')]"),
    ]
    try:
        el = find_first(candidates, timeout=timeout, clickable=False)
        if el:
            disabled = el.get_attribute('disabled')
            logger.debug(f"Found Verify button: disabled={disabled}")
        return el
    except Exception:
        return None


def _set_input_value(el, value: str) -> None:
    logger.debug(f"_set_input_value called with value: {value}")
    try:
        el.click()
    except Exception:
        pass

    # Clear existing value - use select-all approach for React inputs
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

    # Try normal typing first - often required to enable the Verify button
    try:
        el.send_keys(value)
        logger.debug("send_keys completed")
    except Exception as e:
        logger.debug(f"send_keys failed: {e}")
        try:
            from selenium.webdriver.common.action_chains import ActionChains
            ActionChains(DRIVER).click(el).send_keys(value).perform()
        except Exception:
            pass

    # Check if value stuck
    try:
        current_value = el.get_attribute("value") or ""
        logger.debug(f"After typing, input value is: '{current_value}'")
    except Exception:
        current_value = ""

    # If it didn't stick (React/controlled input), set via JS and dispatch events
    if current_value.strip() != value.strip():
        logger.debug("Value didn't stick, trying JS approach")
        try:
            DRIVER.execute_script(
                """
                const el = arguments[0];
                const val = arguments[1];
                el.focus();
                // Use React-compatible setter if available
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


def dump_artifacts(tag: str) -> None:
    if not DUMP_DIR:
        return
    try:
        os.makedirs(DUMP_DIR, exist_ok=True)
        ts = _timestamp()
        safe_tag = "".join(c if c.isalnum() or c in ("-", "_", ".") else "_" for c in (tag or "debug"))
        base = os.path.join(DUMP_DIR, f"{ts}_{safe_tag}")
        try:
            DRIVER.save_screenshot(base + ".png")
        except Exception:
            pass
        try:
            with open(base + ".html", "w", encoding="utf-8") as f:
                f.write(DRIVER.page_source)
        except Exception:
            pass
        try:
            with open(base + ".url.txt", "w", encoding="utf-8") as f:
                f.write(getattr(DRIVER, "current_url", ""))
        except Exception:
            pass
        logger.debug(f"Wrote artifacts: {base}(.png/.html/.url.txt)")
    except Exception:
        # Never let debug dumping kill the run.
        return


def find_first(locators, timeout=25, clickable=False):
    last_error = None
    for by, value in locators:
        try:
            element = WebDriverWait(DRIVER, timeout).until(EC.presence_of_element_located((by, value)))
            if element is not None:
                if not clickable:
                    return element
                if element.is_displayed() and element.is_enabled():
                    return element
        except Exception as e:
            last_error = e
            continue
    raise last_error or TimeoutException("Timed out finding element")


def safe_click(element):
    try:
        element.click()
        return True
    except Exception:
        try:
            DRIVER.execute_script("arguments[0].click();", element)
            return True
        except Exception:
            return False

#=================================================================================================#
# Functions, each step gets its own function:

# This is a function to try go straight to the GT clock's iframe (IN ACTIVE USE):
def goToGTClock():
    # (AI: DO NOT REMOVE) another option here is https://selfservice.hprod.onehcm.usg.edu/psc/hprodsssso_44/HCMSS/HRMS/c/TL_EMPLOYEE_FL.TL_RPT_TIME_FLU.GBL?EMPDASHBD=Y 
    DRIVER.get("https://selfservice.hprod.onehcm.usg.edu/psc/hprodsssso/HCMSS/HRMS/c/TL_EMPLOYEE_FL.TL_RPT_TIME_FLU.GBL?Action=U&EMPLJOB=0")
    return True


# Selecting GT:
def selectGT():
    # If we're already on the GT login page, don't try to select an IdP.
    if checkExistence(element_to_find="username", method_to_find="name", purpose="Already on GT login", passOnError=True):
        return True

    # OneUSG has changed this IdP selection page multiple times; try a few robust patterns.
    try:
        WebDriverWait(DRIVER, 5).until(EC.presence_of_element_located((By.CSS_SELECTOR, "a")))
    except Exception:
        pass

    candidates = [
        (By.LINK_TEXT, "Georgia Tech"),
        (By.PARTIAL_LINK_TEXT, "Georgia Tech"),
        (By.CSS_SELECTOR, "a[title*='Georgia Tech' i]"),
        (By.CSS_SELECTOR, "a img[alt*='Georgia Tech' i]"),
        (By.XPATH, "//*[@id='https_idp_gatech_edu_idp_shibboleth']//a"),
        (By.XPATH, "//*[@id='https_idp_gatech_edu_idp_shibboleth']"),
        (By.XPATH, "//a[contains(@href,'gatech') or contains(@href,'gatech.edu') or contains(.,'Georgia Tech') or contains(.,'Georgia Institute') or .//img[contains(@alt,'Georgia') or contains(@src,'gatech')]]"),
        (By.XPATH, "//button[contains(.,'Georgia Tech') or contains(.,'Georgia Institute') or contains(.,'Gatech')]"),
    ]

    try:
        gt_option = find_first(candidates, timeout=5, clickable=True)
        # If we matched the img inside a link, click the parent anchor.
        try:
            if gt_option.tag_name.lower() == "img":
                gt_option = gt_option.find_element(By.XPATH, "./ancestor::a[1]")
        except Exception:
            pass

        if not safe_click(gt_option):
            raise TimeoutException("Unable to click GT IdP option")
    except TimeoutException:
        # Fallback: try a JS lookup by link text or image alt.
        try:
            gt_js = DRIVER.execute_script(
                """
                const links = Array.from(document.querySelectorAll('a'));
                return links.find(a => /Georgia Tech/i.test(a.textContent || '')
                    || /Georgia Tech/i.test(a.getAttribute('title') || '')
                    || (a.querySelector('img') && /Georgia Tech/i.test(a.querySelector('img').alt || '')));
                """
            )
            if gt_js is not None and safe_click(gt_js):
                return checkExistence(element_to_find="username", method_to_find="name", purpose="Selecting GT")
        except Exception:
            pass

        dump_artifacts("select_gt_not_found")
        print("...")
        print("Unable to find the Georgia Tech IdP selector on the OneUSG page.")
        print("This usually means the IdP selection DOM changed.")
        print("If you re-run with --debug, the script will save a screenshot + HTML for updating selectors.")
        DRIVER.quit()
        return False

    return checkExistence(element_to_find="username", method_to_find="name", purpose="Selecting GT")


# This function logs us in once we are at the GT login Page:
def loginGT():
    global RESTART_REQUESTED
    gatech_login_username = find_first([(By.NAME, "username"), (By.ID, "username")], timeout=25)
    gatech_login_password = find_first([(By.NAME, "password"), (By.ID, "password")], timeout=25)

    gatech_login_username.clear()
    gatech_login_username.send_keys(USERNAME)
    gatech_login_password.clear()
    gatech_login_password.send_keys(PASSWORD)

    submit_button = find_first(
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
                current_url = DRIVER.current_url or ""
                if "idpproxy.usg.edu/asimba/profiles/saml2" in current_url:
                    page = DRIVER.page_source or ""
                    if "HTTP ERROR 400" in page or "Bad Request" in page:
                        dump_artifacts("idpproxy_400")
                        print("...")
                        print("Detected idpproxy HTTP 400. Restarting from the beginning.")
                        global RESTART_REQUESTED
                        RESTART_REQUESTED = True
                        return False
            except Exception:
                pass
            
            # Check if we've successfully logged in (supports old and new UI)
            if isOnClockPage(passOnError=True):
                logger.debug("Successfully found clock page element!")
                break
            
            # Try Duo automation
            try_duo_other_options()
            
            # Fail-fast: detect if we're stuck on the same page
            current_url = DRIVER.current_url or ""
            if iteration % 5 == 0:
                logger.debug(f"Iteration {iteration}, URL: {current_url[:80]}...")
            
            if current_url == last_url:
                stuck_count += 1
                if stuck_count >= MAX_STUCK_ITERATIONS:
                    dump_artifacts("stuck_state")
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
        dump_artifacts("duo_timeout")
        print("...")
        print("Timed out waiting for Duo / OneUSG to finish login.")
        print("If Duo prompts are taking longer, re-run with a higher timeout: --duo-timeout 300")
        DRIVER.quit()
        return False

    # Handle window switching - OneUSG sometimes opens new windows or the original closes
    try:
        _switch_to_valid_window()
    except Exception as e:
        logger.debug(f"Window switch error: {e}")

    # Sometimes the PeopleSoft frame doesn't fully render after auth; wait and refresh
    logger.debug(f"Post-auth URL: {DRIVER.current_url}")
    
    # Give the page extra time after Duo - OneUSG backend auth can be slow
    print("Waiting for OneUSG authentication to complete...")
    time.sleep(6)
    
    # Check if we need to refresh to get to the clock page
    for attempt in range(3):
        try:
            if isOnClockPage(passOnError=True):
                logger.debug("Clock page element found after login")
                return True
        except Exception:
            pass
        
        logger.debug(f"Clock element not found, attempt {attempt + 1}/3, refreshing...")
        
        try:
            # Try switching windows again in case a new one opened
            _switch_to_valid_window()
            DRIVER.refresh()
            time.sleep(3)
        except Exception as e:
            logger.debug(f"Refresh attempt {attempt + 1} error: {e}")
    
    # Fallback: OneUSG auth sometimes gets stuck after Duo redirect.
    # Try opening the clock page directly in a new tab as a workaround.
    logger.debug("Attempting direct clock page navigation in new tab...")
    print("OneUSG redirect seems stuck, trying direct navigation...")
    
    if _try_direct_clock_page_navigation():
        return True
    
    dump_artifacts("post_auth_no_clock")
    
    # If direct navigation also failed, request a full restart
    print("Authentication redirect failed. Will restart from the beginning...")
    RESTART_REQUESTED = True
    return False


def _try_direct_clock_page_navigation():
    """
    Fallback for when OneUSG authentication redirect gets stuck after Duo.
    
    Opens the clock page URL directly in a new tab, which can bypass the stuck
    redirect since the session should already be authenticated.
    
    Returns True if successfully navigated to clock page, False otherwise.
    """
    CLOCK_PAGE_URL = "https://selfservice.hprod.onehcm.usg.edu/psc/hprodsssso/HCMSS/HRMS/c/TL_EMPLOYEE_FL.TL_RPT_TIME_FLU.GBL?Action=U&EMPLJOB=0&"
    
    try:
        # Remember original window
        original_window = DRIVER.current_window_handle
        original_handles = set(DRIVER.window_handles)
        
        # Open clock page in new tab
        logger.debug(f"Opening clock page directly in new tab: {CLOCK_PAGE_URL}")
        DRIVER.execute_script(f"window.open('{CLOCK_PAGE_URL}', '_blank');")
        time.sleep(3)
        
        # Switch to the new tab
        new_handles = set(DRIVER.window_handles) - original_handles
        if new_handles:
            new_tab = new_handles.pop()
            DRIVER.switch_to.window(new_tab)
            logger.debug(f"Switched to new tab, URL: {DRIVER.current_url}")
            
            # Give it extra time to load
            time.sleep(4)
            
            # Check if we're now on the clock page
            for check_attempt in range(3):
                if isOnClockPage(passOnError=True):
                    logger.debug("Successfully reached clock page via direct navigation!")
                    print("Direct navigation successful!")
                    
                    # Close the original stuck tab
                    try:
                        DRIVER.switch_to.window(original_window)
                        DRIVER.close()
                        DRIVER.switch_to.window(new_tab)
                        logger.debug("Closed original stuck tab")
                    except Exception as e:
                        logger.debug(f"Could not close original tab: {e}")
                        # Make sure we're still on the new tab
                        try:
                            DRIVER.switch_to.window(new_tab)
                        except Exception:
                            pass
                    
                    return True
                
                logger.debug(f"Direct nav check {check_attempt + 1}/3, waiting...")
                time.sleep(2)
            
            # New tab didn't work either - close it and return to original
            logger.debug("Direct navigation did not reach clock page")
            try:
                DRIVER.close()
                DRIVER.switch_to.window(original_window)
            except Exception:
                pass
        else:
            logger.debug("No new tab was opened")
        
        return False
        
    except Exception as e:
        logger.debug(f"Direct clock page navigation failed: {e}")
        return False


def _switch_to_valid_window():
    """Switch to a valid window handle if the current one is invalid."""
    try:
        # Test if current window is valid
        _ = DRIVER.current_url
        return True
    except Exception:
        pass
    
    # Current window is invalid, find a valid one
    try:
        handles = DRIVER.window_handles
        logger.debug(f"Available window handles: {len(handles)}")
        if handles:
            DRIVER.switch_to.window(handles[-1])  # Switch to most recent window
            logger.debug(f"Switched to window, URL: {DRIVER.current_url}")
            return True
    except Exception as e:
        logger.debug(f"Failed to switch windows: {e}")
    return False


def dismiss_passkey_dialog():
    """Press Escape to dismiss native passkey/WebAuthn dialog."""
    try:
        from selenium.webdriver.common.action_chains import ActionChains
        ActionChains(DRIVER).send_keys(Keys.ESCAPE).perform()
        time.sleep(0.3)
    except Exception:
        pass


def handle_duo_device_trust_prompt():
    """Handle Duo's 'Is this your device?' prompt by clicking 'Yes, this is my device'."""
    try:
        if "Is this your device?" not in (DRIVER.page_source or ""):
            return False
        
        logger.debug("Detected 'Is this your device?' prompt")
        
        # Try clicking "No, other people use this device" first - this avoids the 
        # device trust flow which can cause idpproxy 400 errors
        no_candidates = [
            (By.XPATH, "//button[contains(.,'No')]"),
            (By.XPATH, "//a[contains(.,'No')]"),
            (By.CSS_SELECTOR, "button.negative"),
        ]
        
        for by, value in no_candidates:
            try:
                btn = WebDriverWait(DRIVER, 2).until(EC.element_to_be_clickable((by, value)))
                if btn and safe_click(btn):
                    logger.debug("Clicked 'No' on device trust prompt")
                    time.sleep(3)  # Wait for redirect without refreshing
                    return True
            except Exception:
                continue
        
        # Fallback: try Yes button if No button not found
        yes_candidates = [
            (By.XPATH, "//button[contains(.,'Yes')]"),
            (By.CSS_SELECTOR, "button.positive"),
        ]
        
        for by, value in yes_candidates:
            try:
                btn = WebDriverWait(DRIVER, 2).until(EC.element_to_be_clickable((by, value)))
                if btn and safe_click(btn):
                    logger.debug("Clicked 'Yes' on device trust prompt")
                    time.sleep(3)  # Wait for redirect without refreshing
                    return True
            except Exception:
                continue
        
        return False
    except Exception:
        return False


def handle_touchid_canceled_prompt():
    """Handle the 'Couldn't use Touch ID' page by clicking 'Other options'."""
    try:
        page = DRIVER.page_source or ""
        if "Couldn't use Touch ID" not in page and "Touch ID has been canceled" not in page:
            return False

        logger.debug("Detected Touch ID canceled; clicking 'Other options'")

        try:
            option = find_first([(By.LINK_TEXT, "Other options"), (By.PARTIAL_LINK_TEXT, "Other options")], timeout=1, clickable=True)
            if safe_click(option):
                return True
        except Exception:
            pass

        return DRIVER.execute_script(
            "const t = Array.from(document.querySelectorAll('button, a')).find(el => /Other options/i.test(el.textContent));"
            "if (t) { t.click(); return true; } return false;"
        ) or False
    except Exception:
        return False


def handle_duo_other_options_page():
    """Handle the 'Other options to log in' page by clicking 'Duo Mobile passcode'."""
    try:
        if "Other options to log in" not in (DRIVER.page_source or ""):
            return False

        logger.debug("Detected 'Other options to log in'; clicking 'Duo Mobile passcode'")

        passcode_candidates = [
            (By.XPATH, "//*[contains(text(),'Duo Mobile passcode')]"),
            (By.XPATH, "//button[contains(.,'Duo Mobile passcode')]"),
        ]

        for by, value in passcode_candidates:
            try:
                option = WebDriverWait(DRIVER, 2).until(EC.element_to_be_clickable((by, value)))
                if option and safe_click(option):
                    time.sleep(0.5)
                    return True
            except Exception:
                continue

        # JS fallback - find and click element containing "Duo Mobile passcode"
        clicked = DRIVER.execute_script(
            "const t = Array.from(document.querySelectorAll('button, a, div, li')).find(el => /Duo Mobile passcode/i.test(el.textContent));"
            "if (t) { t.click(); return true; } return false;"
        )
        if clicked:
            time.sleep(0.5)
            return True
        return False
    except Exception:
        return False


def try_duo_other_options():
    """Try various Duo prompt handlers and passcode entry."""
    try:
        dismiss_passkey_dialog()

        # Handle special pages first
        if handle_touchid_canceled_prompt() or handle_duo_device_trust_prompt() or handle_duo_other_options_page():
            return True

        # Try in top-level document
        if _click_duo_other_options_in_context():
            return True

        # Try inside iframes
        frames = DRIVER.find_elements(By.TAG_NAME, "iframe")
        
        # If page looks empty, refresh
        if len(frames) == 0:
            try:
                if len(DRIVER.find_element(By.TAG_NAME, "body").text.strip()) < 100:
                    logger.debug("Page appears empty, refreshing...")
                    DRIVER.refresh()
                    time.sleep(2)
                    return True
            except Exception:
                pass
        
        for f in frames:
            try:
                src = (f.get_attribute("src") or "").lower()
                if ("duo" in src) or (not src) or ("about:blank" in src):
                    DRIVER.switch_to.frame(f)
                    handle_duo_device_trust_prompt()
                    if _click_duo_other_options_in_context():
                        return True
            finally:
                try:
                    DRIVER.switch_to.default_content()
                except Exception:
                    pass
        return False
    except Exception:
        return False


def _click_duo_other_options_in_context():
    try:
        # Send Escape to dismiss any native OS passkey / WebAuthn dialogs.
        try:
            from selenium.webdriver.common.action_chains import ActionChains
            ActionChains(DRIVER).send_keys(Keys.ESCAPE).perform()
            time.sleep(0.3)
        except Exception:
            pass

        # If we're already on the passcode screen, skip "Other options" and just fill it.
        passcode_input = find_duo_passcode_input(timeout=2)
        if passcode_input is None:
            candidates = [
                (By.LINK_TEXT, "Other options"),
                (By.PARTIAL_LINK_TEXT, "Other options"),
                (By.XPATH, "//button[contains(.,'Other options')]"),
                (By.XPATH, "//a[contains(.,'Other options')]"),
                (By.CSS_SELECTOR, ".other-options-link"),
            ]
            try:
                option = find_first(candidates, timeout=3, clickable=True)
                option.click()
            except Exception:
                # Fallback: JS click by class or text.
                try:
                    clicked = DRIVER.execute_script(
                        """
                        const byClass = document.querySelector('.other-options-link');
                        if (byClass) { byClass.click(); return true; }
                        const buttons = Array.from(document.querySelectorAll('button, a'));
                        const target = buttons.find(el => /Other options/i.test(el.textContent || ''));
                        if (target) { target.click(); return true; }
                        return false;
                        """
                    )
                    if not clicked:
                        return False
                except Exception:
                    return False

        # If not already on passcode screen, choose passcode option.
        if passcode_input is None:
            passcode_candidates = [
                (By.XPATH, "//button[contains(.,'Duo Mobile passcode')]"),
                (By.XPATH, "//a[contains(.,'Duo Mobile passcode')]"),
                (By.XPATH, "//button[contains(.,'Passcode')]"),
                (By.XPATH, "//a[contains(.,'Passcode')]"),
            ]
            try:
                passcode = find_first(passcode_candidates, timeout=3, clickable=True)
                passcode.click()
            except Exception:
                # Fallback: JS click by text
                try:
                    DRIVER.execute_script(
                        """
                        const items = Array.from(document.querySelectorAll('button, a, div, span'));
                        const target = items.find(el => /Duo Mobile passcode/i.test(el.textContent || '') || /Passcode/i.test(el.textContent || ''));
                        if (target) { target.click(); return true; }
                        return false;
                        """
                    )
                except Exception:
                    return True

        # Re-locate passcode input after navigation
        passcode_input = find_duo_passcode_input(timeout=6)

        duo_passcode = get_duo_passcode()
        logger.debug(f"duo_passcode={duo_passcode}, passcode_input={passcode_input is not None}")
        if duo_passcode and passcode_input is not None:
            try:
                time.sleep(0.4)
                _set_input_value(passcode_input, duo_passcode)
                logger.debug(f"Entered Duo passcode into prompt: {duo_passcode}")

                # If Verify is still disabled, re-assert the value once.
                time.sleep(0.3)
                try:
                    btn_probe = find_duo_verify_button(timeout=2)
                    if btn_probe is not None:
                        disabled = btn_probe.get_attribute("disabled")
                        logger.debug(f"Verify button disabled={disabled}")
                        if disabled:
                            logger.debug("Re-asserting passcode value...")
                            _set_input_value(passcode_input, duo_passcode)
                            time.sleep(0.3)
                except Exception:
                    pass

                # Wait for Verify to become enabled, then click.
                start = time.time()
                while time.time() - start < 10:
                    btn = find_duo_verify_button(timeout=2)
                    if btn is not None:
                        try:
                            disabled = btn.get_attribute("disabled")
                            aria_disabled = btn.get_attribute("aria-disabled")
                            logger.debug(f"Verify button check: disabled={disabled}, aria_disabled={aria_disabled}")
                            if disabled or (aria_disabled and aria_disabled.lower() == "true"):
                                time.sleep(0.5)
                                continue
                        except Exception:
                            pass
                        logger.debug("Attempting to click Verify button...")
                        if safe_click(btn):
                            logger.debug("Verify button clicked successfully")
                            break
                    time.sleep(0.5)
                else:
                    # Last resort: hit Enter.
                    logger.debug("Verify button never enabled, trying Enter key...")
                    try:
                        passcode_input.send_keys(Keys.RETURN)
                    except Exception:
                        pass
            except Exception as e:
                logger.debug(f"Failed to submit passcode: {e}")
        return True
    except Exception:
        return False


# This function clocks us in:
def clockHoursIn():
    """Clock in using the punch type dropdown."""
    return selectPunchTypeAndSubmit("1", "In")


def selectPunchTypeAndSubmit(punch_value, punch_name):
    """Select punch type from dropdown and submit.
    
    Args:
        punch_value: "1" for In, "2" for Out, "3" for Meal
        punch_name: Display name for logging
    """
    try:
        if punch_name.lower() == "out" and is_already_clocked_out():
            notify_user(
                "Already clocked out",
                "NOTICE: You're already marked as clocked Out. This usually means the last session didn't stop cleanly or you already clocked out. Please verify your timecard.",
            )
            return False
        # Wait for the punch type dropdown to be present
        punch_dropdown = find_first(
            [
                (By.ID, "TL_RPTD_TIME_PUNCH_TYPE$0"),
                (By.CSS_SELECTOR, "select[id*='PUNCH_TYPE']"),
                (By.XPATH, "//select[contains(@id,'PUNCH_TYPE')]"),
            ],
            timeout=30,
            clickable=True,
        )
        
        logger.debug("Found punch type dropdown")
        
        # Use Select to choose the punch type
        select = Select(punch_dropdown)
        select.select_by_value(punch_value)
        
        logger.debug(f"Selected punch type: {punch_name} (value={punch_value})")
        
        # Wait a moment for any page updates after selection
        time.sleep(1)
        
        # Find and click the Submit button
        submit_button = find_first(
            [
                (By.ID, "TL_WEB_CLOCK_WK_TL_SAVE_PB"),
                (By.XPATH, "//a[@id='TL_WEB_CLOCK_WK_TL_SAVE_PB']"),
                (By.XPATH, "//a[contains(@class,'ps-button') and contains(.,'Submit')]"),
                (By.XPATH, "//*[self::a or self::button][normalize-space()='Submit']"),
            ],
            timeout=10,
            clickable=True,
        )
        
        logger.debug("Found Submit button, clicking...")
        
        safe_click(submit_button)
        
        # Wait for confirmation - check that last action updates
        time.sleep(2)
        
        try:
            last_action_el = DRIVER.find_element(By.ID, "TL_WEB_CLOCK_WK_DESCR50_1")
            last_action_text = last_action_el.text if last_action_el else ""
            logger.debug(f"Last action after submit: {last_action_text}")
        except Exception:
            pass
        
        print(f"You Have Clocked {punch_name}!")
        print("...")
        return True
        
    except Exception as e:
        if punch_name.lower() == "out" and is_already_clocked_out():
            notify_user_with_ack(
                "Already clocked out",
                "NOTICE: You're already marked as clocked Out. This usually means the last session didn't stop cleanly or you already clocked out. Please verify your timecard.",
                require_ack=True,
            )
            return False
        logger.debug(f"Failed to clock {punch_name}: {e}")
        last_action = get_last_action_text()
        dump_artifacts(f"clock_{punch_name.lower()}_failed")
        print(f"Failed to Clock {punch_name}")
        if last_action:
            print(f"Last action text: {last_action}")
        print(f"Error summary: {type(e).__name__}: {e}")
        notify_user_with_ack(
            f"Clock {punch_name} failed",
            "Clock action failed. Please check the terminal output and verify your timecard.",
            require_ack=True,
        )
        print("NOTICE: Please Manually Clock to Avoid Issues")
        return False


# This function clocks us out:
def clockHoursOut():
    """Clock out using the punch type dropdown."""
    return selectPunchTypeAndSubmit("2", "Out")


def isOnClockPage(passOnError=True):
    """Check if we're on the clock page (supports both old and new UI).
    
    Returns True if either:
    - Old UI: TL_RPTD_SFF_WK_GROUPBOX$PIMG exists
    - New UI: Punch type dropdown (TL_RPTD_TIME_PUNCH_TYPE$0) exists
    """
    # Try new UI element first (punch type dropdown)
    try:
        el = DRIVER.find_element(By.ID, "TL_RPTD_TIME_PUNCH_TYPE$0")
        if el:
            logger.debug("Found clock page (new UI - punch type dropdown)")
            return True
    except Exception:
        pass
    
    # Try old UI element
    try:
        el = DRIVER.find_element(By.ID, "TL_RPTD_SFF_WK_GROUPBOX$PIMG")
        if el:
            logger.debug("Found clock page (old UI - groupbox)")
            return True
    except Exception:
        pass
    
    # Also check for Submit button as a fallback
    try:
        el = DRIVER.find_element(By.ID, "TL_WEB_CLOCK_WK_TL_SAVE_PB")
        if el:
            logger.debug("Found clock page (Submit button)")
            return True
    except Exception:
        pass
    
    if not passOnError:
        print("Not on clock page")
    return False


# This function checks to make sure you did duo correctly:
def checkLogin():
    return isOnClockPage(passOnError=False)


# This function opens the clocing menu (legacy - may not be needed for new UI):
def openMenu():
    # Ensure we're on the clock page
    if not isOnClockPage(passOnError=True):
        return False

    # If menu already open, just return
    if checkExistence(element_to_find="win1divTL_RPTD_SFF_WK_GROUPBOXgrp", method_to_find="id", passOnError=True):
        return True

    # Try to open the actions menu (three-dot menu)
    candidates = [
        (By.ID, "TL_RPTD_SFF_WK_GROUPBOX$PIMG"),
        (By.XPATH, "//*[@id='TL_RPTD_SFF_WK_GROUPBOX$PIMG']"),
        (By.XPATH, "//*[contains(@id,'TL_RPTD_SFF_WK_GROUPBOX') and (self::a or self::button or self::img)]"),
    ]

    try:
        menu_trigger = find_first(candidates, timeout=10, clickable=True)
        safe_click(menu_trigger)
    except Exception:
        # Fallback: JS click on the menu trigger
        try:
            DRIVER.execute_script(
                """
                const el = document.getElementById('TL_RPTD_SFF_WK_GROUPBOX$PIMG');
                if (el) { el.click(); return true; }
                const candidates = Array.from(document.querySelectorAll('a,button,img'))
                  .filter(e => (e.id || '').includes('TL_RPTD_SFF_WK_GROUPBOX'));
                if (candidates[0]) { candidates[0].click(); return true; }
                return false;
                """
            )
        except Exception:
            pass

    # Wait for the popup menu container to appear
    if checkExistence(element_to_find="win1divTL_RPTD_SFF_WK_GROUPBOXgrp", method_to_find="id", passOnError=True):
        return True
    if checkExistence(element_to_find="TL_RPTD_SFF_WK_GROUPBOX$divpop", method_to_find="id", passOnError=True):
        return True

    return False


# This function prevents timeouts:
def prevent_timeout():
    DRIVER.refresh()

    # If the timeout box does appear, click the prevent timeout button
    try:
        timeout_button = DRIVER.find_element(By.ID, "BOR_INSTALL_VW$0_row_0")
        timeout_button.send_keys(Keys.RETURN)
        print("Timeout Prevented")
        print("...")
        return True
    except (NoSuchElementException, TimeoutException):
        return True

    except Exception as error:
        dump_artifacts("prevent_timeout_error")
        if logger.level > logging.DEBUG:
            print("...")
            print("Something Unknown Happened, Please Manually Clock out!")
            print(
                "Please Raise an Issue on Github and say the error is in the Timeout Prevention")
            notify_user_with_ack(
                "Clock manager error",
                "Timeout prevention failed. Please check your session and timecard.",
                require_ack=True,
            )
            DRIVER.quit()
        else:
            logger.debug(f"Timeout prevention error: {error}")
        return False


# This function checks to see if the popup for double-clocking comes up
def double_clock_handler():
    try:
        MINI_WAIT.until(lambda d: d.find_element(By.ID, "#ICOK"))
        popup_button = DRIVER.find_element(By.ID, "#ICOK")
        popup_button.send_keys(Keys.RETURN)
        MINI_WAIT.until(lambda d: d.find_element(By.ID, "PT_WORK_PT_BUTTON_BACK"))
        back_button = DRIVER.find_element(By.ID, "PT_WORK_PT_BUTTON_BACK")
        back_button.send_keys(Keys.RETURN)
        print("You were about to double clock, we prevented that.")
        return True

    except (NoSuchElementException, TimeoutException):
        return False

    except Exception as error:
        dump_artifacts("double_clock_handler_error")
        if logger.level > logging.DEBUG:
            print("...")
            print("Something Unknown Happened, Please Manually Clock out!")
            print(
                "Please Raise an Issue on Github and say the error is in the Double Clock Handler")
            notify_user_with_ack(
                "Clock manager error",
                "Double-clock handler failed. Please check your timecard.",
                require_ack=True,
            )
            DRIVER.quit()
        else:
            logger.debug(f"Double clock handler error: {error}")
        return False


# This function handles errors and returns either true or false to indicate success or failure:
# There are probably a lot more cases to handle, but its fine for now.
def checkExistence(element_to_find, method_to_find="id", purpose="Default, Please Specify when Invoking checkExistence", passOnError=False):
    try:
        if method_to_find == "xpath":
            MINI_WAIT.until(lambda d: d.find_element(By.XPATH, element_to_find))
            return True
        elif method_to_find == "id":
            MINI_WAIT.until(lambda d: d.find_element(By.ID, element_to_find))
            return True
        elif method_to_find == "name":
            MINI_WAIT.until(lambda d: d.find_element(By.NAME, element_to_find))
            return True
        elif method_to_find == "css":
            MINI_WAIT.until(lambda d: d.find_element(By.CSS_SELECTOR, element_to_find))
            return True
        else:
            print("method_to_find not right")
            if logger.level > logging.DEBUG and not passOnError:
                DRIVER.quit()
            return False

    except (NoSuchElementException, TimeoutException, ElementNotInteractableException) as error:
        if logger.level > logging.DEBUG and not passOnError:
            dump_artifacts(purpose)
            print("This Element: " + element_to_find + " ")
            print("was not found, this means OneUsg made some changes.")
            print("This element is associated with " + purpose + ". ")
            print("If this error continues, please raise an issue on Github.")
            print("...")
            notify_user_with_ack(
                "Clock manager error",
                "An expected UI element was not found. Please check the terminal output and verify your timecard.",
                require_ack=True,
            )
            DRIVER.quit()
        elif logger.level <= logging.DEBUG and not passOnError:
            logger.debug(f"Element: {element_to_find}")
            logger.debug(f"Purpose: {purpose}")
            logger.debug(f"Error: {error}")
        return False

    except (RuntimeError, TypeError, NameError) as error:
        if logger.level > logging.DEBUG and not passOnError:
            DRIVER.quit()
        else:
            logger.debug(f"Error: {error}")
        return False

    except Exception as error:
        print("...")
        print("Please Raise an Issue on Github!")
        print("Failure with: " + purpose)
        dump_artifacts(purpose)
        if logger.level > logging.DEBUG and not passOnError:
            notify_user_with_ack(
                "Clock manager error",
                "Unexpected error during UI check. Please check the terminal output and verify your timecard.",
                require_ack=True,
            )
            DRIVER.quit()
        else:
            logger.debug(f"Error: {error}")
        return False


def main():
    global USERNAME, PASSWORD
    global MINUTES, TIME_BLOCKS, BLOCKS_DONE
    global DRIVER, WAIT, MINI_WAIT
    global DUMP_DIR, DUO_TIMEOUT_SECONDS, RESTART_REQUESTED

    parser = argparse.ArgumentParser(description='OneUSGAutomaticClock')
    parser.add_argument('-u', '--username', help="GT Username", required=False)
    parser.add_argument('-m', '--minutes', type=float, help="Minutes to clock (required)", required=True)
    parser.add_argument('--headless', action='store_true', help='Run Chrome headless (recommended for troubleshooting/CI)')
    parser.add_argument('--debug', action='store_true', help='Verbose debug output and artifact dumps on failure')
    parser.add_argument('--dump-dir', default=os.environ.get('ONEUSG_DUMP_DIR', ''), help='Directory to write debug artifacts (png/html/url)')
    parser.add_argument('--duo-timeout', type=int, default=int(os.environ.get('ONEUSG_DUO_TIMEOUT', DUO_TIMEOUT_SECONDS)), help='Seconds to wait for Duo/SSO completion')
    args = vars(parser.parse_args())

    if load_dotenv is not None:
        load_dotenv()

    # Set up logging level based on --debug flag
    if args['debug']:
        logging.basicConfig(level=logging.DEBUG, format='[%(levelname)s] %(message)s')
        logger.setLevel(logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO, format='%(message)s')
        logger.setLevel(logging.INFO)

    USERNAME = args['username'] or os.environ.get('ONEUSG_USERNAME', '')
    MINUTES = args['minutes']
    DUMP_DIR = args.get('dump_dir') or None
    DUO_TIMEOUT_SECONDS = int(args.get('duo_timeout') or DUO_TIMEOUT_SECONDS)

    # Password can come from env for convenience; otherwise prompt.
    PASSWORD = os.environ.get('ONEUSG_PASSWORD', '')
    if not PASSWORD:
        PASSWORD = getpass.getpass(prompt='GT Password: ', stream=None)

    if not USERNAME:
        print("Be sure to set your username (or ONEUSG_USERNAME env var).\n")
        sys.exit(1)

    if PASSWORD == "":
        print("Be sure to set your password (or ONEUSG_PASSWORD env var).\n")
        sys.exit(1)

    total_seconds = max(0, int(round(MINUTES * 60)))
    TIME_BLOCKS = None
    BLOCKS_DONE = 0

    chromedriver_autoinstaller.install()

    def init_browser(headless=False):
        """Initialize a fresh Chrome browser with clean session (no cookies)."""
        global DRIVER, WAIT, MINI_WAIT
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

        DRIVER = webdriver.Chrome(options=chrome_options)
        WAIT = WebDriverWait(DRIVER, 25)
        MINI_WAIT = WebDriverWait(DRIVER, 5)

        # Set up a virtual authenticator to auto-handle WebAuthn / passkey prompts.
        try:
            DRIVER.add_virtual_authenticator(
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

    init_browser(headless=args.get('headless'))

    print('\nClocking {0} minutes...\n'.format(MINUTES))
    logger.debug(f"headless={bool(args.get('headless'))} dump_dir={DUMP_DIR or '(disabled)'} duo_timeout={DUO_TIMEOUT_SECONDS}s")

    try:
        # Retry login flow once if idpproxy HTTP 400 occurs
        for attempt in range(2):
            RESTART_REQUESTED = False
            goToGTClock()
            if not selectGT():
                return 1
            if not loginGT():
                if RESTART_REQUESTED and attempt == 0:
                    logger.debug("Restarting login flow after idpproxy 400 - closing browser and starting fresh")
                    # Close the browser completely and start a fresh one with no cookies
                    try:
                        DRIVER.quit()
                    except Exception:
                        pass
                    init_browser(headless=args.get('headless'))
                    continue
                return 1
            if not clockHoursIn():
                return 1
            break

        # This is a little loop to make sure we prevent timeouts and to keep track of how long its been
        # It just refreshes the page every fifteen minutes and keeps track of how much time has passed.
        if total_seconds == 0:
            clockHoursOut()
            return 0

        elapsed_seconds = 0
        refresh_interval = 15 * 60
        while elapsed_seconds < total_seconds:
            if elapsed_seconds == 0 or (elapsed_seconds % refresh_interval) == 0:
                prevent_timeout()

            remaining_seconds = total_seconds - elapsed_seconds
            sleep_chunk = min(60, remaining_seconds)
            time.sleep(sleep_chunk)
            elapsed_seconds += sleep_chunk

            minutes_done = int(elapsed_seconds // 60)
            minutes_left = max(0, round((total_seconds - elapsed_seconds) / 60, 2))
            print(f"{minutes_done} minutes done, roughly {minutes_left} minutes left to go.")
            print("...")

            if elapsed_seconds >= total_seconds:
                clockHoursOut()
                break

        # This is just another safety check to make sure we don't ever leave without clocking out first.
        else:
            try:
                clockHoursOut()
            except Exception:
                dump_artifacts("clock_out_exception")
                print("Make sure you were clocked out please.")
        return 0
    except Exception as e:
        dump_artifacts("unhandled_exception")
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
            if DRIVER is not None:
                DRIVER.quit()
                DRIVER = None
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())


#=================================================================================================#
# DEPRECATED STUFF (may want to use in the future:)
# Go To One USG (DEPRECATED):
# def goToOneUSG():
#     DRIVER.get("https://hcm-sso.onehcm.usg.edu/")
#     return checkExistence(element_to_find="//*[@id='https_idp_gatech_edu_idp_shibboleth']/div/div/a/img", method_to_find="xpath", purpose="Going to One Usg")


# # This function goes through the menus to click the Time and Absence button (DEPRECATED):
# def goToClock():

#     WAIT.until(lambda DRIVER: DRIVER.find_element_by_id(
#         "win0divPTNUI_LAND_REC_GROUPLET$7"))

#     time_and_absence_button = DRIVER.find_element_by_id(
#         "win0divPTNUI_LAND_REC_GROUPLET$7")
#     time_and_absence_button.send_keys(Keys.RETURN)

#     return checkExistence(element_to_find="win0groupletPTNUI_LAND_REC_GROUPLET$3_iframe", purpose="Going to the Clock")
