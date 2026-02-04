# -*- coding: utf-8 -*-
"""
Playwright version of ClaimSimple HK flow + Inline Screenshot Email.

Flow:
1) Open ClaimSimple HK
2) Click Claim
3) Accept T&Cs (checkbox) → Continue
4) Switch to ID option
5) Enter ID + DOB
6) Verify expected error
7) Screenshot
8) Email (inline)
"""

import os
import ssl
import time
import re
from datetime import datetime
from email.message import EmailMessage
from email.utils import make_msgid

import pytest
from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeout,
    Page,
    expect,
)

# Optional .env support
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# -------------------------------------------------------
# Utilities
# -------------------------------------------------------

REQUIRED_VARS = ["SMTP_USERNAME", "SMTP_PASSWORD", "TO_EMAIL"]


def get_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        missing = [v for v in REQUIRED_VARS if not os.getenv(v)]
        raise RuntimeError(
            f"Missing required env: {name}\nMissing: {', '.join(missing)}"
        )
    return val


def normalize_text(s: str) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip().lower()


def build_message_with_inline_image(
    from_email,
    to_email,
    subject,
    text_body,
    html_intro,
    image_path,
):
    msg = EmailMessage()
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Subject"] = subject

    msg.set_content(text_body)

    cid = make_msgid(domain="inline")
    cid_no_brackets = cid[1:-1]

    html_body = f"""
    <html>
      <body>
        <p>{html_intro}</p>
        <img src="cid:{cid_no_brackets}" style="max-width:100%;border:1px solid #ddd;"/>
      </body>
    </html>
    """
    msg.add_alternative(html_body, subtype="html")

    with open(image_path, "rb") as f:
        msg.get_payload()[1].add_related(
            f.read(),
            maintype="image",
            subtype="png",
            cid=cid,
            filename=os.path.basename(image_path),
        )
    return msg


