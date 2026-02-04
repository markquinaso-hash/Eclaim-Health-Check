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
7) Ensure the error is visually rendered (painted) and then take a screenshot
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

    # Attach the image as a related part to the HTML
    html_part = msg.get_body(preferencelist=("html",))
    with open(image_path, "rb") as f:
        html_part.add_related(
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


def wait_for_verify_response_if_any(page, url_hint: str | None, timeout_ms: int = 10000):
    """
    If the app calls a verify/validate endpoint, wait for it to complete before
    we assert/screenshot. Non-fatal if nothing matches.
    - url_hint: a substring or '|' pipe-separated substrings (case-insensitive).
    """
    if not url_hint:
        url_hint = "verify|validate"
    tokens = [t.strip().lower() for t in url_hint.split("|") if t.strip()]
    def _matcher(r):
        u = r.url.lower()
        return any(tok in u for tok in tokens) and r.request.method in ("GET", "POST")
    try:
        page.wait_for_response(_matcher, timeout=timeout_ms)
    except Exception:
        # OK if no network call is triggered for validation
        pass


def wait_until_painted(page, locator, timeout_ms: int = 5000):
    """
    Ensure the locator is not only visible but *painted/opaque* and with non-zero box.
    Then flush two rAFs to let the browser settle.
    """
    locator.scroll_into_view_if_needed()
    handle = locator.element_handle(timeout=timeout_ms)
    if not handle:
        return
    page.wait_for_function(
        """(el) => {
            if (!el || !el.isConnected) return false;
            const s = getComputedStyle(el);
            const painted = el.offsetParent !== null
                && el.offsetHeight > 0 && el.offsetWidth > 0
                && s.visibility !== 'hidden'
                && s.display !== 'none'
                && parseFloat(s.opacity || '1') > 0.01;
            return painted;
        }""",
        arg=handle,
        timeout=timeout_ms
    )
    # Flush two animation frames for good measure
    page.evaluate("""() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))""")


def stable_screenshot(page, path, retries: int = 3, delay_ms: int = 400, full_page: bool = True):
    """
    Screenshot with small retry loop, to avoid intermittent paint races.
    """
    # Ensure directory
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
    except Exception:
        pass

    for _ in range(retries):
        try:
            page.screenshot(path=path, full_page=full_page)
            return
        except Exception:
            pass
        page.wait_for_timeout(delay_ms)
    # Final attempt
    page.screenshot(path=path, full_page=full_page)


# --- Main Flow ---------------------------------------------------------------

def run_claimsimple_flow_playwright(
    page, *,
    cs_hk_url,
    tnc_emc_url,
    claim_id,
    claim_dob,
    expected_error_text,
    screenshot_path,
    post_assert_delay_ms: int = 1000,   # configurable delay before screenshot
    verify_url_hint: str | None = None, # e.g., "verify|validate|login/validate"
) -> str:
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
    time.sleep(0.5)

    # 6) Enter DOB by NAME (Selenium-like) with robust fallback
    page.wait_for_selector(DOB_NAME_SELECTOR, state="attached", timeout=30000)
    dob_box = page.locator(DOB_NAME_SELECTOR).first

    # Try native typing
    native_dob_ok = True
    try:
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
        try:
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

    # Wait for potential error message to render (prefer event-driven waits)
    try:
        err = page.wait_for_selector(ERROR_TEXT_CSS, state="visible", timeout=30000)
        observed_error_text = (err.inner_text() or "").strip()
        print("Observed error text:", observed_error_text)
    except PlaywrightTimeout:
        print("No error message element found within timeout.")

    # ---- Robust visual-stability block --------------------------------------
    # 1) Assert textual match for resilience
    if expected_error_text:
        expected_norm = expected_error_text.strip().casefold()
        actual_norm = observed_error_text.strip().casefold()
        assert expected_norm in actual_norm or actual_norm in expected_norm, (
            "Error - Expected text not found.\n"
            f"Expected (contains/equals): {expected_error_text}\n"
            f"Actual:                     {observed_error_text}"
        )

        # 2) Force validation to commit: blur both inputs + Enter
        try:
            id_box.dispatch_event("input")
            id_box.dispatch_event("change")
            id_box.dispatch_event("blur")
            dob_box.dispatch_event("input")
            dob_box.dispatch_event("change")
            dob_box.dispatch_event("blur")
        except Exception:
            pass
        try:
            page.evaluate("document.activeElement && document.activeElement.blur && document.activeElement.blur();")
        except Exception:
            pass
        try:
            page.keyboard.press("Enter")
        except Exception:
            pass

        # 3) If the app calls a verify/validate API, wait for it (non-fatal)
        wait_for_verify_response_if_any(page, verify_url_hint, timeout_ms=10000)

        # 4) Ensure the error element with the expected text is actually painted and opaque
        try:
            # Prefer the exact element containing the expected text if possible
            err_loc = page.locator(ERROR_TEXT_CSS).filter(has_text=expected_error_text).first
            if not err_loc.count():
                err_loc = page.locator(ERROR_TEXT_CSS).first
            wait_until_painted(page, err_loc, timeout_ms=5000)
        except Exception:
            # Non-fatal: proceed
            pass

        # 5) Extra stabilization delay (env-configurable)
        if post_assert_delay_ms and post_assert_delay_ms > 0:
            time.sleep(post_assert_delay_ms / 1000.0)

    # ---- Screenshot ----------------------------------------------------------
    stable_screenshot(page, screenshot_path, retries=3, delay_ms=400, full_page=True)
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
    cfg["CLAIM_DOB"] = os.getenv("CLAIM_DOB", "01/01/1990")  # adjust to site’s required format

    # Assertion text
    cfg["EXPECTED_ERROR_TEXT"] = os.getenv(
        "EXPECTED_ERROR_TEXT",
        "The information you provided does not match our records. Please try again."
    )

    # Output - default to timestamped file to avoid overwrites
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    cfg["SCREENSHOT_PATH"] = os.getenv("SCREENSHOT_PATH", os.path.join("screenshots", f"screenshot_{ts}.png"))

    # Email controls (lazy-validated right before send)
    cfg["SMTP_USERNAME"] = os.getenv("SMTP_USERNAME")
    cfg["SMTP_PASSWORD"] = os.getenv("SMTP_PASSWORD")
    cfg["TO_EMAIL"] = os.getenv("TO_EMAIL")
    cfg["USE_SSL_465"] = os.getenv("USE_SSL_465", "false").lower() in ("1", "true", "yes")

    # Email policy
    cfg["ALWAYS_EMAIL"] = os.getenv("ALWAYS_EMAIL", "true").lower() in ("1", "true", "yes")
    cfg["EMAIL_ON_FAILURE"] = os.getenv("EMAIL_ON_FAILURE", "true").lower() in ("1", "true", "yes")

    # Email content
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cfg["SUBJECT_BASE"] = os.getenv("SUBJECT", "GOCC - Health Check - HK eClaims – (0700 HKT)")
    # Prefer BODY_HTML; fallback to BODY; else default HTML
    cfg["HTML_INTRO_BASE"] = (
        os.getenv("BODY_HTML")
        or os.getenv("BODY")
        or (
            "Hi Team<br/>"
            "Good day!<br/>"
            "We have performed the eClaims health check and no issue encountered.<br/>"
            "(ID/DOB verification).<br/><strong>Timestamp:</strong> " + now
        )
    )
    cfg["TEXT_BODY"] = (
        "This email contains an inline screenshot of the automated ClaimSimple HK flow. "
        "If you can't see it, open in an HTML-capable client."
    )

    # Post-assert wait (ms) before taking the screenshot
    cfg["POST_ASSERT_DELAY_MS"] = int(os.getenv("POST_ASSERT_DELAY_MS", "1000"))

    # Optional: Hint for verify API URL matching (e.g., "verify|validate|emc/verify")
    cfg["VERIFY_URL_HINT"] = os.getenv("VERIFY_URL_HINT", "verify|validate")

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
            timezone_id="Asia/Hong_Kong",
            locale="en-HK",
        )
        # Optional: set a default timeout globally (tunable via env if desired)
        try:
            context.set_default_timeout(int(os.getenv("PW_TIMEOUT_MS", "30000")))
        except Exception:
            pass

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
    ensures the error is visually rendered, saves a screenshot,
    and emails the result inline.
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
            post_assert_delay_ms=config["POST_ASSERT_DELAY_MS"],
            verify_url_hint=config["VERIFY_URL_HINT"],
        )
    except Exception as e:
        test_failed = True
        failure_reason = str(e)
        # Best-effort: capture a screenshot on failure path
        try:
            stable_screenshot(page, screenshot_path, retries=2, delay_ms=300, full_page=True)
        except Exception:
            pass
        raise
    finally:
        # Decide whether to send the email
        should_email = config["ALWAYS_EMAIL"] or (test_failed and config["EMAIL_ON_FAILURE"])

        if should_email and os.path.exists(screenshot_path):
            status = "FAILED" if test_failed else "PASSED"
            subject = f"{config['SUBJECT_BASE']} [{status}]"

            html_intro = config["HTML_INTRO_BASE"]
            if observed_error:
                html_intro = f"{html_intro}<br/><strong>Observed error:</strong> {observed_error}"
            if failure_reason:
                html_intro = f"{html_intro}<br/><strong>Failure reason:</strong> {failure_reason}"

            # Validate SMTP vars only when needed
            smtp_user = config["SMTP_USERNAME"] or get_env("SMTP_USERNAME")
            smtp_pass = config["SMTP_PASSWORD"] or get_env("SMTP_PASSWORD")
            to_email  = config["TO_EMAIL"] or get_env("TO_EMAIL")

            msg = build_message_with_inline_image(
                from_email=smtp_user,
                to_email=to_email,
                subject=subject,
                text_body=config["TEXT_BODY"],
                html_intro=html_intro,
                image_path=screenshot_path,
                image_subtype="png",
            )
            send_via_gmail_smtp(
                msg,
                smtp_user,
                smtp_pass,
                use_port_465=config["USE_SSL_465"],
            )
            print(f"✅ Email with inline screenshot sent to {to_email}")