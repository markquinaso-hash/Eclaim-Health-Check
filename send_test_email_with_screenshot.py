#!/usr/bin/env python3
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid

# Optional .env support
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

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

def capture_screenshot(url: str, out_path: str, width: int = 1366, height: int = 900):
    """
    Capture a full-page screenshot of the URL using Playwright (Chromium).
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        # Best-effort: wait for network to be idle so the page is fully rendered
        page.goto(url, wait_until="networkidle", timeout=90_000)
        page.screenshot(path=out_path, full_page=True)
        browser.close()

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
    # Strip < > for the src reference, but keep them when attaching
    cid_no_brackets = cid[1:-1]

    html_body = f"""
    <html>
      <body style="font-family:Segoe UI, Arial, sans-serif;">
        <p>{html_intro}</p>
        <p>
          <img src="cid:{cid_no_brackets}" alt="Screenshot" style="max-width:100%; height:auto; border:1px solid #ddd;"/>
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

def main():
    smtp_username = get_env("SMTP_USERNAME")
    smtp_password = get_env("SMTP_PASSWORD")
    to_email = get_env("TO_EMAIL")

    # URL to screenshot: from env or fallback to your New Relic URL
    screenshot_url = os.getenv(
        "SCREENSHOT_URL",
        "https://docs.newrelic.com/docs/apis/nerdgraph/examples/nerdgraph-cloud-integrations-api-tutorial/#list-enabled-provider-accounts",
    )
    screenshot_path = os.getenv("SCREENSHOT_PATH", "screenshot.png")

    # Subject/body (allow override via env)
    subject = os.getenv("SUBJECT", "Inline Image Email Test (via GitHub Actions)")
    text_body = "This email contains an inline screenshot image. If you can't see it, open in an HTML-capable client."
    html_intro = os.getenv(
        "BODY",
        f"Hello! Here is the screenshot of:<br><code>{screenshot_url}</code>"
    )

    # Capture screenshot
    capture_screenshot(screenshot_url, screenshot_path)

    # Build email with inline image
    msg = build_message_with_inline_image(
        from_email=smtp_username,
        to_email=to_email,
        subject=subject,
        text_body=text_body,
        html_intro=html_intro,
        image_path=screenshot_path,
        image_subtype="png",
    )

    # Send (587 by default). If needed, set USE_SSL_465=true in env to switch.
    use_ssl_465 = os.getenv("USE_SSL_465", "false").lower() in ("1", "true", "yes")
    send_via_gmail_smtp(msg, smtp_username, smtp_password, use_port_465=use_ssl_465)
    print(f"✅ Email with inline screenshot sent to {to_email}")

if __name__ == "__main__":
    main()