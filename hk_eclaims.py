# -*- coding: utf-8 -*-
"""
HK eClaims - Multi-flow (3 flows) in ONE file + ONE email with multiple inline screenshots.

Flows (in this order):
  1) Outpatients Claims (DoctorSearch)
  2) My Medical Card (eMedicalCard)
  3) Find My Doctor (Make Claim route)

Each flow:
  - Opens splash
  - Goes to T&C (via click; fallback to provided TNC URL)
  - Accepts checkbox & Continue
  - Switches to ID, enters ID + DOB
  - Verifies expected error text
  - Takes a screenshot

Finally:
  - Sends ONE email with three inline images and statuses
  - Fails pytest if any flow failed (after emailing)
"""

import os
import ssl
import time
import html
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

# -----------------------------------------------------------------------------
# Email / Env Utilities
# -----------------------------------------------------------------------------
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


def build_message_with_multiple_images(
    *,
    from_email: str,
    to_email: str,
    subject: str,
    text_body: str,
    sections: list,
):
    """
    Creates an email with a single HTML body and multiple inline images.
    sections: list of dicts with keys:
        - title (str)
        - html_intro (str)  # may contain HTML entities (we unescape)
        - image_path (str)
        - image_subtype (str) default 'png'
        - observed_error (str|None)
        - failure_reason (str|None)
        - status (str): 'PASSED'|'FAILED'
    """
    msg = EmailMessage()
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Subject"] = subject

    # Plain-text fallback
    msg.set_content(text_body)

    # Build HTML with one <section> per flow
    html_parts = []
    image_cids = []
    for idx, s in enumerate(sections, start=1):
        # Generate CID per image
        cid = make_msgid(domain="inline")
        cid_no_brackets = cid[1:-1]
        image_cids.append((cid, s))

        title = html.escape(s.get("title", f"Flow {idx}"))
        intro = s.get("html_intro", "")
        intro = html.unescape(intro or "")  # turn &lt;br/&gt; into <br/>

        status = html.escape(s.get("status", "UNKNOWN"))
        observed_error = html.escape(s.get("observed_error") or "")
        failure_reason = html.escape(s.get("failure_reason") or "")

        block = f"""
        <section style="margin:14px 0; padding-bottom:12px; border-bottom:1px solid #e8e8e8;">
          <h3 style="font-family:Segoe UI,Arial,sans-serif; margin:0 0 8px;">
            {title} — <span style="color:{'#1a7f37' if status=='PASSED' else '#d92d20'}">{status}</span>
          </h3>
          <div style="font-family:Segoe UI,Arial,sans-serif; font-size:14px; line-height:1.5; color:#222;">
            {intro}
            {"<p><strong>Observed error:</strong> " + observed_error + "</p>" if observed_error else ""}
            {"<p><strong>Failure reason:</strong> " + failure_reason + "</p>" if failure_reason else ""}
          </div>
          <div>
            cid:{cid_no_brackets}
          </div>
        </section>
        """
        html_parts.append(block)

    full_html = f"""
    <html>
      <body style="font-family:Segoe UI, Arial, sans-serif;">
        {''.join(html_parts)}
      </body>
    </html>
    """

    msg.add_alternative(full_html, subtype="html")
    html_part = msg.get_body(preferencelist=("html",))

    # Attach each image as related content
    for cid, s in image_cids:
        img_path = s.get("image_path")
        subtype = s.get("image_subtype", "png")
        if img_path and os.path.exists(img_path):
            with open(img_path, "rb") as f:
                html_part.add_related(
                    f.read(),
                    maintype="image",
                    subtype=subtype,
                    cid=cid,
                    filename=os.path.basename(img_path),
                )
    return msg


