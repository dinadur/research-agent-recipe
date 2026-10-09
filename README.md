# Research agent recipe

A recipe for a self-hosted research agent that answers only from a frozen, receipt-bound evidence packet and
withholds rather than guesses. It is built on Nous Research's
[hermes-agent](https://github.com/NousResearch/hermes-agent) (MIT) and runs entirely on local models.

This repository is not a fork. It contains:

- a 33-patch series against hermes-agent v0.21.3 that adds the research controller;
- the receipt script that the agent's `reliable-research` skill runs;
- two small HTTP adapters (a vLLM front end and a groundedness-judge front end);
- end-to-end test tooling that drives the agent through Telegram from a real user account;
- example systemd units and drop-ins;
- the measured results from a reference deployment on two Intel Arc B70 GPUs.

## What it does

A research request ("Research the best robot vacuum under $500 for pet hair on rugs, comparing A, B and C…") is
routed to a controller, not left to the model's judgement:

1. **Bounded retrieval.** The model may call `web_search` and `web_extract` a fixed number of times (product
   research: 4 searches and 2 extract calls, plus at most one controller-granted repair round). It cannot delegate,
   run shell commands, or write files.
2. **Controller-owned receipt.** The controller, not the model, runs `capture_receipt.py`. The script reads the
   turn's tool calls and results from Hermes' state database. It checks that every extracted URL was returned by a
   search in the same turn, and that enough distinct sources were extracted. It then writes a create-once receipt
   whose URL list is the citation allowlist.
3. **Evidence gate.** Before sealing, the controller checks that the packet can support an answer: at least three
   sources from three sites, at least two independent publishers, and either an extracted page for every named
   alternative (comparisons) or the recommended model's official page and, under a budget, a current price
   (open-ended requests). If the packet falls short, the controller grants one
   repair search and one repair extraction, naming what is missing. A second miss withholds the request.
4. **Quality-first finalization.** The answer is written in one tools-off call over the frozen evidence only. The
   draft is then checked deterministically: citations must point into the frozen evidence, and every named
   alternative, requested criterion and hard constraint must be covered. A draft that fails gets one repair nudge,
   then is withheld.
5. **Withholding.** If any step fails, the user gets a short "withheld" message instead of an unsupported answer.

A separate small model ([Clef-Flash](https://huggingface.co/Cloudflare/clef-flash), by Cloudflare) judges, sentence by sentence, whether delivered claims are supported by the
cited pages. It runs **log-only**: in blind grading it was not reliable enough to show users (see below).

## Architecture at a glance

```
 Telegram ──> Hermes gateway ──> research router ──> product / generic / travel route
                                        │
                     bounded web_search / web_extract (SearXNG, Tavily, ...)
                                        │
                     capture_receipt.py ──> receipt (citation allowlist)
                                        │
                     viability gate ── one repair fetch ── withhold
                                        │
                     quality-first synthesis (tools off, frozen evidence) ──> draft checks ──> deliver / withhold
                                        │
                     log-only groundedness shadow ──> Clef adapter ──> Clef-Flash (llama.cpp /v1/systemone)

 Lanes: main (Qwen3.8-27B, vLLM + MTP behind an adapter) · worker (9B, llama.cpp) · judge (Clef-Flash Q4_K_M)
```

Details: [docs/architecture.md](docs/architecture.md).

## Results (reference deployment, 2026-10-07/08)

Each run sent 20 product-research prompts through Telegram, every task in a fresh session with a single pass and no
retries (10 named three-way comparisons and 10 open-ended "recommend one plus two alternatives"):

| Code | Delivered | Named | Open-ended |
|---|---|---|---|
| Before the delivery fixes | 9/20 | 4/10 | 5/10 |
| Delivery fixes + citation handling | 16/20 | 10/10 | 6/10 |
| Production baseline (all 18 patches, receipt script v4) | 17/20 | 9/10 | 8/10 |

The remaining withholds were genuine evidence gaps: no official page for the chosen model, or no fetched page for
one target after the repair round.

Groundedness judge (Clef-Flash Q4_K_M, threshold 0.85):

- Offline set of 234 cases: 92.3% accuracy, against 85.5% for the Granite Guardian model it replaced.
- Two blind gradings of live decisions: 79-84% agreement with blind graders; 0.85 was the best threshold both
  times.
- The answers' plain facts were almost always right: 2 claims contradicted their cited page, out of 72 graded facts.
- About two thirds of the judge's rejections were on claims the full pages support (30 of 47 in the second round).
  The judge therefore stays log-only.

Full numbers and their sources: [RESULTS.md](RESULTS.md).

## Quick start

1. **Get hermes-agent v0.21.3 and apply the patches.**
   ```sh
   git clone https://github.com/NousResearch/hermes-agent hermes && cd hermes
   git checkout 345cd2b057          # chore(release): v0.21.3
   git am /path/to/research-agent-recipe/patches/hermes-v0.21.3/*.patch
   ```
   Then set up Hermes as its own documentation describes.
2. **Install the receipt skill.** Copy `skills/reliable-research/capture_receipt.py` to
   `~/.hermes/skills/reliable-research/scripts/capture_receipt.py` for the user that runs the gateway. Use
   `skills/reliable-research/SKILL.md` as a starting point for the skill file. See
   [skills/reliable-research/README.md](skills/reliable-research/README.md).
3. **Serve the models.**
   - Main lane: any OpenAI-compatible server. For vLLM, put `adapters/openai_vllm_adapter.py` in front.
   - Judge lane (optional): Clef-Flash on llama.cpp behind `adapters/clef_groundedness_adapter.py`.
   - For the EXL3 / vLLM + MTP build on Intel Arc B70, see 0xSero's repos for the EXL3/XPU build:
     [qwen38-flash-next-b70-offload](https://github.com/0xSero/qwen38-flash-next-b70-offload) and the
     `ghcr.io/0xsero/exl3xpu` image.
4. **Enable the routes.** Use the environment flags in [config/hermes.env.example](config/hermes.env.example), or
   the systemd drop-ins in `config/systemd/hermes-gateway.service.d/`. Add the
   [judge config snippet](config/hermes-config.example.yaml) if you run the judge.
5. **Test end to end.** Follow [testing/README.md](testing/README.md): start with `smoke`, then `product-20`.

## Documentation

- [docs/architecture.md](docs/architecture.md): routes, research loop, budgets, receipts, finalization and
  withholding.
- [docs/groundedness-judge.md](docs/groundedness-judge.md): the judge lane, the Clef adapter, shadow mode and the
  blind grading.
- [docs/testing.md](docs/testing.md): Telegram end-to-end testing, suite format, delivery measurement and replay
  rules.
- [docs/lessons.md](docs/lessons.md): failure modes found and fixed.
- [docs/reference-deployment-b70.md](docs/reference-deployment-b70.md): the two-GPU Intel Arc B70 worked example.
- [patches/hermes-v0.21.3/README.md](patches/hermes-v0.21.3/README.md): patch list and how to apply.

## Credits and license

- [hermes-agent](https://github.com/NousResearch/hermes-agent) is by Nous Research (MIT). The patches modify and
  contain parts of it; see [NOTICE](NOTICE).
- The EXL3 XPU serving work for Intel Arc is 0xSero's:
  [qwen38-flash-next-b70-offload](https://github.com/0xSero/qwen38-flash-next-b70-offload) and `ghcr.io/0xsero/exl3xpu`.
- Everything else here is MIT licensed ([LICENSE](LICENSE)).

This is a recipe from one deployment, not a supported product. Budgets, thresholds and source rules were tuned on
one hardware setup and one prompt set; measure on your own before relying on them.
