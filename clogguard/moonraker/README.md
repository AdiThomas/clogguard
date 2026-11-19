# clogguard.moonraker

Async JSON-RPC 2.0 client for Moonraker's websocket (`ws://<pi>:7125/websocket`), wire format
as agreed in `PLAN.md` / the shared wire notes.

```python
client = MoonrakerClient("ws://127.0.0.1:7125/websocket", api_key_env="MOONRAKER_API_KEY")
await client.connect(retry=True)            # identify; API key read from the env var, never logged
status = await client.subscribe({"extruder": ["temperature", "power"], "print_stats": None})
async for diff, eventtime in client.updates():
    merge_status(status, diff)              # deep merge of notify_status_update diffs
result = await client.call("printer.objects.query", {"objects": {"webhooks": None}}, timeout=2)
await client.close()
```

- `call` matches responses by id; JSON-RPC errors raise `MoonrakerError` (`.code`, `.method`);
  timeouts raise `MoonrakerTimeoutError`; no connection raises `MoonrakerConnectionError`.
- Reconnects with exponential backoff; the last subscription is re-sent and its full status is
  pushed into `updates()` as one diff so merged state resynchronises.
- `klippy_events()` / `on_klippy_state(cb)` report `ready` / `shutdown` / `disconnected`.
- Satisfies the service's client protocol (`call`, `subscribe`, `updates`, `connected`).
- Generic `call` only: no G-code helpers. `clogguard.safety` is the only code that builds
  printer commands.
