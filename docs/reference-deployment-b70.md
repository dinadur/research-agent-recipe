# Reference deployment: two Intel Arc B70 GPUs

This is the setup the results in this repository were measured on (October 2026). It is one worked example, not a
requirement. Any OpenAI-compatible main model with good tool calling, plus an optional small judge, can run the
recipe. Hostnames are loopback and paths are examples; adjust them to your machine.

## Lanes

| Lane | Model | Engine | Ports | GPU |
|---|---|---|---|---|
| Main | Qwen3.8-27B, EXL3 4.00 bpw | vLLM + MTP speculative decoding (k=3) in a container, behind `openai_vllm_adapter.py` | adapter 8242 → vLLM 8260 | card A (alone) |
| Worker | a 9B instruction model, GGUF | llama.cpp v0.6.0 with a SYCL performance PR | 8243 | card B |
| Judge ("Guardian") | Clef-Flash Q4_K_M (5.7 GB) | llama.cpp v0.6.0, `/v1/systemone`, behind `clef_groundedness_adapter.py` | adapter 8244 → 8297 | card B |

Hermes uses the main lane for conversation and research, and the worker lane for delegation and context
compression. The judge lane serves only the groundedness checks. Everything binds to 127.0.0.1.

**Relays.** In the reference box, Hermes reaches each lane through a small loopback relay (8316 main, 8317 worker,
8318 judge). They are specific to that host's history, and the recipe does not depend on them. Point Hermes straight
at the adapters unless you need a hop.

### Main lane: EXL3 on vLLM + MTP

For the EXL3 / vLLM + MTP serving on Intel Arc B70 (the XPU build, the container image and the serve recipe), see
0xSero's repos for the EXL3/XPU build:

- https://github.com/0xSero/qwen38-flash-next-b70-offload
- the `ghcr.io/0xsero/exl3xpu` image (pin it by digest)

What this deployment adds on top:

- **The qualified serve flags:**
  - `--enable-prefix-caching --enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser qwen3`
    `--enable-prompt-tokens-details`;
  - fp8 KV cache, 16 sequences, `max_model_len` 262144;
  - Hermes caps prompts at 106,496 tokens.
- **The adapter** ([adapters/openai_vllm_adapter.py](../adapters/openai_vllm_adapter.py)) in front of vLLM. It
  presents a llama.cpp-style lane to the agent (`/health`, `/props`, `/slots`, `/v1/models`) and applies the request
  policy:
  - the alias becomes the served name;
  - `min_p` and `repeat_penalty` are dropped (vLLM with MTP rejects `min_p`);
  - sampler defaults are 1.0 / 0.95 / top-k 20 / repetition 1.0;
  - `thinking_token_budget` is 4096;
  - streams get `enable_thinking`, `include_reasoning` and `include_usage`.

  It also aborts the upstream request on client disconnect and on a degenerate stream (64 identical deltas of at
  most 2 characters).
- **Vision stays enabled.** `--language-model-only` saved 0.87 GiB of weights, but vLLM then profiled 2.85 GiB of
  peak activation instead of 1.0 GiB, and the KV pool shrank from 310,472 to 300,220 tokens.

Measured on this lane before adoption:

- engine healthy through the gateway's preflight in 226 s from a cold start;
- 64K-token prefill TTFT 49.5 s, with concurrent short requests at 7 s TTFT;
- in a longer endurance run, long-prefill TTFT of 48 / 146 / 400 s at 64K / 128K / 226K tokens;
- cancellation freed the slot in 0.27 s (3/3);
- a 60K prefill abandoned after 4 s freed its slot in 26.5 s (the llama.cpp lane it replaced: about 40 s);
- MTP acceptance 56-66% across the Telegram test runs.

**VRAM.** vLLM runs at 0.965 memory utilisation, which leaves about 1.1 GiB of headroom on the main card. The KV
pool holds about 310K tokens and fills only with prompts beyond Hermes' cap. A device-memory dashboard may not count
the KV pool; plan from vLLM's own numbers.

### Worker lane: llama.cpp + SYCL

llama.cpp v0.6.0 plus a SYCL performance PR, with context 262144, 4 parallel slots, batch 2048 and ubatch 512. An
A/B against the previous v0.4.1 build with the worker settings gave:

- decode about equal (+2% at concurrency 1);
- prefill +10-21%;
- abandon drain 9.6 s instead of 12.6 s;
- 14/16 identical greedy outputs.

Plain v0.6.0 without the PR was much slower at prefill on this hardware (-63% on the main-lane settings), so test
your exact build.

### Judge lane: Clef-Flash

