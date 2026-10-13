# Copilot Gateway

A local, OpenAI-compatible `/v1/chat/completions` endpoint backed by Microsoft 365 Copilot Web.
A Playwright-driven browser sends each prompt to Copilot and returns the reply.

This is a personal workaround for environments that block LLM APIs but allow the Copilot web app.
It is for local use on `127.0.0.1` only.

> **Before you use it:** this drives the Copilot web UI with automation, and the
> company may have blocked LLM API access on purpose. Check with IT or security first.

## Status

- The browser logic, API, and retry paths are covered by tests against a **mock** Copilot page.
- **The real Copilot page has not been tested.** The CSS selectors in
  `copilot_gateway/browser/selectors.py` are guesses. Verify them first (see
  [First run](#first-run)).

## Setup

Requires Python 3.11+ and Chromium.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium      # or set CHROMIUM_PATH to an existing Chromium binary
```

Create your local settings file from the template. `.env` is git-ignored, so it stays on your machine:

```powershell
Copy-Item .env.example .env      # Windows PowerShell
```

Edit `.env` to change the port or the headless setting. Real environment variables override it, so
close any shell where you ran `$env:HEADLESS = ...` before.

## First run

```bash
python -m copilot_gateway
```

The gateway opens Copilot in a visible browser window using the profile in `playwright-profile/` at
the repo root. Sign in there (SSO and MFA happen in that window).

With `HIDE_WHEN_READY=true`, the window moves off-screen after Copilot is ready, once you have signed
in to Copilot and to the proxy. Its rendering keeps running, so requests still work. If the gateway
later needs you to sign in again, it restarts the browser, and the window comes back on its own.

Cookies that have an expiry survive restarts. Session-only cookies are dropped when the browser
closes. So when Microsoft asks **Stay signed in?**, choose **Yes**. Do the same for any proxy sign-in
that offers it. Stop the gateway with Ctrl+C, not by closing the window, so the browser shuts down
cleanly. `GET /health` reports `browser_ready: true` once the chat
box is found.

If a selector is wrong, the gateway logs an error and saves a screenshot to
`copilot_gateway/storage/debug/`. Fix the entry in `selectors.py` and restart.

## Try it

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"copilot-web","messages":[{"role":"user","content":"hello"}]}'
```

## Connect a coding agent

No changes to the agent's source code are needed. Point its OpenAI base URL at the gateway.
These configs have **not been tested** against the real tools or the real Copilot page.

**Aider**

```bash
aider --openai-api-base http://127.0.0.1:8000/v1 --openai-api-key dummy --model openai/copilot-web
```

**Continue** (`config.yaml`)

```yaml
models:
  - name: Copilot Web
    provider: openai
    model: copilot-web
    apiBase: http://127.0.0.1:8000/v1
    apiKey: dummy
```

**OpenCode** (`opencode.json`, OpenAI-compatible provider)

```json
{
  "provider": {
    "copilot": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Copilot Web",
      "options": { "baseURL": "http://127.0.0.1:8000/v1" },
      "models": { "copilot-web": { "name": "Copilot Web" } }
    }
  }
}
```

To keep each agent conversation in its own Copilot thread, send an `X-Session-Id` header or the
`user` field. Without either, the gateway fingerprints the first message of the conversation.

## Configuration

Settings come from `.env` in the repo root (copied from `.env.example`). The table lists the defaults
used when a variable is missing.

| Variable | Default | Purpose |
|---|---|---|
| `GATEWAY_HOST` / `GATEWAY_PORT` | `127.0.0.1` / `8000` | Must be a loopback address |
| `COPILOT_URL` | `https://copilot.cloud.microsoft/` | Copilot page to drive |
| `PLAYWRIGHT_PROFILE_DIR` | `playwright-profile/` in the repo root | Persistent browser profile (cookies, sign-in) |
| `HEADLESS` | `false` | Keep `false` for the first sign-in |
| `PROXY_SERVER` | unset | Corporate proxy, for example `http://proxy.example.com:8080`. Unset means no proxy |
| `PROXY_USERNAME` / `PROXY_PASSWORD` | unset | Proxy login. Passing them lets the browser pass the proxy without a popup |
| `HIDE_WHEN_READY` | `false` | With `HEADLESS=false`, move the window off-screen once Copilot is ready. It reappears if sign-in is needed again |
| `CHROMIUM_PATH` | unset | Use an existing Chromium binary |
| `REQUEST_TIMEOUT` | `300` | Seconds for one request, retries included |
| `MAX_RETRY` | `3` | Retries after the first attempt, each on a fresh thread |
| `LOGIN_TIMEOUT` | `600` | Seconds to wait for sign-in |
| `RESPONSE_STABLE_SECONDS` | `3` | Reply counts as finished after this long without change |
| `MAX_INLINE_CHARS` | `30000` | Longer prompts are sent as an attached `conversation.txt` |
| `MAX_FILE_BYTES` | `20971520` | Per-file limit for attachments |

## How it works

- **One session, one thread.** The session key (the `X-Session-Id` header, the `user` field, or a
  fingerprint of the first message) maps to one Copilot thread URL in
  `copilot_gateway/storage/sessions.json`. Each request sends only the user turns the thread has not
  seen yet. A new session key, or a session whose thread URL was never recorded, starts a new thread
  with the full prompt.
- Without `X-Session-Id`, requests whose first message is identical share a thread. Send a distinct
  `X-Session-Id` to keep unrelated conversations apart.
- **Prompt format.** New threads get `SYSTEM / CONVERSATION / USER: / ASSISTANT:` as one prompt.
- **Files.** Content parts of type `file` (`.py .md .txt .docx .pdf .json .yaml .yml`) are
  attached to Copilot through its file input. The gateway does not parse them.
- **Replies.** Plain text. Code blocks keep their ``` fences, because agents like Aider parse edits
  from them.
- **Retries and recovery.** Empty replies, Playwright errors, and navigation failures are retried on
  a new thread. A closed tab or dead browser is reopened on the same profile.

## Limitations

- **No tool or function calling.** Agents that rely on native `tools` (Cline, Roo Code, and the
  agent modes of others) will not work well. Edit-block agents such as Aider fit better.
- **One request at a time.** Requests share one browser tab and are serialized.
- **Streaming is fake.** With `stream: true` the full reply comes back as one SSE chunk.
- **Usage numbers are estimates** (about 4 characters per token).
- **Session mapping is a heuristic** unless the client sends `X-Session-Id` or `user`.
- **Sensitive data on disk.** The profile holds session cookies. Debug screenshots can show
  conversation text. Both live in gitignored paths.
- **Latency.** Each reply takes at least `RESPONSE_STABLE_SECONDS` plus Copilot's own generation time.

## Tests

```bash
pytest
```

Browser tests drive a local Chromium and need `CHROMIUM_PATH` if Playwright's own browser is not
installed. They run against `tests/fixtures/mock_copilot.html`, which mimics the selectors but is
**not** the real Copilot page.
