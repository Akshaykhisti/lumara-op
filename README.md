# Lumara Home — Ops Dashboard

Live: **https://akshaykhisti.github.io/lumara-op/**

A single-page operations dashboard for Lumara Home: orders and fulfilment, Meta ads
performance, stock health, and Klaviyo status. Replaces the daily ops email.

- `index.html` — the whole UI. Fetches `data.json` on load and every 5 minutes.
- `data.json` — the metrics payload. This is the only file the hourly refresh rewrites.
- `build_data.py` — turns a raw connector dump (`raw.json`) into `data.json`.
- `publish.py` — pushes files here via the GitHub Contents API.

Refreshed hourly by a scheduled Claude task that pulls Shopify Admin, Meta Ads and
Klaviyo, rebuilds `data.json`, and pushes it. `noindex` on the page; the repo is
public, so treat the URL as the only thing keeping this semi-private.
