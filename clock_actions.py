import time
from selenium.webdriver.support.ui import Select
from selenium.webdriver.common.by import By

from browser_utils import AppContext
import browser_utils
import selector_defs as selectors
from notifications import notify_user_with_ack


def select_punch_and_submit(ctx: AppContext, punch_value, punch_name):
    try:
        if punch_name.lower() == "out" and is_already_clocked_out(ctx):
            notify_user_with_ack(
                "Already clocked out",
                "NOTICE: You're already marked as clocked Out. This usually means the last session didn't stop cleanly or you already clocked out. Please verify your timecard.",
                require_ack=False,
            )
            return False
        punch_dropdown = browser_utils.find_first(ctx, selectors.PUNCH_DROPDOWN_SELECTORS, timeout=30, clickable=True)
        Select(punch_dropdown).select_by_value(punch_value)
        time.sleep(1)
        submit_button = browser_utils.find_first(ctx, selectors.SUBMIT_BUTTON_SELECTORS, timeout=10, clickable=True)
        browser_utils.safe_click(ctx, submit_button)
        time.sleep(2)
        print(f"You Have Clocked {punch_name}!")
        print("...")
        return True
    except Exception as e:
        print(f"Failed to Clock {punch_name}: {e}")
        notify_user_with_ack(
            f"Clock {punch_name} failed",
            "Clock action failed. Please check the terminal output and verify your timecard.",
            require_ack=True,
        )
        return False


def clock_in(ctx: AppContext):
    return select_punch_and_submit(ctx, "1", "In")


def clock_out(ctx: AppContext):
    return select_punch_and_submit(ctx, "2", "Out")


def is_on_clock_page(ctx: AppContext):
    """Check if we're on the clock page by looking for known elements."""
    clock_page_indicators = [
        "TL_RPTD_TIME_PUNCH_TYPE$0",
        "TL_RPTD_SFF_WK_GROUPBOX$PIMG",
        "TL_WEB_CLOCK_WK_TL_SAVE_PB",
    ]
    for element_id in clock_page_indicators:
        try:
            if ctx.driver.find_element(By.ID, element_id):
                return True
        except Exception:
            pass
    return False


def is_already_clocked_out(ctx: AppContext):
    candidates = ["TL_WEB_CLOCK_WK_DESCR50_1", "TL_RPTD_SFF_WK_DESCR50_1"]
    for element_id in candidates:
        try:
            el = ctx.driver.find_element(By.ID, element_id)
            if el and el.text and "out" in el.text.lower():
                return True
        except Exception:
            continue
    return False
