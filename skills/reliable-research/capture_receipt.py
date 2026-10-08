#!/usr/bin/env python3
"""Capture a create-once receipt for one bounded generic research turn."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Upper bounds (B70, 2026-09-27): the runtime controller enforces each request's budget (3/2 by default,
# 5/3 for open-ended requests); the receipt only rejects turns beyond the largest budget.
MAX_SEARCH_CALLS = 5
# The model-visible product budget remains three searches. The deterministic
# controller may add one site-scoped provenance-recovery search when the model
# supplies a remembered extraction path instead of an exact search result.
# Hermes admits 4 product searches plus one controller-granted repair search
# (HERMES_PRODUCT_RESEARCH_REPAIR_FETCH, 2026-10-07); its own limiter enforces
# the per-turn budget and has admitted 6 in a repaired turn, so this outer bound
# is 6.
MAX_PRODUCT_SEARCH_CALLS = 6
MAX_EXTRACT_CALLS = 3
MIN_SUCCESSFUL_SOURCES = 2
MIN_PRODUCT_SOURCES = 3
MAX_TOOL_RESULT_CHARS = 2_000_000
MAX_RECOVERABLE_CONTROLLER_REJECTIONS = 2
ALLOWED_PRIOR_TOOLS = {"skill_view", "web_search", "web_extract"}
RECOVERABLE_CONTROLLER_REJECTION_SIGNATURES = {
    ("terminal", "product_research_terminal_forbidden"): {
        "terminal_failure": True,
        "retryable": False,
    },
    ("web_extract", "generic_research_source_budget_retry"): {
        "terminal_failure": False,
        "retryable": True,
    },
    ("web_extract", "generic_research_source_budget_retry_exhausted"): {
        "terminal_failure": True,
        "retryable": False,
    },
}
# A typed product-route capability rejection is recoverable for any tool the
# model tried (for example read_file on a cached page): the controller refused
# it before execution, so it cannot have contributed evidence (2026-10-07).
ANY_TOOL_RECOVERABLE_REJECTION_SIGNATURES = {
    "product_research_capability_forbidden": {
        "terminal_failure": True,
        "retryable": False,
    },
}
CONTINUATION_NUDGE_PREFIXES = (
    "[System: Your previous response was truncated by the output length limit.",
    "[System: The previous response was cut off by a network error mid-stream.",
    "[System: Your previous tool call (",
)
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
TRUNCATED_TOOL_RESULT_RE = re.compile(
    r"\n\[Truncated: tool response was "
    r"(?P<chars>\d{1,3}(?:,\d{3})*) chars\. "
    r"Full output could not be saved to sandbox\.\]"
    r"\n</untrusted_tool_result>\s*$"
)
PERSISTED_TOOL_RESULT_RE = re.compile(
    r'^<untrusted_tool_result source="web_extract">\n'
    r"The following content was retrieved from an external source\.[^\n]*\n\n"
    r"<persisted-output>\n"
    r"This tool result was too large \((?P<chars>\d{1,3}(?:,\d{3})*) characters, "
    r"\d+(?:\.\d+)? KB\)\.\n"
    r"Full output saved to: /tmp/hermes-results/[A-Za-z0-9_-]{20,128}\.txt\n"
    r"Use the read_file tool with offset and limit to access specific sections of this output\.\n\n"
    r"Preview \(first 1500 chars\):\n[\s\S]*\n\.\.\.\n"
    r"</persisted-output>\n</untrusted_tool_result>\s*$"
)
PRODUCT_RESEARCH_ROUTE_ENV = "HERMES_PRODUCT_RESEARCH_ROUTE"
PRIMARY_SOURCE_REQUEST_RE = re.compile(
    r"\b(?:primary|official)\s+(?:source|sources|documentation|docs)\b",
    re.IGNORECASE,
)
SECONDARY_HOST_TOKENS = (
    "dev.to",
    "medium.com",
    "reddit.com",
    "stackoverflow.com",
    "stackexchange.com",
    "readthedocs.io",
)
NON_PRIMARY_GITHUB_PATH_RE = re.compile(
    r"/(?:discussions|issues|pull|pulls)(?:/|$)", re.IGNORECASE
)


# Same research-intent test as the runtime route (agent/generic_research_route.py _RESEARCH_INTENT_RE), so
# "I'd like to research ..." and "Can you research ..." count as research requests.
RESEARCH_INTENT_RE = re.compile(
    r"\b(?:research|investigate|look\s+up|find\s+(?:current|recent|official)|"
    r"current\s+best\s+practices?|"
    r"check\s+(?:the\s+)?(?:official\s+|latest\s+|current\s+)?(?:docs|documentation|sources?|release\s+notes))\b",
    re.IGNORECASE,
)
# Same as the runtime's _EXPLICIT_RESEARCH_ASK_RE (Hermes 48a142f): look-into and
# fact-check asks are research requests without the word "research".
EXPLICIT_RESEARCH_ASK_RE = re.compile(
    r"^\s*(?:(?:hey|hi)\b[\s,!]*)?"
    r"(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?|please\s+"
    r"|i(?:'|\u2019)?d\s+like\s+you\s+to\s+|i\s+would\s+like\s+you\s+to\s+|help\s+me\s+)?"
    r"(?:look\s+into(?!\s+(?:my|this|that|these|your)\b)|find\s+out|verify|fact[-\s]?check|"
    r"check\s+(?:what|whether|if|how|when))\b"
    r"|^\s*is\s+it\s+true\b",
    re.IGNORECASE,
)


def is_research_request(prompt: str) -> bool:
    prompt = prompt or ""
    return (
        RESEARCH_INTENT_RE.search(prompt) is not None
        or EXPLICIT_RESEARCH_ASK_RE.search(prompt) is not None
    )


class ReceiptError(RuntimeError):
    """A bounded, operator-safe receipt failure."""


def get_hermes_home() -> Path:
    """Resolve Hermes home without relying on repository-only imports.

    This file runs as a standalone script from the installed skill directory,
    where the Hermes repository root is not on ``sys.path``. Match the
    runtime's normal environment/default behavior.
    """

    configured = os.environ.get("HERMES_HOME", "").strip()
    return Path(configured) if configured else Path.home() / ".hermes"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _load_json_object(value: str, *, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ReceiptError(f"{label} was not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ReceiptError(f"{label} was not a JSON object")
    return parsed


def _tool_payload(value: str, *, label: str) -> dict[str, Any]:
    if not isinstance(value, str) or len(value) > MAX_TOOL_RESULT_CHARS:
        raise ReceiptError(f"{label} result was empty or oversized")
    stripped = value.strip()
    if stripped.startswith("<untrusted_tool_result"):
        first_newline = stripped.find("\n")
        end_marker = stripped.rfind("\n</untrusted_tool_result>")
        if first_newline < 0 or end_marker <= first_newline:
            raise ReceiptError(f"{label} result wrapper was malformed")
        stripped = stripped[first_newline + 1 : end_marker]
        object_start = stripped.find("{")
        if object_start < 0:
            raise ReceiptError(f"{label} result JSON was missing")
        stripped = stripped[object_start:]
    try:
        parsed, consumed = json.JSONDecoder().raw_decode(stripped)
    except json.JSONDecodeError as exc:
        raise ReceiptError(f"{label} result was not valid JSON") from exc
    if stripped[consumed:].strip() or not isinstance(parsed, dict):
        raise ReceiptError(f"{label} result was not one JSON object")
    return parsed


def _tool_result_was_truncated(value: str) -> bool:
    """Recognize only the runtime's exact bounded truncation envelopes."""

    if not isinstance(value, str) or not value.startswith("<untrusted_tool_result"):
        return False
    match = TRUNCATED_TOOL_RESULT_RE.search(value)
    if match is None:
        match = PERSISTED_TOOL_RESULT_RE.fullmatch(value)
        if match is None:
            return False
    original_chars = int(match.group("chars").replace(",", ""))
    return len(value) < original_chars <= MAX_TOOL_RESULT_CHARS


