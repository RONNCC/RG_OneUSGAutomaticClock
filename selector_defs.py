from selenium.webdriver.common.by import By

CLOCK_PAGE_URL = "https://selfservice.hprod.onehcm.usg.edu/psc/hprodsssso/HCMSS/HRMS/c/TL_EMPLOYEE_FL.TL_RPT_TIME_FLU.GBL?Action=U&EMPLJOB=0"

GT_IDP_SELECTORS = [
    (By.LINK_TEXT, "Georgia Tech"),
    (By.PARTIAL_LINK_TEXT, "Georgia Tech"),
    (By.CSS_SELECTOR, "a[title*='Georgia Tech' i]"),
    (By.CSS_SELECTOR, "a img[alt*='Georgia Tech' i]"),
    (By.XPATH, "//*[@id='https_idp_gatech_edu_idp_shibboleth']//a"),
    (By.XPATH, "//*[@id='https_idp_gatech_edu_idp_shibboleth']"),
    (By.XPATH, "//a[contains(@href,'gatech') or contains(@href,'gatech.edu') or contains(.,'Georgia Tech') or contains(.,'Georgia Institute') or .//img[contains(@alt,'Georgia') or contains(@src,'gatech')]]"),
    (By.XPATH, "//button[contains(.,'Georgia Tech') or contains(.,'Georgia Institute') or contains(.,'Gatech')]")
]

PUNCH_DROPDOWN_SELECTORS = [
    (By.ID, "TL_RPTD_TIME_PUNCH_TYPE$0"),
    (By.CSS_SELECTOR, "select[id*='PUNCH_TYPE']"),
    (By.XPATH, "//select[contains(@id,'PUNCH_TYPE')]")
]

SUBMIT_BUTTON_SELECTORS = [
    (By.ID, "TL_WEB_CLOCK_WK_TL_SAVE_PB"),
    (By.XPATH, "//a[@id='TL_WEB_CLOCK_WK_TL_SAVE_PB']"),
    (By.XPATH, "//a[contains(@class,'ps-button') and contains(.,'Submit')]"),
    (By.XPATH, "//*[self::a or self::button][normalize-space()='Submit']")
]

PASSCODE_INPUT_SELECTORS = [
    (By.ID, "passcode-input"),
    (By.NAME, "passcode-input"),
    (By.CSS_SELECTOR, "#passcode-input"),
    (By.CSS_SELECTOR, "input[name='passcode-input']"),
    (By.CSS_SELECTOR, "input.passcode-input"),
    (By.NAME, "passcode"),
    (By.ID, "passcode"),
    (By.CSS_SELECTOR, "input[name='passcode']"),
    (By.CSS_SELECTOR, "input[aria-label='Passcode']"),
    (By.CSS_SELECTOR, "input[inputmode='numeric']")
]

VERIFY_BUTTON_SELECTORS = [
    (By.CSS_SELECTOR, "button[data-testid='verify-button']"),
    (By.CSS_SELECTOR, "button.verify-button"),
    (By.XPATH, "//button[@type='submit' and normalize-space()='Verify']"),
    (By.XPATH, "//button[contains(.,'Verify')]")
    ,
    (By.CSS_SELECTOR, "button[type='submit']"),
    (By.XPATH, "//button[contains(.,'Log in')]")
]

OTHER_OPTIONS_SELECTORS = [
    (By.LINK_TEXT, "Other options"),
    (By.PARTIAL_LINK_TEXT, "Other options"),
    (By.XPATH, "//button[contains(.,'Other options')]")
    ,
    (By.XPATH, "//a[contains(.,'Other options')]")
    ,
    (By.CSS_SELECTOR, ".other-options-link")
]

PASSCODE_OPTION_SELECTORS = [
    (By.XPATH, "//*[contains(text(),'Duo Mobile passcode')]"),
    (By.XPATH, "//button[contains(.,'Duo Mobile passcode')]")
    ,
    (By.XPATH, "//a[contains(.,'Duo Mobile passcode')]")
    ,
    (By.XPATH, "//button[contains(.,'Passcode')]")
    ,
    (By.XPATH, "//a[contains(.,'Passcode')]")
    ,
]

DEVICE_TRUST_NO_SELECTORS = [
    (By.XPATH, "//button[contains(.,'No')]")
]

DEVICE_TRUST_YES_SELECTORS = [
    (By.XPATH, "//button[contains(.,'Yes')]")
]
