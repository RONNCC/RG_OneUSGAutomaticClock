import time
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from browser_utils import AppContext
import browser_utils
import selector_defs as selectors


def _click_first(ctx: AppContext, candidates, timeout=3, clickable=True):
    try:
        el = browser_utils.find_first(ctx, candidates, timeout=timeout, clickable=clickable)
        if el:
            return browser_utils.safe_click(ctx, el)
    except Exception:
        return False
    return False


def _click_by_text_js(ctx: AppContext, pattern: str, selectors_query: str) -> bool:
    try:
        return ctx.driver.execute_script(
            """
            const pattern = arguments[0];
            const selectors = arguments[1];
            const re = new RegExp(pattern, 'i');
            const items = Array.from(document.querySelectorAll(selectors));
            const target = items.find(el => re.test(el.textContent || ''));
            if (target) { target.click(); return true; }
            return false;
            """,
            pattern,
            selectors_query,
        ) or False
    except Exception:
        return False


def find_duo_passcode_input(ctx: AppContext, timeout=6):
    try:
        return browser_utils.find_first(ctx, selectors.PASSCODE_INPUT_SELECTORS, timeout=timeout, clickable=True)
    except Exception:
        return None


def find_duo_verify_button(ctx: AppContext, timeout=6):
    try:
        return browser_utils.find_first(ctx, selectors.VERIFY_BUTTON_SELECTORS, timeout=timeout, clickable=False)
    except Exception:
        return None


def dismiss_passkey_dialog(ctx: AppContext):
    try:
        from selenium.webdriver.common.action_chains import ActionChains
        ActionChains(ctx.driver).send_keys(Keys.ESCAPE).perform()
        time.sleep(0.3)
    except Exception:
        pass


def handle_duo_device_trust_prompt(ctx: AppContext):
    if "Is this your device?" not in (ctx.driver.page_source or ""):
        return False
    for by, value in selectors.DEVICE_TRUST_NO_SELECTORS + selectors.DEVICE_TRUST_YES_SELECTORS:
        try:
            btn = WebDriverWait(ctx.driver, 2).until(EC.element_to_be_clickable((by, value)))
            if btn and browser_utils.safe_click(ctx, btn):
                time.sleep(3)
                return True
        except Exception:
            continue
    return False


def handle_touchid_canceled_prompt(ctx: AppContext):
    page = ctx.driver.page_source or ""
    if "Couldn't use Touch ID" not in page and "Touch ID has been canceled" not in page:
        return False
    if _click_first(ctx, selectors.OTHER_OPTIONS_SELECTORS, timeout=1, clickable=True):
        return True
    return _click_by_text_js(ctx, "Other options", "button, a")


def handle_duo_other_options_page(ctx: AppContext):
    if "Other options to log in" not in (ctx.driver.page_source or ""):
        return False
    for by, value in selectors.PASSCODE_OPTION_SELECTORS:
        try:
            option = WebDriverWait(ctx.driver, 2).until(EC.element_to_be_clickable((by, value)))
            if option and browser_utils.safe_click(ctx, option):
                time.sleep(0.5)
                return True
        except Exception:
            continue
    if _click_by_text_js(ctx, "Duo Mobile passcode", "button, a, div, li"):
        time.sleep(0.5)
        return True
    return False


def _click_duo_other_options_in_context(ctx: AppContext, get_duo_passcode, set_input_value):
    try:
        dismiss_passkey_dialog(ctx)
        passcode_input = find_duo_passcode_input(ctx, timeout=2)
        if passcode_input is None:
            if not _click_first(ctx, selectors.OTHER_OPTIONS_SELECTORS, timeout=3, clickable=True):
                if not _click_by_text_js(ctx, "Other options", "button, a"):
                    return False
        if passcode_input is None:
            if not _click_first(ctx, selectors.PASSCODE_OPTION_SELECTORS, timeout=3, clickable=True):
                if not _click_by_text_js(ctx, "Duo Mobile passcode|Passcode", "button, a, div, span"):
                    return True
        passcode_input = find_duo_passcode_input(ctx, timeout=6)
        duo_passcode = get_duo_passcode()
        if duo_passcode and passcode_input is not None:
            time.sleep(0.4)
            set_input_value(passcode_input, duo_passcode)
            time.sleep(0.3)
            # Wait for verify button to become enabled and click it
            btn_probe = find_duo_verify_button(ctx, timeout=2)
            if btn_probe and btn_probe.get_attribute("disabled"):
                set_input_value(passcode_input, duo_passcode)
                time.sleep(0.3)
            start = time.time()
            while time.time() - start < 10:
                btn = find_duo_verify_button(ctx, timeout=2)
                if btn:
                    disabled = btn.get_attribute("disabled")
                    aria_disabled = btn.get_attribute("aria-disabled")
                    if disabled or (aria_disabled and aria_disabled.lower() == "true"):
                        time.sleep(0.5)
                        continue
                    if browser_utils.safe_click(ctx, btn):
                        break
                time.sleep(0.5)
            else:
                passcode_input.send_keys(Keys.RETURN)
        return True
    except Exception:
        return False


def try_duo_other_options(ctx: AppContext, get_duo_passcode, set_input_value):
    try:
        dismiss_passkey_dialog(ctx)
        if handle_touchid_canceled_prompt(ctx) or handle_duo_device_trust_prompt(ctx) or handle_duo_other_options_page(ctx):
            return True
        if _click_duo_other_options_in_context(ctx, get_duo_passcode, set_input_value):
            return True
        frames = ctx.driver.find_elements(By.TAG_NAME, "iframe")
        if len(frames) == 0:
            try:
                if len(ctx.driver.find_element(By.TAG_NAME, "body").text.strip()) < 100:
                    ctx.driver.refresh()
                    time.sleep(2)
                    return True
            except Exception:
                pass
        for f in frames:
            try:
                src = (f.get_attribute("src") or "").lower()
                if ("duo" in src) or (not src) or ("about:blank" in src):
                    ctx.driver.switch_to.frame(f)
                    handle_duo_device_trust_prompt(ctx)
                    if _click_duo_other_options_in_context(ctx, get_duo_passcode, set_input_value):
                        return True
            finally:
                try:
                    ctx.driver.switch_to.default_content()
                except Exception:
                    pass
        return False
    except Exception:
        return False