def _recoverable_controller_rejection_code(row: sqlite3.Row) -> str | None:
    """Recognize an audited controller rejection that executed no tool.

    A small model may request a forbidden terminal action or an invalid
    exact-two extraction and then recover. The controller persists that
    rejection for auditability. It must not be treated as an executed tool,
    but only an exact, typed controller signature is recoverable; malformed,
    ambiguous, unknown, or executed rows continue to fail closed.
    """

    tool_name = row["tool_name"]
    if not isinstance(tool_name, str) or not tool_name:
        return None
    content = row["content"]
    if not isinstance(content, str) or len(content) > 10_000:
        return None
    try:
        payload = _tool_payload(content, label="controller rejection")
    except ReceiptError:
        return None
    code = payload.get("code")
    expected = RECOVERABLE_CONTROLLER_REJECTION_SIGNATURES.get(
        (tool_name, code)
    ) or ANY_TOOL_RECOVERABLE_REJECTION_SIGNATURES.get(code)
    if (
        expected is not None
        and payload.get("status") == "rejected"
        and payload.get("executed") is False
        and payload.get("research_contract") is True
        and payload.get("terminal_failure") is expected["terminal_failure"]
        and payload.get("retryable") is expected["retryable"]
    ):
        return str(code)
    return None


def _safe_https_url(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        parsed = urlparse(value)
        username = parsed.username
        password = parsed.password
        hostname = parsed.hostname
    except ValueError:
        return None
    if parsed.scheme != "https" or not hostname or username:
        return None
    if password:
        return None
    return value


def _url_key(value: str) -> str:
    """Match extraction results to searched URLs without client-only fragments."""

    return urlparse(value)._replace(fragment="").geturl()


def _primary_source_like(value: str) -> bool:
    url = _safe_https_url(value)
    if url is None:
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    if any(token in host for token in SECONDARY_HOST_TOKENS):
        return False
    if host == "github.com" and NON_PRIMARY_GITHUB_PATH_RE.search(parsed.path):
        return False
    return True


def _call_map(rows: list[sqlite3.Row]) -> dict[str, dict[str, Any]]:
    calls: dict[str, dict[str, Any]] = {}
    for row in rows:
        raw_calls = row["tool_calls"]
        if row["role"] != "assistant" or not raw_calls:
            continue
        try:
            values = json.loads(raw_calls)
        except json.JSONDecodeError as exc:
            raise ReceiptError("assistant tool-call record was invalid") from exc
        if not isinstance(values, list):
            raise ReceiptError("assistant tool-call record was invalid")
        for value in values:
            if not isinstance(value, dict):
                continue
            call_id = value.get("id") or value.get("call_id")
            function = value.get("function")
            if not isinstance(call_id, str) or not isinstance(function, dict):
                continue
            name = function.get("name")
            arguments = function.get("arguments", "{}")
            if not isinstance(name, str) or not isinstance(arguments, str):
                continue
            calls[call_id] = {
                "name": name,
                "arguments": _load_json_object(
                    arguments, label=f"{name} tool arguments"
                ),
            }
    return calls


def _read_turn(home: Path, session_id: str) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
    database = home / "state.db"
    if database.is_symlink() or not database.is_file():
        raise ReceiptError("Hermes state database is unavailable")
    uri = database.resolve().as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        # The turn starts at the latest operator message.  Hermes persists its
        # own length/network continuation nudges as user rows; starting there
        # hid the turn's earlier searches (2026-10-08: "web_extract requested a
        # URL not returned by search" for a URL found before the nudge).
        user = next(
            (
                row
                for row in connection.execute(
                    """
                    SELECT id, role, content, timestamp
                    FROM messages
                    WHERE session_id = ? AND role = 'user'
                    ORDER BY id DESC
                    LIMIT 50
                    """,
                    (session_id,),
                )
                if not (
                    isinstance(row["content"], str)
                    and row["content"].strip().startswith(CONTINUATION_NUDGE_PREFIXES)
                )
            ),
            None,
        )
        if user is None or not isinstance(user["content"], str):
            raise ReceiptError("current research turn was not found")
        rows = connection.execute(
            """
            SELECT id, role, content, tool_call_id, tool_calls, tool_name, timestamp
            FROM messages
            WHERE session_id = ? AND id > ?
            ORDER BY id
            """,
            (session_id, user["id"]),
        ).fetchall()
    except sqlite3.Error as exc:
        raise ReceiptError("Hermes state database could not be read") from exc
    finally:
        if "connection" in locals():
            connection.close()
    return user, rows


def build_receipt(
    *,
    home: Path | None = None,
    session_id: str | None = None,
    now: dt.datetime | None = None,
) -> tuple[Path, dict[str, Any], str]:
    resolved_home = Path(home) if home is not None else get_hermes_home()
    resolved_session = session_id or os.environ.get("HERMES_SESSION_ID", "")
    if not SESSION_ID_RE.fullmatch(resolved_session):
        raise ReceiptError("current Hermes session identity is unavailable")

    user, rows = _read_turn(resolved_home, resolved_session)
    prompt = user["content"].strip()
    product_route_authenticated = (
        os.environ.get(PRODUCT_RESEARCH_ROUTE_ENV, "") == "1"
    )
    if (
        not is_research_request(prompt)
        and not product_route_authenticated
    ):
        raise ReceiptError("current turn is not a research request")
    # Product research has a typed controller-owned receipt command that sets
    # this exact environment flag.  Do not infer the route again from prose:
    # travel and other comparison prompts can also contain "compare",
    # "recommend", "options", and a dollar budget, but remain valid generic
    # two-source turns.
    is_product_request = product_route_authenticated
    primary_sources_required = (
        not is_product_request
        and PRIMARY_SOURCE_REQUEST_RE.search(prompt) is not None
    )

    all_tool_rows = [row for row in rows if row["role"] == "tool"]
    controller_rejections = [
        (row, code)
        for row in all_tool_rows
        if (code := _recoverable_controller_rejection_code(row)) is not None
    ]
    if len(controller_rejections) > MAX_RECOVERABLE_CONTROLLER_REJECTIONS:
        raise ReceiptError("too many rejected disallowed tool attempts")
    rejected_ids = {row["id"] for row, _code in controller_rejections}
    tool_rows = [row for row in all_tool_rows if row["id"] not in rejected_ids]
    tool_names = [row["tool_name"] for row in tool_rows]
    disallowed = sorted(
        {name for name in tool_names if name and name not in ALLOWED_PRIOR_TOOLS}
    )
    if disallowed:
        raise ReceiptError("research turn used a disallowed tool")
    search_rows = [row for row in tool_rows if row["tool_name"] == "web_search"]
    extract_rows = [row for row in tool_rows if row["tool_name"] == "web_extract"]
    max_search_calls = (
        MAX_PRODUCT_SEARCH_CALLS if is_product_request else MAX_SEARCH_CALLS
    )
    if not (1 <= len(search_rows) <= max_search_calls):
        if is_product_request:
            raise ReceiptError("product research must use one to six searches")
        raise ReceiptError("research turn must use one to five searches")
    if not (1 <= len(extract_rows) <= MAX_EXTRACT_CALLS):
        raise ReceiptError("research turn must use one to three extract calls")

    calls = _call_map(rows)
    searches: list[dict[str, Any]] = []
    discovered_urls: set[str] = set()
    for row in search_rows:
        call = calls.get(row["tool_call_id"])
        if call is None or call["name"] != "web_search":
            raise ReceiptError("web_search call/result binding was missing")
        query = call["arguments"].get("query")
        if not isinstance(query, str) or not query.strip():
            raise ReceiptError("web_search query was missing")
        content = row["content"] or ""
        payload = _tool_payload(content, label="web_search")
        data = payload.get("data")
        web = data.get("web") if isinstance(data, dict) else None
        admitted_urls: list[str] = []
        if isinstance(web, list):
            for value in web:
                if not isinstance(value, dict):
                    continue
                url = _safe_https_url(value.get("url"))
                if url is not None:
                    admitted_urls.append(url)
                    discovered_urls.add(_url_key(url))
        searches.append(
            {
                "tool_message_id": row["id"],
                "query_sha256": sha256_text(query.strip()),
                "discovered_urls": sorted(set(admitted_urls)),
                "response_sha256": sha256_text(content),
            }
        )
    if not discovered_urls:
        raise ReceiptError("searches returned no HTTPS source URLs")

    sources_by_url: dict[str, dict[str, Any]] = {}
    extract_batches: list[dict[str, Any]] = []
    for row in extract_rows:
        call = calls.get(row["tool_call_id"])
        if call is None or call["name"] != "web_extract":
            raise ReceiptError("web_extract call/result binding was missing")
        requested = call["arguments"].get("urls")
        if not isinstance(requested, list):
            raise ReceiptError("web_extract URL list was missing")
        requested_urls = [url for value in requested if (url := _safe_https_url(value))]
        if len(requested_urls) != len(requested) or not requested_urls:
            raise ReceiptError("web_extract URL list was invalid")
        requested_keys = {_url_key(url) for url in requested_urls}
        if any(key not in discovered_urls for key in requested_keys):
            raise ReceiptError("web_extract requested a URL not returned by search")
        content = row["content"] or ""
        truncated = False
        try:
            payload = _tool_payload(content, label="web_extract")
        except ReceiptError:
            if not _tool_result_was_truncated(content):
                raise
            # The persisted row cannot prove any source from this batch. Keep
            # the call in the audit trail with zero successes and let a later
            # complete extraction establish the receipt's source allowlist.
            truncated = True
            payload = {"results": []}
        results = payload.get("results")
        if not isinstance(results, list):
            raise ReceiptError("web_extract result list was missing")
        successful_urls: list[str] = []
        for value in results:
            if not isinstance(value, dict) or value.get("error"):
                continue
            url = _safe_https_url(value.get("url"))
            page = value.get("content")
            if url is None or _url_key(url) not in requested_keys:
                continue
            if not isinstance(page, str) or not page.strip():
                continue
            successful_urls.append(url)
            sources_by_url[url] = {
                "url": url,
                "title": str(value.get("title") or "")[:500],
                "content_sha256": sha256_text(page),
                "content_chars": len(page),
                "extract_tool_message_id": row["id"],
            }
        batch = {
            "tool_message_id": row["id"],
            "requested_urls": requested_urls,
            "successful_urls": sorted(set(successful_urls)),
            "response_sha256": sha256_text(content),
        }
        if truncated:
            batch["truncated"] = True
        extract_batches.append(batch)

    admitted_sources_by_url = {
        url: source
        for url, source in sources_by_url.items()
        if not primary_sources_required or _primary_source_like(url)
    }
    allowlist = sorted(admitted_sources_by_url)
    minimum_sources = (
        MIN_PRODUCT_SOURCES if is_product_request else MIN_SUCCESSFUL_SOURCES
    )
    if len(allowlist) < minimum_sources:
        if minimum_sources == MIN_PRODUCT_SOURCES:
            raise ReceiptError(
                "product research requires three distinct successfully extracted sources"
            )
        raise ReceiptError("fewer than two distinct sources extracted successfully")

    created = now or dt.datetime.now(dt.timezone.utc)
    if created.tzinfo is None:
        created = created.replace(tzinfo=dt.timezone.utc)
    receipt = {
        "schema_version": 1,
        "kind": "hermes-generic-research-receipt",
        "created_utc": created.astimezone(dt.timezone.utc).isoformat(),
        "session_id": resolved_session,
        "turn_user_message_id": user["id"],
        "turn_prompt_sha256": sha256_text(prompt),
        "searches": searches,
        "extract_batches": extract_batches,
        "sources": [admitted_sources_by_url[url] for url in allowlist],
        "final_citation_allowlist": allowlist,
        "primary_sources_required": primary_sources_required,
        "limits": {
            "search_calls": len(search_rows),
            "extract_calls": len(extract_rows),
            "delegation_calls": 0,
            "rejected_unexecuted_calls": len(controller_rejections),
        },
        "controller_rejections": [
            {
                "tool_message_id": row["id"],
                "tool_name": row["tool_name"],
                "code": code,
                "executed": False,
            }
            for row, code in controller_rejections
        ],
        "production_promotion_authorized": False,
    }
    payload = canonical_json_bytes(receipt)
    digest = hashlib.sha256(payload).hexdigest()

    receipt_root = resolved_home / "research-receipts"
    if receipt_root.is_symlink():
        raise ReceiptError("research receipt directory is linked")
    receipt_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    output = receipt_root / f"{resolved_session}--turn-{user['id']}.json"
    try:
        descriptor = os.open(
            output,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as exc:
        raise ReceiptError("research receipt already exists for this turn") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return output, receipt, digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture a bounded generic-research source receipt."
    )
    parser.parse_args(argv)
    try:
        _path, receipt, digest = build_receipt()
    except ReceiptError as exc:
        print(f"RESEARCH_RECEIPT_ERROR {exc}", file=sys.stderr)
        return 1
    print(
        "RESEARCH_RECEIPT_OK "
        f"receipt_sha256={digest} source_count={len(receipt['sources'])}"
    )
    for url in receipt["final_citation_allowlist"]:
        print(f"SOURCE {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
