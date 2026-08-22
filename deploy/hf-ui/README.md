---
title: Unilog Enrichment UI
emoji: 🔧
colorFrom: blue
colorTo: gray
sdk: streamlit
sdk_version: 1.40.0
app_file: app.py
pinned: false
short_description: Front end for the Unilog product-enrichment API
---

# Unilog Product Enrichment — front end

Streamlit UI for the enrichment API. It holds no product logic and does no
scraping; it renders whatever the API returns, including the per-field
citations and the list of values the pipeline refused to emit.

## Required secret

Set this in **Settings → Variables and secrets**:

| Name | Value |
| --- | --- |
| `API_BASE_URL` | `https://Anirban511-unihack-api.hf.space` |

Without it the app defaults to `http://127.0.0.1:8000`, which only works when
you are running the API on the same machine.
