"""Per-call NDJSON event log.

One file per conversation: ``logs/calls/<conv_id>.ndjson``. Append-only,
``cat | jq``-friendly. Designed so the agent (via its bash skill) and Claude
Code can both grep call history without parsing transcripts or wading through
``server.log``.

For cross-call queries, ``summarize_calls()`` derives an index on demand --
no real-time index file to keep consistent.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger("ttyc.events")


def calls_dir(base_log_dir: Path) -> Path:
    d = base_log_dir / "calls"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_call_event(base_log_dir: Path, conv_id: str, event: str, **fields: Any) -> None:
    """Append one structured event to the per-call NDJSON file.

    Never raises -- diagnostic logging must not break the live path.
    """
    if not conv_id:
        return
    record = {"ts": time.time(), "conv_id": conv_id, "event": event, **fields}
    try:
        path = calls_dir(base_log_dir) / f"{conv_id}.ndjson"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:  # noqa: BLE001
        log.exception("event log write failed (conv=%s event=%s)", conv_id, event)


def iter_call_events(base_log_dir: Path, conv_id: str) -> Iterator[dict]:
    path = calls_dir(base_log_dir) / f"{conv_id}.ndjson"
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def compute_routing_metrics(base_log_dir: Path, conv_id: str) -> dict:
    """Derive routing metrics from the per-call NDJSON event stream.

    A "voice turn" is one user utterance (``client_user_turn``).

    Headline metrics post-cutover:
      - ``tool_calls`` and per-tool counts (narrow tools + ``deep_research``)
      - ``ask_ratio`` = any tool call / voice turn (escalation rate)
      - ``deep_research_ratio`` = deep_research / voice turn
      - ``in_context_followup_rate`` = of user turns *immediately following*
        any tool answer, fraction served WITHOUT another tool call. This is
        the qualitative "did the model carry the prior tool result forward"
        signal — the central regression test for the dossier + toolkit
        refactor. Targets: ≥0.6 dossier-only, ≥0.8 full toolkit.

    Legacy ``ask_agent_*`` fields preserved so old transcripts still parse;
    they now coalesce ``ask_agent_done`` + ``tool_call_done`` so a pre-cutover
    call still summarizes coherently.
    """
    voice_turns = 0
    tool_calls = 0
    deep_research_count = 0
    tool_counts: dict[str, int] = {}
    tool_latencies: dict[str, list[int]] = {}
    tool_err_count = 0
    # Followup tracking: ordered event stream gives us the "did a tool turn
    # immediately precede this user turn, and did that user turn dispatch
    # another tool?" question.
    followup_total = 0           # user turns following a tool answer
    followup_in_context = 0      # of those, ones with no fresh tool call
    last_user_was_after_tool = False
    user_turn_dispatched_tool = False
    user_just_started = False    # gate: did we just see a client_user_turn?
    # Legacy ask_agent counters
    ask_agent_count = 0
    ask_agent_done = 0
    ask_agent_err = 0
    intent_counts: dict[str, int] = {}
    freshness_required_count = 0
    ask_latencies_ms: list[int] = []

    def _finalize_user_turn():
        nonlocal followup_total, followup_in_context, last_user_was_after_tool
        if last_user_was_after_tool:
            followup_total += 1
            if not user_turn_dispatched_tool:
                followup_in_context += 1

    for evt in iter_call_events(base_log_dir, conv_id):
        name = evt.get("event")
        if name == "client_user_turn":
            # Finalize the previous user turn before opening the next one.
            if user_just_started:
                _finalize_user_turn()
            voice_turns += 1
            user_just_started = True
            user_turn_dispatched_tool = False
            # last_user_was_after_tool is "set on the most recent tool_answer";
            # don't reset here.
        elif name == "tool_call_spawned":
            tool_calls += 1
            tool = evt.get("tool") or "unknown"
            tool_counts[tool] = tool_counts.get(tool, 0) + 1
            user_turn_dispatched_tool = True
        elif name == "tool_call_done":
            tool = evt.get("tool") or "unknown"
            lm = evt.get("latency_ms")
            if isinstance(lm, (int, float)):
                tool_latencies.setdefault(tool, []).append(int(lm))
            if evt.get("error_type"):
                tool_err_count += 1
            last_user_was_after_tool = True
        elif name == "deep_research_spawned":
            tool_calls += 1
            deep_research_count += 1
            user_turn_dispatched_tool = True
        elif name == "deep_research_done":
            lm = evt.get("latency_ms")
            if isinstance(lm, (int, float)):
                tool_latencies.setdefault("deep_research", []).append(int(lm))
            if evt.get("status") != "done":
                tool_err_count += 1
            last_user_was_after_tool = True
        # Legacy ask_agent events — kept so historical transcripts still
        # compute. New code emits tool_call_* / deep_research_* exclusively.
        elif name == "ask_agent_spawned":
            ask_agent_count += 1
            tool_calls += 1
            user_turn_dispatched_tool = True
            it = evt.get("intent_type") or "unknown"
            intent_counts[it] = intent_counts.get(it, 0) + 1
            if evt.get("freshness_required"):
                freshness_required_count += 1
        elif name == "ask_agent_done":
            if evt.get("status") == "done":
                ask_agent_done += 1
                lm = evt.get("latency_ms")
                if isinstance(lm, (int, float)):
                    ask_latencies_ms.append(int(lm))
            else:
                ask_agent_err += 1
            last_user_was_after_tool = True
        elif name == "tool_error":
            tool_err_count += 1
    # Finalize the trailing user turn (if any).
    if user_just_started:
        _finalize_user_turn()

    local_answer_turns = max(0, voice_turns - tool_calls)
    ask_ratio = (tool_calls / voice_turns) if voice_turns else None
    deep_ratio = (deep_research_count / voice_turns) if voice_turns else None
    in_context_followup_rate = (
        followup_in_context / followup_total if followup_total else None
    )
    # Per-tool latency stats
    per_tool_latency_avg: dict[str, int] = {}
    per_tool_latency_p95: dict[str, int] = {}
    for tool, vals in tool_latencies.items():
        per_tool_latency_avg[tool] = round(sum(vals) / len(vals))
        if len(vals) >= 5:
            per_tool_latency_p95[tool] = sorted(vals)[int(len(vals) * 0.95)]
    ask_latency_avg = (
        round(sum(ask_latencies_ms) / len(ask_latencies_ms)) if ask_latencies_ms else None
    )
    ask_latency_p95 = (
        sorted(ask_latencies_ms)[int(len(ask_latencies_ms) * 0.95)]
        if len(ask_latencies_ms) >= 5 else None
    )
    return {
        "voice_turns": voice_turns,
        "local_answer_turns": local_answer_turns,
        "tool_calls": tool_calls,
        "tool_counts": tool_counts,
        "tool_err_count": tool_err_count,
        "deep_research_count": deep_research_count,
        "ask_ratio": round(ask_ratio, 3) if ask_ratio is not None else None,
        "deep_research_ratio": round(deep_ratio, 3) if deep_ratio is not None else None,
        "in_context_followup_rate": (
            round(in_context_followup_rate, 3) if in_context_followup_rate is not None else None
        ),
        "in_context_followup_n": followup_total,
        "per_tool_latency_ms_avg": per_tool_latency_avg,
        "per_tool_latency_ms_p95": per_tool_latency_p95,
        # Legacy fields — preserved for back-compat with old transcripts.
        "ask_agent_count": ask_agent_count or tool_calls,
        "ask_agent_done": ask_agent_done,
        "ask_agent_err": ask_agent_err,
        "intent_counts": intent_counts,
        "freshness_required_count": freshness_required_count,
        "ask_latency_ms_avg": ask_latency_avg,
        "ask_latency_ms_p95": ask_latency_p95,
    }


def summarize_calls(base_log_dir: Path) -> list[dict]:
    """Walk every per-call NDJSON file and emit one summary row per conv.

    Used for cross-call queries. At ~30 calls/day this scans in milliseconds;
    no need for a maintained index.
    """
    out: list[dict] = []
    d = calls_dir(base_log_dir)
    for path in sorted(d.glob("*.ndjson")):
        conv_id = path.stem
        started_at = ended_at = None
        end_reason = None
        for evt in iter_call_events(base_log_dir, conv_id):
            name = evt.get("event")
            if name == "session_minted" and started_at is None:
                started_at = evt.get("ts")
            elif name in ("ended", "reaped"):
                ended_at = evt.get("ts")
                end_reason = evt.get("reason")
        metrics = compute_routing_metrics(base_log_dir, conv_id)
        out.append({
            "conv_id": conv_id,
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_s": (ended_at - started_at) if started_at and ended_at else None,
            "end_reason": end_reason,
            **metrics,
        })
    return out


if __name__ == "__main__":
    import sys
    base = Path(__file__).parent / "logs"
    rows = summarize_calls(base)
    json.dump(rows, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
