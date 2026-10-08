# Results

All numbers are from the reference deployment ([docs/reference-deployment-b70.md](docs/reference-deployment-b70.md))
on 2026-10-07 and 2026-10-08. They come from one host and one prompt set, so treat them as indicative.

## Product research delivery (Telegram, fresh sessions)

The prompts are in [testing/suites/product-20.json](testing/suites/product-20.json): 10 named three-way comparisons
and 10 open-ended requests. "Fresh single pass" means each prompt was sent once, in a new session, on one code
version.

| Run | Code (patches) | Delivered | Named | Open-ended |
|---|---|---|---|---|
| 20 fresh | 0001-0014 | 9/20 | 4/10 | 5/10 |
| Rerun of the 11 withheld | 0001-0015 | 7/11 | | |
| Rerun of the last 4 | 0001-0016 | 4/4 | | |
| 20 fresh | 0001-0016 | **16/20** | 10/10 | 6/10 |
| 10 open-ended fresh | 0001-0017 | | | 8/10 |
| 20 fresh | 0001-0018, receipt script v4 | **17/20** | 9/10 | 8/10 |

The reruns are not fresh passes; they show which fixes unblocked which prompts.

Withhold causes:

- **9/20 run (11 withheld):**
  - 6 short of evidence after the repair fetch;
  - 2 with a named alternative missing from the draft;
  - 1 missing citations;
  - 2 `RESEARCH_RECEIPT_ERROR` from continuation nudges resetting the turn window.
- **16/20 run (4 withheld):** all `source_packet_incomplete` after the repair fetch: no official page for the chosen
  model, too few independent reviews, or no in-budget price.
- **17/20 run (3 withheld):** one target without a fetched page after repair (a named comparison), and two picks
  without an official page (open-ended). There were 0 receipt-script errors.

On the final code, three drafts had one outside citation unlinked instead of being withheld, and one turn was carried
by an earlier researched candidate.

Delivered product answers took 96-231 s end to end. In earlier small runs before the repair fetch and the parser fix,
both named comparisons tried were withheld. Changing the prompt wording (asking for retailer pages, or spelling out
an extraction plan) did not change that.

## Groundedness judge (Clef-Flash Q4_K_M)

### Offline, 234 cases through the production request path

| | Clef-Flash Q4_K_M | Granite Guardian 4.1 8B |
|---|---|---|
| Accuracy | 92.3% | 85.5% |
| Unsupported caught | 101/116 | 84/116 |
| False rejects | 3/118 | 2/118 |
| Latency p50 | 0.54 s | 0.32 s |

The threshold was chosen by 2-fold cross-validation split by excerpt (40 splits): CV accuracy 92.5%, chosen range
0.85-0.89, deployed value 0.85.

### Live, blind-graded

| | Round 1 | Round 2 |
|---|---|---|
| Claims | 79 decisions from 4 delivered answers | 72 fact-classified claims from 9 delivered answers |
| Agreement with graders (judge's own question) | 66/79 (84%) | 57/72 (79%) |
| Best threshold | 0.85 (tied with 0.80) | 0.85 |
| Claims contradicting their cited page | 0 of 26 plain facts | 2 of 72 (both rejected by the judge) |
| Rejections on claims the full page supports | 26 of 52 (20 excerpt misses, 6 judge errors) | 30 of 47 (19 excerpt misses, 11 judge errors) |

Round 1 threshold sweep: 66 agreements at 0.80 and at 0.85, 64 at 0.9, 50 at 0.5.

Excerpt-picker change between the rounds, rerun on the 38 kept graded claims:

- supported facts accepted went from 17/24 to 19/24;
- unsupported claims rejected stayed at 10/14;
- agreement with full-page truth at 0.85 went from 27/38 to 29/38.

**Decision:** keep the judge log-only. The answers' facts were almost always right. About two thirds of the judge's
rejections were on supported claims (64% in round 2), so a visible flag would mostly be noise. Details:
[docs/groundedness-judge.md](docs/groundedness-judge.md).

## Main lane (Qwen3.8-27B EXL3 4.00 bpw, vLLM + MTP, one Arc B70)

| Check | Result |
|---|---|
| Cold start to healthy through the agent's preflight | 226 s |
| 64K prefill TTFT (with concurrent short requests) | 49.5 s (short requests 7 s) |
| Long-prefill TTFT at 64K / 128K / 226K | 48 / 146 / 400 s |
| Cancellation, slot freed | 0.27 s (3/3) |
| 60K prefill abandoned after 4 s, slot freed | 26.5 s (llama.cpp lane: about 40 s) |
| MTP acceptance during the Telegram runs | 56-66% |
| VRAM headroom | about 1.1 GiB (KV pool about 310K tokens) |

One request collapsed into a NaN loop (repeated "!" tokens, 0% MTP acceptance) and could not be reproduced. The
adapter's degenerate-stream guard now ends such streams after 64 identical deltas. No engine faults were recorded
across about 7 hours of testing for the production baseline.

## Worker lane (llama.cpp v0.6.0 + SYCL PR vs v0.4.1, worker settings)

- decode: +2% at concurrency 1, equal at concurrency 2;
- prefill: +10-21%;
- abandon drain: 9.6 s instead of 12.6 s;
- 14/16 identical greedy outputs.
