# Hera News Engine v3

This repository's website and news automation are isolated by file boundary.
The workflow executes `update_news.py` and may commit only `news.json` and
`.news_engine_state.json`. It does not edit the website HTML, assets, tones,
advertising files, or privacy pages.

## Data flow

1. GitHub Actions reads public news from ITN, Lankadeepa, and Ada Derana.
2. The engine removes expired, duplicate, promotional, gossip, and irrelevant
   foreign stories. Up to 100 valid stories from the last seven days are kept.
3. Curated items are written to Firebase Realtime Database under `news/feed`
   and `news/items`, newest first by `published_at_ms`.
4. Only high-impact, urgent, or emergency stories are eligible for FCM. Normal
   stories, ordinary sports updates, and relevant foreign context remain feed
   only. FCM messages are data-only so the app can honour each user's sound and
   notification settings.

## Required repository secret

- `FIREBASE_SERVICE_ACCOUNT`: the Firebase service-account JSON. Never place
  this JSON, a GitHub token, or a Firebase private key in the repository,
  workflow file, dispatch URL, logs, or `news.json`.

The native GitHub schedule is the default. GitHub documents scheduled workflows
as best-effort, so an exact uninterrupted ten-minute cadence cannot be
guaranteed. The engine compensates by retaining history, using publication time,
and catching up after a delayed run. Optional `repository_dispatch` support is
available for an independently managed watchdog, but it is not required and
should not be given a broad classic personal access token.

## Safety controls

- A seven-minute duplicate-trigger guard prevents accidental dispatch floods.
- Workflow concurrency prevents overlapping writers.
- A first-run notification baseline prevents an old backlog from being sent.
- Source-wide failure makes the workflow fail instead of showing a false green
  result.
- TLS certificate verification remains enabled for source requests.
- Python tests run before every live engine execution.

Run a no-write local validation with:

```powershell
$env:HERA_DRY_RUN = 'true'
$env:HERA_FORCE_RUN = 'true'
python update_news.py
```

After one successful v3 production run and Firebase read-back verification,
disable the old Google Apps Script time triggers to prevent two writers from
competing for the same feed.
