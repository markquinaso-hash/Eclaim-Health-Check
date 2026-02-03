name: Email Inline Screenshot (Playwright)

on:
  push:
    branches: [ main ]

jobs:
  send-email:
    runs-on: ubuntu-latest
    steps:
      - name: Check out repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install Python dependencies (Playwright + pytest + dotenv)
        run: |
          python -m pip install --upgrade pip
          python -m pip install playwright pytest python-dotenv

      - name: Install Playwright browsers (Chromium) + OS deps
        run: |
          python -m playwright install --with-deps chromium

      - name: Run pytest (Playwright)
        env:
          # Gmail SMTP (use App Password!)
          SMTP_USERNAME: ${{ secrets.SMTP_USERNAME }}
          SMTP_PASSWORD: ${{ secrets.SMTP_PASSWORD }}
          TO_EMAIL:      ${{ secrets.TO_EMAIL }}

          # Browser behavior
          HEADLESS: "true"
          WINDOW_W: "1366"
          WINDOW_H: "900"

          # App inputs
          CS_HK_URL: "https://www.claimsimple.hk/#/"
          TNC_EMC_URL: "https://www.claimsimple.hk/DoctorSearch#/"
          CLAIM_ID: "A0000000"
          CLAIM_DOB: "01/01/1990"
          EXPECTED_ERROR_TEXT: "The information you provided does not match our records. Please try again."

          # Email behavior + content
          ALWAYS_EMAIL: "true"
          EMAIL_ON_FAILURE: "true"
          USE_SSL_465: "false"
          SUBJECT: "ClaimSimple HK Check – Inline Screenshot"
          BODY: "Hello from CI. Inline screenshot attached below."
          SCREENSHOT_PATH: "screenshot.png"
        run: |
          pytest -q -s send_test_email_with_screenshot_playwright.py