# -----------------------------------------------------------------------------
# Playwright Selectors (shared) + Helpers
# -----------------------------------------------------------------------------
CHECKBOX_INPUT = 'input.ui-checkbox__input[name="terms"]'
ID_TOGGLE_ICON = ".ui-selection__symbol"
ID_INPUT = ".qna__input"                 # First .qna__input = ID field in the flow
DOB_NAME_SELECTOR = "input[name='dob']"  # name-based selector
ERROR_TEXT_CSS = ".error-tip-text"


def set_input_value_js(page, css_selector: str, value: str):
    """Set value via JS and dispatch input/change so frameworks commit state."""
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


def commit_and_press_enter(locator):
    """Commit value (input/change/blur) then send Enter key."""
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
    If the app calls a verify/validate endpoint, wait it out (non-fatal).
    url_hint may be a '|' pipe-separated set of substrings.
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
        pass


def wait_until_painted(page, locator, timeout_ms: int = 5000):
    """Ensure the element is visible, painted, non-zero size; flush two rAFs."""
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
    page.evaluate("""() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))""")


def stable_screenshot(page, path, retries: int = 3, delay_ms: int = 400, full_page: bool = True):
    """Screenshot with small retry loop to avoid paint races."""
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
    page.screenshot(path=path, full_page=full_page)


# -----------------------------------------------------------------------------
# Main single-flow runner (parameterized by selectors & URLs)
# -----------------------------------------------------------------------------
def run_claimsimple_flow_playwright(
    page, *,
    cs_hk_url,
    tnc_emc_url,
    claim_id,
    claim_dob,
    expected_error_text,
    screenshot_path,
    claim_btn_selector,
    continue_btn_selector,
    post_assert_delay_ms: int = 1000,
    verify_url_hint: str | None = None,
) -> str:
    """
    Runs ONE flow and takes a screenshot at the end.
    Returns the observed error text (if found).
    """
    observed_error_text = ""

    # Navigate to splash (tolerate SPA redirects)
    page.goto(cs_hk_url, wait_until="domcontentloaded")

    # 1) Click the flow-specific Claim button
    page.wait_for_selector(claim_btn_selector, state="visible", timeout=30000)
    page.locator(claim_btn_selector).scroll_into_view_if_needed()
    page.locator(claim_btn_selector).click()
    print(f"Clicked flow button: {claim_btn_selector}")

    # 2) Wait for checkbox; if not, route directly to T&C and retry
    try:
        page.wait_for_selector(CHECKBOX_INPUT, state="visible", timeout=20000)
    except Exception:
        print("Checkbox not visible after click; attempting direct hash-route and retry.")
        page.evaluate(f"location.href = '{tnc_emc_url}'")
        page.wait_for_selector(CHECKBOX_INPUT, state="visible", timeout=30000)

    # 3) Accept T&Cs
    page.locator(CHECKBOX_INPUT).scroll_into_view_if_needed()
    page.locator(CHECKBOX_INPUT).check(force=True)
    print("Checkbox clicked.")

    # 4) Continue (flow-specific button)
    page.wait_for_selector(continue_btn_selector, state="visible", timeout=30000)
    page.locator(continue_btn_selector).scroll_into_view_if_needed()
    page.locator(continue_btn_selector).click()
    print("Clicked Continue.")

    # 5) Switch to ID option
    page.wait_for_selector(ID_TOGGLE_ICON, state="visible", timeout=30000)
    page.locator(ID_TOGGLE_ICON).first.scroll_into_view_if_needed()
    page.locator(ID_TOGGLE_ICON).first.click()
    print("Switched to ID entry.")

    # 6) Enter ID
    page.wait_for_selector(ID_INPUT, state="visible", timeout=30000)
    id_box = page.locator(ID_INPUT).first
    id_box.scroll_into_view_if_needed()
    id_box.click()
    id_box.fill("")
    id_box.fill(claim_id)
    commit_and_press_enter(id_box)
    print("Entered ID.")
    time.sleep(0.4)

    # 7) Enter DOB by name='dob' with fallback to JS
    page.wait_for_selector(DOB_NAME_SELECTOR, state="attached", timeout=30000)
    dob_box = page.locator(DOB_NAME_SELECTOR).first

    native_dob_ok = True
    try:
        try:
            dob_box.scroll_into_view_if_needed()
        except Exception:
            pass
        dob_box.click(timeout=1000)
        dob_box.fill("")
        dob_box.type(claim_dob, delay=18)
        commit_and_press_enter(dob_box)
        print("Entered DOB via native typing.")
    except Exception as e:
        native_dob_ok = False
        print(f"Native DOB typing failed; fallback to JS. Reason: {e}")

    if not native_dob_ok:
        set_input_value_js(page, DOB_NAME_SELECTOR, claim_dob)
        try:
            try:
                dob_box.evaluate("el => el.focus()")
            except Exception:
                pass
            commit_and_press_enter(dob_box)
            print("Entered DOB via JS setter + Enter.")
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
            print("Entered DOB via JS; dispatched Enter at document level.")

    # 8) Wait for potential error message to render
    try:
        err = page.wait_for_selector(ERROR_TEXT_CSS, state="visible", timeout=30000)
        observed_error_text = (err.inner_text() or "").strip()
        print("Observed error text:", observed_error_text)
    except PlaywrightTimeout:
        print("No error message element found within timeout.")

    # 9) Assert text + visually stabilize before screenshot
    if expected_error_text:
        expected_norm = expected_error_text.strip().casefold()
        actual_norm = observed_error_text.strip().casefold()
        assert expected_norm in actual_norm or actual_norm in expected_norm, (
            "Error - Expected text not found.\n"
            f"Expected (contains/equals): {expected_error_text}\n"
            f"Actual:                     {observed_error_text}"
        )

        # Commit validation + wait for network verify/validate if any
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

        wait_for_verify_response_if_any(page, verify_url_hint, timeout_ms=10000)

        try:
            err_loc = page.locator(ERROR_TEXT_CSS).filter(has_text=expected_error_text).first
            if not err_loc.count():
                err_loc = page.locator(ERROR_TEXT_CSS).first
            wait_until_painted(page, err_loc, timeout_ms=5000)
        except Exception:
            pass

        if post_assert_delay_ms and post_assert_delay_ms > 0:
            time.sleep(post_assert_delay_ms / 1000.0)

    # 10) Screenshot
    stable_screenshot(page, screenshot_path, retries=3, delay_ms=400, full_page=True)
    print(f"Screenshot saved to {screenshot_path}")

    return observed_error_text


