# Configuration examples

All paths and names are examples. The reference layout uses `/opt/research-agent` for code, `/srv/models` for
weights, `/srv/wiki` for the wiki vault, `/etc/research-agent` for flags and env files, and a `llm` service user for
the engines.

| File | Purpose |
|---|---|
| `hermes.env.example` | Hermes environment flags used by the recipe (all off unless exactly `1`) |
| `hermes-config.example.yaml` | `product_research.groundedness_judge` snippet for the Hermes config |
| `adapter.env.example` | `/etc/research-agent/adapter.env` for the main-lane adapter |
| `systemd/hermes-gateway.service.d/*.conf` | One drop-in per behaviour; delete a file to turn that behaviour off |
| `systemd/llm-*.service` | Engine and adapter units (main on vLLM, judge on llama.cpp) |
| `systemd/llm-main.service.d/90-vllm-swap.conf`, `systemd/llm-judge-granite.service.d/90-clef-swap.conf` | Flag-file guards that hold the previous engines off |
| `bin/hermes-preflight` | `ExecStartPre` commit pin: start only the reviewed, clean checkout |
| `bin/lane-swap.sh` | Flag-file engine swap and rollback, without `Conflicts=` |

`systemd` does not expand `~` in `Environment=` lines. Use the absolute home of the gateway's service user, for
example `/var/lib/hermes/.hermes`.

See [docs/reference-deployment-b70.md](../docs/reference-deployment-b70.md) for how these fit together.
