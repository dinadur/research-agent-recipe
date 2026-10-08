# Architecture

The design goal is narrow: **an answer is delivered only if it can be traced to pages fetched in the same turn, and
otherwise it is withheld.** The model does the searching, reading and writing. Deterministic code owns everything
that decides whether the result is trustworthy enough to send: budgets, custody of fetched pages, the receipt, the
citation allowlist and the final checks.

All file and function names below refer to the patched hermes-agent tree (see
[patches/hermes-v0.21.3](../patches/hermes-v0.21.3/README.md)).

## Routes

The gateway arms at most one research route per incoming user message (`gateway/research_routing.py`). Product
intent is evaluated first, so an incidental word such as "travel" in a product request cannot select the travel
route.

| Route | Selected when | What it may do | Output |
|---|---|---|---|
| Product research (`agent/product_route.py`, `gateway/product_routing.py`) | Buying/comparison intent plus a product or constraint signal ("best … under $300", "compare A, B and C", "recommend") | `web_search`, `web_extract`; the receipt command is issued by the controller | One answer, or a withheld message. Publication to the wiki needs a separate, explicit approval turn |
| Generic research (`agent/generic_research_route.py`) | Research intent ("research", "investigate", "look up", "find current/official …") without product intent | `skill_view` (only `reliable-research`), `web_search`, `web_extract`, the exact receipt command | One answer of at most 900 words; if the request explicitly asks to save to the wiki, the controller saves it as a new, unreviewed note |
| Travel publication (`agent/travel_route.py`, `gateway/travel_routing.py`) | Explicit trip, itinerary, destination or travel-wiki intent | A pinned travel controller script run from the terminal; bounded retrieval waves | Wiki pages written by the controller, not by the model |

Everything else is an ordinary Hermes conversation.

The travel route depends on a pinned external travel-planning skill and controller script, which this recipe does
not ship. The patches keep it so that the series applies unchanged; treat it as an example of the same pattern.

## The research loop

`agent/research_loop.py` adds research phases on top of the upstream agent loop. It owns only research
transitions; provider retries, cancellation, token accounting and persistence stay upstream. One product turn goes
through these phases:

1. **Discovery.** The controller injects a route note: the budget, the allowed tools and the evidence the receipt
   will require. The model searches and extracts. `agent/research_tool_policy.py` enforces the allowlist at tool
   dispatch, outside any observer middleware, so a plugin cannot rewrite or approve a call.
2. **Receipt request.** When the model asks for the receipt, or the controller decides the packet is ready, the
   controller checks viability (below). It then binds `capture_receipt.py` to the current session and runs it.
3. **Finalization.** For product research with `HERMES_PRODUCT_RESEARCH_QUALITY_FIRST=1`, a separate tools-off
   synthesis call runs over the frozen evidence (see Quality-first finalization).
4. **Delivery or withholding.** `agent/research_finalizer.py` validates before anything is persisted or sent.
   Observer hooks receive copies and cannot approve output.

### Bounded budgets

| Route | Searches | Extract calls | Notes |
|---|---|---|---|
| Generic, targeted | 3 | 2 | Receipt captured automatically once 2 HTTPS pages were extracted |
| Generic, open-ended ("best", "options", "recommend", budgets) | 5 | 3 | The receipt waits once for at least 4 extracted pages |
| Product | 4 | 2 | The route note tells the model three searches; the controller may add one provenance-recovery search |
| Product repair round (`HERMES_PRODUCT_RESEARCH_REPAIR_FETCH=1`) | +1 | +1 | Granted once, only when the receipt is requested with an unready packet after the budget is spent |

The receipt script has outer bounds of its own: 5 searches for generic research, 6 for product research (the
product limiter has admitted 6 in a repaired turn), 3 extract calls, and at least 2 (generic) or 3 (product)
successfully extracted sources. The controller enforces the per-request budget, and the script rejects anything
beyond the largest one.

