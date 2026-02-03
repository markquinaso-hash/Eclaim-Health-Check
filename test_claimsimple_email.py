# -*- coding: utf-8 -*-
"""
Pytest version of the merged Selenium workflow + Inline Screenshot Email.

Flow:
1) Open ClaimSimple HK
2) Click Claim → navigate to EMC
3) Accept T&Cs (checkbox) → Continue
4) Switch to ID option
5) Enter ID + DOB
6) Verify the expected error message
7) Take a screenshot
8) Email the screenshot inline (always or only on failure, controlled by env)

Author: MJ
"""

import os
import ssl
import time
from datetime import datetime
from email.message import EmailMessage
from email.utils import make_msgid

import pytest

# Optional .env support
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# --- Email / Env Utilities ----------------------------------------------------

REQUIRED_VARS = ["SMTP_USERNAME", "SMTP_PASSWORD", "TO_EMAIL"]


def get_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        missing = [v for v in REQUIRED_VARS if not os.getenv(v)]
        raise RuntimeError(
            f"Missing required environment variable: {name}\n"
            f"Currently missing: {', '.join(missing)}"
        )
    return val


def build_message_with_inline_image(
    from_email: str,
    to_email: str,
    subject: str,
    text_body: str,
    html_intro: str,
    image_path: str,
    image_subtype: str = "png",
) -> EmailMessage:
    """
    Creates a multipart/alternative + related email:
      - text/plain part
      - text/html part referencing inline image via CID
    """
    msg = EmailMessage()
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Subject"] = subject

    # Plain text fallback
    msg.set_content(text_body)

    # Generate a CID for the image
    cid = make_msgid(domain="inline")  # e.g., <...@inline>
    cid_no_brackets = cid[1:-1]        # strip < >

    html_body = f"""
    <html>
      <body style="font-family:Segoe UI, Arial, sans-serif;">
        <p>{html_intro}</p>
        <p>
          <img src="cid:{cid_no_brackets}" alt="Screenshot"
               style="max-width:100%; height:auto; border:1px solid #ddd;"/>
        </p>
      </body>
    </html>
    """

    # Add HTML alternative
    msg.add_alternative(html_body, subtype="html")

    # Attach the image as a related part to the HTML (part index 1)
    with open(image_path, "rb") as f:
        msg.get_payload()[1].add_related(
            f.read(),
            maintype="image",
            subtype=image_subtype,
            cid=cid,
            filename=os.path.basename(image_path),
        )

    return msg


def send_via_gmail_smtp(msg: EmailMessage, username: str, password: str, use_port_465=False):
    """
    Sends the message via Gmail SMTP.
    - Default: STARTTLS on 587
    - Optionally: implicit SSL on 465
    """
    smtp_server = "smtp.gmail.com"
    if use_port_465:
        port = 465
        context = ssl.create_default_context()
        import smtplib
        with smtplib.SMTP_SSL(smtp_server, port, context=context, timeout=60) as server:
            server.login(username, password)
            server.send_message(msg)
    else:
        port = 587
        import smtplib
        with smtplib.SMTP(smtp_server, port, timeout=60) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
            server.login(username, password)
            server.send_message(msg)


# --- Selenium Setup & Helpers -------------------------------------------------

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# CSS Selectors (same as original NR Script)
CLAIM_BTN = ".splash__body_search-doctor"
CHECKBOX_INPUT = 'input.ui-checkbox__input[name="terms"]'
CONTINUE_BTN = ".button-primary.button-primary--full.button-doctorsearch-continue"
ID_TOGGLE_ICON = ".ui-selection__symbol"
ID_INPUT = ".qna__input"
DOB_INPUT = ".qna__input.aDOB"
ERROR_TEXT_CSS = ".qna__input-error"


def make_chrome_driver(headless: bool = True, window_w: int = 1366, window_h: int = 900):
    """
    Create a Chrome WebDriver, optionally headless.
    Attempts webdriver_manager if available; falls back to default chromedriver on PATH.
    """
    from selenium.webdriver.chrome.options import Options

    options = Options()
    if headless:
        # headless=new for modern Chrome
        options.add_argument("--headless=new")
    options.add_argument(f"--window-size={window_w},{window_h}")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    chrome_binary = os.getenv("CHROME_BINARY")
    if chrome_binary:
        options.binary_location = chrome_binary

    driver = None
    # If user explicitly wants webdriver_manager
    use_wdm = os.getenv("USE_WEBDRIVER_MANAGER", "false").lower() in ("1", "true", "yes")
    if use_wdm:
        try:
            from webdriver_manager.chrome import ChromeDriverManager
            driver = webdriver.Chrome(service=ChromeService(ChromeDriverManager().install()), options=options)
        except Exception:
            pass

    if driver is None:
        # Optional CHROMEDRIVER path (or rely on PATH)
        chromedriver_path = os.getenv("CHROMEDRIVER")
        if chromedriver_path:
            driver = webdriver.Chrome(service=ChromeService(chromedriver_path), options=options)
        else:
            driver = webdriver.Chrome(options=options)

    return driver


