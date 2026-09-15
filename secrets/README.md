# secrets/

OAuth credentials and tokens live here. Everything in this directory except this
README is **gitignored** — never commit it.

## `gdrive_credentials.json` *(you provide)*

The OAuth client for the Google Drive publisher:

1. https://console.cloud.google.com/apis/credentials
2. Create (or reuse) a project → **Enable the Google Drive API**
3. OAuth consent screen → External, add yourself as a test user
4. Create Credentials → OAuth client ID → **Desktop app**
5. Download the JSON → rename to `gdrive_credentials.json` → drop it here

## `gdrive_token.json` *(auto-generated)*

Written by the first `intelnet drive init` after you approve access in the
browser. Refreshed silently afterwards. Delete it to force re-consent.

## Scope

`https://www.googleapis.com/auth/drive.file` — the app can only see and manage
files **it created** (the Intelligence Network folder, the daily digest docs,
the fixed-link "Latest" doc). It cannot read the rest of your Drive.

Revoke at any time: delete `gdrive_token.json` and remove the app at
https://myaccount.google.com/permissions.
