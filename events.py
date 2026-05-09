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
    """Derive Phase-0 routing metrics from the per-call NDJSON event stream.

    A "voice turn" is one user utterance, signalled by ``client_user_turn``
    (fired client-side on input_audio_transcription.completed). We count
    user turns rather than Realtime ``response.done`` events because the
    Realtime API emits two ``response.done`` per ask_agent turn (one for
    the function_call, one for speaking the answer back) - that would
    double-count.

    ``local_answer_turns = max(0, voice_turns - ask_agent_count)``: turns
    where no ask_agent dispatched. Goes slightly off if the model loops on
    multiple ask_agent calls in one turn; max() floors to zero.

    Latency stats include only successful (non-error, non-cancelled) ask_agent
    runs to avoid mixing in fast-fail timings.
    """
    voice_turns = 0
    ask_agent_count = 0
    ask_agent_done = 0
    ask_agent_err = 0
    intent_counts: dict[str, int] = {}
    freshness_required_count = 0
    latencies_ms: list[int] = []
    for evt in iter_call_events(base_log_dir, conv_id):
        name = evt.get("event")
        if name == "client_user_turn":
            voice_turns += 1
        elif name == "ask_agent_spawned":
            ask_agent_count += 1
            it = evt.get("intent_type") or "unknown"
            intent_counts[it] = intent_counts.get(it, 0) + 1
            if evt.get("freshness_required"):
                freshness_required_count += 1
        elif name == "ask_agent_done":
            if evt.get("status") == "done":
                ask_agent_done += 1
                lm = evt.get("latency_ms")
                if isinstance(lm, (int, float)):
                    latencies_ms.append(int(lm))
            else:
                ask_agent_err += 1
        elif name == "tool_error":
            ask_agent_err += 1
    local_answer_turns = max(0, voice_turns - ask_agent_count)
    ask_ratio = (ask_agent_count / voice_turns) if voice_turns else None
    latency_avg = round(sum(latencies_ms) / len(latencies_ms)) if latencies_ms else None
    latency_p95 = sorted(latencies_ms)[int(len(latencies_ms) * 0.95)] if len(latencies_ms) >= 5 else None
    return {
        "voice_turns": voice_turns,
        "local_answer_turns": local_answer_turns,
        "ask_agent_count": ask_agent_count,
        "ask_agent_done": ask_agent_done,
        "ask_agent_err": ask_agent_err,
        "ask_ratio": round(ask_ratio, 3) if ask_ratio is not None else None,
        "intent_counts": intent_counts,
        "freshness_required_count": freshness_required_count,
        "ask_latency_ms_avg": latency_avg,
        "ask_latency_ms_p95": latency_p95,
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
