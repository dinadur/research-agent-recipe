#!/bin/bash
# Swap the main lane between the old engine (llm-main, e.g. llama.cpp) and the new one (llm-main-vllm + adapter)
# with a flag file instead of Conflicts=. Run as root. The gateway and relays are handled by the caller.
#   lane-swap.sh to-vllm   install the guard, set the flag, stop the old engine, start the new one
#   lane-swap.sh to-old    clear the flag and guard, stop the new engine, start the old one (rollback)
set -euo pipefail
FLAG=/etc/research-agent/main-vllm.active
GUARD_DIR=/etc/systemd/system/llm-main.service.d
HERE=$(cd "$(dirname "$0")/.." && pwd)
case "${1:-}" in
to-vllm)
  mkdir -p "$GUARD_DIR" "$(dirname "$FLAG")"
  install -m 644 "$HERE/systemd/llm-main.service.d/90-vllm-swap.conf" "$GUARD_DIR/90-vllm-swap.conf"
  touch "$FLAG"
  systemctl daemon-reload
  systemctl stop llm-main
  systemctl enable --now llm-main-vllm llm-main-adapter ;;
to-old)
  rm -f "$FLAG" "$GUARD_DIR/90-vllm-swap.conf"
  systemctl daemon-reload
  systemctl disable llm-main-vllm llm-main-adapter || true
  systemctl stop llm-main-adapter llm-main-vllm
  systemctl start llm-main ;;
*) echo "usage: $0 to-vllm|to-old"; exit 64 ;;
esac
