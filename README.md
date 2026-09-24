# Minia

Minia—short for **MiniAgent**—is a small, single-file coding-agent harness for constrained or isolated development environments.

It is designed for an OpenAI-compatible **Chat Completions** endpoint where native function/tool calling may be unavailable. Instead of relying on API-level tools, the model requests local actions through a compact text protocol:

```xml
<tool_call>
{"name":"read_file","arguments":{"path":"src/app.py","start_line":1,"end_line":220}}
</tool_call>
```

The Python host parses the request, executes the local tool, returns a `<tool_result>`, and continues the agent loop.

> **Disclaimer:** Minia is an independent experimental project. It is not affiliated with, endorsed by, or sponsored by OpenAI.

## Why

Minia is intended for environments where you have a capable chat-completions model endpoint and a Python runtime, but cannot install a larger coding-agent stack.

The design is deliberately small:

- Python standard library only;
- one executable file;
- no Node.js dependency;
- no native function-calling requirement;
- durable sessions stored inside each project;
- Git-aware review before completion.

The text-tool approach is useful when an API gateway accepts ordinary Chat Completions but strips or does not support native `tools` / `tool_choice`.

## Features

- Python standard library only; no `pip install` required.
- OpenAI-compatible `/chat/completions` client.
- Text/JSON tool-call protocol.
- Workspace-confined file operations.
- File reading, listing, regex search, targeted patching, and full-file writes.
- Local shell command execution with timeouts and a destructive-command blocklist.
- Git-aware project detection and final diff review.
- `AGENTS.md` project instructions.
- Automatic planning for nontrivial tasks.
- Persistent per-project plans.
- Automatic context compaction.
- Lossless JSONL transcript storage.
- Resumable sessions.
- Multiple independent projects.
- Atomic state checkpoints.
- Interactive and one-shot CLI modes.
- ANSI terminal UI with distinct `YOU`, `MINIA`, tool, and error output.
- `NO_COLOR` and `--no-color` support.

## Requirements

- Python 3.11+.
- Git is strongly recommended.
- An OpenAI-compatible Chat Completions endpoint.
- An API key/token accepted by that endpoint.

## Installation

Minia does not require a package manager or installer. Put the single `minia.py` file in any convenient directory and run it with Python.

For example, on Windows:

```powershell
New-Item -ItemType Directory -Force H:\minia
```

Then place the code from `minia.py` in:

```text
H:\minia\minia.py
```

You can copy the file there directly, or create `H:\minia\minia.py` in an editor and paste the source code into it.

Minia does **not** need to live inside the project you are working on. Run it from the root of whichever project you want it to operate on:

```powershell
cd H:\my-project
python H:\minia\minia.py
```

Minia detects the current project directory and stores that project's session state under its local `.agent/` directory.

## Configuration

Environment variables:

```powershell
$env:MINIA_API_KEY = "YOUR_API_KEY"
$env:MINIA_BASE_URL = "http://localhost:8081/dss/api/v1/proxy/v1/"
$env:MINIA_MODEL = "gpt-5"
```

The bundled defaults currently use:

```text
MINIA_BASE_URL=http://localhost:8081/dss/api/v1/proxy/v1/
MINIA_MODEL=gpt-5
```

An approximate context budget can also be set:

```powershell
$env:MINIA_CONTEXT_BUDGET = "120000"
```

### Environment-specific headers

The API client currently sends a small set of compatibility headers modeled on an environment where they were required by a local gateway. They are **not a general requirement for OpenAI-compatible APIs** and may need to be removed or changed for other environments.

## Quick tutorial

Assume Minia is installed at `H:\minia\minia.py` and the project you want to work on is `H:\my-project`.

Open PowerShell and move into the project:

```powershell
cd H:\my-project
```

Then start Minia interactively:

```powershell
python H:\minia\minia.py
```

Or give it a task immediately:

```powershell
python H:\minia\minia.py "inspect this project and explain its architecture"
```

Because Minia uses the **current working directory** as the project workspace, the executable itself can remain in `H:\minia` while you use it across many unrelated repositories.

## Usage

From the root of a project:

```powershell
python H:\minia\minia.py "inspect this project and fix the failing tests"
```

Start an interactive session:

```powershell
python H:\minia\minia.py
```

Resume the latest session for the current project:

```powershell
python H:\minia\minia.py --resume
```

Resume and immediately provide another instruction:

```powershell
python H:\minia\minia.py --resume "continue debugging the parser failure"
```

List sessions:

```powershell
python H:\minia\minia.py --sessions
```

Resume a specific session:

```powershell
python H:\minia\minia.py --resume --session 20260924-113800-fix-parser
```

Force context compaction:

```powershell
python H:\minia\minia.py --resume --compact
```

Disable color:

```powershell
python H:\minia\minia.py --no-color
```

or:

```powershell
$env:NO_COLOR = "1"
python H:\minia\minia.py
```

## Terminal UI

Interactive sessions visually separate your input, Minia's final responses, and intermediate tool activity:

```text
YOU
› Fix the parser and run the tests.

  • read_file  src/parser.py
  • search_files  parse_record
  • apply_patch  src/parser.py
  • execute_command  python -m pytest

MINIA
Fixed the parser and verified the test suite.
```

Normal mode keeps tool output terse. Use `--debug` when you want raw API responses and context diagnostics.

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

For Git repositories, Minia attempts to add `.agent/` to `.git/info/exclude`, avoiding changes to the project's tracked `.gitignore`.

### Compaction

The full transcript remains in `transcript.jsonl`. When active context grows too large, older turns are summarized into `summary.md` while recent turns remain verbatim. Resume reconstructs working context from persistent state, the compacted summary, and recent transcript entries.

### Planning

Nontrivial tasks are automatically classified for planning. The plan is stored in `state.json`, survives compaction, and remains available when the session is resumed later.

## Safety

`execute_command` is **not a security sandbox**.

File-oriented tools enforce workspace boundaries, and Minia blocks several obvious destructive shell/Git commands, but arbitrary shell execution remains powerful. Run it with the permissions and isolation appropriate for the repository you are working in.

## Status

Experimental. The implementation is intentionally small and expected to evolve based on real-world use.
