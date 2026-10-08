# reliable-research skill: receipt script

`capture_receipt.py` is the custody step of the research controller (see
[docs/architecture.md](../../docs/architecture.md#controller-owned-receipts)). This is the version that ran in the
reference deployment ("v4", sha256 `88e0ade3…`), unchanged.

## Install

For the user that runs the Hermes gateway:

```sh
mkdir -p ~/.hermes/skills/reliable-research/scripts
install -m 0644 capture_receipt.py ~/.hermes/skills/reliable-research/scripts/capture_receipt.py
install -m 0644 SKILL.md ~/.hermes/skills/reliable-research/SKILL.md     # minimal example; adapt it
```

The patched Hermes accepts only the exact command
`python3 ~/.hermes/skills/reliable-research/scripts/capture_receipt.py`, run by the controller with
`HERMES_SESSION_ID=<session>` (and `HERMES_PRODUCT_RESEARCH_ROUTE=1` for product research) prepended. If your Hermes
home is elsewhere, see [patches/hermes-v0.21.3/README.md](../../patches/hermes-v0.21.3/README.md#sanitization-notes).

`SKILL.md` here is a minimal example written for this recipe. The reference deployment's own skill file (and its
`run_research.py` helper, which the patches reference for a fixed demo command) are not published.

## What it checks

- The turn, read from `$HERMES_HOME/state.db` in read-only mode, starts at the latest operator message. Hermes'
  continuation nudges ("[System: Your previous response was truncated…") are skipped.
- Only `skill_view`, `web_search` and `web_extract` calls are allowed. Typed controller rejections that never
  executed are tolerated (at most 2).
- Search and extract bounds:
  - 1-5 searches for generic research and 1-6 for product research (`HERMES_PRODUCT_RESEARCH_ROUTE=1`);
  - 1-3 extract calls.
- Every extracted URL was returned by a search in the same turn.
- At least 2 successfully extracted distinct HTTPS sources, or 3 for product research. Primary-source requests
  exclude community and secondary hosts.

## Output

- `$HERMES_HOME/research-receipts/<session>--turn-<user-message-id>.json` is created once (`O_EXCL`, mode 0600) and
  never replaced. It holds the searches, the extract batches, each source (URL, title, content hash and length),
  the citation allowlist and the limits used.
- stdout: `RESEARCH_RECEIPT_OK receipt_sha256=<hash> source_count=<n>`, then `SOURCE <url>` lines.
- On failure: `RESEARCH_RECEIPT_ERROR <reason>` on stderr, and exit code 1.

## Warning: it writes files

`build_receipt()` writes the receipt file as a side effect. Never call it against the live Hermes home from a test or
replay. Use `build_receipt(home=<temporary directory>, session_id=...)` and point the turn reader at a copy of the
database, or at the live one read-only. See [docs/testing.md](../../docs/testing.md#replays-must-never-write-into-live-state).
