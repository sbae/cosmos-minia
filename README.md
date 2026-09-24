# cosmos-minia

A small, single-file coding-agent harness for constrained or isolated development environments.

It is designed for an OpenAI-compatible **Chat Completions** endpoint where native function/tool calling may be unavailable. Instead of relying on API-level tools, the model requests local actions through a compact text protocol:

```xml
<tool_call>
{"name":"read_file","arguments":{"path":"src/app.py","start_line":1,"end_line":220}}
</tool_call>
```

The Python host parses the request, executes the local tool, returns a `<tool_result>`, and continues the agent loop.

> [!CAUTION]
> ## NOT YET TESTED ON THE TARGET SYSTEM
>
> This is an initial prototype and **has not yet been run end-to-end in the intended isolated Windows 10 workspace**.
>
> The code was prepared for that environment and checked outside it, but the following remain unverified on the actual target system:
>
> - compatibility with the local `localhost:8081` proxy;
> - whether the target `gpt-5` endpoint reliably follows the `<tool_call>` protocol;
> - whether all Kilo-compatible request headers are necessary or sufficient;
> - Windows shell quoting and command behavior across real project workflows;
> - automatic planning and compaction over long sessions;
> - multi-day session resume/recovery;
> - editing/testing behavior on nontrivial repositories.
>
> Treat this repository as experimental until those checks are completed. Start in a disposable repository and use `--debug` for the first runs.

## Why

This was built for an environment with:

- Windows 10;
- Python 3.13 available;
- Git available;
- no usable Node.js CLI;
- no Codex/OpenCode installation;
- an existing Kilo Code 4.81 setup that can reach a local OpenAI-like proxy;
- a proxy/model combination that appears to accept Chat Completions but not native OpenAI tool calls.

Kilo 4.81 was observed to implement tools through a prompt-defined XML protocol rather than native API function calls. `miniagent.py` uses the same general idea while keeping the harness small and adding durable sessions, compaction, and planning.

## Current features

- Python standard library only; no `pip install` required.
- OpenAI-compatible `/chat/completions` client.
- Text/JSON tool-call protocol.
- Workspace-confined file operations.
- File reading, listing, regex search, targeted patching, and full-file writes.
- Local shell command execution with timeouts and a small destructive-command blocklist.
- Git-aware project detection and final diff review.
- `AGENTS.md` project instructions.
- Automatic planning for nontrivial tasks.
- Persistent per-project plans.
- Automatic context compaction.
- Lossless JSONL transcript storage.
- Resumable sessions.
- Multiple independent projects.
- Atomic state checkpoints after activity.
- Interactive and one-shot CLI modes.

## Requirements

- Python 3.11+.
- Git is strongly recommended.
- An OpenAI-compatible Chat Completions endpoint.
- An API key/token accepted by that endpoint.

The original target environment uses Python 3.13.9 and Git 2.55.0.

## Configuration

Environment variables:

```powershell
$env:MINIAGENT_API_KEY = "YOUR_API_KEY"
$env:MINIAGENT_BASE_URL = "http://localhost:8081/dss/api/v1/proxy/v1/"
$env:MINIAGENT_MODEL = "gpt-5"
```

`MINIAGENT_BASE_URL` and `MINIAGENT_MODEL` already default to those last two values, so normally only the API key is required in the target environment.

An approximate context budget can also be set:

```powershell
$env:MINIAGENT_CONTEXT_BUDGET = "120000"
```

### Environment-specific headers

The API client currently sends headers observed from Kilo Code 4.81, including:

- `HTTP-Referer: https://kilocode.ai`
- `X-Title: Kilo Code`
- `X-KiloCode-Version: 4.81.0`
- `User-Agent: Kilo-Code/4.81.0`
- several `X-Stainless-*` headers.

These appear to be relevant to the local enterprise proxy. They are **not a general requirement for OpenAI-compatible APIs** and may need to be removed or changed for other environments.

## Usage

From the root of a project:

```powershell
python path\to\miniagent.py "inspect this project and fix the failing tests"
```

Start an interactive session:

```powershell
python path\to\miniagent.py
```

Resume the latest session for the current project:

```powershell
python path\to\miniagent.py --resume
```

Resume and immediately provide another instruction:

```powershell
python path\to\miniagent.py --resume "continue debugging the parser failure"
```

List sessions for the current project:

```powershell
python path\to\miniagent.py --sessions
```

Resume a specific session by ID or unique prefix:

```powershell
python path\to\miniagent.py --resume --session 20260924-113800-fix-parser
```

Force context compaction before continuing:

```powershell
python path\to\miniagent.py --resume --compact
```

For initial testing on the target machine:

```powershell
python path\to\miniagent.py --debug "inspect the repository and explain its architecture"
```

## Agent tools

The model can request:

- `read_file`
- `list_files`
- `search_files`
- `apply_patch`
- `write_file`
- `execute_command`
- `update_plan`
- `ask_user`
- `finish`

The protocol intentionally avoids native function calling so it can work through constrained Chat Completions proxies.

## Sessions and persistence

Each project gets a local state directory:

```text
.agent/
├── project.json
└── sessions/
    └── <session-id>/
        ├── state.json
        ├── summary.md
        └── transcript.jsonl
```

For Git repositories, the harness attempts to add `.agent/` to `.git/info/exclude`, avoiding changes to the project's tracked `.gitignore`.

### Compaction

The full transcript remains in `transcript.jsonl`. When active context grows too large, older turns are summarized into `summary.md` while recent turns remain verbatim. Resume reconstructs working context from persistent state, the compacted summary, and recent transcript entries.

### Planning

Nontrivial tasks are automatically classified for planning. The plan is stored in `state.json`, survives compaction, and remains available when the session is resumed later.

## Safety notes

`execute_command` is **not a security sandbox**.

File-oriented tools enforce workspace boundaries, and the harness blocks several obvious destructive shell/Git commands, but arbitrary shell execution remains powerful. A capable or misbehaving model can still run commands with the permissions of the user running `miniagent.py`.

Recommended initial use:

1. Use a disposable test repository.
2. Keep important work committed.
3. Run with `--debug`.
4. Review `git status` and `git diff` after each early task.
5. Do not expose secrets in project files or prompts unless the endpoint is approved to receive them.

## Status

Prototype / experimental.

The immediate next milestone is an end-to-end test on the intended isolated Windows 10 system, followed by fixes based on real proxy, shell, compaction, and multi-session behavior.
