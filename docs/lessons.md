# Lessons: failure modes found and fixed

Each item lists the symptom, the cause and the fix. Patch numbers refer to
[patches/hermes-v0.21.3](../patches/hermes-v0.21.3/README.md). Most of these were found by sending real requests
through the chat front end, not by unit tests.

## Request parsing

1. **"comparing A, B and C" was not a comparison** (0009). The target extractor recognised only "compare X, Y and Z
   for/on" and "research X for", and used the first matching clause. "Research the best X under $N for Y, comparing
   A, B and C" therefore yielded no alternatives, so named three-way comparisons took the single-candidate path and
   failed its price rule. Fix: add the "comparing" clause, and use the first clause that names 2-8 alternatives.
2. **Model names ending in a digit swallowed the next sentence** (0015). A full stop after a digit was never treated
   as a sentence end. "…, comparing … Honeywell Home T9. Use each manufacturer's …" made the last target run on,
   producing bogus targets such as "warranty". Fix: a sentence ends at any `[.!?]` not followed by a digit, and
   target clauses stay inside one sentence. "Aventon Level.2" still parses. All 10 named-comparison prompts then
   yielded their three products.
3. **Parenthetical qualifiers broke coverage checks** (0015). "Google Nest Learning Thermostat (4th gen)" was
   reported as missing from drafts that named it. Alternative coverage now ignores parenthetical qualifiers.
4. **Open-ended detection over-matched** (0005, 0006). "Current best practices", "travel around Lisbon" and "how
   heat pumps compare to furnaces" were treated as open-ended buying questions and got the larger budget and
   delayed receipt. Detection now keys on buying and choice wording ("best" but not "best practices", options,
   alternatives, recommend, "top N", budgets). Requests for primary or official sources are never open-ended.

## Turn window and receipt binding

5. **Continuation nudges reset the turn window** (0015, receipt script v4). Hermes stores its own "[System: Your
   previous response was truncated…]" length and network nudges as user rows. The controller treated such a row as a
   new turn, so the search and extract counts read 0 and the receipt failed with `RESEARCH_RECEIPT_ERROR`. The
   receipt script made the same mistake: it started the turn at the nudge, hid earlier searches, and reported "URL
   not returned by search". Fix: both skip continuation nudges, and the turn starts at the latest operator message.
   The first hypothesis for one of these failures (a URL normalisation mismatch) was wrong; replaying the stored
   session found the real cause.
6. **Receipt binding after a spent recovery** (0011). Recovery gated everything on the turn's one-shot extraction
   recovery being unspent, including the pure normalisation that binds a generic `capture_receipt.py` command to
   the current session. Once a terminal detour had used that recovery, the model's later unbound receipt call was
   rejected. Fix: binding stays available (the extraction rewrite stays one-shot). Any timeout up to 600 s is
   accepted and normalised to 30 s, since a timeout of 60 had also been rejected.
7. **Receipt requested before any extraction** (0015). A receipt or terminal call after searches but with no
   extraction is now rewritten into the first extraction: round-robin over the search batches, one URL per source
   family.
8. **Repair round spent on a search alone** (0018). In a granted repair round the model sometimes searched and then
   asked for the receipt without extracting, so the round added nothing. While the granted extraction is unused, a
   receipt call is rewritten into an extraction of the newest searched URLs not yet requested.
9. **The receipt script rejected harmless refused calls** (receipt script). A controller rejection coded
   `product_research_capability_forbidden` (for example, a refused `read_file` on a cached page) failed the whole
   receipt as "disallowed tool". Such typed, never-executed rejections are now recoverable for any tool. The
   script's product search cap went from 4 to 6 to admit the repair search; Hermes' own limiter still enforces the
   per-request budget.

## Evidence rules

10. **A fixed budget with no second chance** (0009). Three named products, each needing an official page, a price
    and two independent reviews, rarely fit in 4 searches and 2 extract calls. The receipt check rejected on the
    first attempt. Rewording the prompt (asking for retailer pages, or spelling out an extraction plan) did not
    help, because the model did not follow the plan. Fix: one controller-granted repair search plus extraction that
    names the missing evidence. A second miss withholds.
11. **Official-page false positives** (0017). Whatever host the query text named counted as official, so
    "best blender under $200" editorial pages and a review publisher counted as official. The publisher then also
    dropped out of the independent pool. In an intermediate draft of the fix, a kitchenware retailer counted as
    official. Fix: editorial paths, review publishers and retailers are never official.
12. **Official-page false negatives** (0017). A manufacturer page for the right model was not accepted when the
    query had not named that host. Fix: as a fallback, a manufacturer URL whose path names the model counts as
    official.
