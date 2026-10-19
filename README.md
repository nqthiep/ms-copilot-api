# Copilot Gateway

A local, OpenAI-compatible `/v1/chat/completions` endpoint backed by Microsoft 365 Copilot.
It talks to Copilot's own SignalR/WebSocket chat API directly — no browser at request time,
with true token streaming.

This is a personal workaround for environments that block LLM APIs but allow Copilot.
It is for local use on `127.0.0.1` only.

> **Before you use it:** this drives Copilot through an undocumented API, and the
> company may have blocked LLM API access on purpose. Check with IT or security first.

## Status

- The Sydney (SignalR) provider, the PKCE auth flow, the protocol framing, and the
  tool-calling emulation are covered by tests, including scripted WebSocket servers.
- **Live Microsoft traffic has not been tested end to end yet.** The protocol was
  reverse-engineered from public field notes (June–October 2026); Microsoft can change
  it without notice. If a turn fails, the gateway logs the raw error — please report it.
- A Playwright provider that drives the Copilot web UI remains as a fallback
  (`COPILOT_PROVIDER=browser`). Its selectors were updated against the live page's
  Lexical composer, but the response selector is still unverified.

## Setup

Requires Python 3.11+.

One command does everything (virtualenv, dependencies, Playwright Chromium, `.env`) and then starts
the gateway:

- **macOS, Linux, WSL, or Git Bash on Windows:**

  ```bash
  bash setup.sh
  ```

- **Windows PowerShell:**

  ```powershell
  powershell -ExecutionPolicy Bypass -File .\setup.ps1
  ```

Add `--no-start` (`-NoStart` in PowerShell) to set up without starting, or `--no-browser`
(`-NoBrowser`) to skip Chromium. Sign-in needs Chromium, so skip it only when `CHROMIUM_PATH` in `.env`
points to a browser you already have.