# -----------------------------------------------------------------------------
# Pytest Fixtures (single browser across flows)
# -----------------------------------------------------------------------------
@pytest.fixture(scope="session")
def config():
    """
    Collect configuration from environment variables (with safe defaults).
    """
    cfg = {}

    # Browser
    cfg["HEADLESS"] = os.getenv("HEADLESS", "true").lower() in ("1", "true", "yes")
    cfg["WINDOW_W"] = int(os.getenv("WINDOW_W", "1920"))
    cfg["WINDOW_H"] = int(os.getenv("WINDOW_H", "1080"))

    # URLs per flow (override via env)
    cfg["CS_HK_URL1"] = os.getenv("CS_HK_URL1", "https://www.claimsimple.hk/#/")
    cfg["TNC_EMC_URL1"] = os.getenv("TNC_EMC_URL1", "https://www.claimsimple.hk/#/tnc")
    cfg["CS_HK_URL2"] = os.getenv("CS_HK_URL2", "https://www.claimsimple.hk/#/")
    cfg["TNC_EMC_URL2"] = os.getenv("TNC_EMC_URL2", "https://www.claimsimple.hk/eMedicalCard#")
    cfg["CS_HK_URL3"] = os.getenv("CS_HK_URL3", "https://www.claimsimple.hk/#/")
    cfg["TNC_EMC_URL3"] = os.getenv("TNC_EMC_URL3", "https://www.claimsimple.hk/DoctorSearch#/")

    # Inputs (shared)
    cfg["CLAIM_ID"] = os.getenv("CLAIM_ID", "A0000000")
    cfg["CLAIM_DOB"] = os.getenv("CLAIM_DOB", "01/01/1990")

    # Assertion text
    cfg["EXPECTED_ERROR_TEXT"] = os.getenv(
        "EXPECTED_ERROR_TEXT",
        "The information you provided does not match our records. Please try again."
    )

    # Screenshots: read explicit per-flow paths if provided, else derive
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    default_base = os.path.join("screenshots", f"screenshot_{ts}")
    cfg["SHOT1"] = os.getenv("SCREENSHOT_PATH1", f"{default_base}_1.png")
    cfg["SHOT2"] = os.getenv("SCREENSHOT_PATH2", f"{default_base}_2.png")
    cfg["SHOT3"] = os.getenv("SCREENSHOT_PATH3", f"{default_base}_3.png")

    # Email controls
    cfg["SMTP_USERNAME"] = os.getenv("SMTP_USERNAME")
    cfg["SMTP_PASSWORD"] = os.getenv("SMTP_PASSWORD")
    cfg["TO_EMAIL"] = os.getenv("TO_EMAIL")
    cfg["USE_SSL_465"] = os.getenv("USE_SSL_465", "false").lower() in ("1", "true", "yes")

    cfg["ALWAYS_EMAIL"] = os.getenv("ALWAYS_EMAIL", "true").lower() in ("1", "true", "yes")
    cfg["EMAIL_ON_FAILURE"] = os.getenv("EMAIL_ON_FAILURE", "true").lower() in ("1", "true", "yes")

    # Email content (per flow)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cfg["SUBJECT_BASE"] = os.getenv("SUBJECT", "GOCC - Health Check - HK eClaims – (0700 HKT)")
    cfg["BODY1"] = os.getenv("BODY1") or (
        "Hi Team,<br/><br/>Good day!<br/><br/>"
        "<strong>OUTPATIENTS CLAIMS:</strong><br/>"
        f"<em>Timestamp: {now_str}</em>"
    )
    cfg["BODY2"] = os.getenv("BODY2") or (
        "Hi Team,<br/><br/>Good day!<br/><br/>"
        "<strong>MY MEDICAL CARD:</strong><br/>"
        f"<em>Timestamp: {now_str}</em>"
    )
    cfg["BODY3"] = os.getenv("BODY3") or (
        "Hi Team,<br/><br/>Good day!<br/><br/>"
        "<strong>FIND MY DOCTOR:</strong><br/>"
        f"<em>Timestamp: {now_str}</em>"
    )
    cfg["TEXT_BODY"] = (
        "This email contains inline screenshots of the automated HK eClaims flows. "
        "If you can't see them, open in an HTML-capable client."
    )

    cfg["POST_ASSERT_DELAY_MS"] = int(os.getenv("POST_ASSERT_DELAY_MS", "1000"))
    cfg["VERIFY_URL_HINT"] = os.getenv("VERIFY_URL_HINT", "verify|validate")

    return cfg