13. **Only the last-searched candidate was judged** (0017). The model researches alternatives after its leading
    pick, so viability was judged on whatever it searched last. Fix: if that candidate falls short, any other
    searched candidate with full evidence carries the receipt (logged). Replay over 74 product turns: 4 not-ready →
    ready, 0 regressions.
14. **Not fixed, by design: model-number drift.** One turn searched a rice-cooker model number that does not exist;
    the official page listed two neighbouring sizes. A fuzzy match would let an answer recommend a product that
    does not exist, so this stays a withhold.

## Finalization

15. **Outside citations withheld good answers** (0016). A draft that linked one URL outside the frozen evidence was
    withheld (`citation_outside_frozen_evidence`). Now such links are unlinked (the label is kept) and logged, so
    every remaining citation points at fetched evidence. Bare evidence URLs count as citations, which fixed
    `citations_missing` for models that cite as "(https://…)".
16. **Raw chain-of-thought delivered as the answer** (0007). A reasoning budget can end thinking mid-thought and stop
    with an empty answer. A workaround then promoted the reasoning text to the reply (one case was 5092 characters).
    Fix: run the thinking-only prefill retries first, and promote reasoning only after they are exhausted.

## Groundedness judge

17. **The judge saw no live traffic** (0008). Quality-first answers cite Markdown links, so the exact-quote
    groundedness gate never ran for them. Fix: a log-only shadow check after delivery.
18. **Citation-list answers produced zero claims** (0010). Answers that cite only in a trailing "Sources:" list had
    no inline-linked sentences. Their substantive sentences are now judged against all listed pages.
19. **Judge requests overflowed the 1024-token batch** (0010, 0012). Three 800-character windows of dense spec text
    reached about 1080 tokens, and llama-server answered "input too large". Fix: a shared per-claim excerpt budget
    (1400, then 1200 characters) and one retry with halved excerpts. The strict gate still lacks the retry; see
    [groundedness-judge.md](groundedness-judge.md).
20. **The judge mostly rejected opinions and bad excerpts** (0013, 0014). Most rejections were advice, absence and
    meta sentences, or windows that missed the supporting passage. Fixes:
    - facts-only judging;
    - coverage-based excerpt selection;
    - a label filter for lines such as "Recommended if under $500:", which leaked through as facts until the
      pattern allowed text before the colon.

## Engine and serving

21. **NaN collapse on a single request.** One main-lane request streamed only `"reasoning":"!"` chunks (token 0) at
    22 tok/s, with 0% MTP acceptance. Hermes waited ("no stream output for 600s"), while a fresh direct request to
    the engine answered correctly. A gateway restart cleared it, because the client disconnect aborted the upstream
    request. It could not be reproduced. Mitigation: the adapter's degenerate-stream guard aborts after 64 identical
    deltas of at most 2 characters and ends the stream with an error event, so the agent fails fast or retries.
22. **`thinking_token_budget` was silently ignored.** The vLLM build used enforces it only when the server starts
    with `--reasoning-config`, which the qualified recipe lacked. A NaN loop would therefore run until `max_tokens`.
    If you rely on a reasoning budget, verify it is enforced.
23. **Cumulative metrics hide short failures.** The health check reported cumulative MTP acceptance only, which
    cannot show a zero-acceptance window. Sample per-window engine metrics during test runs.
24. **Client disconnects must abort the engine.** A proxy that does not notice a vanished client keeps the engine
    generating for nobody. The adapter watches the client socket and closes the upstream connection at once. With
    that, cancellation freed the slot in 0.27 s (3/3), and a 60K-token prefill abandoned after 4 s freed its slot
    in 26.5 s (the previous llama.cpp lane took about 40 s).
25. **Dropping the vision tower shrank the KV pool.** `--language-model-only` saved 0.87 GiB of weights, but vLLM
    then profiled 2.85 GiB of peak activation instead of 1.0 GiB. The KV pool fell from 310,472 to 300,220 tokens,
    so the multimodal configuration was kept.

## Operations

26. **`Conflicts=` plus `Wants=` silently swapped engines back.** The worker and judge units `Wants=` the old main
    unit, and the new engine units declared `Conflicts=` with it. Restarting the judge pulled in the old main
    engine, which stopped the new one. The main lane then ran the old engine for 22 minutes without an alert. Fix:
    no `Conflicts=`. A drop-in on the old unit, `ConditionPathExists=!<flag>`, turns any pull into a no-op while the
    flag file exists. The dashboard also lists the new engine units separately, so a fallback shows.
