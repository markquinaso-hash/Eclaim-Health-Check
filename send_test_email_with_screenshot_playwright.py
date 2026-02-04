# -*- coding: utf-8 -*-
"""
Playwright version of the Selenium workflow + Inline Screenshot Email.

Flow:
1) Open ClaimSimple HK
2) Click Claim → navigate to EMC
3) Accept T&Cs (checkbox) → Continue
4) Switch to ID option
5) Enter ID + DOB
   - DOB: try native typing by name="dob" + Enter (Selenium-like)
   - Fallback to JS setter + commit + Enter if masked/hidden
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
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

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

    # Proper HTML with an inline image referencing the CID
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


# --- Playwright Selectors -----------------------------------------------------

CLAIM_BTN = ".splash__body_search-doctor"
CHECKBOX_INPUT = 'input.ui-checkbox__input[name="terms"]'
CONTINUE_BTN = ".button-primary.button-primary--full.button-doctorsearch-continue"
ID_TOGGLE_ICON = ".ui-selection__symbol"
ID_INPUT = ".qna__input"                 # First .qna__input = ID field in your flow
DOB_NAME_SELECTOR = "input[name='dob']"  # name-based selector as requested
ERROR_TEXT_CSS = ".error-tip-text"


def set_input_value_js(page, css_selector: str, value: str):
    """
    Sets the value via JS and dispatches 'input' and 'change' events so
    reactive frameworks update their state, even if the element is hidden.
    """
    page.wait_for_selector(css_selector, state="attached", timeout=30000)
    page.evaluate(
        """([sel, val]) => {
            const el = document.querySelector(sel);
            if (!el) throw new Error('Element not found for selector: ' + sel);
            try { el.focus(); } catch (e) {}
            const nativeDescriptor = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
            if (nativeDescriptor && nativeDescriptor.set) {
                nativeDescriptor.set.call(el, val);
            } else {
                el.value = val;
            }
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        [css_selector, value],
    )


# --- Helpers -----------------------------------------------------------------

def commit_and_press_enter(locator):
    """
    Ensure the element's value is committed (input/change/blur),
    then press Enter on the element itself.
    """
    try:
        locator.dispatch_event("input")
        locator.dispatch_event("change")
        locator.dispatch_event("blur")
    except Exception:
        pass
    try:
        locator.press("Enter")
    except Exception:
        pass


def run_claimsimple_flow_playwright(page, *, cs_hk_url, tnc_emc_url, claim_id, claim_dob,
                                    expected_error_text, screenshot_path) -> str:
    """
    Runs the ClaimSimple HK flow and takes a screenshot at the end.
    Returns the observed error text (if found).
    """
    observed_error_text = ""

    # Navigate to splash (tolerate SPA redirects)
    page.goto(cs_hk_url, wait_until="domcontentloaded")

    # 1) Click Claim Button (this should route into DoctorSearch/EMC)
    page.wait_for_selector(CLAIM_BTN, state="visible", timeout=30000)
    page.locator(CLAIM_BTN).scroll_into_view_if_needed()
    page.locator(CLAIM_BTN).click()
    print("Claim button clicked.")

    # Avoid explicit goto; SPA hash-route often causes ERR_ABORTED.
    # Instead, wait for the first destination element.
    try:
        page.wait_for_selector(CHECKBOX_INPUT, state="visible", timeout=20000)
    except Exception:
        print("Checkbox not visible after click; attempting direct hash-route and retry.")
        page.evaluate(f"location.href = '{tnc_emc_url}'")
        page.wait_for_selector(CHECKBOX_INPUT, state="visible", timeout=30000)

    # 2) Click checkbox
    page.locator(CHECKBOX_INPUT).scroll_into_view_if_needed()
    page.locator(CHECKBOX_INPUT).check(force=True)
    print("Checkbox clicked.")

    # 3) Continue
    page.wait_for_selector(CONTINUE_BTN, state="visible", timeout=30000)
    page.locator(CONTINUE_BTN).scroll_into_view_if_needed()
    page.locator(CONTINUE_BTN).click()
    print("Clicked Continue.")

    # 4) Select ID option (if multiple, click first)
    page.wait_for_selector(ID_TOGGLE_ICON, state="visible", timeout=30000)
    page.locator(ID_TOGGLE_ICON).first.scroll_into_view_if_needed()
    page.locator(ID_TOGGLE_ICON).first.click()
    print("Switched to ID entry.")

    # 5) Enter ID (normal fill via Playwright)
    page.wait_for_selector(ID_INPUT, state="visible", timeout=30000)
    id_box = page.locator(ID_INPUT).first
    id_box.scroll_into_view_if_needed()
    id_box.click()  # ensure focus
    id_box.fill("")
    id_box.fill(claim_id)

    # Commit + Enter (same pattern we’ll use for DOB fallback too)
    commit_and_press_enter(id_box)

    print("Entered ID.")
    time.sleep(0.8)

    # 6) Enter DOB by NAME (Selenium-like) with robust fallback
    # First attempt: behave like Selenium — focus, type, press Enter
    page.wait_for_selector(DOB_NAME_SELECTOR, state="attached", timeout=30000)
    dob_box = page.locator(DOB_NAME_SELECTOR).first

    # Try native typing
    native_dob_ok = True
    try:
        # If it is interactable, click & type like Selenium
        try:
            dob_box.scroll_into_view_if_needed()
        except Exception:
            pass
        dob_box.click(timeout=1000)
        dob_box.fill("")  # clear if any
        dob_box.type(claim_dob, delay=20)  # slight delay to mimic real typing
        commit_and_press_enter(dob_box)
        print("Entered DOB via native typing + Enter on name='dob'.")
    except Exception as e:
        native_dob_ok = False
        print(f"Native DOB typing failed (possibly masked/hidden). Fallback to JS. Reason: {e}")

    # Fallback if the element is masked or blocks typing:
    if not native_dob_ok:
        set_input_value_js(page, DOB_NAME_SELECTOR, claim_dob)
        # Re-commit + Enter on the same element after JS set
        try:
            # Ensure focus even if hidden; some frameworks bind key handlers above
            try:
                dob_box.evaluate("el => el.focus()")
            except Exception:
                pass
            commit_and_press_enter(dob_box)
            print("Entered DOB via JS setter + Enter on name='dob'.")
        except Exception:
            # Absolute last resort: dispatch Enter at document level
            page.evaluate(
                """() => {
                    const opts = { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true };
                    document.dispatchEvent(new KeyboardEvent('keydown', opts));
                    document.dispatchEvent(new KeyboardEvent('keypress', opts));
                    document.dispatchEvent(new KeyboardEvent('keyup', opts));
                }"""
            )
            print("Entered DOB via JS setter; dispatched Enter at document level.")

    time.sleep(5)

    # Wait for potential error message to render
    try:
        el = page.wait_for_selector(ERROR_TEXT_CSS, state="visible", timeout=15000)
        observed_error_text = (el.inner_text() or "").strip()
        print("Observed error text:", observed_error_text)
    except PlaywrightTimeout:
        print("No error message element found within timeout.")

    # Assert if EXPECTED_TEXT provided
    if expected_error_text:
        assert observed_error_text == expected_error_text, (
            f"Error - Expected text not found.\n"
            f"Expected: {expected_error_text}\n"
            f"Actual:   {observed_error_text}"
        )

    # Save screenshot
    page.screenshot(path=screenshot_path, full_page=True)
    print(f"Screenshot saved to {screenshot_path}")

    return observed_error_text