Small budgets are deliberate. They bound latency (delivered product answers took 96-231 s end to end in the logged
runs),
and they keep the packet small enough to freeze and audit. The cost is that some requests cannot be evidenced within
budget, and those are withheld.

## Controller-owned receipts

`skills/reliable-research/capture_receipt.py` is the custody step. It is run by the controller with the session
bound in the environment (`HERMES_SESSION_ID=<id> [HERMES_PRODUCT_RESEARCH_ROUTE=1] python3 …/capture_receipt.py`),
never with arguments the model chose. It:

- reads the current turn from Hermes' `state.db` read-only. The turn starts at the latest operator message and skips
  Hermes' own length/network continuation nudges;
- accepts only `skill_view`, `web_search` and `web_extract` calls, plus a small set of typed controller rejections
  that never executed;
- requires every extracted URL to have been returned by a search in the same turn;
- counts successful HTTPS extractions and rejects a turn outside the bounds;
- writes `research-receipts/<session>--turn-<id>.json` once (`O_EXCL`; an existing receipt is never replaced) and
  prints `RESEARCH_RECEIPT_OK receipt_sha256=… source_count=…` plus one `SOURCE <url>` line per allowed citation.

**Why answers are bound to it.** The receipt is the only list of URLs the answer may cite, and the frozen text of
those pages is the only evidence the finalizer sees. Without it, a model can cite a URL it never fetched, or
"remember" a spec from training data, and nothing downstream can tell. With it, every citation in a delivered answer
points at bytes the controller holds, and a later audit can re-read exactly what the model was shown.

The command matcher in `agent/verified_tool_delivery.py` accepts only exact command strings (the `~/.hermes/…`
skill path, bound to a safe session ID). A command with shell metacharacters or a foreign session is rejected. If
your Hermes home is not `~/.hermes` for the gateway user, add your absolute path to
`GENERIC_RESEARCH_RECEIPT_COMMANDS`.

## Evidence rules: official pages and independent sources

Product viability (`_product_receipt_viability`) requires:

- at least 3 successful sources from 3 different source families (roughly: sites);
- **independent evidence:** at least 2 families that are not the brand's own site, not a retailer (Amazon, Best Buy,
  Walmart, Home Depot, …) and, for quality-first, not a community thread (Reddit);
- **named comparisons:** at least one extracted page that matches each named alternative, by brand plus a model
  identifier;
- **single recommendations:** the recommended model's **official page**; with a budget in the request, a page that
  shows a current in-budget price for that model.

Official-page rules (`_product_url_never_official` and friends):

- Editorial pages are never official, whatever host the query named. That covers review publishers, "best of"
  lists, comparison pages, guides and paths containing `review`, `best`, `vs` or `top-10`. Retailers are never
  official either.
- When no query-named official page matches, a manufacturer URL whose path names the model (for example
  `…/archer-be9700/`, `…/BL770.html`) counts as official.
- Model identity is strict. A page for a sibling model does not count, and searching a model number that does not
  exist does not fuzzy-match a real one. A fuzzy match would let an answer recommend a non-existent product.

The model usually researches alternatives after its first pick. If the candidate behind the last extraction fails,
any other searched candidate with full evidence can carry the receipt. This is logged as "receipt viable through an
earlier candidate".

## The one-time repair fetch

The first product runs withheld most named comparisons on the first unready receipt request. Three products, each
needing an official page, a price and two independent reviews, rarely fit in 4 searches and 2 extract calls.
Changing the prompt wording did not help.