def send_via_gmail_smtp(msg, username, password, use_ssl=False):
    import smtplib
    smtp_server = "smtp.gmail.com"

    if use_ssl:
        with smtplib.SMTP_SSL(smtp_server, 465, timeout=60) as s:
            s.login(username, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(smtp_server, 587, timeout=60) as s:
            s.ehlo()
            s.starttls(context=ssl.create_default_context())
            s.ehlo()
            s.login(username, password)
            s.send_message(msg)


# -------------------------------------------------------
# Selectors
# -------------------------------------------------------

CLAIM_BTN = ".splash__body_search-doctor"
CHECKBOX_INPUT = 'input.ui-checkbox__input[name="terms"]'
CONTINUE_BTN = ".button-primary.button-primary--full.button-doctorsearch-continue"
ID_TOGGLE_ICON = ".ui-selection__symbol"
ID_INPUT = ".qna__input"
DOB_NAME_SELECTOR = "input[name='dob']"
ERROR_TEXT_CSS = ".qna__input-error"
ERROR_CONTAINER = ERROR_TEXT_CSS


# -------------------------------------------------------
# JavaScript helper
# -------------------------------------------------------

def set_input_value_js(page, selector, value):
    page.wait_for_selector(selector, state="attached")
    page.evaluate(
        """([sel, val]) => {
            const el = document.querySelector(sel);
            if (!el) throw new Error("Not found: " + sel);
            const d = Object.getOwnPropertyDescriptor(
                HTMLInputElement.prototype,
                "value"
            );
            if (d && d.set) d.set.call(el, val);
            else el.value = val;
            el.dispatchEvent(new Event("input", {bubbles:true}));
            el.dispatchEvent(new Event("change", {bubbles:true}));
        }""",
        [selector, value],
    )


# -------------------------------------------------------
# Main Flow (No TNC_EMC_URL)
# -------------------------------------------------------

def run_claimsimple_flow_playwright(
    page: Page,
    *,
    cs_hk_url: str,
    claim_id: str,
    claim_dob: str,
    expected_error_text: str,
    screenshot_path: str,
) -> str:

    observed_error = ""

    # Load main page
    page.goto(cs_hk_url, wait_until="domcontentloaded")

    # Click Claim
    page.wait_for_selector(CLAIM_BTN, state="visible")
    page.locator(CLAIM_BTN).click()
    print("Clicked Claim.")

    # Expect T&C checkbox to appear naturally
    page.wait_for_selector(CHECKBOX_INPUT, state="visible")
    page.locator(CHECKBOX_INPUT).click()
    print("Clicked T&C checkbox.")

    # Continue
    page.wait_for_selector(CONTINUE_BTN, state="visible")
    page.locator(CONTINUE_BTN).click()
    print("Clicked Continue.")

    # Switch to ID entry
    page.wait_for_selector(ID_TOGGLE_ICON, state="visible")
    page.locator(ID_TOGGLE_ICON).first.click()

    # Enter ID
    page.wait_for_selector(ID_INPUT, state="visible")
    id_box = page.locator(ID_INPUT).first
    id_box.fill(claim_id)
    id_box.press("Enter")

    # Enter DOB via JS
    set_input_value_js(page, DOB_NAME_SELECTOR, claim_dob)
    page.keyboard.press("Tab")
    time.sleep(0.5)

    # Check error
    try:
        err = page.locator(ERROR_TEXT_CSS)
        expect(err).to_be_visible(timeout=8000)
        raw = err.inner_text()
        observed_error = normalize_text(raw)
    except:
        observed_error = ""

    # Screenshot
    page.screenshot(path=screenshot_path, full_page=True)
    print("Saved screenshot.")

    # Assertion
    if expected_error_text:
        expected = normalize_text(expected_error_text)
        if expected not in observed_error:
            raise AssertionError(
                f"Expected: {expected}\nGot: {observed_error}"
            )

    return observed_error


# -------------------------------------------------------
# Pytest Fixtures
# -------------------------------------------------------

@pytest.fixture(scope="session")
def config():
    cfg = {}
    cfg["HEADLESS"] = os.getenv("HEADLESS", "true").lower() in ("1", "true")

    cfg["CS_HK_URL"] = os.getenv("CS_HK_URL", "https://www.claimsimple.hk/#/")

    cfg["CLAIM_ID"] = os.getenv("CLAIM_ID", "A0000000")
    cfg["CLAIM_DOB"] = os.getenv("CLAIM_DOB", "01/01/1990")

    cfg["EXPECTED_ERROR_TEXT"] = os.getenv(
        "EXPECTED_ERROR_TEXT",
        "The information you provided does not match our records"
    )

    cfg["SCREENSHOT_PATH"] = os.getenv("SCREENSHOT_PATH", "screenshot.png")

    cfg["SMTP_USERNAME"] = get_env("SMTP_USERNAME")
    cfg["SMTP_PASSWORD"] = get_env("SMTP_PASSWORD")
    cfg["TO_EMAIL"] = get_env("TO_EMAIL")
    cfg["USE_SSL_465"] = os.getenv("USE_SSL_465", "false").lower() in ("1", "true")

    cfg["ALWAYS_EMAIL"] = os.getenv("ALWAYS_EMAIL", "true").lower() in ("1", "true")
    cfg["EMAIL_ON_FAILURE"] = os.getenv("EMAIL_ON_FAILURE", "true").lower() in ("1", "true")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cfg["SUBJECT_BASE"] = "GOCC - Health Check - HK eClaims – (0700 HKT)"
    cfg["HTML_INTRO_BASE"] = (
        f"Hi Team<br>"
        f"Good day!<br>"
        f"Health check successfully executed.<br>"
        f"<strong>Timestamp:</strong> {now}"
    )
    cfg["TEXT_BODY"] = "This email contains an inline screenshot."

    return cfg


@pytest.fixture
def page(config):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=config["HEADLESS"])
        ctx = browser.new_context(viewport={"width": 1920, "height": 1080})

        pg = ctx.new_page()
        yield pg

        ctx.close()
        browser.close()


# -------------------------------------------------------
# TEST
# -------------------------------------------------------

def test_claimsimple_id_dob_flow_screenshot_email(page, config):
    screenshot_path = config["SCREENSHOT_PATH"]
    observed_error = ""
    failed = False
    reason = None

    try:
        observed_error = run_claimsimple_flow_playwright(
            page,
            cs_hk_url=config["CS_HK_URL"],
            claim_id=config["CLAIM_ID"],
            claim_dob=config["CLAIM_DOB"],
            expected_error_text=config["EXPECTED_ERROR_TEXT"],
            screenshot_path=screenshot_path,
        )

    except Exception as e:
        failed = True
        reason = str(e)
        try:
            page.screenshot(path=screenshot_path, full_page=True)
        except:
            pass
        raise

    finally:
        send_email = config["ALWAYS_EMAIL"] or (failed and config["EMAIL_ON_FAILURE"])

        if send_email and os.path.exists(screenshot_path):
            status = "FAILED" if failed else "PASSED"
            subject = f"{config['SUBJECT_BASE']} [{status}]"

            intro = config["HTML_INTRO_BASE"]
            if observed_error:
                intro += f"<br><strong>Observed Error:</strong> {observed_error}"
            if reason:
                intro += f"<br><strong>Failure Reason:</strong> {reason}"

            msg = build_message_with_inline_image(
                config["SMTP_USERNAME"],
                config["TO_EMAIL"],
                subject,
                config["TEXT_BODY"],
                intro,
                screenshot_path,
            )

            send_via_gmail_smtp(
                msg,
                config["SMTP_USERNAME"],
                config["SMTP_PASSWORD"],
                use_ssl=config["USE_SSL_465"],
            )

            print(f"Email sent → {config['TO_EMAIL']}")