27. **Replays wrote into live state.** `build_receipt()` creates receipt files with `O_EXCL`. A replay against the
    live home created two real, root-owned receipt files, which had to be removed. Replays now use a temporary home
    (see [testing.md](testing.md)).
28. **Every code deploy must move the commit pin.** The gateway's preflight refuses to start unless `HEAD` is the
    pinned commit and the worktree is clean. This is deliberate. Keep a dated backup of the preflight with each pin
    move, so a rollback is two steps: reset the checkout and restore the preflight.
29. **A canary's per-task timeout does not stop the agent.** Check the gateway's active-agent count before starting
    the next task. Restarting the gateway cancels the in-flight request.

## Travel

30. **Approval previews arrived twice** (0019). The travel route pushed the controller's approval preview through the
    stream callback and also left it for the gateway's final send, so chat showed it twice (6/6 runs). Fix: deliver it
    once, through the final send.
31. **The search ceiling was below the route's own target** (0019). With one direct official lookup per venue, 12
    searches could not reach 16 candidates; all 6 runs stopped at 8-15. With 24, 3 of 4 runs reached 16.
32. **The delivery-error message arrived twice** (0020). Same cause as 30, but the second copy carried the failed-turn
    footer, so it did not look like a duplicate to an exact-match check. Compare messages by prefix when you scan
    transcripts for duplicates.
33. **Reruns of a destination reused the old run directory** (0020). The model named the previous attempt's run
    directory, the request was rejected as "run_dir must be new or empty", and the turn's single controller attempt
    was spent. Fix: the runner moves the run to a new sibling directory, prints a note before any controller output,
    and leaves the old directory untouched; the request validation itself stays strict.
34. **Installing a pinned controller without moving the pin broke every travel request.** The runner refuses a
    controller whose sha256 does not match its pin. Install the controller and move the pin in one step, keep the old
    controller beside it, and check `python3 -m agent.research_harness.travel_runner --verify-pin` from every
    checkout the runner may be started from before sending traffic.
35. **The fix ran from the wrong checkout** (0021). `python3 -m <runner>` imports from the working directory, and the
    model sometimes passed an older Hermes checkout as `workdir`, so a rerun still hit the old run-directory error.
    Admission now binds the working directory to the deployed checkout. With it, an identical rerun moved to
    `<name>-rerun-2` and delivered (11 verified).
36. **`json_object` guarantees syntax, not shape** (0022). A reviewer reply used the key `", "` instead of `verdict`.
    vLLM enforces a strict `json_schema` response format (a test schema forced an unnatural required key), so the
    reference controller v4 sends each review chunk with a schema for its exact keys, verdict values, identifiers
    and item count. Salvage of valid entries stays as a backstop.
37. **Discovery stopped with half the searches unused** (0023). One run used 12 of 24 searches, wrote a 7-candidate
    manifest and published 6; the controller only warned about breadth afterwards. The first controller call of a
    turn now reads the manifest and, if it is short of the controller's own targets while at least 4 searches
    remain, sends it back once with the counts. On its first live firing a 12-candidate manifest became 14 (12
    published). Most runs search enough on their own and never see it.
38. **Natural phrasing reached no research route** (0024). The product route required the message to start with
    "research", and the generic route needs source wording, so "Can you research the best options for a 55 inch
    TV ..." got no evidence controls: one session tried a delegation route and was rejected, another answered from
    two publishers. Polite lead-ins now select the product route; across 101 canary prompts only that prompt
    changed route. Test your routes with the phrasings people actually use, not just the ones in your suites.
39. **A canary leaves the chat in its last session.** The canary signs in as the operator's chat account, so the
    operator's next message landed in a travel-locked canary session. End each canary batch with `/new`.
40. **Most everyday phrasings reached no route** (0025). A sweep of natural phrasings found product 1 of 12, generic 3
    of 10 and travel 3 of 5 routed. Widening the patterns is easy; the risk is false positives, because a casual
    question ("a good recipe for banana bread") sent to the product route becomes minutes of shopping research
    ending in a withheld answer, which is worse than an unrouted reply. So a natural shopping ask also needs a
    product signal, and every change is checked against a phrasing corpus with chat negatives plus a fresh set of
    phrasings the patterns were not tuned on (18/18 and 19/20).
41. **Two copies of one rule drift apart** (0025). The receipt script keeps its own copy of the generic research-intent
    pattern. After the route was widened, the first live fact-check turns were routed correctly and then rejected
    by the receipt script ("current turn is not a research request"). Change both in the same deploy, and test that
    every phrasing the runtime routes is accepted by the script.