def wait_visible(driver, css: str, timeout: int = 15):
    return WebDriverWait(driver, timeout).until(
        EC.visibility_of_element_located((By.CSS_SELECTOR, css)),
        message=f"Not visible: {css}"
    )


def wait_clickable(driver, css: str, timeout: int = 15):
    return WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, css)),
        message=f"Not clickable: {css}"
    )


def scroll_into_view(driver, el):
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    time.sleep(0.25)


def click_with_fallback(driver, el):
    try:
        el.click()
    except Exception:
        scroll_into_view(driver, el)
        driver.execute_script("arguments[0].click();", el)


def run_claimsimple_flow(driver, *, cs_hk_url, tnc_emc_url, claim_id, claim_dob,
                         expected_error_text, screenshot_path) -> str:
    """
    Runs the ClaimSimple HK flow and takes a screenshot at the end.
    Returns the observed error text (if found).
    """
    observed_error_text = ""

    driver.get(cs_hk_url)

    # 1) Click Claim Button
    claim_btn = wait_visible(driver, CLAIM_BTN, 20)
    scroll_into_view(driver, claim_btn)
    click_with_fallback(driver, claim_btn)
    print("Claim button clicked.")

    # Navigate to EMC
    driver.get(tnc_emc_url)

    # 2) Click checkbox
    checkbox = wait_visible(driver, CHECKBOX_INPUT, 20)
    scroll_into_view(driver, checkbox)
    click_with_fallback(driver, checkbox)
    print("Checkbox clicked.")

    # 3) Continue
    continue_btn = wait_clickable(driver, CONTINUE_BTN, 20)
    scroll_into_view(driver, continue_btn)
    click_with_fallback(driver, continue_btn)
    print("Clicked Continue.")

    # 4) Select ID option
    id_toggle = wait_clickable(driver, ID_TOGGLE_ICON, 15)
    scroll_into_view(driver, id_toggle)
    click_with_fallback(driver, id_toggle)
    print("Switched to ID entry.")

    # 5) Enter ID
    id_input = wait_visible(driver, ID_INPUT, 15)
    scroll_into_view(driver, id_input)
    id_input.clear()
    id_input.send_keys(claim_id)
    id_input.send_keys(Keys.ENTER)
    print("Entered ID.")
    time.sleep(2)

    # 6) Enter DOB
    dob_input = wait_visible(driver, DOB_INPUT, 15)
    scroll_into_view(driver, dob_input)
    dob_input.clear()
    dob_input.send_keys(claim_dob)
    dob_input.send_keys(Keys.ENTER)
    print("Entered DOB.")
    time.sleep(2)

    # Wait for potential error message to render
    try:
        err_el = WebDriverWait(driver, 10).until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, ERROR_TEXT_CSS))
        )
        observed_error_text = (err_el.text or "").strip()
        print("Observed error text:", observed_error_text)
    except Exception:
        print("No error message element found within timeout.")

    # Assert if EXPECTED_TEXT provided
    if expected_error_text:
        assert observed_error_text == expected_error_text, (
            f"Error - Expected text not found.\n"
            f"Expected: {expected_error_text}\n"
            f"Actual:   {observed_error_text}"
        )

    # Save screenshot
    driver.save_screenshot(screenshot_path)
    print(f"Screenshot saved to {screenshot_path}")

    return observed_error_text


# --- Pytest Fixtures ----------------------------------------------------------

