"""IP-allowlist middleware for the voice sidecar.

Only requests originating from CIDR ranges in VOICE_ALLOWED_CIDR are served.
Default is loopback only. To expose on a private overlay network (e.g.,
Tailscale's 100.64.0.0/10 CGNAT block), add that CIDR to VOICE_ALLOWED_CIDR
in your .env. Anything outside the allowlist gets a 403 before request
handlers run.
"""
from __future__ import annotations

import ipaddress
import logging
import os
from typing import List

from aiohttp import web

log = logging.getLogger("ttyc.auth")


def _parse_cidrs(raw: str) -> List[ipaddress._BaseNetwork]:
    nets: List[ipaddress._BaseNetwork] = []
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            nets.append(ipaddress.ip_network(chunk, strict=False))
        except ValueError:
            log.warning("ignoring invalid CIDR %r", chunk)
    return nets


# Default: loopback only. Example for Tailscale: "100.64.0.0/10,127.0.0.1/32,::1/128".
_ALLOWED = _parse_cidrs(os.getenv("VOICE_ALLOWED_CIDR", "127.0.0.1/32,::1/128"))


def _peer_ip(request: web.Request) -> str | None:
    peer = request.transport.get_extra_info("peername") if request.transport else None
    return peer[0] if peer else None


def is_allowed(request: web.Request) -> bool:
    ip = _peer_ip(request)
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in _ALLOWED)


@web.middleware
async def tailnet_middleware(request: web.Request, handler):
    # Allow `/api/health` from anywhere - used by launchd / monitoring.
    if request.path == "/api/health":
        return await handler(request)
    if not is_allowed(request):
        ip = _peer_ip(request) or "?"
        log.warning("denied %s from %s", request.path, ip)
        return web.json_response({"error": "forbidden"}, status=403)
    return await handler(request)