With `HERMES_PRODUCT_RESEARCH_REPAIR_FETCH=1`, an unready receipt request after the budget is spent gets exactly one
more `web_search` and one more `web_extract`. The nudge names what is missing (for example "independent hands-on
reviews from at least two different publishers", or "an extracted page for every alternative the operator named")
and lists the citable URLs. During the repair round:

- a receipt or terminal call made before the granted extraction was used is rewritten into that extraction, using
  the newest searched URLs not yet requested, one per new source family;
- a receipt request after searches but no extraction becomes the first extraction.

A second unready request withholds.

## Quality-first finalization

`agent/quality_first_route.py` has no agent-loop or tool dependencies. Once retrieval is finished:

1. `compile_task_contract` copies only explicit structure from the request: the named alternatives ("compare A, B
   and C", "comparing A, B and C"), the criteria, and sentence-level hard constraints (prices, "must", "without").
   Nothing is inferred.
2. `freeze_evidence` freezes the admitted page text in receipt order, within explicit bounds (300,000 characters in
   total, 90,000 per source).
3. One synthesis request is built with no discovery history and no tool schemas. Its system prompt says: use only the
   evidence; compare every alternative on every criterion; mark each hard constraint pass, fail or unverified;
   missing evidence is not a negative fact; cite every factual paragraph with the exact allowed URLs. The request
   allows 16,384 output tokens and an 8,192-token reasoning budget, and caps the answer at 2,000 words.
4. `response_diagnostics` checks the draft deterministically. It flags an empty answer, citations outside the frozen
   evidence, no citations, a missing alternative, criterion or hard constraint, and raw tool-call text.
5. Links to URLs outside the evidence are unlinked (the label is kept) and logged rather than withheld. Bare
   evidence URLs count as citations.
6. A failing draft gets one repair nudge that names its defects, then is withheld.

The strict alternative (`HERMES_PRODUCT_RESEARCH_QUALITY_FIRST` unset) makes the model write a short draft with
`[[E###]]` evidence-row tokens on every material line. That draft then passes an exact-quote check and a
groundedness judge that fails closed (see [groundedness-judge.md](groundedness-judge.md)). The reference
deployment ran quality-first, so all measured results in this repository are for that path.

## Withholding

Every route has a fixed withheld message. For example: "Product research withheld: Hermes did not complete the
bounded current-turn source receipt and citation checks, so no recommendation was delivered." The reason code
(`source_packet_incomplete`, `quality_first_citations_missing`, `RESEARCH_RECEIPT_ERROR …`) goes to the log, not to
the user.

Withholding is the expected outcome when evidence is missing. In the reference runs, every withhold left on the
final code was a genuine gap: no official page for the chosen model, too few independent reviews, or no in-budget
price within the budget.

## Wiki saves (optional)

A generic research request that explicitly asks to save to the wiki ("… and save it to the wiki") arms the guarded
generic route. After the answer passes the receipt check, the controller (never the model) creates one new note
under `research/` in `OBSIDIAN_VAULT_PATH`. The note is created with `O_EXCL` and never overwrites; a same-day
collision becomes `-2`, `-3` and so on. It records the request, the date, the receipt sources and
`review_status: unreviewed`. The controller rereads and hash-checks the note, then appends "Saved to wiki: <path>"
to the reply. A negated request ("do not save") does not save. A saved note shows custody, not factual
verification.

## Other runtime controls in the series

- **Retrieval and tool guardrails** (patch 0001): bounded retrieval policy on the turn controller's decision path,
  and a foreground timeout cap for terminal commands.
- **Gateway silence tokens** (patch 0001).
- **Direct replay of a verified answer** (patch 0001, `HERMES_VERIFIED_RECEIPT_DIRECT_REPLAY_CANARY=1`): if a
  receipt-bound Telegram turn committed a verified answer but delivery failed, the exact stored bytes are resent once.
  There is no model call.
- **Reasoning-only clean stops** (patch 0007): when a reasoning budget ends thinking with an empty answer, the
  thinking-only prefill retries run first. The reasoning is promoted to the reply only after those retries are
  exhausted, so raw chain-of-thought is not delivered as an answer.
- **Search provider resilience** (patch 0002): a SearXNG circuit breaker, Tavily direct extraction and provider
  telemetry.
