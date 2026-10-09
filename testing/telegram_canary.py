#!/usr/bin/env python3
"""End-to-end canary: send a suite of prompts to your Hermes Telegram bot from a real user account.

Why a user account: the Bot API cannot message another bot, and testing through the chat front end exercises the
whole path a real request takes (gateway routing, session handling, the research controller, delivery splitting).

Safety rules built in:
  * The client signs in as one pinned Telegram user (TELEGRAM_CANARY_USER_ID) and sends only to one pinned bot
    (TELEGRAM_CANARY_PEER plus its numeric TELEGRAM_CANARY_PEER_ID; the resolved peer must be a bot).
  * Every task starts a fresh session ("/new canary-<task-id>") unless "fresh_session" is false.
  * A read-only safety footer is appended to every prompt.
  * Approval-like prompts ("yes", "approve", "publish it", ...) are refused at load time. The only way a canary
    replies to an approval prompt is an explicit per-task "approval_followup" ("yes", "more" or "publish it"),
    sent once after the first reply has settled. Use it only where publishing test pages is acceptable.
  * A per-task "followup" ({"after_seconds": N, "text": ...}) sends a second message while the first turn may
    still be running, to test busy-input handling. It is subject to the same approval-prompt check.
  * After the last task the client sends "/new", so the operator's next real message starts a clean session.
  * It refuses to run as root, and requires the Telethon session file to be mode 0600.

Completion of a task = the gateway was seen active and returned to idle (read from Hermes' gateway_state.json),
and the delivered Telegram messages have not changed for --settle-seconds. A task passes if, in addition, the reply
has at least "minimum_response_chars" characters and equals "expected_exact" when that is set.

Config file (KEY=VALUE, default ~/.config/research-agent/telegram-canary.env; keep it mode 0600, never commit it):
  TELEGRAM_API_ID=123456
  TELEGRAM_API_HASH=<from my.telegram.org>
  TELEGRAM_CANARY_USER_ID=<your numeric Telegram user id; must also be allowed by the bot>
  TELEGRAM_CANARY_PEER=@your_research_bot
  TELEGRAM_CANARY_PEER_ID=<the bot's numeric id>
  TELEGRAM_CANARY_SESSION=~/.config/research-agent/telegram-canary/user   (".session" is appended)

Create the session once, interactively:  python3 -c "from telethon.sync import TelegramClient as C;
  C('<session path>', <api id>, '<api hash>').start()"

Usage: telegram_canary.py --suite suites/smoke.json --output-dir results/smoke-$(date -u +%Y%m%dT%H%M%SZ)
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import pathlib
import re
import time
from datetime import datetime, timezone
from typing import Any

from telethon import TelegramClient

READ_ONLY_FOOTER = """
[AUTOMATED TELEGRAM CANARY SAFETY BOUNDARY]
Answer the request, but take no action outside this conversation. Do not use
any tool that changes state, and do not confirm or approve a pending action.
You may use read-only tools. Return the requested answer in this turn. Do not
ask this canary to confirm or approve a later action.
""".strip()

FORBIDDEN_STANDALONE_PROMPTS = {
    "approve", "approved", "confirm", "confirmed", "do it", "go ahead", "let's publish",
    "publish it", "save it", "send it", "yes",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_env(path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def safe_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-.")
    if not cleaned or cleaned != value:
        raise ValueError(f"task id must already be filesystem-safe: {value!r}")
    return cleaned


def load_suite(path: pathlib.Path) -> list[dict[str, Any]]:
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(parsed, dict):
        parsed = parsed.get("tasks")
    if not isinstance(parsed, list) or not parsed or len(parsed) > 100:
        raise ValueError("suite must be {\"tasks\": [...]} or a list, with 1-100 tasks")
    tasks, seen = [], set()
    for index, row in enumerate(parsed, 1):
        task_id = safe_id(str(row.get("id", f"task-{index:03d}")))
        if task_id in seen:
            raise ValueError(f"duplicate task id: {task_id}")
        seen.add(task_id)
        prompt = str(row.get("prompt", "")).strip()
        if not prompt:
            raise ValueError(f"task {task_id} has no prompt")
        if re.sub(r"[^a-z' ]+", "", prompt.casefold()).strip() in FORBIDDEN_STANDALONE_PROMPTS:
            raise ValueError(f"task {task_id} is an approval-like prompt and is forbidden")
        followup = row.get("followup")
        if followup is not None:
            text = str(followup.get("text", "")).strip() if isinstance(followup, dict) else ""
            after = int(followup.get("after_seconds", 0)) if isinstance(followup, dict) else 0
            if not text or not 10 <= after <= 1800:
                raise ValueError(f"task {task_id} followup needs text and after_seconds 10-1800")
            if re.sub(r"[^a-z' ]+", "", text.casefold()).strip() in FORBIDDEN_STANDALONE_PROMPTS:
                raise ValueError(f"task {task_id} followup is an approval-like prompt and is forbidden")
            followup = {"text": text, "after_seconds": after}
        approval = row.get("approval_followup")
        if approval is not None and approval not in {"yes", "more", "publish it"}:
            raise ValueError(f"task {task_id} approval_followup must be yes, more or publish it")
        footer = str(row.get("footer", "read_only"))
        if footer not in {"read_only", "none"}:
            raise ValueError(f"task {task_id} footer must be read_only or none")
        tasks.append({
            "id": task_id,
            "prompt": prompt,
            "followup": followup,
            "approval_followup": approval,
            # "none" sends the prompt unchanged: travel requests must write their manifest, and a footer that
            # forbids writes would switch the travel route off.
            "footer": footer,
            "expected_exact": row.get("expected_exact"),
            "minimum_response_chars": max(1, min(10000, int(row.get("minimum_response_chars", 1)))),
            "timeout_seconds": max(30, min(7200, int(row.get("timeout_seconds", 900)))),
            "fresh_session": row.get("fresh_session", True) is not False,
            "require_gateway_activity": row.get("require_gateway_activity", True) is not False,
            "continue_on_failure": bool(row.get("continue_on_failure", False)),
        })
    return tasks


def write_json(path: pathlib.Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def gateway_status(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"read_error": type(exc).__name__, "active_agents": None}
    return {"gateway_state": value.get("gateway_state"), "active_agents": value.get("active_agents"),
            "updated_at": value.get("updated_at")}


async def wait_gateway_idle(path: pathlib.Path, timeout: float) -> dict[str, Any]:
    deadline, last = time.monotonic() + timeout, {}
    while time.monotonic() < deadline:
        last = gateway_status(path)
        if last.get("gateway_state") == "running" and last.get("active_agents") == 0:
            return last
        await asyncio.sleep(1)
    raise TimeoutError(f"gateway did not become idle: {last}")


async def incoming_after(client: TelegramClient, peer: Any, message_id: int) -> list[dict[str, Any]]:
    rows = []
    async for m in client.iter_messages(peer, min_id=message_id, reverse=True, limit=100):
        if not m.out:
            text = m.raw_text or ""
            rows.append({"id": int(m.id), "edit_date": m.edit_date.isoformat() if m.edit_date else None,
                         "text": text, "text_sha256": sha256_text(text)})
    return rows


def delivery_signature(rows: list[dict[str, Any]]) -> str:
    reduced = [(r["id"], r["edit_date"], r["text_sha256"]) for r in rows]
    return hashlib.sha256(json.dumps(reduced).encode()).hexdigest()


async def wait_for_reply(client, peer, after_id, gateway_state, timeout, poll_seconds, settle_seconds,
                         followup_state=None, require_activity=True):
    """Wait until the gateway was active, returned to idle, and delivery stopped changing."""
    start = time.monotonic()
    active_seen = idle_after_active = False
    last_signature, last_change, final_rows, status = "", start, [], "timeout"
    while time.monotonic() < start + timeout:
        if followup_state is not None and not followup_state.get("sent"):
            active_seen = idle_after_active = False  # not complete before the follow-up is out
        gw = gateway_status(gateway_state)
        active = gw.get("active_agents")
        if isinstance(active, int) and active > 0:
            active_seen = True
        if active_seen and active == 0:
            idle_after_active = True
        rows = await incoming_after(client, peer, after_id)
        signature = delivery_signature(rows)
        if signature != last_signature:
            last_signature, last_change, final_rows = signature, time.monotonic(), rows
        complete = idle_after_active or (not require_activity and active == 0)
        if complete and rows and time.monotonic() - last_change >= settle_seconds:
            status = "pass"
            break
        await asyncio.sleep(poll_seconds)
    return status, final_rows, active_seen, idle_after_active


async def run_task(client, peer, task, task_dir, gateway_state, poll_seconds, settle_seconds) -> dict[str, Any]:
    await wait_gateway_idle(gateway_state, 120)
    if task["fresh_session"]:
        new = await client.send_message(peer, f"/new canary-{task['id']}")
        deadline = time.monotonic() + 60
        while not await incoming_after(client, peer, int(new.id)):
            if time.monotonic() > deadline:
                raise TimeoutError("bot did not acknowledge /new")
            await asyncio.sleep(1)
        await wait_gateway_idle(gateway_state, 120)

    sent_text = task["prompt"] if task["footer"] == "none" else f"{task['prompt']}\n\n{READ_ONLY_FOOTER}"
    (task_dir / "prompt-sent.md").write_text(sent_text + "\n", encoding="utf-8")
    sent = await client.send_message(peer, sent_text)
    sent_utc, start = utc_now(), time.monotonic()
    followup_state = {} if task["followup"] else None

    async def send_followup():
        await asyncio.sleep(task["followup"]["after_seconds"])
        message = await client.send_message(peer, task["followup"]["text"])
        followup_state.update(sent=True, id=int(message.id), sent_utc=utc_now())
        write_json(task_dir / "followup-sent.json", followup_state)

    followup_job = asyncio.create_task(send_followup()) if task["followup"] else None
    status, final_rows, active_seen, idle_after_active = await wait_for_reply(
        client, peer, int(sent.id), gateway_state, task["timeout_seconds"], poll_seconds, settle_seconds,
        followup_state, task["require_gateway_activity"])
    if followup_job is not None and not followup_job.done():
        followup_job.cancel()

    response = "\n\n".join(r["text"] for r in final_rows).strip()
    exact = None if task["expected_exact"] is None else response == task["expected_exact"]
    reason = None
    if status == "pass" and len(response) < task["minimum_response_chars"]:
        status, reason = "fail", "response_too_short"
    elif status == "pass" and exact is False:
        status, reason = "fail", "exact_mismatch"
    (task_dir / "response.md").write_text(response + "\n", encoding="utf-8")
    write_json(task_dir / "telegram-messages.json", final_rows)
    if task["approval_followup"] and status == "pass":
        await asyncio.sleep(settle_seconds)
        approval = await client.send_message(peer, task["approval_followup"])
        a_status, a_rows, _seen, _idle = await wait_for_reply(
            client, peer, int(approval.id), gateway_state, task["timeout_seconds"], poll_seconds, settle_seconds)
        write_json(task_dir / "approval.json", {"text": task["approval_followup"], "status": a_status,
                                                "messages": a_rows})
    row = {"id": task["id"], "status": status, "failure_reason": reason, "sent_utc": sent_utc,
           "elapsed_seconds": round(time.monotonic() - start, 1), "response_chars": len(response),
           "response_sha256": sha256_text(response), "exact_match": exact,
           "gateway_activity_seen": active_seen, "gateway_idle_after_activity": idle_after_active}
    write_json(task_dir / "result.json", row)
    return row


async def async_main(args: argparse.Namespace) -> int:
    cfg = load_env(args.config)
    api_id, api_hash = int(cfg["TELEGRAM_API_ID"]), cfg["TELEGRAM_API_HASH"]
    user_id, peer_id = int(cfg["TELEGRAM_CANARY_USER_ID"]), int(cfg["TELEGRAM_CANARY_PEER_ID"])
    peer_name = cfg["TELEGRAM_CANARY_PEER"].lstrip("@")
    session_base = pathlib.Path(cfg.get("TELEGRAM_CANARY_SESSION",
                                        "~/.config/research-agent/telegram-canary/user")).expanduser()
    session_file = pathlib.Path(str(session_base) + ".session")
    if not session_file.is_file():
        raise RuntimeError("Telegram user session is missing; run the one-time login first")
    if session_file.stat().st_mode & 0o077:
        raise RuntimeError("Telegram session permissions are broader than 0600")

    tasks = load_suite(args.suite.resolve(strict=True))
    out = args.output_dir.resolve()
    (out / "tasks").mkdir(parents=True, exist_ok=False)
    started, results = utc_now(), []
    client = TelegramClient(str(session_base), api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram user session is no longer authorized")
        me = await client.get_me()
        if me is None or int(me.id) != user_id:
            raise RuntimeError("signed-in Telegram user does not match TELEGRAM_CANARY_USER_ID")
        peer = await client.get_entity(peer_name)
        if int(peer.id) != peer_id or not getattr(peer, "bot", False):
            raise RuntimeError("resolved peer does not match the pinned bot identity")
        for task in tasks:
            task_dir = out / "tasks" / task["id"]
            task_dir.mkdir()
            result = await run_task(client, peer, task, task_dir, args.gateway_state,
                                    args.poll_seconds, args.settle_seconds)
            results.append(result)
            print(json.dumps(result))
            if result["status"] != "pass" and not task["continue_on_failure"]:
                break
            await asyncio.sleep(args.cooldown_seconds)
        # Leave the operator's chat in a fresh session, not in the last canary's.
        try:
            await wait_gateway_idle(args.gateway_state, 120)
            await client.send_message(peer, "/new")
        except Exception as exc:  # recorded, never fails the batch
            print(json.dumps({"closing_new": "failed", "error": type(exc).__name__}))
    finally:
        await client.disconnect()
    summary = {"kind": "hermes-telegram-read-only-canary", "started_utc": started, "completed_utc": utc_now(),
               "requested": len(tasks), "completed": len(results),
               "passed": sum(r["status"] == "pass" for r in results), "results": results}
    write_json(out / "summary.json", summary)
    return 0 if summary["passed"] == len(tasks) else 1


def main() -> int:
    hermes_home = pathlib.Path(os.environ.get("HERMES_HOME") or pathlib.Path.home() / ".hermes")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--suite", required=True, type=pathlib.Path)
    p.add_argument("--output-dir", required=True, type=pathlib.Path)
    p.add_argument("--config", type=pathlib.Path,
                   default=pathlib.Path("~/.config/research-agent/telegram-canary.env").expanduser())
    p.add_argument("--gateway-state", type=pathlib.Path, default=hermes_home / "gateway_state.json")
    p.add_argument("--poll-seconds", type=float, default=3.0)
    p.add_argument("--settle-seconds", type=float, default=15.0)
    p.add_argument("--cooldown-seconds", type=float, default=10.0)
    args = p.parse_args()
    if os.geteuid() == 0:
        raise SystemExit("refusing to run the Telegram client as root")
    if args.poll_seconds < 1 or args.settle_seconds < 5 or args.cooldown_seconds < 0:
        raise SystemExit("invalid polling, settling or cooldown value")
    try:
        return asyncio.run(async_main(args))
    except Exception as exc:
        print(json.dumps({"status": "error", "error": type(exc).__name__, "detail": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
