# Testing

Unit tests in the patch series cover the controller's rules. They cannot show whether a real request, sent the way a
user sends it, gets a useful answer. Three layers were used:

1. **Unit and route tests** in the patched hermes-agent tree (`tests/agent/test_product_route.py`,
   `test_product_repair_fetch.py`, `test_quality_first_route.py`, `test_product_groundedness.py`,
   `test_generic_research_route.py` and others).
2. **End-to-end canaries** through the real chat front end (Telegram), described below.
3. **Viability replay** of stored sessions against changed code, as a regression check before deploying.

## End-to-end through Telegram

### Why a user-account client

The Bot API cannot send messages to another bot, and a test that skips the chat front end skips gateway routing,
session handling and message splitting. [testing/telegram_canary.py](../testing/telegram_canary.py) signs in as a
real Telegram **user** account with [Telethon](https://github.com/LonamiWebs/Telethon) and talks to the bot exactly
as a person would.

### Safety rules

- **Pinned identities.** The client must be signed in as the configured user ID. It sends only to one bot,
  configured by username (`@your_research_bot`) and numeric ID, and the resolved peer must be a bot. A mismatch
  aborts the run.
- **Read-only footer.** Every prompt gets this footer appended:
  ```
  [AUTOMATED TELEGRAM CANARY SAFETY BOUNDARY]
  Answer the request, but take no action outside this conversation. Do not use
  any tool that changes state, and do not confirm or approve a pending action. ...
  ```
- **No approvals.** Approval-like prompts ("yes", "approve", "publish it", "go ahead", …) are refused when the suite
  is loaded. A canary can never approve a pending action, such as a wiki publication.
- **A fresh session per task.** The runner sends `/new canary-<task-id>` and waits for the acknowledgement and for
  the gateway to go idle before sending the prompt. Results then do not depend on earlier tasks' context.
- **Session file hygiene.** The Telethon session must be mode 0600, the client refuses to run as root, and the
  config and session files live outside the repository.

### Completion and pass criteria

A task is complete when both of these hold:

- the gateway was seen active and then idle, read from Hermes' `gateway_state.json` (`active_agents`);
- the delivered Telegram messages have not changed for 15 s (the settle window, which covers multi-message
  delivery and edits).

A completed task passes if the reply is at least `minimum_response_chars` long and equals `expected_exact` when that
field is set. A withheld product answer is short, so a 400-character minimum classifies it as not delivered.

The runner's per-task timeout does not stop Hermes. If a task hangs, check `active_agents` in
`gateway_state.json` and the engine's running-request count. Restarting the gateway cancels the in-flight request.

### Suite format

```json
{
  "tasks": [
    {
      "id": "n02-wireless-earbuds",
      "prompt": "Research the best wireless earbuds under $250 for commuting, comparing ...",
      "timeout_seconds": 1500,
      "minimum_response_chars": 400,
      "expected_exact": null,
      "fresh_session": true,
      "require_gateway_activity": true,
      "continue_on_failure": true
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `id` | Filesystem-safe task ID; it names the result directory and the `/new` session title |
| `prompt` | Sent as-is, plus the safety footer |
| `timeout_seconds` | Upper bound for the task (1500 s for research prompts) |
| `minimum_response_chars` | Shorter replies fail as `response_too_short` |
| `expected_exact` | Optional exact reply, for smoke tests ("CANARY-OK") |
| `fresh_session` | Send `/new` first (default true) |
| `require_gateway_activity` | Completion requires seeing the gateway go active (default true) |
| `continue_on_failure` | Keep running the suite after this task fails (default false) |

Example suites are in [testing/suites/](../testing/suites/):

- `smoke.json`: exact-reply smoke, plain chat and two short cited research questions.
- `product-20.json`: the 20 product prompts used for the delivery measurements:
  - `n01`-`n10`: named three-way comparisons ("…, comparing A, B and C. Use each manufacturer's official product
    page for …, and a current retailer or manufacturer price for each. Recommend one model plus two
    alternatives and cite the pages.");
  - `o01`-`o10`: open-ended requests ("Research the best X under $N for Y. Recommend one model plus two
    alternatives and cite your sources.").

Each run writes `summary.json` plus, per task, `prompt-sent.md`, `response.md`, `telegram-messages.json` and
`result.json`. While a suite runs, also sample the engine log, for example the speculative-decoding acceptance
metrics and the adapter's "aborted a degenerate stream" lines. That way a bad answer can be told apart from a bad
engine window.

## How delivery was measured

- **Delivered** means the bot sent a full answer that passed the controller's checks: the response passed the
  runner's criteria and was not a withheld message.
- **Fresh single pass** means each of the 20 prompts was sent once, in a new session, on one code version. A rerun
  of only the failed prompts is reported separately and never merged into a fresh-pass number.
- The withheld reason for every miss was taken from the gateway log, which separates evidence gaps from bugs.

| Run | Code | Result |
|---|---|---|
| 20-prompt fresh | before the delivery fixes (patches 0001-0014) | **9/20** (named 4/10, open-ended 5/10) |
| Rerun of the 11 withheld | + 0015 | 7/11 (not a fresh pass) |
| Rerun of the last 4 | + 0016 | 4/4 (not a fresh pass) |
| 20-prompt fresh | 0001-0016 | **16/20** (named 10/10, open-ended 6/10) |
| 10 open-ended fresh | 0001-0017 | 8/10 |
| 20-prompt fresh | 0001-0018 + receipt script v4 | **17/20** (named 9/10, open-ended 8/10) |

What the 11 withholds in the first run were:

- 6 still short of evidence after the repair fetch;
- 2 with a named alternative missing from the draft;
- 1 missing citations;
- 2 `RESEARCH_RECEIPT_ERROR` from the turn-window bug.

In the final run, the three withholds were genuine evidence gaps: no fetched page for one drill target after repair,
and no official page for the office chair or webcam picks. Results vary between fresh runs: named comparisons went
10/10 in one run and 9/10 in the next (the drill). Treat a single run's per-category numbers as noisy.

## Viability replay against stored sessions

Before deploying a controller change, replay the stored product turns through the changed code. For each turn, read
the session's tool calls and results from `state.db`, rebuild the evidence packet, and ask the new viability gate
whether the receipt would have been ready. Compare with what actually happened:

- **not-ready → ready**: the change helps. Check one or two by hand, because the gate may now accept something it
  should not.
- **ready → not-ready**: a regression. Treat any as blocking.

One replay of 74 product turns gave 4 not-ready → ready and 0 regressions. An earlier draft of the same change
showed 1 regression (a retailer counted as an official page), which was fixed before deployment.

Replay also confirmed the bug fixes: offline replay of a failed turn returned "no binding" on the deployed code and
a binding on the fix.

### Replays must never write into live state

`capture_receipt.build_receipt()` is not a pure function. It creates
`<HERMES_HOME>/research-receipts/<session>--turn-<id>.json` with `O_EXCL`. A replay against the live home therefore
creates real receipt files. These can block the real turn's receipt later ("research receipt already exists"), and
they leave root-owned files if the replay runs as root. This happened once in the reference deployment, and the two
stray files had to be removed by hand.

Safe pattern:

- run replays with a **temporary home**: `build_receipt(home=Path(tmpdir), …)`;
- wrap or monkeypatch the turn reader so it reads the live `state.db` read-only (the script already opens it with
  `mode=ro` and `PRAGMA query_only`);
- never point a replay at the live receipt directory, and never run it as the gateway user or as root.

The same caution applies to the judge. Replaying answers through the groundedness shadow adds rows to the judge's
decision log, so tag or filter them.
