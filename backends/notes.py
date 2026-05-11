"""Obsidian vault search via ripgrep.

Direct fs grep — no LLM, no indexing service, ~100–500ms typical. Returns
top-k matches as `[{path, snippet, score}]` where `score` is a crude
inverse rank (top result scores highest).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from pathlib import Path

log = logging.getLogger("ttyc.backends.notes")

OBSIDIAN_VAULT = Path(os.path.expanduser(os.getenv(
    "OBSIDIAN_VAULT",
    "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Obsidian Vault",
)))
RG = shutil.which("rg") or "/opt/homebrew/bin/rg"


async def search_notes(query: str, k: int = 5) -> list[dict] | dict:
    if not query or not query.strip():
        return {"error": "empty_query"}
    if not OBSIDIAN_VAULT.exists():
        return {"error": "vault_not_found"}
    if not Path(RG).exists():
        return {"error": "ripgrep_missing"}
    k = max(1, min(int(k), 20))
    # Case-insensitive, fixed-string match, plus 1 line of context. Limit
    # max-count to k matches per file so a noisy file doesn't dominate.
    args = [
        RG, "--no-heading", "--with-filename", "--line-number",
        "--smart-case", "--fixed-strings", "--max-count", str(k),
        "--max-columns", "200",
        "--type-add", "md:*.md", "-tmd",
        query.strip(), str(OBSIDIAN_VAULT),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=6.0)
        except asyncio.TimeoutError:
            proc.kill()
            return {"error": "ripgrep_timeout"}
    except Exception as e:  # noqa: BLE001
        log.warning("ripgrep failed: %s", e)
        return {"error": "ripgrep_failed"}
    out: list[dict] = []
    seen_paths: set[str] = set()
    line_re = re.compile(r"^(?P<path>.+?):(?P<line>\d+):(?P<text>.*)$")
    for raw in stdout.decode(errors="replace").splitlines():
        m = line_re.match(raw)
        if not m:
            continue
        path = m.group("path")
        text = m.group("text").strip()
        # Take the first match per file (already capped via --max-count, but
        # we still rank by first hit to avoid duplicate paths in the output).
        if path in seen_paths:
            continue
        seen_paths.add(path)
        rel = path
        try:
            rel = str(Path(path).relative_to(OBSIDIAN_VAULT))
        except ValueError:
            pass
        out.append({
            "path": rel,
            "snippet": text[:280],
            "score": 0,  # placeholder; rank below
        })
        if len(out) >= k:
            break
    for i, hit in enumerate(out):
        hit["score"] = round(1.0 - (i / max(1, len(out))), 3)
    return out
