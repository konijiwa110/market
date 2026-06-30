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

### Prerequisite: Python 3.7+ on your PATH

The proxy **is** a Python script (pure stdlib — no `pip install`, no venv), launched by the SessionStart hook. You just need a Python interpreter reachable on your `PATH`:

- **macOS / Linux** — almost always already present (`python3`). Nothing to do.
- **Windows** — install from [python.org](https://www.python.org/downloads/) and tick *"Add python.exe to PATH"*. **Avoid the Microsoft Store `python3` stub**: it registers a hollow shim that *looks* installed (`where python` finds it) but runs nothing (or pops the Store), which the hook treats as "no Python". Verify with:

  ```
  python -c "print('ok')"
  ```

  If that prints `ok`, you're set.

**No Python? Nothing breaks.** The plugin detects the missing interpreter, disables itself, and fails open — Claude Code keeps working against your real upstream, just without rolling-context compression (see `~/.claude/rolling-context-hook.log` for a one-line note). Install Python and restart the terminal to enable compression.

```
/plugin marketplace add konijiwa110/market
/plugin install rolling-context@konijiwa-plugin
```

Or from the terminal:

```
claude plugin marketplace add konijiwa110/market
claude plugin install rolling-context@konijiwa-plugin
```

On the **first start**, the SessionStart hook configures `ANTHROPIC_BASE_URL` and starts the proxy. Since the env var only takes effect on the next terminal, **restart your terminal once** — after that everything works automatically.

### Troubleshooting: `marketplace add` fails with "Host key verification failed"

If `marketplace add` errors with `SSH host key is not in your known_hosts` / `No ED25519 host key is known for github.com` / `Host key verification failed`, the machine is cloning over **SSH** (`git@github.com`) and has never accepted GitHub's host key. The trailing `make sure you have the correct access rights` line is generic git boilerplate — this is **not** an access-rights problem. This is a public repo, so use HTTPS instead:

```
/plugin marketplace add https://github.com/konijiwa110/market.git
```

If the explicit HTTPS URL is *still* rewritten to SSH, a global git config is forcing the rewrite. Check and remove it:

```
git config --global --get-regexp insteadOf
git config --global --unset url.git@github.com:.insteadOf
```

(Alternatively, to keep using SSH, register GitHub's host key once: `ssh-keyscan -t ed25519,rsa github.com >> ~/.ssh/known_hosts`.)

## Updating & lifecycle

The proxy runs from Claude Code's plugin **cache** (`…/rolling-context/<version>/proxy`), and Claude Code owns its lifecycle — the SessionStart hook just makes sure it's running.

- **Update**: `/plugin update rolling-context@konijiwa-plugin` pulls the new version into the cache; the next session's hook sees the newer version and restarts the proxy onto it. No manual steps, no terminal restart.
- **Fail-open**: the hook points `ANTHROPIC_BASE_URL` at the local proxy **only after** it answers `/health`. If the proxy can't start, the hook falls back to your real upstream so Claude Code keeps working (just without compression) instead of stalling on a dead port.
- **Singleton (bind-as-lock)**: the proxy holds the port as its lock — concurrent sessions share one instance, and a duplicate launch exits cleanly rather than racing. On bind it self-writes an authoritative pidfile + version, so liveness/version is read from the live `/health`, never guessed.
- **Dev mode (authors only)**: set `ROLLING_CONTEXT_DEV=<repo-root>` to run the proxy straight from a working clone instead of the cache — iterate on `proxy/server.py` without `/plugin update`. Unset it to return to the normal cached lifecycle. `hooks/refresh-proxy.{ps1,sh}` restarts the local gateway in place after a code edit.

## Configuration

Drop a `~/.claude/rolling-context.json` to override defaults. Every key is **config-first, environment-fallback**: if it's in the file it wins, otherwise it's read from the environment, otherwise the default applies.

| Item | config key | environment fallback |
|------|------------|----------------------|
| Upstream baseURL | `upstream` | `ROLLING_CONTEXT_UPSTREAM` → `settings.json` `ANTHROPIC_BASE_URL` (auto-chain) |
| Auth key | `apikey` | `ROLLING_CONTEXT_APIKEY` → Claude Code's passed-through `ANTHROPIC_AUTH_TOKEN` |
| Trigger tokens | `trigger` | `ROLLING_CONTEXT_TRIGGER` (default `160000`) |
| Target tokens kept | `target` | `ROLLING_CONTEXT_TARGET` (default `40000`) |
| Summarizer model | `model` | `ROLLING_CONTEXT_MODEL` (default `claude-haiku-4-5-20251001`) |
| Context window override | `context_window` | `ROLLING_CONTEXT_CONTEXT_WINDOW` (default `0` = auto-detect from the `anthropic-beta` header) |
| Proactive compression | `proactive_compress` | `ROLLING_CONTEXT_PROACTIVE_COMPRESS` (default `1` = on) |
| Emergency compression | `emergency_compress` | `ROLLING_CONTEXT_EMERGENCY_COMPRESS` (default `1` = on) |
| Client disguise | `disguise_client` | `ROLLING_CONTEXT_DISGUISE` (default `1` = on). Proxy-initiated requests (compression / emergency) reuse the client headers (UA / `x-app` / `x-stainless-*` / `anthropic-*`) of the most recent over-`target` request so they look like genuine Claude Code traffic to the upstream `claude_code_only` check; auth headers always come from the live request. Set `0` to pass through the triggering request's own headers instead. |
| Listen port | `port` | `ROLLING_CONTEXT_PORT` (default `5588`) |
| Listen host | `host` | `ROLLING_CONTEXT_HOST` (default `127.0.0.1`, loopback only). Set `0.0.0.0` to let other devices on your LAN reach the proxy/dashboard. ⚠️ The proxy forwards your `ANTHROPIC_AUTH_TOKEN`, so anyone who can reach the port can spend your key and read the traffic — only expose it on a trusted network, never the public internet. |

The proxy can't see your model's real context limit, only the request body — so the effective trigger is automatically capped at **90% of the detected window** so a `trigger` set above your real limit can't silently disable compression. The window is detected per request from the `anthropic-beta` header (`context-1m-*` → 1M, otherwise 200k, matching how Claude Code's `model[1m]` alias works). Set `context_window` only when a third-party endpoint advertises 1M via the header but actually caps lower — pin it (e.g. `200000`) to override the header detection.

**Proactive vs. emergency compression** are two safety nets, both on by default. *Proactive* fires **before** forwarding: when a cache-miss request is estimated (from body size) to exceed the effective trigger, the proxy compresses it synchronously and forwards the shrunk body — this kills the cold-start stall on `resume`, where the first request would otherwise ship the entire transcript to a cold upstream cache (tens of seconds to minutes) before any background compression can land. *Emergency* fires **after** an upstream `400 prompt too long`: it compresses and retries once so Claude Code never sees the 400. Leave both on unless you're debugging the raw forwarding path.

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
curl http://127.0.0.1:5588/health             # {"status":"ok","version":"1.9.0","pid":12345, ...}
curl http://127.0.0.1:5588/debug/compressions
```

`/health` reports the running `version` and `pid` — the same fields the SessionStart hook uses to decide reuse-vs-restart and fail-open. A live dashboard is also served at `http://127.0.0.1:5588/stats`.

## Uninstall

```
claude plugin uninstall rolling-context@konijiwa-plugin
```

## License

MIT — see [LICENSE](./LICENSE). This is a fork of an MIT-licensed project; the original copyright notice is retained in `LICENSE` per the license terms.