@pytest.fixture(scope="session")
def page(config):
    """One browser/page used for all three flows."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=config["HEADLESS"])
        context = browser.new_context(
            viewport={"width": config["WINDOW_W"], "height": config["WINDOW_H"]},
            ignore_https_errors=True,
            timezone_id="Asia/Hong_Kong",
            locale="en-HK",
        )
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


# -----------------------------------------------------------------------------
# The single pytest "test" that runs all flows and sends one email
# -----------------------------------------------------------------------------
def test_all_flows_single_email(page, config):
    """
    Runs 3 flows in sequence and sends one email with all results & screenshots.
    Fails the test at the end if any flow failed.
    """
    flows = [
        {
            "title": "Outpatients Claims",
            "cs_hk_url": config["CS_HK_URL1"],
            "tnc_url": config["TNC_EMC_URL1"],
            "claim_btn_selector": ".splash__body_search-doctor",
            "continue_btn_selector": ".button-primary.button-primary--full.button-doctorsearch-continue",
            "screenshot": config["SHOT1"],
            "html_intro": config["BODY1"],
        },
        {
            "title": "My Medical Card",
            "cs_hk_url": config["CS_HK_URL2"],
            "tnc_url": config["TNC_EMC_URL2"],
            "claim_btn_selector": ".splash__body_get-emedicard",
            "continue_btn_selector": ".button-primary.button-primary--full.button-emedicalcard-continue",
            "screenshot": config["SHOT2"],
            "html_intro": config["BODY2"],
        },
        {
            "title": "Find My Doctor",
            "cs_hk_url": config["CS_HK_URL3"],
            "tnc_url": config["TNC_EMC_URL3"],
            "claim_btn_selector": ".splash__body_make-claim",
            "continue_btn_selector": ".button-primary.button-primary--full.button-doctorsearch-continue",
            "screenshot": config["SHOT3"],
            "html_intro": config["BODY3"],
        },
    ]

    results = []
    any_fail = False

    for f in flows:
        observed_error = ""
        status = "PASSED"
        failure_reason = None

        try:
            observed_error = run_claimsimple_flow_playwright(
                page,
                cs_hk_url=f["cs_hk_url"],
                tnc_emc_url=f["tnc_url"],
                claim_id=config["CLAIM_ID"],
                claim_dob=config["CLAIM_DOB"],
                expected_error_text=config["EXPECTED_ERROR_TEXT"],
                screenshot_path=f["screenshot"],
                claim_btn_selector=f["claim_btn_selector"],
                continue_btn_selector=f["continue_btn_selector"],
                post_assert_delay_ms=config["POST_ASSERT_DELAY_MS"],
                verify_url_hint=config["VERIFY_URL_HINT"],
            )
        except Exception as e:
            status = "FAILED"
            any_fail = True
            failure_reason = str(e)
            # Best-effort screenshot on failure
            try:
                stable_screenshot(page, f["screenshot"], retries=2, delay_ms=300, full_page=True)
            except Exception:
                pass

        results.append({
            "title": f["title"],
            "status": status,
            "observed_error": observed_error,
            "failure_reason": failure_reason,
            "image_path": f["screenshot"],
            "image_subtype": "png",
            "html_intro": f["html_intro"],
        })

    # ---------------- Send one email (ALWAYS or only on failure) --------------
    should_email = config["ALWAYS_EMAIL"] or (any_fail and config["EMAIL_ON_FAILURE"])
    if should_email:
        subject_status = "FAILED" if any_fail else "PASSED"
        subject = f"{config['SUBJECT_BASE']} [{subject_status}]"

        # Validate SMTP vars only when needed
        smtp_user = config["SMTP_USERNAME"] or get_env("SMTP_USERNAME")
        smtp_pass = config["SMTP_PASSWORD"] or get_env("SMTP_PASSWORD")
        to_email  = config["TO_EMAIL"] or get_env("TO_EMAIL")

        msg = build_message_with_multiple_images(
            from_email=smtp_user,
            to_email=to_email,
            subject=subject,
            text_body=config["TEXT_BODY"],
            sections=results,
        )
        send_via_gmail_smtp(
            msg,
            smtp_user,
            smtp_pass,
            use_port_465=config["USE_SSL_465"],
        )
        print(f"✅ Single email sent to {to_email} with {len(results)} inline screenshots.")

    # Finally, fail the test if any flow failed (after sending email)
    if any_fail:
        failed = [r for r in results if r["status"] == "FAILED"]
        details = "\n".join(
            f"- {r['title']}: {r['failure_reason'] or 'Unknown error'}"
            for r in failed
        )
        raise AssertionError(f"One or more flows FAILED:\n{details}")