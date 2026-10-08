# Patch series for hermes-agent v0.21.3

- **Upstream:** https://github.com/NousResearch/hermes-agent (MIT, Nous Research).
- **Base commit:** `345cd2b057` "chore(release): v0.21.3".
- **Patches:** 28, in order, produced with `git format-patch`.

## Apply

```sh
git clone https://github.com/NousResearch/hermes-agent hermes
cd hermes
git checkout -b research-agent 345cd2b057
git am /path/to/research-agent-recipe/patches/hermes-v0.21.3/*.patch
```

`git am` prints whitespace warnings ("new blank line at EOF") for a few new files. They are harmless.

## Test

From the patched tree, using the Hermes virtualenv (or any Python 3.11 with Hermes' dependencies):

```sh
python -m pytest -q -p no:cacheprovider \
  tests/agent/test_product_repair_fetch.py \
  tests/agent/test_product_groundedness.py \
  tests/agent/test_quality_first_route.py
```

Results on the published series:

- that subset: 61 passed;
- a wider research selection (13 test files, including product route, generic route, research contract, travel route
  and verified delivery): 511 passed, 2 skipped, 1 failed (0001-0018);
- after 0019-0025, every test file matching product_route, generic_research, research_contract, travel_route,
  verified_tool, research_harness, research_loop, quality_first, groundedness or repair_fetch (15 files) plus the routing corpus: 598 passed,
  1 skipped, with a pinned controller copy present.

The one failure, `test_research_harness_travel_runner.py::test_installed_controller_matches_accepted_pin`, needs a
pinned copy of an external travel-planning skill (`B70_MIGRATION_TRAVEL_PIN`), which is not part of this recipe.

## Patches

| # | Subject | What it does |
|---|---|---|
| 0001 | Port bounded B70 runtime controls to Hermes 0.21.3 | Retrieval and tool-call guardrails, travel terminal admission policy, foreground terminal timeout cap, gateway silence tokens, and fail-closed direct replay of a verified receipt-bound answer after a delivery failure |
| 0002 | Port B70 research custody, routing, bounded delegation and provider contracts | The research controller: gateway routing (product / generic / travel), bounded research loop, receipt-bound finalizer, product route and viability gate, quality-first route, groundedness gate, reference store and synthesis validation, typed research delegation, file custody, SearXNG circuit breaker, Tavily direct extract, provider telemetry, and a test that an older state database migrates cleanly |
| 0003 | Save receipt-verified generic research to the wiki on explicit request | Controller-owned, create-only wiki note (O_EXCL, hash-checked, `review_status: unreviewed`) when the request explicitly asks to save |
| 0004 | Open-ended research with explicit wiki-save wording stays on the direct-save route | Save requests stay on the guarded generic route; open-ended requests get 5 searches / 3 extract calls and a 4-page receipt target |
| 0005 | Short research-note titles, strip note preamble, keep docs research on default budget | Note titles capped at 10 words without lead-ins; model preamble dropped from saved notes; primary/official-source requests are never open-ended |
| 0006 | Narrow open-ended research wording | Open-ended detection keys on buying/choice wording; "best practices", bare "compare"/"around"/"which" no longer qualify |
| 0007 | Retry reasoning-only clean stops before promoting reasoning | Runs thinking-only prefill retries before promoting reasoning text to the reply, so raw chain-of-thought is not delivered |
| 0008 | Log-only Guardian groundedness check for quality-first product answers | `HERMES_PRODUCT_GROUNDEDNESS_SHADOW=1`: judges linked sentences after delivery on a daemon thread; JSONL log only |
| 0009 | Product comparisons by 'comparing' + one repair fetch before withholding | Parses "…, comparing A, B and C"; `HERMES_PRODUCT_RESEARCH_REPAIR_FETCH=1` grants one more search + extract that names the missing evidence |
| 0010 | Shadow groundedness judges citation-list answers; cleaner claims | Judges answers that cite only in a trailing Sources list; better sentence splitting; 1400-char shared excerpt budget |
| 0011 | Bind a generic product receipt even after the one-shot recovery | Receipt binding to the session stays available after the extraction recovery is spent; timeouts up to 600 s normalised to 30 |
| 0012 | Shadow groundedness retries an oversized judge request once | Excerpt budget 1200 chars; one retry with halved excerpts on a judge error |
| 0013 | Shadow groundedness judges facts only, with coverage-based excerpts | Claim classifier (table/absence/meta/advice/fact); greedy set-cover excerpt picker with idf weighting and number+unit terms |
| 0014 | Shadow claim filter catches labelled recommendation lines | "Recommended if …:", "Alternative:" and "must be" lines are classified as advice |
| 0015 | Product delivery fixes from the 20-prompt run | Sentence-bounded target parsing (digit-ended model names); continuation nudges no longer reset the turn window; zero-extract receipt becomes the first extraction; coverage ignores "(4th gen)" qualifiers; repair nudge names missing alternatives and citable URLs |
| 0016 | Quality-first drafts keep evidence citations instead of withholding | Outside-evidence links are unlinked and logged instead of withholding; bare evidence URLs count as citations |
| 0017 | Open-ended product receipts judge every researched candidate | Editorial pages, review publishers and retailers are never official; manufacturer URL naming the model counts as official as a fallback; any fully evidenced searched candidate can carry the receipt |
| 0018 | Spend an unused repair extraction instead of a premature receipt | In a granted repair round, a receipt call before the granted extraction is rewritten into that extraction |
| 0019 | Travel previews sent once; travel search ceiling 24 | The approval preview is delivered only by the gateway's final send (it was also streamed, so chat showed it twice); the travel search ceiling rises from 12 to 24 so the route can reach its own 16-candidate target |
| 0020 | Travel controller pin; one delivery-error message; fresh run directory on reruns | Moves the controller pin to the reference deployment's controller v3 (set it to your own controller's sha256); the delivery-error message is no longer streamed as well; a reused, non-empty run directory moves to a new `<name>-rerun-N` sibling instead of spending the turn's one controller attempt |
| 0021 | Run the versioned travel runner from the deployed checkout | Admission binds the runner's working directory to the checkout Hermes runs from; the model sometimes chose an older checkout, which ran an older runner |
| 0022 | Travel controller v4 pin (schema-enforced review chunks) | Moves the pin to the reference controller v4, which sends review chunks with a strict `json_schema` response format (set it to your own controller's sha256) |
| 0023 | Send a thin travel manifest back to discovery once | Before the turn's single controller attempt, a manifest below 8 candidates per category (16 total) is returned once while at least 4 searches remain; not a failure and not the controller boundary |
| 0024 | Product route accepts polite lead-ins before "research" | "Can/could/would you (please)", "please", "I'd like you to", "help me" (and a leading hey/hi) before "research" select the product route |
| 0025 | Route natural product, fact-check and travel phrasings | Natural shopping asks with a product signal (price, buying verb, model token or common product category) select the product route; look-into / find-out / verify / "is it true" asks select generic research; "heading to <place>" and "N days in" count as travel. `tests/agent/test_route_phrasing_corpus.py` holds 97 phrasings with expected routes. Needs receipt script v5 |
| 0026 | Queue messages during research turns; deliver batched travel previews | While a travel, product or generic research turn runs, a new message is queued instead of steered or interrupting (/stop and /new still cancel); a travel preview is delivered when other tool calls are batched with the one controller call |
| 0027 | Product answers in plain language, 400-800 words | The quality-first synthesis prompt forbids internal words (packet, rows, controller, "the supplied evidence"), names sources by publisher, and targets 400-800 words (max 1,200) |
| 0028 | A different research request starts a new session | In a travel- or product-locked session, a research request for another route (or a new product request) moves to a fresh session, idle or queued, with a /resume hint; continuations and approvals stay |

Each patch's commit message has the evidence behind the change (failing turns, replay counts, test counts).

## Sanitization notes

These patches are the series that ran in the reference deployment, with only these changes for publication:

- Author lines are set to a project identity. Dates and subjects are unchanged.
- Hard-coded absolute paths under the operator's home directory were replaced with `~/.hermes/...` in the
  receipt-command and travel-controller allowlists in `agent/verified_tool_delivery.py` and in tests. The original
  allowlists listed both the `~/.hermes/...` form and an absolute form, so they now contain a harmless duplicate
  entry. If the gateway user's Hermes home is not `~/.hermes`, add your absolute path to
  `GENERIC_RESEARCH_RECEIPT_COMMANDS` and `TRAVEL_RESEARCH_CONTROLLER_SCRIPTS`.
- The historical default wiki vault path is now `/opt/research-agent/wiki` (`LOCAL_DEFAULT_OBSIDIAN_VAULT` and
  `DEFAULT_OBSIDIAN_VAULT`). Set `OBSIDIAN_VAULT_PATH` explicitly in any real deployment.
- One session ID used as test data was replaced with a synthetic ID of the same format.

The code otherwise matches what was measured. Test fixtures contain public product URLs (manufacturer, review and
retailer pages) and the product prompts from the test suites.
