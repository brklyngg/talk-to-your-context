"""Direct-backend dispatch for the granular tool layer.

Each module exposes pure async functions that hit a real data source —
Supabase, gws-MCP, the Obsidian vault — and return structured JSON. No
LLM in the dispatch path; the latency floor is the backend itself, not a
chat-completion round-trip.

Designed so the same functions can be re-exposed via remote MCP in Phase D
without changes (`server.py` registers HTTP routes today; an MCP server
would register them as MCP tools).
"""
