# Groundedness judge (Guardian lane)

A third model lane runs a small judge that answers one question per claim: **do these evidence excerpts support
this sentence?** In the reference deployment it is Clef-Flash Q4_K_M on llama.cpp, and it runs log-only. This page
explains how it is wired, how it was evaluated live, and why it stays log-only.

## Where the judge is called

Hermes has two call sites, both in `agent/product_groundedness.py`:

- **Strict product gate** (`evaluate_product_claims`). It is used only when quality-first finalization is off. Each
  material claim carries exact-quote evidence rows; the judge must say yes, or the finalizer gets one bounded
  rewrite. If the judge is unreachable, the gate fails closed (`semantic_judge_unavailable`) and the answer is
  withheld.
- **Log-only shadow** (`run_groundedness_shadow`, `HERMES_PRODUCT_GROUNDEDNESS_SHADOW=1`). Quality-first answers
  cite plain Markdown links, so the strict gate never runs for them. Before the shadow existed, the judge received
  no live traffic at all. After a quality-first answer is delivered, a daemon thread splits it into claims, picks
  excerpts from the frozen extracted pages and asks the judge about each claim. Results go to
  `$HERMES_HOME/logs/groundedness-shadow.jsonl` (override with `HERMES_PRODUCT_GROUNDEDNESS_SHADOW_LOG`): one row
  per claim plus one summary row per answer. The delivered answer, its timing and the agent state are never
  changed. Any failure is logged and swallowed.

Both read `product_research.groundedness_judge` from the Hermes config (`enabled`, `base_url`, `model`, `timeout`,
`max_retries`, `max_claims`, optional `api_key`); see
[config/hermes-config.example.yaml](../config/hermes-config.example.yaml).

## The Clef-Flash adapter

The model is Cloudflare's [Clef-Flash](https://huggingface.co/Cloudflare/clef-flash) (a 9B decision model). The reference deployment converted it to GGUF with
llama.cpp's `convert_hf_to_gguf.py` (v0.6.0 tree) and quantized it to Q4_K_M (5.7 GB); Q8_0 needs about 10 GB of
VRAM and gave the same decisions in testing.

Hermes speaks the Granite Guardian chat format:

```
user:      "Use only these frozen evidence excerpts:\n\nEXCERPT 1\nURL: …\nTEXT: …"
assistant: <the claim>
user:      "<guardian>… ### Criteria: <strict support rule> ### Scoring Schema: … yes/no"
```

and expects `<score> yes </score>` or `<score> no </score>` back.

Clef-Flash is served by llama.cpp's `/v1/systemone` endpoint, which answers structured yes/no ("noul") questions
with a probability. [adapters/clef_groundedness_adapter.py](../adapters/clef_groundedness_adapter.py) translates
between the two:

- The criteria text becomes the question's `instructions`. The state is
  `{evidence_excerpts, assistant_claim}`, and the true/false criteria read "Every factual clause of the claim is
  proven by the excerpts" / "At least one factual clause is not proven".
- The verdict is `yes` if and only if P(yes) ≥ `CLEF_THRESHOLD` (0.85). The reply also carries
  `{"clef": {"p_yes", "threshold"}}`, and the shadow log records the probability.
- `/health`, `/props`, `/slots` and `/v1/models` answer like a llama.cpp lane, so the agent's health preflight needs
  no change.
- Every decision can be appended to a JSONL log (`CLEF_DECISION_LOG`).

The adapter's request was byte-identical to the evaluated payload on all 234 cases of the offline set.

**Batch limit.** The judge's llama-server runs with `-c 2048 -b 1024 -ub 1024`. One request (criterion plus
excerpts) must fit in the 1024-token batch, or the server answers "input too large". Dense spec text runs near
3 characters per token, so the shadow gives all excerpts for one claim a shared budget of 1200 characters. On a
judge error it retries once with every excerpt halved (`excerpts_halved`). The strict gate does not have this retry
yet: it sends up to several 800-character excerpts per claim and can overflow on dense pages. If you run the strict
gate, raise `-b/-ub` to 2048 (measure VRAM first) or add the same retry.

## How the shadow picks claims and excerpts

These are the final rules, after two rounds of grading:

- **Claims:**
  - inline-linked sentences;
  - for answers that cite only in a trailing "Citations:"/"Sources:" list, substantive sentences of at least 40
    characters, judged against all listed pages (`mode: list`);
  - at most 40 claims per answer.
- **Facts only.** `shadow_claim_kind` classifies each claim as table, absence, meta, advice or fact. Only facts are
  sent to the judge; the rest are logged as `skipped_non_fact`. Labelled lines such as "Recommended if under $500:"
  or "Alternative:" count as advice.
- **Coverage-based excerpts** (`shadow_select_evidence`):
  - greedy set cover over sentence-snapped segments of every cited page;
  - idf-weighted whole-token matching with light stemming;
  - number+unit terms, so "4 hrs" matches "4 hours", while a bare one- or two-digit number does not match on its
    own;
  - the 1200-character budget per claim, falling back to the start of the page when nothing matches.

