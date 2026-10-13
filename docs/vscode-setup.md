# Using the gateway with the GitHub Copilot extension for VSCode

VSCode's Copilot Chat supports **custom endpoints**: any OpenAI-compatible
`/chat/completions` URL can appear as a model in the chat model picker,
including in **Agent mode**. This page wires the gateway in as such a model.

> What keeps working through GitHub: inline completions (ghost text) still need
> your GitHub Copilot license. What moves to the gateway: chat and agent-mode
> requests, billed to your Microsoft 365 Copilot usage instead.

## 1. Start the gateway

```bash
python -m copilot_gateway
```

On the first run a browser window opens for the one-time Microsoft sign-in
(the refresh token is then kept in `storage/tokens.json`, so later runs are
silent). `GET /health` reports `"auth_ready": true` once the sign-in is done:

```bash
curl http://127.0.0.1:8765/health
```

Keep the gateway running while you use VSCode.

## 2. Add the custom endpoint in VSCode

1. Open the Command Palette (`Ctrl+Shift+P` / `Cmd+Shift+P`) and run
   **Chat: Manage Language Models**.
2. Choose **Add** → **Custom Endpoint**.
3. Fill in:
   - **Name:** `M365 Copilot`
   - **API type:** `chat-completions`
   - **Base URL / model URL:** `http://127.0.0.1:8765/v1/chat/completions`
     (adjust the port if you changed `GATEWAY_PORT`)
   - **API key:** anything, e.g. `dummy` (the gateway binds to loopback and
     does not check it)
   - **Model ID:** `m365-copilot`
   - **Tool calling:** on (lets Agent mode use tools)
   - **Vision:** on (the gateway accepts `image_url` parts)
   - **Max input tokens:** `128000`, **max output tokens:** `16000`
4. Save. The model now appears in the Copilot Chat model picker — pick it for
   Ask, Edit, or Agent mode.

### Alternative: `chatLanguageModels.json`

The same configuration as a file (useful to share with a team):

```json
[
  {
    "name": "M365 Copilot",
    "vendor": "customendpoint",
    "apiKey": "dummy",
    "apiType": "chat-completions",
    "models": [
      {
        "id": "m365-copilot",
        "name": "M365 Copilot",
        "url": "http://127.0.0.1:8765/v1/chat/completions",
        "toolCalling": true,
        "vision": true,
        "maxInputTokens": 128000,
        "maxOutputTokens": 16000
      }
    ]
  }
]
```

## 3. Models

`GET /v1/models` lists what the gateway offers; any of these works as the
Model ID above:

| Model ID          | Backend                              |
|-------------------|--------------------------------------|
| `m365-copilot`    | Default Copilot (auto routing)       |
| `copilot-web`     | Alias of `m365-copilot`              |
| `m365-quick`      | Faster chat model                    |
| `m365-think-deeper` | Reasoning model                    |
| `claude-sonnet`   | Claude Sonnet via Copilot            |

Each ID maps to an internal Copilot `tone`. Microsoft retires tones freely — if a
model stops working with a `Failed to invoke 'Chat'` error, its mapping needs
updating.

## 4. How Agent mode works here

M365 Copilot has no native function-calling API, so the gateway emulates it:
it describes your tools to the model, parses the `tool_calls` block the model
emits, and feeds tool results back as follow-up turns. In practice this makes
Agent mode usable — file reads, shell commands, and edits flow through the
extension's normal tool loop.

Two caveats:

- The emulation depends on the model following the output format. If a reply
  comes back as plain prose instead of tool calls, ask again or break the task
  into smaller steps.
- Very large prompts can trip Copilot's safety filter and come back empty; the
  gateway retries once on a fresh conversation, then surfaces the error.

## 5. Sessions

Each VSCode chat session maps to one server-side Copilot conversation, so
follow-up questions keep context. The mapping is by conversation content unless
the client sends `X-Session-Id`; starting a **New Chat** in VSCode starts a new
Copilot conversation automatically.

## Troubleshooting

- **401 `authentication_error`** — the Microsoft token expired or was revoked.
  Restart the gateway and complete the sign-in window.
- **Empty replies on huge prompts** — split the request; the safety filter
  rejects very large or jailbreak-looking prompts.
- **The model is not in the picker** — re-run **Chat: Manage Language Models**
  and check the URL has no trailing slash issues; the gateway also serves the
  unprefixed `/chat/completions` alias.

## Disclaimer

This setup drives Microsoft 365 Copilot through an undocumented,
reverse-engineered API while impersonating Microsoft's first-party client.
Microsoft may change, throttle, or block it without notice. The gateway is not
affiliated with Microsoft or GitHub and is provided as-is, without warranty.
You are responsible for complying with Microsoft's Terms of Service, your
Copilot license terms, and your organization's IT/security policies.