Model: [Clef-Flash](https://huggingface.co/Cloudflare/clef-flash), converted to GGUF with llama.cpp's `convert_hf_to_gguf.py` and quantized to Q4_K_M.

llama-server with `-c 2048 -np 1 -b 1024 -ub 1024 -ngl 99`, behind the Clef adapter at threshold 0.85. Latency p50
was 0.54 s on the offline set, and about 0.4-1.0 s per claim live.

**VRAM.** With the worker and the judge resident, the second card reported 0.14 GB free of 34.24 GB. That is why
the judge's batch stays at 1024 tokens. Any setting that grows buffers on that card (a larger judge batch, more
worker slots) needs a VRAM plan first.

## systemd layout

Example units are in [config/systemd/](../config/systemd/):

```
llm-main-vllm.service          container engine (main)
llm-main-adapter.service       PartOf=llm-main-vllm, Wants=llm-main-vllm
llm-main.service               previous engine (llama.cpp), kept as the rollback (your unit, not provided)
  llm-main.service.d/90-vllm-swap.conf     ConditionPathExists=!/etc/research-agent/main-vllm.active
llm-worker.service             llama.cpp worker (your unit, not provided)
llm-judge-clef.service         Clef-Flash engine
llm-judge-adapter.service      PartOf=llm-judge-clef
llm-judge-granite.service      previous judge, kept as the rollback (your unit, not provided)
  llm-judge-granite.service.d/90-clef-swap.conf
hermes-gateway.service         from the Hermes install
  hermes-gateway.service.d/
    20-wiki-vault.conf                       OBSIDIAN_VAULT_PATH
    95-product-quality-first.conf            HERMES_PRODUCT_RESEARCH_QUALITY_FIRST=1
    96-verified-receipt-direct-replay-canary.conf
    97-product-groundedness-shadow.conf      HERMES_PRODUCT_GROUNDEDNESS_SHADOW=1
    97-product-research-repair-fetch.conf    HERMES_PRODUCT_RESEARCH_REPAIR_FETCH=1
    98-hermes-checkout.conf                  pinned checkout + ExecStartPre preflight
```

Each behaviour is a separate drop-in, so turning one off is: delete the file, `systemctl daemon-reload`, restart the
gateway. No code change and no config edit is needed.

### Preflight commit pin

`98-hermes-checkout.conf` runs [config/bin/hermes-preflight](../config/bin/hermes-preflight) as `ExecStartPre`. The
gateway starts only if all of these hold:

- the checkout's filesystem is the expected one (optional UUID pin);
- `HEAD` equals the pinned commit;
- the worktree is clean, with no untracked files.

Every code deploy moves the pin, and keeps a dated backup of the previous preflight. Rollback is then `git reset
--hard <previous>` in the checkout plus restoring the old preflight. The reference deployment moved the pin 11 times
in under 8 hours this way.

### Swapping engines: flag files, never `Conflicts=`

The worker and judge units declare `Wants=` on the main unit, and the gateway `Wants=` the judge unit. If the new
engine declares `Conflicts=` with the old one, any restart of a unit that `Wants=` the old one starts the old
engine, and that stops the new one. This happened in the reference deployment: the main lane silently ran the old
engine for 22 minutes after a judge restart.

The pattern used instead:

1. The new engine units have no `Conflicts=`.
2. The old unit gets a drop-in `ConditionPathExists=!/etc/research-agent/<lane>.active`.
3. A swap script ([config/bin/lane-swap.sh](../config/bin/lane-swap.sh)) touches the flag, installs the drop-in,
   stops the old unit and starts the new ones. Rolling back reverses those steps.
4. While the flag exists, any `Wants=` pull of the old unit is skipped by systemd. This was verified by restarting
   the worker and the gateway.
5. The health check and the dashboard list the new units separately, so a fallback is visible.

Timings in the reference deployment:

- main cutover: the agent was down about 4-5 minutes while the engine loaded, and the script rolls back
  automatically if the lane is not healthy within 20 minutes;
- main rollback to llama.cpp: about 1 minute;
- judge rollback: about 11 s.

## Health check

[tools/health_check.py](../tools/health_check.py) writes a markdown report covering:

- unit states, including units that must stay off;
- the flag files;
- the engine fields behind the adapters;
- vLLM counters (finished requests by reason, preemptions, MTP acceptance);
- judge decision statistics;
- warning-level journal lines per unit;
- kernel GPU and OOM faults;
- free disk space.

Run it after every cutover and after long test runs. MTP acceptance in the report is cumulative, so for incident
work sample the engine log's per-window metrics instead.

## Host software (for reference)

Ubuntu with a 7.0 kernel; Intel compute runtime 26.31, Level Zero 1.32, IGC 2.40 and the oneAPI 2026.1 compiler and
runtime. The llama.cpp lanes are SYCL builds. Pin and record exact package versions, and rerun the lane checks after
any driver or runtime update.