## Evaluation

### Offline (before deployment)

234 self-built groundedness cases (claims against bank excerpts) were sent through the full request path:

| | Clef-Flash Q4_K_M | Granite Guardian 4.1 8B |
|---|---|---|
| Accuracy | 92.3% | 85.5% |
| Unsupported claims caught | 101/116 | 84/116 |
| False rejects | 3/118 | 2/118 |
| Latency p50 | 0.54 s | 0.32 s |

The threshold was chosen by 2-fold cross-validation, split by excerpt (40 splits). Cross-validated accuracy was
92.5%, and the chosen range was 0.85-0.89. The caveat: the set was self-built, so live behaviour had to be checked
separately.

### Blind live grading method

1. Collect the shadow log's judged claims from answers that were actually delivered over Telegram.
2. Have graders who cannot see the judge's verdicts judge each claim twice under the judge's own strict rule:
   - against the excerpts the judge was shown (is the judge right about its own question?);
   - against the full cited page text as stored by the agent (is the claim actually supported?).
3. Compare. Where the judge says no but the full page supports the claim, the cause is either the excerpt picker or
   a judge error. Where both say no, the cause is either a real catch or an opinion sentence.

### Round 1: 79 decisions from 4 delivered answers (mesh Wi-Fi, robot vacuum, stick vacuum, air purifier)

- Agreement with the graders on the judge's own question: **66/79 (84%)**.
  - 7 false accepts (out of 27 accepts), mostly a fact with a small added inference.
  - 6 false rejects (out of 52 rejects).
- Threshold sweep: 66 agreements at both 0.80 and 0.85, 64 at 0.9, 50 at 0.5. **0.85 is right.**
- The 52 rejections broke down as:

  | Category | Count | Meaning |
  |---|---|---|
  | True catch | 26 | Not supported even by the full page: 10 advice, 12 fact plus judgement, 2 meta, 1 absence, 1 plain fact |
  | Excerpt miss | 20 | The full page supports the claim, but the picker showed the wrong window |
  | Judge false reject | 6 | The excerpts did support the claim |

- All 26 plain factual claims were supported or mostly supported by the full pages (23 yes, 3 partial).

The facts-only filter and coverage excerpts were built from this round. Rerun on the 38 kept graded claims:

| | Old picker | New picker |
|---|---|---|
| Supported facts accepted (p ≥ 0.85) | 17/24 | 19/24 |
| Unsupported claims rejected | 10/14 | 10/14 |
| Agreement with full-page truth at 0.85 | 27/38 | 29/38 (31 at 0.80) |

The remaining misses were mostly the judge's strictness on long, attributed, multi-part sentences (scores 0.70-0.85
with the evidence present), which better excerpts cannot fix.

### Round 2: 72 fact-classified claims from 9 delivered answers (20-prompt run)

- Against the full pages: 52 supported, 18 partly supported, 2 unsupported.
- **Factual errors (a claim contradicting its cited page): 2 of 72.** The judge rejected both (p 0.28 and 0.52).
  Both were absence/meta-style sentences the filter let through.
- Agreement on the judge's own question: **57/72 (79%)**. 0.85 was again the best threshold.
- The judge's 47 "no" verdicts:
  - 30 on claims the full pages support: 19 excerpt misses (11 in citation-list mode) and 11 judge errors;
  - 15 on facts with an added judgement;
  - 2 on real errors.

## Why it stays log-only

Over both rounds, 151 claims were judged:

- **The answers' facts were almost always right.** Round 2 found 2 contradictions in 72 graded facts; round 1 found
  0 among 26 plain facts. What is unsupported is mostly the model's own judgement and advice layered on top, which
  is expected in a recommendation.
- **The judge catches real errors but buries them.** It rejected both real errors, but about two thirds of its
  rejections (30 of 47, 64%, in round 2) were on supported claims. A visible "unverified" marker would be wrong most
  of the time.
- **Delivery was the larger lever.** At the time of grading, 11 of 20 requests were withheld.

If a user-facing check is ever wanted, the realistic route is:

1. split compound claims into atomic facts;
2. give the judge a larger context and batch (the reference judge card has about 14 GB free, so this fits);
3. re-grade a fresh sample;
4. add a visible marker only if true factual catches show up at a useful rate.

## Operating notes

- Change the threshold with `CLEF_THRESHOLD` on the adapter, then restart it. No agent change is needed.
- Turn the shadow off by removing `HERMES_PRODUCT_GROUNDEDNESS_SHADOW` (drop-in 97) and restarting the gateway.
- Replaying old answers through the shadow writes real rows to the judge's decision log. Mark or filter those rows
  (a replay in the reference deployment added 84 test rows).
- Fall back to the previous judge with the same flag-file swap pattern as the main lane (see
  [reference-deployment-b70.md](reference-deployment-b70.md)). The reference rollback to Granite took about 11 s.
