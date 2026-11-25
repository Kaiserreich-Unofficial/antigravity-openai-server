# antigravity-openai-server

**English** | [简体中文](#简体中文)

Use **Google Antigravity** from any OpenAI-compatible client.

A small HTTP server that makes the `agy` CLI look like an OpenAI API (the way LM Studio or Ollama do). Point any OpenAI client — hermes, the OpenAI Python SDK, Open WebUI, ZCode, etc. — at it and you get Antigravity's agent, using your existing Google-account sign-in shared with the Antigravity IDE (no API key, no per-token billing).

Under the hood it drives `agy` as one subprocess per turn over its `stream-json` output protocol; conversation history lives server-side in agy and follow-up turns resume it via `--conversation <id>`.

> This fork tracks **agy 1.5.x** (verified against 1.5.17): current model table, `tools` frontmatter as a list, explicit upstream error surfacing, and a Windows service wrapper.

## What it does

- **OpenAI surface:** `GET /v1/models` and `POST /v1/chat/completions` (streaming + non-streaming). Includes full support for `stream_options: {"include_usage": true}`.
- **OpenAI function calling:** your client's `tools` are passed through to Antigravity via an in-process MCP bridge. The model calls them through agy's MCP tool, the server returns a normal `tool_calls` response, your client runs the tool and sends the result back, and the same agy subprocess resumes — the whole multi-step tool loop is one conversation kept alive across continuations.
- **Antigravity's own built-in tools** (file ops, shell, browser automation, subagents, …) run internally the whole time.
- **Conversation cache:** a follow-up request whose history matches a previous turn resumes the stored agy conversation and sends only the newest user message — cheaper, and immune to the prompt-length cap (see below).
- **System prompts via managed agents** (default on): agy has no system-prompt flag, but it does support custom agents. The request's system message is materialized as an agent definition inside a server-managed plugin and agy is spawned with `--agent <slug>`. When that path is unavailable — or when the account returns upstream 429 on managed-agent turns (see Troubleshooting) — set `AGI_SYSTEM_PROMPT_VIA_AGENT=false` and the system message is injected as an instruction preamble at the top of the first turn instead.
- **Reasoning passthrough** (always on): the model's thinking surfaces as OpenAI-style `reasoning_content` — a delta chunk before the answer text (streaming) or `message.reasoning_content` (non-streaming). agy's stream-json hides the thinking text; it is read best-effort from the CLI's internal brain transcript. Blocks arrive whole per model step; there is no incremental thinking stream. If the file/format changes, responses simply carry no reasoning.
- **Image input**: data-URL images in OpenAI `image_url` content parts are written to `<workdir>/attachments/` (content-addressed, deduplicated) and the prompt references the file so the model looks at it. Remote `image_url` URLs are not fetched and are skipped.
- **Markdown table flattening** (default on): rewrites pipe tables to fenced ASCII so they render in clients that don't support Markdown tables.
- **Ollama / llama.cpp compatibility shims** so capability-probing frontends (Open WebUI, etc.) accept the server.
- **Explicit upstream errors:** when agy dies before producing a result, the client gets an error carrying the exit code and the stderr tail instead of a silent empty finish; `RESOURCE_EXHAUSTED` / HTTP 429 map to status 429.

## Requirements

- The `agy` CLI installed and signed in (it shares auth with the Antigravity IDE/app). Windows: `irm https://antigravity.google/cli/install.ps1 | iex`; other platforms: see the [install docs](https://antigravity.google/docs/cli-install). Check with `agy models`.
- Python 3.11+ (tested on 3.13, Windows and Linux).
- A Google account eligible for Antigravity. Eligibility is judged against the account's country (and, on the backend, the request IP); a VPN alone does not fix an ineligible account.

## Install

Linux/macOS:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

Windows:

```bat
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

If your pip is pointed at a mirror that lacks `setuptools`, add `-i https://pypi.org/simple`.

## Run

```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8788
```

Or via the installed console script:

```bash
.venv/bin/antigravity-interface        # Windows: .venv\Scripts\antigravity-interface.exe
```

Configuration comes from `AGI_*` environment variables; a `.env` file in the repository root is loaded too (environment variables win).

### Windows service

`service.py` wraps the server in a real Windows service (visible in `services.msc`, start type automatic), so it starts at boot:

```powershell
# from an elevated shell
.venv\Scripts\python.exe service.py install     # install + start
.venv\Scripts\python.exe service.py status
.venv\Scripts\python.exe service.py stop|start|restart|uninstall
```

The service is registered as `LocalSystem` but does **not** run the server as SYSTEM: on startup it takes the token of the logged-on user (`WTSQueryUserToken`) and starts the server with `CreateProcessAsUser`. That is deliberate — agy's Google sign-in lives in that user's Credential Manager entry (`gemini:antigravity`) and it needs the user's `HTTP(S)_PROXY`, neither of which a SYSTEM process can reach. No Windows password is stored or prompted for. If there is no user session yet (boot before logon) the service retries every few seconds.

For machines that need a fixed service account instead, `service.py install --account DOMAIN\User` prompts for that account's password and grants `Log on as a service`.

The script pins `127.0.0.1:8790` (display name `Antigravity OpenAI Server (8790)`); edit `HOST`/`PORT` in `service.py` for another port. Logs go to `logs/agi-server.log`.

## Configuration

Copy `.env.example` to `.env` and edit, or export the variables directly. Every field is overridable via `AGI_<FIELD>`.

```bash
# ── HTTP server ──────────────────────────────────────────────────────────────
AGI_HOST=127.0.0.1
AGI_PORT=8788

# ── Auth ─────────────────────────────────────────────────────────────────────
# Optional bearer token gating every /v1 request. Safe to leave UNSET only on a
# loopback bind. The server REFUSES to start on a non-loopback host unless this
# is set.
# AGI_API_KEY=choose-a-long-random-secret

# ── Antigravity CLI ──────────────────────────────────────────────────────────
AGI_AGY_BIN=agy
AGI_DEFAULT_MODEL=Gemini 3.8 Flash (Medium)
# AGI_DEFAULT_EFFORT=high

# ── Prompt handling ──────────────────────────────────────────────────────────
AGI_MAX_PROMPT_CHARS=28000

# ── System prompt via managed agent ──────────────────────────────────────────
# Set to false if managed-agent turns return HTTP 429 RESOURCE_EXHAUSTED; the
# system message then rides as a prompt preamble instead.
AGI_SYSTEM_PROMPT_VIA_AGENT=true
# AGI_AGENT_PLUGIN_PATH=~/.gemini/config/plugins/hermes-bridge
AGI_AGENT_CACHE_MAX=64

# ── Reasoning / thinking passthrough ─────────────────────────────────────────
# AGI_AGY_BRAIN_PATH=~/.gemini/antigravity-cli/brain

# ── Image input ──────────────────────────────────────────────────────────────
AGI_IMAGE_INPUT=true

# ── Conversation continuity ──────────────────────────────────────────────────
AGI_CONVERSATION_CACHE_MAX=256
AGI_CONVERSATION_CACHE_TTL_S=3600

# ── MCP config registration (tool calling) ──────────────────────────────────
AGI_MANAGE_MCP_CONFIG=true
# AGI_MCP_CONFIG_PATH=~/.gemini/config/mcp_config.json

# ── Output formatting ────────────────────────────────────────────────────────
AGI_FLATTEN_MARKDOWN_TABLES=true

# ── Workspace ────────────────────────────────────────────────────────────────
AGI_DEFAULT_WORKDIR=~/agi-workspace
# AGI_ALLOWED_WORKDIR_ROOTS=["C:\\Projects"]

# ── Lifecycle (seconds) ──────────────────────────────────────────────────────
AGI_REQUEST_TIMEOUT_S=600
AGI_SUSPENDED_TTL_S=300
AGI_IDLE_SESSION_TTL_S=900
AGI_GC_INTERVAL_S=30

# ── Logging ──────────────────────────────────────────────────────────────────
AGI_LOG_LEVEL=INFO
AGI_TIMING_LOG=false
```

### Config reference

| Var | Default | Meaning |
|-----|---------|---------|
| `AGI_HOST` | `127.0.0.1` | Bind host. Non-loopback requires `AGI_API_KEY`. |
| `AGI_PORT` | `8788` | Bind port. Also used for the per-conversation MCP callback URL. |
| `AGI_API_KEY` | _(unset)_ | Bearer token required on every `/v1` request when set. Mandatory for a non-loopback bind. |
| `AGI_AGY_BIN` | `agy` | Path to the `agy` CLI. |
| `AGI_DEFAULT_MODEL` | `Gemini 3.8 Flash (Medium)` | Model when the request names none / an unknown id. |
| `AGI_DEFAULT_EFFORT` | _(unset)_ | Effort used when the request carries none (`low`/`medium`/`high`/`xhigh`). |
| `AGI_MAX_PROMPT_CHARS` | `28000` | Fresh-fold prompt cap; larger prompts are sent over stdin as one stream-json turn message. |
| `AGI_SYSTEM_PROMPT_VIA_AGENT` | `true` | Deliver the system message as a generated agent (`--agent <slug>`). Set `false` on agy 1.5 if managed-agent turns return 429; falls back to a prompt preamble. |
| `AGI_AGENT_PLUGIN_PATH` | `~/.gemini/config/plugins/hermes-bridge` | Managed agent-plugin directory. |
| `AGI_AGENT_CACHE_MAX` | `64` | Max generated agent files kept (LRU). |
| `AGI_AGY_BRAIN_PATH` | `~/.gemini/antigravity-cli/brain` | Brain transcript directory, source of `reasoning_content`. |
| `AGI_IMAGE_INPUT` | `true` | Materialize data-URL images under `<workdir>/attachments/`. |
| `AGI_CONVERSATION_CACHE_MAX` | `256` | Max history→conversation-id cache entries. |
| `AGI_CONVERSATION_CACHE_TTL_S` | `3600` | Cache entry lifetime. |
| `AGI_MANAGE_MCP_CONFIG` | `true` | Allow per-conversation entries in the shared MCP config. |
| `AGI_MCP_CONFIG_PATH` | `~/.gemini/config/mcp_config.json` | Shared Antigravity MCP config location. |
| `AGI_FLATTEN_MARKDOWN_TABLES` | `true` | Rewrite pipe tables to fenced ASCII. |
| `AGI_DEFAULT_WORKDIR` | `~/agi-workspace` | Working dir granted to agy via `--add-dir` (also its cwd). |
| `AGI_ALLOWED_WORKDIR_ROOTS` | `[]` | JSON list of extra roots a per-request `workdir` may resolve under. |
| `AGI_REQUEST_TIMEOUT_S` | `600` | Per-turn upstream timeout. |
| `AGI_SUSPENDED_TTL_S` | `300` | How long a tool-suspended conversation waits for results. |
| `AGI_IDLE_SESSION_TTL_S` | `900` | Idle conversation eviction age. |
| `AGI_GC_INTERVAL_S` | `30` | GC sweep interval. |
| `AGI_LOG_LEVEL` | `INFO` | Log level. |
| `AGI_TIMING_LOG` | `false` | Per-turn latency metrics on the `agi.timing` logger. |

## Models

Model ids are OpenAI-friendly slugs; the server maps them onto the exact display
names `agy models` reports. `effort` (or a `-low`/`-medium`/`-high`/`-xhigh`
slug suffix, which wins over `effort`) picks the variant. `max`/`ultra` are
treated as `high`. With no effort the server prefers the family's medium
variant; single-variant families ignore effort entirely.

| Slug | agy model | Levels |
|------|-----------|--------|
| `gemini-4-neon` | `Gemini 4 Neon (Low/Medium/High/XHigh)` | low, medium, high, xhigh |
| `gemini-4-carbon` | `Gemini 4 Carbon (Low/Medium/High)` | low, medium, high |
| `gemini-4-argon` | `Gemini 4 Argon (Low/Medium/High)` | low, medium, high |
| `gemini-4-hydrogen` | `Gemini 4 Hydrogen (Low/Medium/High/XHigh)` | low, medium, high, xhigh |
| `gemini-3.8-flash` | `Gemini 3.8 Flash (Low/Medium/High)` | low, medium, high |
| `claude-opus-5.5` | `Claude Opus 5.5 (Low/Medium/High)` | low, medium, high |
| `claude-sonnet-5.5` | `Claude Sonnet 5.5 (Low/Medium/High)` | low, medium, high |
| `gpt-oss-120b` | `GPT-OSS 120B (Medium)` | fixed |
| `orchestrator-neon` | `Orchestrator (Neon)` | fixed |
| `orchestrator-carbon` | `Orchestrator (Carbon)` | fixed |
| `orchestrator-argon` | `Orchestrator (Argon)` | fixed |

Aliases: `gemini`/`neon` → `gemini-4-neon`, `flash` → `gemini-3.8-flash`,
`carbon`/`argon`/`hydrogen`, `sonnet`/`opus`, `orchestrator` →
`orchestrator-carbon`, `gpt-oss`. Unknown ids fall back to `AGI_DEFAULT_MODEL`.
Legacy slugs from older agy builds (`gemini-3.5-flash`, `gemini-3.1-pro`,
`claude-*-4.6`) no longer exist upstream and also fall back to the default.

`GET /v1/models` lists both the slugs and the exact agy display names.

## Security

The server drives the agy CLI with `--dangerously-skip-permissions`, which means
**anyone who can reach the port can run arbitrary code as your user.** Two
interlocks guard this:

1. **Non-loopback bind requires an API key.** `create_app()` refuses to start
   when `AGI_HOST` is not loopback and `AGI_API_KEY` is unset.
2. **Bearer auth on `/v1`.** When `AGI_API_KEY` is set, every `/v1` request must
   carry `Authorization: Bearer <key>` (constant-time compared). The MCP mount is
   exempt — it is reached only by the local agy subprocess over loopback.

Keep the bind on `127.0.0.1`. If you must expose it, set a long random
`AGI_API_KEY` and put TLS in front.

## Endpoints

**OpenAI**
- `POST /v1/chat/completions` — streaming + non-streaming, tool calling
- `GET /v1/models`

**Health / info**
- `GET /healthz` — `{"status":"ok",…}`
- `GET /` — service info

**Ollama / llama.cpp compatibility**
- `GET /api/tags`, `POST /api/show`, `GET /api/version`, `GET /version`
- `GET /v1/props`, `GET /props`, `GET /api/v1/models`

**Internal**
- `/mcp/<conv_id>` — in-process MCP bridge, dialed only by the spawned agy over loopback

## Use it

```bash
curl http://127.0.0.1:8788/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"gemini-4-neon","effort":"high","messages":[{"role":"user","content":"hello"}]}'
```

With `AGI_API_KEY` set, add `-H "Authorization: Bearer $AGI_API_KEY"`.

From hermes (`~/.hermes/config.yaml`):

```yaml
providers:
  antigravity:
    name: Antigravity
    base_url: http://127.0.0.1:8788/v1
    api_key: any-string-accepted   # or your AGI_API_KEY if one is set
    api_mode: chat_completions
    default_model: gemini-4-neon
    models: [gemini-4-neon, gemini-4-carbon, gemini-3.8-flash, claude-sonnet-5.5, claude-opus-5.5, gpt-oss-120b]
```

All of them bill through the same Google subscription the Antigravity IDE uses.

## The shared MCP config

The only MCP configuration agy honors is `~/.gemini/config/mcp_config.json` —
the same file the Antigravity IDE manages, including your own servers and their
secrets. For tool calling this server merges per-conversation entries into it
and removes them at conversation end:

- Foreign entries are **never modified**; writes are atomic and serialized behind one lock.
- Leftover `hermes*` entries from a crashed run are cleaned at startup (matched by our port).
- Set `AGI_MANAGE_MCP_CONFIG=false` to forbid touching the file entirely — tool calling then returns 400; plain chat is unaffected.

The file must be valid JSON. An empty (0-byte) or truncated file makes the
manager refuse to touch it, and tool turns fail; write `{}` (or a valid config)
to recover.

## The managed agent plugin

For system-prompt delivery the server owns one plugin directory,
`~/.gemini/config/plugins/hermes-bridge/` (override with
`AGI_AGENT_PLUGIN_PATH`). Each distinct system prompt becomes
`agents/hermes-<sha1[:12]>.md`, created on first use, discovered by agy at
conversation start, reused on repeat requests, and pruned LRU beyond
`AGI_AGENT_CACHE_MAX`. Only files matching the `hermes-<hash>.md` pattern are
ever pruned.

On agy >= 1.5 the agent frontmatter must list native tool names:

```yaml
tools: [read_file, edit_file, write_file, run_command, manage_task, search_web]
```

The scalar form `tools: "*"` is not valid YAML for agy 1.5 (the loader skips the
whole agent, and `--agent <slug>` then fails with `unknown agent`), and a `"*"`
list element is rejected at runtime (`unknown component: tool "*" not found in
registry`). MCP bridge tools are injected dynamically and are not part of that
registry. Old generated agents are rewritten automatically when their content
differs from the template.

Some accounts get HTTP 429 `RESOURCE_EXHAUSTED` when a turn is driven through a
managed agent, even though the same text works as a plain prompt. Set
`AGI_SYSTEM_PROMPT_VIA_AGENT=false` to use the preamble fallback.

The base Antigravity system context (identity, environment, date) stays
underneath the agent prompt; there is no equivalent of a "bare model" replace
mode.

## Deploying as a systemd service (Linux)

`~/.config/systemd/user/agi-server.service`:

```ini
[Unit]
Description=Antigravity OpenAI Server (AGI)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/youruser/antigravity-openai-server
EnvironmentFile=/home/youruser/antigravity-openai-server/.env
Environment=PATH=/home/youruser/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=HOME=/home/youruser
ExecStart=/home/youruser/antigravity-openai-server/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8788
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now agi-server.service
loginctl enable-linger "$USER"
```

The `agy` binary must be on the unit's `PATH` and signed in for the same user.

## Test

```bash
.venv/bin/python -m pytest -q          # Windows: .venv\Scripts\python.exe -m pytest -q
```

Live scripts (need `agy` signed in; e2e also needs a running server):

```bash
.venv/bin/python tests/scripts/e2e_autonomous.py http://127.0.0.1:8788
.venv/bin/python tests/scripts/e2e_tool.py --base http://127.0.0.1:8788
.venv/bin/python tests/scripts/smoke_session.py
.venv/bin/python tests/scripts/probe_mcp.py
```

### Benchmark

`scripts/bench.py` launches its own throwaway uvicorn instance and writes
p50/p95 of spawn / TTFT / total / throughput to JSON:

```bash
.venv/bin/python scripts/bench.py --port 8799 --iters 3 --out bench.json
```

## How it works

```
OpenAI client ──HTTP /v1──▶ agi-server ──argv|stdin + stream-json (stdout)──▶ agy CLI
       ▲                        │                                          │
       └──── tool_calls ────────┤          call_mcp_tool(hermesN,…)        │
       │                        │◀──────── in-process MCP bridge ◀─────────┘
       └──── tool result ──────▶┘           (/mcp/<conv_id>, loopback)
```

- **Autonomous turn** (no `tools`): the conversation is folded into one prompt
  for a fresh agy conversation, or resumed via the conversation cache. Prompts
  over `AGI_MAX_PROMPT_CHARS` go over stdin (`--input-format stream-json`,
  one NDJSON line) instead of argv; it is UTF-8 by construction, unlike a
  staged file, which agy's reader can mistake for GBK.
- **Tool turn:** a conversation owns one agy subprocess, an MCP bridge, and an
  entry in the shared MCP config. When the model calls a bridged function the
  subprocess blocks inside the MCP call; the server returns `tool_calls` and
  parks the conversation `SUSPENDED`. The next request with the tool results
  resumes the same subprocess.
- **finish_reason** is inferred (`stop`, `tool_calls`); `usage` carries
  prompt/completion/total tokens plus `completion_tokens_details.reasoning_tokens`.
- A background GC reaps suspended-too-long and idle conversations.
- Upstream failures (non-SUCCESS result line, or exit without a result) come back
  as OpenAI errors with the exit code and stderr tail; 429 is preserved.
- A turn rejected as `INVALID_ARGUMENT` (HTTP 400) is retried once on a fresh
  process — but only before any tool result round-trip, since a restart re-runs
  the create-time prompt.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Eligibility check failed: … not available in your location` | Google judged the account (country) or request IP ineligible. Sign in with an eligible account; a VPN does not change the account's country. |
| `RESOURCE_EXHAUSTED (code 429)` | Upstream rate/quota window, or the managed-agent path. Retry; if it only happens with a system prompt, set `AGI_SYSTEM_PROMPT_VIA_AGENT=false`. `agy -p "/usage"` shows remaining quota. |
| `unknown agent "hermes-…"` | The generated agent was skipped (old frontmatter). Upgrade this repo; pending agent files are rewritten on the next request. |
| Tool turns fail on the first request | `~/.gemini/config/mcp_config.json` is empty or invalid JSON. Write `{}`. |
| `You are not logged into Antigravity` in `agy` logs | Run `agy` once as the same Windows user and complete sign-in. |
| Empty reply with `finish_reason: stop` | Older builds ended turns without a result line; this fork now reports the exit code and stderr instead of a silent finish. Check `logs/agi-server.log`. |

---

## 简体中文

**English** | 简体中文

在任何 OpenAI 兼容客户端里使用 **Google Antigravity**。

这个小 HTTP 服务把 `agy` CLI 包装成 OpenAI API（类似 LM Studio 或 Ollama）。把任意 OpenAI 客户端（hermes、OpenAI Python SDK、Open WebUI、ZCode 等）指向它，就能用 Antigravity 的 agent，并复用与 Antigravity IDE 共享的 Google 账号登录（不需要 API key，不按 token 计费）。

底层每一轮对话都会以子进程方式启动一次 `agy`，走它的 `stream-json` 输出协议；对话历史保存在 agy 服务端，后续轮次通过 `--conversation <id>` 续接。

> 本 fork 适配 **agy 1.5.x**（按 1.5.17 验证）：更新了模型表、`tools` frontmatter 改为列表、上游错误明确上报，并附带 Windows 服务脚本。

### 功能

- **OpenAI 接口**：`GET /v1/models` 与 `POST /v1/chat/completions`（流式与非流式），完整支持 `stream_options: {"include_usage": true}`。
- **OpenAI function calling**：客户端传入的 `tools` 经进程内 MCP 桥转给 Antigravity。模型调用后返回标准 `tool_calls`，客户端执行并把结果回传，同一个 agy 子进程继续跑完多步工具循环。
- **Antigravity 自带工具**（文件、shell、浏览器自动化、子 agent 等）全程可用。
- **会话缓存**：历史前缀匹配的后续请求会续接已有 agy 会话，只发送最新一条用户消息，更省 token，也不受 prompt 长度上限影响。
- **通过托管 agent 传递 system prompt**（默认开启）：agy 没有 system prompt 参数，但支持自定义 agent。服务端把 system message 写成插件里的 agent 定义，并用 `--agent <slug>` 启动 agy。这条路径不可用，或账号在托管 agent 轮次返回上游 429 时，把 `AGI_SYSTEM_PROMPT_VIA_AGENT` 设为 `false`，system message 会改为首轮提示词前缀注入。
- **思考内容透传**（始终开启）：模型的 thinking 以 OpenAI 风格的 `reasoning_content` 返回——流式时是答案文本前的 delta，非流式时是 `message.reasoning_content`。agy 的 stream-json 不直接输出思考文本，服务端尽量从 CLI 的 brain transcript 读取；按模型步骤整块到达，没有增量流。格式变化时只是没有 reasoning，不影响响应。
- **图片输入**：`image_url` 里的 data-URL 会写入 `<workdir>/attachments/`（按内容寻址、去重），prompt 里引用该文件，让模型用内置文件工具查看。远程 URL 不会抓取，直接跳过。
- **Markdown 表格扁平化**（默认开启）：管道表格改写为代码块 ASCII，方便不支持 Markdown 表格的客户端显示。
- **Ollama / llama.cpp 兼容层**：让 Open WebUI 一类会探测能力的客户端接受该服务。
- **明确的上游错误**：agy 未产出 result 就退出时，客户端会收到带退出码和 stderr 尾部的错误，而不是静默的空结束；`RESOURCE_EXHAUSTED` / HTTP 429 会映射为 429。

### 环境要求

- 安装并登录 `agy` CLI（与 Antigravity IDE/应用共享登录）。Windows：`irm https://antigravity.google/cli/install.ps1 | iex`；其他平台见[安装文档](https://antigravity.google/docs/cli-install)。可用 `agy models` 检查。
- Python 3.11+（在 3.13、Windows 与 Linux 下验证）。
- 具备 Antigravity 资格的 Google 账号。资格按账号所在国家（后端还会看请求 IP）判定，单纯挂 VPN 不能解决账号资格问题。

### 安装

Linux/macOS：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

Windows：

```bat
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

如果 pip 指向的镜像缺少 `setuptools`，加 `-i https://pypi.org/simple`。

### 运行

```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8788
```

或用安装好的命令：

```bash
.venv/bin/antigravity-interface        # Windows: .venv\Scripts\antigravity-interface.exe
```

配置来自 `AGI_*` 环境变量；仓库根目录的 `.env` 也会加载（环境变量优先）。

#### Windows 服务

`service.py` 把服务端包装成真正的 Windows 服务（`services.msc` 可见，自动启动），开机自启：

```powershell
# 在管理员权限的 shell 中
.venv\Scripts\python.exe service.py install     # 安装并启动
.venv\Scripts\python.exe service.py status
.venv\Scripts\python.exe service.py stop|start|restart|uninstall
```

服务本身以 `LocalSystem` 注册，但**不会**用 SYSTEM 跑服务端：启动后取当前登录用户的令牌（`WTSQueryUserToken`），再用 `CreateProcessAsUser` 以该用户身份拉起服务端。这是刻意设计——agy 的 Google 登录在相应用户的凭据管理器条目 `gemini:antigravity` 里，还需要该用户的 `HTTP(S)_PROXY`，SYSTEM 进程都拿不到。全程不需要保存或输入 Windows 密码。开机早于登录、还没有用户会话时，服务会每隔几秒重试。

如果机器需要固定服务账号，可用 `service.py install --account DOMAIN\User`，会提示输入该账号密码并授予“作为服务登录”。

脚本固定使用 `127.0.0.1:8790`（显示名 `Antigravity OpenAI Server (8790)`），换端口改 `service.py` 里的 `HOST`/`PORT`。日志在 `logs/agi-server.log`。

### 配置

把 `.env.example` 复制为 `.env` 修改，或直接导出环境变量。所有字段都可用 `AGI_<FIELD>` 覆盖。

```bash
AGI_HOST=127.0.0.1
AGI_PORT=8788
# AGI_API_KEY=choose-a-long-random-secret
AGI_AGY_BIN=agy
AGI_DEFAULT_MODEL=Gemini 3.8 Flash (Medium)
# AGI_DEFAULT_EFFORT=high
AGI_MAX_PROMPT_CHARS=28000
# 托管 agent 轮次出现 429 时设为 false，改用提示词前缀
AGI_SYSTEM_PROMPT_VIA_AGENT=true
AGI_AGENT_CACHE_MAX=64
AGI_IMAGE_INPUT=true
AGI_CONVERSATION_CACHE_MAX=256
AGI_CONVERSATION_CACHE_TTL_S=3600
AGI_MANAGE_MCP_CONFIG=true
AGI_FLATTEN_MARKDOWN_TABLES=true
AGI_DEFAULT_WORKDIR=~/agi-workspace
AGI_REQUEST_TIMEOUT_S=600
AGI_SUSPENDED_TTL_S=300
AGI_IDLE_SESSION_TTL_S=900
AGI_GC_INTERVAL_S=30
AGI_LOG_LEVEL=INFO
AGI_TIMING_LOG=false
```

#### 配置项说明

| 变量 | 默认值 | 含义 |
|------|--------|------|
| `AGI_HOST` | `127.0.0.1` | 绑定地址。非 loopback 必须设置 `AGI_API_KEY`。 |
| `AGI_PORT` | `8788` | 端口，也用于每会话的 MCP 回调地址。 |
| `AGI_API_KEY` | 未设置 | 设置后所有 `/v1` 请求需携带 Bearer token；非 loopback 必填。 |
| `AGI_AGY_BIN` | `agy` | `agy` CLI 路径。 |
| `AGI_DEFAULT_MODEL` | `Gemini 3.8 Flash (Medium)` | 请求未指定或指定未知模型时使用。 |
| `AGI_DEFAULT_EFFORT` | 未设置 | 请求未带 effort 时的默认档（`low`/`medium`/`high`/`xhigh`）。 |
| `AGI_MAX_PROMPT_CHARS` | `28000` | 首次折叠的 prompt 上限；超长时经 stdin 以一条 stream-json 消息投递。 |
| `AGI_SYSTEM_PROMPT_VIA_AGENT` | `true` | 用生成 agent（`--agent`）传递 system prompt。agy 1.5 下若托管 agent 轮次 429，设为 `false` 回退为提示词前缀。 |
| `AGI_AGENT_PLUGIN_PATH` | `~/.gemini/config/plugins/hermes-bridge` | 托管 agent 插件目录。 |
| `AGI_AGENT_CACHE_MAX` | `64` | 保留的生成 agent 文件数（LRU）。 |
| `AGI_AGY_BRAIN_PATH` | `~/.gemini/antigravity-cli/brain` | brain transcript 目录，`reasoning_content` 来源。 |
| `AGI_IMAGE_INPUT` | `true` | 把 data-URL 图片写入 `<workdir>/attachments/`。 |
| `AGI_CONVERSATION_CACHE_MAX` | `256` | 历史→会话 ID 缓存条目上限。 |
| `AGI_CONVERSATION_CACHE_TTL_S` | `3600` | 缓存有效期。 |
| `AGI_MANAGE_MCP_CONFIG` | `true` | 允许向共享 MCP 配置写入会话条目。 |
| `AGI_MCP_CONFIG_PATH` | `~/.gemini/config/mcp_config.json` | 共享 MCP 配置路径。 |
| `AGI_FLATTEN_MARKDOWN_TABLES` | `true` | 管道表格改写为 ASCII。 |
| `AGI_DEFAULT_WORKDIR` | `~/agi-workspace` | 通过 `--add-dir` 授予 agy 的工作目录（也是 cwd）。 |
| `AGI_ALLOWED_WORKDIR_ROOTS` | `[]` | JSON 列表，允许请求里的 `workdir` 落在这些根目录下。 |
| `AGI_REQUEST_TIMEOUT_S` | `600` | 单轮上游超时。 |
| `AGI_SUSPENDED_TTL_S` | `300` | 工具挂起会话等待结果的时长。 |
| `AGI_IDLE_SESSION_TTL_S` | `900` | 空闲会话回收时间。 |
| `AGI_GC_INTERVAL_S` | `30` | 后台回收间隔。 |
| `AGI_LOG_LEVEL` | `INFO` | 日志级别。 |
| `AGI_TIMING_LOG` | `false` | 向 `agi.timing` 输出每轮延迟指标。 |

### 模型

模型 id 是 OpenAI 风格的 slug，服务端映射到 `agy models` 显示的精确名字。`effort`（或 slug 后缀 `-low`/`-medium`/`-high`/`-xhigh`，后缀优先）决定档位；`max`/`ultra` 按 `high` 处理。未指定 effort 时优先该家族的 medium；单档家族忽略 effort。

| Slug | agy 模型 | 可选档位 |
|------|----------|----------|
| `gemini-4-neon` | `Gemini 4 Neon (Low/Medium/High/XHigh)` | low, medium, high, xhigh |
| `gemini-4-carbon` | `Gemini 4 Carbon (Low/Medium/High)` | low, medium, high |
| `gemini-4-argon` | `Gemini 4 Argon (Low/Medium/High)` | low, medium, high |
| `gemini-4-hydrogen` | `Gemini 4 Hydrogen (Low/Medium/High/XHigh)` | low, medium, high, xhigh |
| `gemini-3.8-flash` | `Gemini 3.8 Flash (Low/Medium/High)` | low, medium, high |
| `claude-opus-5.5` | `Claude Opus 5.5 (Low/Medium/High)` | low, medium, high |
| `claude-sonnet-5.5` | `Claude Sonnet 5.5 (Low/Medium/High)` | low, medium, high |
| `gpt-oss-120b` | `GPT-OSS 120B (Medium)` | 固定 |
| `orchestrator-neon` | `Orchestrator (Neon)` | 固定 |
| `orchestrator-carbon` | `Orchestrator (Carbon)` | 固定 |
| `orchestrator-argon` | `Orchestrator (Argon)` | 固定 |

别名：`gemini`/`neon` → `gemini-4-neon`，`flash` → `gemini-3.8-flash`，`carbon`/`argon`/`hydrogen`，`sonnet`/`opus`，`orchestrator` → `orchestrator-carbon`，`gpt-oss`。未知 id 回退到 `AGI_DEFAULT_MODEL`。旧版 agy 的 slug（`gemini-3.5-flash`、`gemini-3.1-pro`、`claude-*-4.6`）在上游已不存在，同样回退到默认模型。

`GET /v1/models` 同时列出 slug 和 agy 的精确显示名。

### 安全

服务用 `--dangerously-skip-permissions` 驱动 agy，**任何能访问该端口的人都能以你的用户身份执行任意代码**。两道保险：

1. **非 loopback 必须有 API key**：`AGI_HOST` 非 loopback 且未设 `AGI_API_KEY` 时服务拒绝启动。
2. **`/v1` Bearer 鉴权**：设置 key 后所有 `/v1` 请求都要带 `Authorization: Bearer <key>`（常量时间比较）。MCP 挂载点豁免，因为只有本地 agy 子进程走 loopback 访问。

保持绑定在 `127.0.0.1`。确实要暴露时，设置长随机 `AGI_API_KEY` 并放在 TLS 之后。

### 接口

**OpenAI**
- `POST /v1/chat/completions` — 流式/非流式，工具调用
- `GET /v1/models`

**健康/信息**
- `GET /healthz`、`GET /`

**Ollama / llama.cpp 兼容**
- `GET /api/tags`、`POST /api/show`、`GET /api/version`、`GET /version`
- `GET /v1/props`、`GET /props`、`GET /api/v1/models`

**内部**
- `/mcp/<conv_id>` — 进程内 MCP 桥，仅由本地 agy 子进程走 loopback 访问

### 使用

```bash
curl http://127.0.0.1:8788/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"gemini-4-neon","effort":"high","messages":[{"role":"user","content":"你好"}]}'
```

设置了 `AGI_API_KEY` 时加上 `-H "Authorization: Bearer $AGI_API_KEY"`。

hermes（`~/.hermes/config.yaml`）：

```yaml
providers:
  antigravity:
    name: Antigravity
    base_url: http://127.0.0.1:8788/v1
    api_key: any-string-accepted
    api_mode: chat_completions
    default_model: gemini-4-neon
    models: [gemini-4-neon, gemini-4-carbon, gemini-3.8-flash, claude-sonnet-5.5, claude-opus-5.5, gpt-oss-120b]
```

它们都计费到 Antigravity IDE 所用的同一个 Google 订阅。

### 共享 MCP 配置

agy 唯一识别的 MCP 配置是 `~/.gemini/config/mcp_config.json`——与 Antigravity IDE 共用的那个文件，包含你自己的服务器和密钥。工具调用时服务端会把每会话条目合并进去，会话结束再删除：

- 不修改任何外部条目；写入原子化并加锁串行。
- 崩溃遗留的 `hermes*` 条目会在启动时按端口清理。
- `AGI_MANAGE_MCP_CONFIG=false` 可完全禁止写该文件，此时工具调用返回 400，纯聊天不受影响。

文件必须是合法 JSON。0 字节或截断时管理器会拒绝改动，工具轮次失败；写 `{}`（或写回有效配置）即可恢复。

### 托管 agent 插件

system prompt 走托管插件目录 `~/.gemini/config/plugins/hermes-bridge/`（可用 `AGI_AGENT_PLUGIN_PATH` 覆盖）。每个不同的 system prompt 生成 `agents/hermes-<sha1[:12]>.md`，首次使用创建、会话启动时被 agy 发现、重复请求复用，超过 `AGI_AGENT_CACHE_MAX` 按 LRU 清理；只清理匹配 `hermes-<hash>.md` 的文件。

agy 1.5 起，agent frontmatter 的 `tools` 必须是原生工具名列表：

```yaml
tools: [read_file, edit_file, write_file, run_command, manage_task, search_web]
```

标量写法 `tools: "*"` 在 agy 1.5 下不是合法 YAML（loader 会跳过整个 agent，`--agent <slug>` 随即报 `unknown agent`），列表元素 `"*"` 也会在运行时被拒（`unknown component: tool "*" not found in registry`）。MCP 桥工具是动态注入的，不在这张注册表里。旧生成的 agent 文件内容与模板不一致时会被自动重写。

部分账号在托管 agent 轮次会遇到 HTTP 429 `RESOURCE_EXHAUSTED`，而同样的文本作为普通 prompt 却正常。此时设 `AGI_SYSTEM_PROMPT_VIA_AGENT=false` 走提示词前缀回退。

Antigravity 的基础系统上下文（身份、环境、日期）仍会垫在 agent prompt 之下，没有真正的“裸模型”替换模式。

### systemd（Linux）

`~/.config/systemd/user/agi-server.service`：

```ini
[Unit]
Description=Antigravity OpenAI Server (AGI)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/youruser/antigravity-openai-server
EnvironmentFile=/home/youruser/antigravity-openai-server/.env
Environment=PATH=/home/youruser/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=HOME=/home/youruser
ExecStart=/home/youruser/antigravity-openai-server/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8788
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now agi-server.service
loginctl enable-linger "$USER"
```

`agy` 必须在 unit 的 `PATH` 中，并且由同一用户完成登录。

### 测试

```bash
.venv/bin/python -m pytest -q     # Windows: .venv\Scripts\python.exe -m pytest -q
```

联调脚本（需要 agy 已登录；e2e 还需要服务在跑）：

```bash
.venv/bin/python tests/scripts/e2e_autonomous.py http://127.0.0.1:8788
.venv/bin/python tests/scripts/e2e_tool.py --base http://127.0.0.1:8788
.venv/bin/python tests/scripts/smoke_session.py
.venv/bin/python tests/scripts/probe_mcp.py
```

#### 基准

`scripts/bench.py` 会自建临时 uvicorn 实例，把 spawn / TTFT / 总耗时 / 吞吐的 p50/p95 写入 JSON：

```bash
.venv/bin/python scripts/bench.py --port 8799 --iters 3 --out bench.json
```

### 工作原理

```
OpenAI 客户端 ──HTTP /v1──▶ agi-server ──argv 或 stdin + stream-json──▶ agy CLI
       ▲                        │                                   │
       └──── tool_calls ────────┤        call_mcp_tool(hermesN,…)    │
       │                        │◀───── 进程内 MCP 桥 ◀──────────────┘
       └──── 工具结果 ─────────▶┘        (/mcp/<conv_id>, loopback)
```

- **自主轮次**（无 `tools`）：整段对话折叠成一个 prompt，新建 agy 会话或经缓存续接。超过 `AGI_MAX_PROMPT_CHARS` 的 prompt 改走 stdin（`--input-format stream-json`，一条 NDJSON 消息），编码由本服务显式定为 UTF-8；不再落盘，避免 agy 的文件读取器把中文为主的 UTF-8 误判成 GBK。
- **工具轮次**：一个会话对应一个 agy 子进程、一个 MCP 桥和共享 MCP 配置里的一个条目。模型调用桥接函数时子进程阻塞在 MCP 调用里；服务端返回 `tool_calls` 并把会话置为 `SUSPENDED`。带工具结果的下一次请求唤醒同一个子进程。
- **finish_reason** 为推断值（`stop`、`tool_calls`）；`usage` 含 prompt/completion/total 以及 `completion_tokens_details.reasoning_tokens`。
- 后台 GC 回收挂起过久和空闲的会话。
- 上游失败（result 非 SUCCESS，或未产出 result 就退出）以 OpenAI 错误返回，带退出码与 stderr 尾部；429 保持为 429。
- 被上游判为 `INVALID_ARGUMENT`（HTTP 400）的轮次会在新进程上重试一次；只限尚无工具结果往返的阶段，因为重启会重跑建会话时的 prompt。

### 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| `Eligibility check failed: … not available in your location` | Google 判定账号（国家）或请求 IP 无资格。换有资格的账号；VPN 不改变账号国家。 |
| `RESOURCE_EXHAUSTED (code 429)` | 上游速率/配额窗口，或托管 agent 路径。重试；只在带 system prompt 时出现就设 `AGI_SYSTEM_PROMPT_VIA_AGENT=false`。`agy -p "/usage"` 可看剩余额度。 |
| `unknown agent "hermes-…"` | 生成的 agent 被跳过（旧 frontmatter）。升级本仓库；残留 agent 文件会在下次请求时重写。 |
| 工具轮次首次请求失败 | `~/.gemini/config/mcp_config.json` 为空或非法 JSON。写 `{}`。 |
| agy 日志出现 `You are not logged into Antigravity` | 用同一个 Windows 用户跑一次 `agy` 完成登录。 |
| 回复为空且 `finish_reason: stop` | 旧版本会在没有 result 时静默结束；本 fork 现在会返回退出码和 stderr，查 `logs/agi-server.log`。 |