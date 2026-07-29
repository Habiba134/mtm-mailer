# SBP Mark-to-Market Revaluation Exchange Rate — Automated PDF Pipeline

Monitors the State Bank of Pakistan website daily, downloads the
"Mark-to-Market Revaluation Exchange Rate" PDF as soon as it's published,
and emails it automatically — no manual steps needed.

## How it works
- Runs automatically via GitHub Actions, Mon–Fri, starting 4:00 PM PKT
- Polls SBP every 10 minutes until the file is found (cutoff: 7:00 PM PKT)
- Extracts the rate table into the email body + attaches the original PDF
- Sends a failure alert to the admin if nothing is published by the cutoff

## One-time setup
1. Push this repo to GitHub (see chat instructions for the click-by-click steps)
2. Go to **Settings → Secrets and variables → Actions** and add:
   - `SMTP_HOST` (e.g. `smtp.gmail.com`)
   - `SMTP_PORT` (e.g. `587`)
   - `SMTP_USER` (your email)
   - `SMTP_PASSWORD` (Gmail App Password, not your normal password)
   - `EMAIL_FROM` (your email)
   - `EMAIL_TO` (comma-separated: `sir@example.com, you@example.com`)
   - `ADMIN_EMAIL` (optional — failure alerts go here)
3. Go to the **Actions** tab → select the workflow → **Run workflow** to test

After that, it runs completely on its own, every weekday.
