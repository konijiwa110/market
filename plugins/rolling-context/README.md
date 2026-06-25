# Rolling Context for Claude Code

A transparent proxy that gives Claude Code **rolling context compression** — old messages get automatically summarized while recent messages stay fully verbatim. You never hit the context wall, and you never lose important details. Pure Python stdlib, zero dependencies.

This build adds **third-party baseURL support**: it can route through any upstream (not just `api.anthropic.com`), with a config-first, environment-fallback policy for both the upstream and the auth key.

> Claude Code's built-in `/compact` replaces your **entire** conversation with a lossy summary. After a few compactions, you're summarizing a summary of a summary. This plugin only compresses old messages — recent context stays untouched.

## How It Works

```
Claude Code  ──►  Rolling Context Proxy (:5588)  ──►  Upstream API
                         │
                         ├─ context < trigger? pass through unchanged
                         │
                         └─ context > trigger?
                              1. summarize old messages with Haiku (background, async)
                              2. keep recent messages verbatim
                              3. inject compressed context on next request
                              4. never blocks, never adds latency
```

1. **Keeps recent messages untouched** — recent context stays verbatim
2. **Only compresses when needed** — triggers at the real API token count, compresses old messages, grows until the next trigger
3. **Merges summaries** — each cycle merges with the previous summary into a rolling timeline
4. **Never blocks** — compression runs in the background, applied on the next request
5. **Full transcripts preserved** — Claude Code still saves everything to JSONL in `~/.claude/projects/`

## Install

```
/plugin marketplace add konijiwa110/market
/plugin install rolling-context@konijiwa-plugin
```

Or from the terminal:

```
claude plugin marketplace add konijiwa110/market
claude plugin install rolling-context@konijiwa-plugin
```

On the **first start**, the SessionStart hook configures `ANTHROPIC_BASE_URL` and starts the proxy. Since the env var only takes effect on the next terminal, **restart your terminal once** — after that everything works automatically. Requires Python 3.7+ (no pip install needed — pure stdlib).

## Configuration

Drop a `~/.claude/rolling-context.json` to override defaults. Every key is **config-first, environment-fallback**: if it's in the file it wins, otherwise it's read from the environment, otherwise the default applies.

| Item | config key | environment fallback |
|------|------------|----------------------|
| Upstream baseURL | `upstream` | `ROLLING_CONTEXT_UPSTREAM` → `settings.json` `ANTHROPIC_BASE_URL` (auto-chain) |
| Auth key | `apikey` | `ROLLING_CONTEXT_APIKEY` → Claude Code's passed-through `ANTHROPIC_AUTH_TOKEN` |
| Trigger tokens | `trigger` | `ROLLING_CONTEXT_TRIGGER` (default `160000`) |
| Target tokens kept | `target` | `ROLLING_CONTEXT_TARGET` (default `40000`) |
| Summarizer model | `model` | `ROLLING_CONTEXT_MODEL` (default `claude-haiku-4-5-20251001`) |
| Listen port | `port` | `ROLLING_CONTEXT_PORT` (default `5588`) |

**The default config does not include `upstream` or `apikey`** — they follow your environment (`settings.json`'s `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN`), so switching environments or accounts is picked up automatically on the next session. Only pin them in `rolling-context.json` when you want a fixed third-party endpoint that does not vary with the environment. Copy [`rolling-context.example.json`](./rolling-context.example.json) to get started — it holds only the environment-independent compression preferences:

```json
{
  "trigger": 160000,
  "target": 40000,
  "model": "claude-haiku-4-5-20251001"
}
```

When `upstream` is set in the config, the hook treats it as authoritative and stops deriving the upstream from the environment. When the config file is absent, behavior matches the upstream original (backward compatible): if `ANTHROPIC_BASE_URL` is already set at install, it's saved as `ROLLING_CONTEXT_UPSTREAM` and the proxy inserts itself in front (proxy chaining).

The summarizer (Haiku) call goes through the **same** upstream and key as the main traffic. (The upstream original only read the summarizer endpoint from environment variables the proxy process never receives, so for any non-Anthropic baseURL the summary call would always hit `api.anthropic.com` and fail — fixed here.)

## How Compression Works

When the message array exceeds the trigger threshold:

```
BEFORE (hit trigger):
  [msg1] [msg2] ... [msg60] [msg61] ... [msg100]
  |<————————————— exceeds trigger —————————————>|

AFTER (compressed):
  [rolling summary] [ack] [msg61] ... [msg100]
  |<— small summary —>|    |<—— verbatim ——————>|
```

The summary preserves a structured record: active goal, previous goals, a chronological timeline of every file change/decision/error/user instruction, current state, and key details (paths, configs). User instructions are never lost.

## Architecture

The proxy is **fully stateless** — no sessions, no databases. It hashes message content: when a response returns with a high token count it compresses and stores the result keyed by content hashes; on the next request it hashes incoming messages and swaps in a match transparently. Multiple conversations, subagents, and branches all work automatically; restart the proxy anytime.

## Health Check / Debug

```
curl http://127.0.0.1:5588/health
curl http://127.0.0.1:5588/debug/compressions
```

## Uninstall

```
claude plugin uninstall rolling-context@konijiwa-plugin
```

## License

MIT — see [LICENSE](./LICENSE). This is a fork of an MIT-licensed project; the original copyright notice is retained in `LICENSE` per the license terms.