Or step by step:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium   # one-time: the Microsoft sign-in opens a real browser window
```

Create your local settings file from the template. `.env` is git-ignored, so it stays on your machine:

```powershell
Copy-Item .env.example .env      # Windows PowerShell
```

## First run

```bash
python -m copilot_gateway
```

With no stored token, a browser window opens for the one-time Microsoft sign-in
(SSO and MFA happen in that window). The gateway captures the auth code, keeps the
refresh token in `copilot_gateway/storage/tokens.json` (mode `0600`), and silently
refreshes it afterwards. `GET /health` reports `"auth_ready": true` once signed in.

Headless alternative: set `M365_REFRESH_TOKEN` in the shell and skip the browser entirely.

## Try it

```bash
curl http://127.0.0.1:8765/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"m365-copilot","messages":[{"role":"user","content":"hello"}]}'
```

Streaming works for real — deltas arrive as the model generates:

```bash
curl -N http://127.0.0.1:8765/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"m365-copilot","messages":[{"role":"user","content":"hello"}],"stream":true}'
```

## Use it with the GitHub Copilot extension for VSCode

See [docs/vscode-setup.md](docs/vscode-setup.md). In short: Command Palette →
**Chat: Manage Language Models** → Add → **Custom Endpoint**, with

- URL: `http://127.0.0.1:8765/v1/chat/completions`
- API key: `dummy`
- Model ID: `m365-copilot`
- Tool calling: on (Agent mode works through the gateway's tool emulation)

## Connect a coding agent

No changes to the agent's source code are needed. Point its OpenAI base URL at the gateway.

**Aider**

```bash
aider --openai-api-base http://127.0.0.1:8765/v1 --openai-api-key dummy --model openai/m365-copilot
```

**Continue** (`config.yaml`)

```yaml
models:
  - name: M365 Copilot
    provider: openai
    model: m365-copilot
    apiBase: http://127.0.0.1:8765/v1
    apiKey: dummy
```

**OpenCode** (`opencode.json`, OpenAI-compatible provider)

```json
{
  "provider": {
    "copilot": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "M365 Copilot",
      "options": { "baseURL": "http://127.0.0.1:8765/v1" },
      "models": { "m365-copilot": { "name": "M365 Copilot" } }
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
| `GATEWAY_HOST` / `GATEWAY_PORT` | `127.0.0.1` / `8765` | Must be a loopback address |
| `COPILOT_PROVIDER` | `sydney` | `sydney` (direct API) or `browser` (Playwright fallback) |
| `M365_CLIENT_ID` | officeweb client | First-party client id for the PKCE login |
| `M365_REFRESH_TOKEN` | unset | Headless auth: a refresh token, rotated automatically |
| `M365_ACCESS_TOKEN` | unset | Short-lived token for testing (~1h) |
| `M365_AUTH_TIMEOUT` | `600` | Seconds to wait for the interactive sign-in |
| `M365_DEFAULT_TONE` | `magic` | Model tone when the requested model id is unknown |
| `M365_NO_CODE_INTERPRETER` | `false` | `true` disables the server-side Python sandbox |
| `TOKEN_PATH` | `storage/tokens.json` | Where the refresh token is kept |
| `MAX_INLINE_FILE_CHARS` | `100000` | Text files larger than this are rejected |
| `COPILOT_URL` | `https://copilot.cloud.microsoft/` | Copilot page (browser provider) |
| `PLAYWRIGHT_PROFILE_DIR` | `playwright-profile/` | Browser profile (browser provider) |
| `HEADLESS` | `false` | Browser provider: `false` opens the sign-in window |
| `HIDE_WHEN_READY` | `false` | Browser provider: move the window off-screen when ready |
| `PROXY_SERVER` / `PROXY_USERNAME` / `PROXY_PASSWORD` | unset | Corporate proxy for both providers |
| `REQUEST_TIMEOUT` | `300` | Seconds for one request, retries included |
| `MAX_RETRY` | `3` | Retries after the first attempt, each on a fresh conversation |

## How it works

- **Auth.** OAuth2 PKCE against `login.microsoftonline.com` with Microsoft's first-party
  Office-web client id. The auth code is captured from the navigation request to the
  `nativeclient` redirect (a real browser bounces past it to `/common/wrongplace`).
  Refresh tokens rotate and are persisted; later runs never open a browser.
- **Chat.** Each turn opens a WebSocket to `wss://substrate.office.com/m365Copilot/Chathub`,
  performs the SignalR handshake, and sends the chat invocation plus the mandatory
  `Metrics` frame in one send. The model is picked by a `tone` string (`magic` by default).
- **One session, one conversation.** The session key (the `X-Session-Id` header, the `user`
  field, or a fingerprint of the first message) maps to one server-side conversation in
  `copilot_gateway/storage/sessions.json`. Each request sends only the user/tool turns the
  conversation has not seen yet. A new chat that reuses an earlier first message starts a new
  conversation when it sends fewer turns than the stored one, or when it does not repeat the
  assistant's reply. See `docs/vscode-setup.md` section 5.
- **Files.** Text files (`.py .md .txt .json .yaml .yml`) are inlined into the prompt, which
  suits coding agents. Images (`image_url` data URLs) are uploaded through Copilot's own
  upload endpoint and attached to the turn. `.docx`/`.pdf` are rejected — convert them to
  text first.
- **Tools.** M365 Copilot has no native function calling, so the gateway emulates it: tool
  schemas go into the prompt, the model emits a fenced ` ```tool_calls ` JSON block, and the
  gateway returns OpenAI-style `tool_calls`. The client's `role: tool` results feed back as
  follow-up turns. This is what makes VSCode's Agent mode work.
- **Models.** `GET /v1/models` lists `m365-copilot` (default), `copilot-web` (alias),
  `m365-quick` (fast answers), `m365-think-deeper` (reasoning), and `claude-sonnet`.
  Each id maps to an internal Copilot `tone`; Microsoft retires tones freely, so a
  model that stops working with a `Failed to invoke 'Chat'` error needs its mapping
  updated — please report it.
- **Retries.** Empty replies, protocol errors, and network failures are retried on a fresh
  conversation. A rejected token triggers one silent refresh before surfacing a 401.

## Limitations

- **Tool calling is emulated**, not native. It works well in practice but depends on the
  model following the output format.
- **One request at a time.** Turns are serialized per gateway instance.
- **Usage numbers are estimates** (about 4 characters per token).
- **Session mapping is a heuristic** unless the client sends `X-Session-Id` or `user`.
- **Sensitive data on disk.** The token store holds a Microsoft refresh token; treat
  `storage/` like a credential.
- **Latency.** Each turn opens a fresh WebSocket; expect a small handshake overhead.
- **Undocumented API.** Microsoft can change the protocol without notice.

## Disclaimer

- **Undocumented API.** This project talks to Microsoft 365 Copilot through an
  undocumented, reverse-engineered API while impersonating Microsoft's own first-party
  client. Microsoft can change, throttle, or block it at any time without notice, and
  there is no guarantee it keeps working.
- **No affiliation.** A personal, unofficial project. Not affiliated with, endorsed,
  or supported by Microsoft or GitHub.
- **Your responsibility.** How you use it is on you: make sure it complies with
  Microsoft's Terms of Service, your Copilot license terms, and your organization's
  IT/security policies — especially if your company blocked LLM APIs on purpose.
  The refresh token in `storage/tokens.json` is a live credential: protect it like
  a password and never commit it.
- **As is.** Provided as-is, without warranty of any kind. Model behavior,
  availability, quotas, and rate limits are entirely up to Microsoft.

## Tests

```bash
pytest
```

Browser tests drive a local Chromium (installed via `playwright install chromium`) against
`tests/fixtures/mock_copilot.html`. Protocol tests speak to scripted local WebSocket servers.