# --- Pytest Fixtures ----------------------------------------------------------

@pytest.fixture(scope="session")
def config():
    """
    Collect all configuration from environment variables (with safe defaults).
    """
    cfg = {}

    # Browser
    cfg["HEADLESS"] = os.getenv("HEADLESS", "true").lower() in ("1", "true", "yes")
    cfg["WINDOW_W"] = int(os.getenv("WINDOW_W", "1920"))
    cfg["WINDOW_H"] = int(os.getenv("WINDOW_H", "1080"))

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
    cfg["SUBJECT_BASE"] = os.getenv("SUBJECT", "GOCC - Health Check - HK eClaims – (0700 HKT) - PASS")
    cfg["HTML_INTRO_BASE"] = os.getenv(
        "BODY",
        f"Hi Team</br>"
        f"Good day!</br>"
        f"We have performed the eClaims health check and no issue encountered.</br>"
        f"(ID/DOB verification).<br><strong>Timestamp:</strong> {now}"
    )
    cfg["TEXT_BODY"] = (
        "This email contains an inline screenshot of the automated ClaimSimple HK flow. "
        "If you can't see it, open in an HTML-capable client."
    )
    return cfg


@pytest.fixture
def page(config):
    """
    Provide a Playwright page with headless toggle and viewport sizing from config.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=config["HEADLESS"])
        context = browser.new_context(
            viewport={"width": config["WINDOW_W"], "height": config["WINDOW_H"]},
            ignore_https_errors=True,
        )
        pg = context.new_page()
        yield pg
        try:
            context.close()
        finally:
            browser.close()


# --- The Test ----------------------------------------------------------------

def test_claimsimple_id_dob_flow_screenshot_email(page, config):
    """
    Executes the ClaimSimple flow, asserts the expected error text,
    saves a screenshot, and emails the result inline.
    """
    screenshot_path = config["SCREENSHOT_PATH"]
    observed_error = ""
    test_failed = False
    failure_reason = None

    try:
        observed_error = run_claimsimple_flow_playwright(
            page,
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
            page.screenshot(path=screenshot_path, full_page=True)
        except Exception:
            pass
        raise
    finally:
        # Decide whether to send the email
        should_email = config["ALWAYS_EMAIL"] or (test_failed and config["EMAIL_ON_FAILURE"])

        if should_email and os.path.exists(screenshot_path):
            status = "FAILED" if test_failed else "PASSED"
            subject = f"{config['SUBJECT_BASE']} [{status}] "

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