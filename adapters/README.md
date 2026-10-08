# Adapters

Two small, dependency-free (standard library only) HTTP adapters. Each makes a different engine look like the lane
the agent already expects, so swapping an engine needs no agent change.

| File | Front for | Listens | What it adds |
|---|---|---|---|
| `openai_vllm_adapter.py` | vLLM (OpenAI-compatible) | 127.0.0.1:8242 | Alias rewrite, sampler policy, `min_p`/`repeat_penalty` removal, reasoning flags, client-disconnect abort, degenerate-stream guard, llama.cpp-style `/health` `/props` `/slots` `/v1/models` |
| `clef_groundedness_adapter.py` | Clef-Flash on llama.cpp `/v1/systemone` | 127.0.0.1:8244 | Granite-Guardian-style judge chat → Clef "noul" question → `<score> yes|no </score>` at a probability threshold; optional decision log |

Both are configured by environment variables documented at the top of each file. Example units:
[config/systemd/llm-main-adapter.service](../config/systemd/llm-main-adapter.service) and
[llm-judge-adapter.service](../config/systemd/llm-judge-adapter.service).

For the EXL3 / vLLM + MTP engine itself on Intel Arc B70, see 0xSero's repos for the EXL3/XPU build:
https://github.com/0xSero/qwen38-flash-next-b70-offload and the `ghcr.io/0xsero/exl3xpu` image.

## Tests

Offline (mock upstreams on free loopback ports; nothing else is contacted):

```sh
python3 adapters/tests/test_openai_vllm_adapter.py
python3 adapters/tests/test_clef_adapter.py
```

## Notes

- The degenerate-stream guard trips on 64 identical reasoning/content deltas of at most 2 characters in a row. It
  aborts the upstream request and ends the stream with `{"error": {"code": "degenerate_stream", ...}}`, which the
  OpenAI SDK raises as `APIError`. 63 identical deltas pass untouched. Set `ADAPTER_DEGENERATE_RUN=0` to disable it.
- `thinking_token_budget` is only a request field. Whether vLLM enforces it depends on the build and on server flags
  (`--reasoning-config` in the build used here). Verify it before relying on it.
- The judge adapter's verdict threshold (`CLEF_THRESHOLD`, 0.85) was chosen by cross-validation on an offline set
  and confirmed by two live gradings; see [docs/groundedness-judge.md](../docs/groundedness-judge.md).
