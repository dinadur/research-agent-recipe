---
name: reliable-research
description: "Bounded, receipt-verified web research: search, extract, then cite only receipt-bound sources."
version: 0.1.0
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [Research, Citations, Verification]
---

# Reliable research

Minimal example skill file for the research routes in this recipe. The controller (the patched Hermes routes)
enforces the rules below; this file only tells the model what to expect. Adapt the wording to your deployment.

## Workflow

1. Search with `web_search` and read pages with `web_extract`, within the budget the controller states for this turn.
   Extract only exact URLs returned by your searches in this turn.
2. Prefer official pages (manufacturer, project or vendor documentation) for specifications, and independent
   hands-on reviews from at least two different publishers for limitations. Retailer pages count only as price
   evidence.
3. Do not delegate, browse through the terminal, read cached files or write files.
4. The receipt is captured by the controller with exactly:

   ```
   python3 ~/.hermes/skills/reliable-research/scripts/capture_receipt.py
   ```

   It prints `RESEARCH_RECEIPT_OK ...` followed by one `SOURCE <url>` line per page you may cite, or
   `RESEARCH_RECEIPT_ERROR <reason>`.
5. Answer only from the receipt-bound pages. Cite every factual paragraph with those exact URLs. If the evidence does
   not establish something (a current price, a spec, a compatibility claim), say it is unverified; never fill the
   gap from memory.
