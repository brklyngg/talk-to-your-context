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
        ask_count = ask_done = ask_err = 0
        end_reason = None
        for evt in iter_call_events(base_log_dir, conv_id):
            name = evt.get("event")
            if name == "session_minted" and started_at is None:
                started_at = evt.get("ts")
            elif name == "ask_agent_spawned":
                ask_count += 1
            elif name == "ask_agent_done":
                ask_done += 1
            elif name == "tool_error":
                ask_err += 1
            elif name in ("ended", "reaped"):
                ended_at = evt.get("ts")
                end_reason = evt.get("reason")
        out.append({
            "conv_id": conv_id,
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_s": (ended_at - started_at) if started_at and ended_at else None,
            "ask_count": ask_count,
            "ask_done": ask_done,
            "ask_err": ask_err,
            "end_reason": end_reason,
        })
    return out


if __name__ == "__main__":
    import sys
    base = Path(__file__).parent / "logs"
    rows = summarize_calls(base)
    json.dump(rows, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
