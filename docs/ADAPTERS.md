# Adapters

> TODO: fill in.

The sidecar has one optional outbound integration today: post a finished call's transcript to a messaging channel. The shape is intentionally narrow so other backends can drop in.

## Adapter interface (concept)

```python
async def post_call(
    *,
    conv_id: str,
    started_at: float,
    ended_at: float,
    entries: list[dict],   # {role, text, ts}
    config: dict,          # adapter-specific (token, channel, etc.)
) -> None:
    ...
```

The current Slack implementation lives inline in `transcripts.py::post_to_slack`. Future adapters should expose the same callable signature so `server.py` can dispatch by config.

## Slack

Implemented. See `transcripts.py` and the `SLACK_BOT_TOKEN` / `SLACK_CALL_CHANNEL_ID` env vars in `.env.example`.

## Telegram

> TODO: implement. Bot API `sendMessage` to a fixed chat; chunk on entry boundaries to fit Telegram's 4096-char limit.

## Discord

> TODO: implement. Webhook POST; chunk to fit 2000-char message limit.

## Matrix

> TODO: implement. Client-Server API `room/{roomId}/send/m.room.message`.

## Email

> TODO: implement. Single message per call; transcript inline as preformatted text.
