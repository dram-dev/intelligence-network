# secrets/

OAuth credentials and tokens live here. Everything in this directory except this
README is **gitignored** — never commit it.

## `gdrive_credentials.json` *(you provide, once)*

The OAuth client for the Google Drive publisher. In Google Cloud Console
(console.cloud.google.com), signed in as the account whose Drive should hold
the digest:

1. **Project** — create one named `intelligence-network`
   (console.cloud.google.com/projectcreate) and select it.
2. **Drive API** — APIs & Services → Library → *Google Drive API* → **Enable**
   (console.cloud.google.com/apis/library/drive.googleapis.com).
3. **Google Auth Platform → Get started** (console.cloud.google.com/auth/overview):
   app name *Intelligence Network*, your e-mail as support + contact,
   Audience **External**, accept the policy → **Create**.
4. **Data Access** → Add or remove scopes → tick
   `https://www.googleapis.com/auth/drive.file` → Update → **Save**.
   (Non-sensitive: no verification needed.)
5. **Audience** → Publishing status → **Publish app** → Confirm.
   In *Testing*, Google expires refresh tokens after 7 days and the nightly
   publish would break every week.
6. **Clients** → **Create client** → type **Desktop app** → name
   *intelnet mac mini* → Create → **Download JSON** now (the secret is only
   shown at creation).
7. Move it here:
   `mv "$(ls -t ~/Downloads/client_secret_*.json | head -1)" secrets/gdrive_credentials.json && chmod 600 secrets/gdrive_credentials.json`

## `gdrive_token.json` *(auto-generated)*

Written by `uv run intelnet drive init` after you approve access in the browser
(run it from the project root, in your own terminal on the Mac mini). Refreshed
silently afterwards. The nightly run and the bot **never** open a browser: if
the token is missing or revoked they report "run `uv run intelnet drive init`".

Switching Google accounts: delete this file and run `drive init` again — the
app notices the old folder isn't reachable and creates a new one.

## Scope

`https://www.googleapis.com/auth/drive.file` — the app can only see and manage
files **it created** (the Intelligence Network folder, the daily digest docs,
the fixed-link "Latest" doc). It cannot read the rest of your Drive.

Revoke at any time: delete `gdrive_token.json` and remove the app at
https://myaccount.google.com/permissions.
