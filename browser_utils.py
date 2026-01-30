import time
import logging
from dataclasses import dataclass
from typing import Optional

from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException, NoSuchElementException, ElementNotInteractableException


@dataclass
class AppContext:
    driver: Optional[WebDriver]
    wait: Optional[WebDriverWait]
    mini_wait: Optional[WebDriverWait]
    dump_dir: Optional[str]
    logger: Optional[logging.Logger]

_METHOD_MAP = {
    "xpath": "xpath",
    "id": "id",
    "name": "name",
    "css": "css selector",
}


def find_first(ctx: AppContext, locators, timeout=25, clickable=False):
    last_error = None
    for by, value in locators:
        try:
            element = WebDriverWait(ctx.driver, timeout).until(lambda d: d.find_element(by, value))
            if element is not None:
                if not clickable:
                    return element
                if element.is_displayed() and element.is_enabled():
                    return element
        except Exception as e:
            last_error = e
            continue
    raise last_error or TimeoutException("Timed out finding element")


def safe_click(ctx: AppContext, element):
    try:
        element.click()
        return True
    except Exception:
        try:
            ctx.driver.execute_script("arguments[0].click();", element)
            return True
        except Exception:
            return False


def dump_artifacts(ctx: AppContext, tag: str) -> None:
    if not ctx.dump_dir:
        return
    try:
        import os
        os.makedirs(ctx.dump_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        safe_tag = "".join(c if c.isalnum() or c in ("-", "_", ".") else "_" for c in (tag or "debug"))
        base = os.path.join(ctx.dump_dir, f"{ts}_{safe_tag}")
        try:
            ctx.driver.save_screenshot(base + ".png")
        except Exception:
            pass
        try:
            with open(base + ".html", "w", encoding="utf-8") as f:
                f.write(ctx.driver.page_source)
        except Exception:
            pass
        try:
            with open(base + ".url.txt", "w", encoding="utf-8") as f:
                f.write(getattr(ctx.driver, "current_url", ""))
        except Exception:
            pass
        if ctx.logger:
            ctx.logger.debug(f"Wrote artifacts: {base}(.png/.html/.url.txt)")
    except Exception:
        return


def prevent_timeout(ctx: AppContext):
    """Refresh page and dismiss any timeout dialogs."""
    ctx.driver.refresh()
    try:
        el = ctx.driver.find_element("id", "BOR_INSTALL_VW$0_row_0")
        el.send_keys("\r")
        print("Timeout Prevented")
    except (NoSuchElementException, TimeoutException):
        pass


def check_existence(ctx: AppContext, element_to_find, method_to_find="id"):
    """Check if element exists within mini_wait timeout."""
    method = _METHOD_MAP.get(method_to_find)
    if not method:
        return False
    try:
        ctx.mini_wait.until(lambda d: d.find_element(method, element_to_find))
        return True
    except Exception:
        return False