@pytest.fixture(scope="session")
def config():
    """
    Collect all configuration from environment variables (with safe defaults),
    mirroring the merged script behavior.
    """
    cfg = {}

    # Selenium / Browser
    cfg["HEADLESS"] = os.getenv("HEADLESS", "true").lower() in ("1", "true", "yes")
    cfg["WINDOW_W"] = int(os.getenv("WINDOW_W", "1366"))
    cfg["WINDOW_H"] = int(os.getenv("WINDOW_H", "900"))

    # URLs (overridable via env)
    cfg["CS_HK_URL"] = os.getenv("CS_HK_URL", "https://www.claimsimple.hk/#/")
    cfg["TNC_EMC_URL"] = os.getenv("TNC_EMC_URL", "https://www.claimsimple.hk/DoctorSearch#/")

    # Inputs
    cfg["CLAIM_ID"] = os.getenv("CLAIM_ID", "A0000000")
    cfg["CLAIM_DOB"] = os.getenv("CLAIM_DOB", "01/01/1990")

    # Assertion text
    cfg["EXPECTED_ERROR_TEXT"] = os.getenv(
        "EXPECTED_ERROR_TEXT",
        "The information you provided does not match our records. Please try again."
    )

    # Output
    cfg["SCREENSHOT_PATH"] = os.getenv("SCREENSHOT_PATH", "screenshot.png")

    # Email controls
    cfg["SMTP_USERNAME"] = get_env("SMTP_USERNAME")
    cfg["SMTP_PASSWORD"] = get_env("SMTP_PASSWORD")
    cfg["TO_EMAIL"] = get_env("TO_EMAIL")
    cfg["USE_SSL_465"] = os.getenv("USE_SSL_465", "false").lower() in ("1", "true", "yes")

    # Email policy
    cfg["ALWAYS_EMAIL"] = os.getenv("ALWAYS_EMAIL", "true").lower() in ("1", "true", "yes")
    cfg["EMAIL_ON_FAILURE"] = os.getenv("EMAIL_ON_FAILURE", "true").lower() in ("1", "true", "yes")

    # Email content
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cfg["SUBJECT_BASE"] = os.getenv("SUBJECT", "ClaimSimple HK Check – Inline Screenshot")
    cfg["HTML_INTRO_BASE"] = os.getenv(
        "BODY",
        f"Hello! Here is the screenshot from the automated ClaimSimple HK flow "
        f"(ID/DOB verification).<br><strong>Timestamp:</strong> {now}"
    )
    cfg["TEXT_BODY"] = (
        "This email contains an inline screenshot of the automated ClaimSimple HK flow. "
        "If you can't see it, open in an HTML-capable client."
    )
    return cfg


@pytest.fixture
def driver(config):
    """
    Provide a Selenium Chrome WebDriver, with headless toggle and sizing from config.
    """
    drv = make_chrome_driver(
        headless=config["HEADLESS"],
        window_w=config["WINDOW_W"],
        window_h=config["WINDOW_H"]
    )
    yield drv
    try:
        drv.quit()
    except Exception:
        pass


# --- The Test ----------------------------------------------------------------

def test_claimsimple_id_dob_flow_screenshot_email(driver, config):
    """
    Executes the ClaimSimple flow, asserts the expected error text,
    saves a screenshot, and emails the result inline.
    """
    screenshot_path = config["SCREENSHOT_PATH"]
    observed_error = ""
    test_failed = False
    failure_reason = None

    try:
        observed_error = run_claimsimple_flow(
            driver,
            cs_hk_url=config["CS_HK_URL"],
            tnc_emc_url=config["TNC_EMC_URL"],
            claim_id=config["CLAIM_ID"],
            claim_dob=config["CLAIM_DOB"],
            expected_error_text=config["EXPECTED_ERROR_TEXT"],
            screenshot_path=screenshot_path,
        )
    except Exception as e:
        test_failed = True
        failure_reason = str(e)
        # Best-effort: capture a screenshot on failure path
        try:
            driver.save_screenshot(screenshot_path)
        except Exception:
            pass
        raise
    finally:
        # Decide whether to send the email
        should_email = config["ALWAYS_EMAIL"] or (test_failed and config["EMAIL_ON_FAILURE"])

        if should_email and os.path.exists(screenshot_path):
            status = "FAILED" if test_failed else "PASSED"
            subject = f"[{status}] {config['SUBJECT_BASE']}"

            html_intro = config["HTML_INTRO_BASE"]
            if observed_error:
                html_intro = f"{html_intro}<br><strong>Observed error:</strong> {observed_error}"
            if failure_reason:
                html_intro = f"{html_intro}<br><strong>Failure reason:</strong> {failure_reason}"

            msg = build_message_with_inline_image(
                from_email=config["SMTP_USERNAME"],
                to_email=config["TO_EMAIL"],
                subject=subject,
                text_body=config["TEXT_BODY"],
                html_intro=html_intro,
                image_path=screenshot_path,
                image_subtype="png",
            )
            send_via_gmail_smtp(
                msg,
                config["SMTP_USERNAME"],
                config["SMTP_PASSWORD"],
                use_port_465=config["USE_SSL_465"],
            )
            print(f"✅ Email with inline screenshot sent to {config['TO_EMAIL']}")