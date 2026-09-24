#!/usr/bin/env python3
"""
miniagent.py - single-file, stdlib-only coding agent harness.

Designed for an OpenAI-compatible Chat Completions endpoint that may NOT support
native function calling. The model requests local tools through a compact
text/JSON protocol wrapped in <tool_call>...</tool_call> tags.

Python: 3.11+ (tested syntactically with 3.13-compatible stdlib APIs)
Dependencies: none

Environment variables:
  MINIAGENT_BASE_URL   default: http://localhost:8081/dss/api/v1/proxy/v1/
  MINIAGENT_API_KEY    API key/token
  MINIAGENT_MODEL      default: gpt-5
  MINIAGENT_CONTEXT_BUDGET default: 120000 (approx input-token budget)

Examples:
  python miniagent.py "fix the failing tests"
  python miniagent.py --resume
  python miniagent.py --resume --session 20260924-113800-fix-parser
  python miniagent.py --sessions
  python miniagent.py --resume "continue and finish the refactor"
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import fnmatch
import getpass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Iterable


APP_NAME = "miniagent"
STATE_DIR_NAME = ".agent"
DEFAULT_BASE_URL = "http://localhost:8081/dss/api/v1/proxy/v1/"
DEFAULT_MODEL = "gpt-5"
DEFAULT_CONTEXT_BUDGET = 120_000
DEFAULT_MAX_STEPS = 80
DEFAULT_COMMAND_TIMEOUT = 180
COMPACTION_THRESHOLD = 0.68
RECENT_MESSAGES_TO_KEEP = 14
MAX_TOOL_OUTPUT_CHARS = 60_000
MAX_DIFF_REVIEW_CHARS = 30_000
MAX_FILE_READ_CHARS = 80_000
MAX_SEARCH_FILE_BYTES = 2_000_000
MAX_SEARCH_MATCHES = 250
MAX_LIST_ENTRIES = 1000

TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


SYSTEM_PROMPT = r"""
You are MiniAgent, a coding agent operating inside one local project workspace.
You do not have native API tools. Instead, request exactly one host tool at a
time by emitting one JSON object wrapped in <tool_call> tags.

Example:
<tool_call>
{"name":"read_file","arguments":{"path":"src/app.py","start_line":1,"end_line":220}}
</tool_call>

Do not put a tool call in a Markdown code fence. When you request a tool, output
only the <tool_call> block and no explanatory prose. The host will execute it
and return a <tool_result> message. Continue from that result.

Available tools:

1) read_file
   Read a UTF-8-ish text file with line numbers.
   arguments: {"path": string, "start_line"?: int, "end_line"?: int}

2) list_files
   List files/directories below the workspace.
   arguments: {"path"?: string, "recursive"?: bool, "pattern"?: string}

3) search_files
   Regex search text files with line numbers and context.
   arguments: {"path"?: string, "regex": string, "file_pattern"?: string,
               "ignore_case"?: bool, "context"?: int}

4) apply_patch
   Exact targeted replacement in one file. Prefer this over rewriting a file.
   arguments: {"path": string, "old": string, "new": string,
               "occurrence"?: int}
   occurrence is 1-based and defaults to 1. The host returns useful nearby text
   if the exact old text does not match.

5) write_file
   Create or completely rewrite a text file. Use only when a targeted patch is
   inappropriate.
   arguments: {"path": string, "content": string}

6) execute_command
   Run a shell/CLI command with the workspace as cwd unless a workspace-relative
   cwd is provided.
   arguments: {"command": string, "cwd"?: string, "timeout"?: int}

7) update_plan
   Persist the current plan. Use it when scope or findings change.
   arguments: {"steps": [{"text": string, "status": "pending"|"in_progress"|"done"}]}
   Keep only one step in_progress. Plans should be short and operational.

8) ask_user
   Ask a blocking clarification only when the missing information cannot be
   discovered from the workspace.
   arguments: {"question": string}

9) finish
   Request completion after the task is genuinely done.
   arguments: {"summary": string, "tests"?: string}
   The first finish request may trigger an automatic Git review. If so, inspect
   the returned diff/status and fix anything needed, then call finish again.

Operating rules:
- Inspect before editing. Search rather than guessing file locations.
- For nontrivial work, follow the persisted plan and update it when findings
  materially change the approach.
- Prefer precise edits over whole-file rewrites.
- Never intentionally access or modify files outside the workspace.
- Preserve pre-existing user changes. Never use destructive Git commands such
  as reset --hard, clean -fd, checkout -- ., or restore .
- Run relevant tests/checks after code changes when feasible.
- Inspect the resulting Git diff before completion. The host also performs an
  automatic final Git review.
- Tool errors are information: diagnose and recover rather than pretending the
  action succeeded.
- If the task can be completed, do the work rather than only describing it.
- Do not claim a test passed unless its command output says it passed.
""".strip()


PLANNER_PROMPT = r"""
Decide whether this coding task needs an explicit multi-step plan. Return ONLY
valid JSON, no Markdown:
{"needs_plan": true|false, "steps": ["step 1", "step 2", ...]}

Use needs_plan=false only for genuinely trivial single-action work such as a
small typo or one obvious localized edit. For debugging, implementation,
refactors, multi-file work, unclear failures, or tasks requiring tests, use a
short 3-6 step plan. Include testing/verification as the final step.
""".strip()


COMPACTION_PROMPT = r"""
You are compacting a coding-agent session for durable continuation later.
Produce a concise but information-dense Markdown working summary. Preserve only
facts useful for continuing the task, especially:
- original/current task and success criteria
- decisions and rationale that constrain future work
- current plan/progress
- files inspected and why they matter
- files modified and exact nature of changes
- important symbols, interfaces, commands, errors, and test results
- unresolved problems / next action
- user preferences or constraints stated in this session

Do NOT include conversational filler. Do NOT invent anything. Distinguish
completed work from proposed work. This summary will replace older raw turns in
the model's active context, while the full transcript remains stored on disk.
""".strip()


class MiniAgentError(RuntimeError):
    pass


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def slugify(text: str, max_len: int = 42) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "-", text.strip().lower()).strip("-")
    return (text[:max_len].rstrip("-") or "session")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def run_process(args: list[str], cwd: Path, timeout: int = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        timeout=timeout,
        shell=False,
    )


def detect_project_root(cwd: Path) -> tuple[Path, bool]:
    try:
        cp = run_process(["git", "rev-parse", "--show-toplevel"], cwd, timeout=5)
        if cp.returncode == 0 and cp.stdout.strip():
            return Path(cp.stdout.strip()).resolve(), True
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    return cwd.resolve(), False


def get_git_remote(root: Path) -> str | None:
    try:
        cp = run_process(["git", "config", "--get", "remote.origin.url"], root, timeout=5)
        value = cp.stdout.strip()
        return value or None
    except Exception:
        return None


def ensure_agent_ignored(root: Path, is_git: bool) -> None:
    """Ignore .agent locally without modifying the repository's .gitignore."""
    if not is_git:
        return
    try:
        cp = run_process(["git", "rev-parse", "--git-path", "info/exclude"], root, timeout=5)
        if cp.returncode != 0 or not cp.stdout.strip():
            return
        info_exclude = Path(cp.stdout.strip())
        if not info_exclude.is_absolute():
            info_exclude = (root / info_exclude).resolve()
        if not info_exclude.exists():
            return
        existing = info_exclude.read_text(encoding="utf-8", errors="replace")
        marker = f"{STATE_DIR_NAME}/"
        if marker not in {line.strip() for line in existing.splitlines()}:
            with info_exclude.open("a", encoding="utf-8") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write(marker + "\n")
    except (OSError, subprocess.SubprocessError):
        pass


def normalize_endpoint(base_url: str) -> str:
    base_url = base_url.strip()
    if not base_url:
        raise MiniAgentError("Missing base URL. Set MINIAGENT_BASE_URL or use --base-url.")
    if base_url.rstrip("/").endswith("/chat/completions"):
        return base_url.rstrip("/")
    return base_url.rstrip("/") + "/chat/completions"


def normalize_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return str(content)


class APIClient:
    def __init__(self, base_url: str, api_key: str, model: str, debug: bool = False):
        self.endpoint = normalize_endpoint(base_url)
        self.api_key = api_key
        self.model = model
        self.debug = debug
        self.last_usage: dict[str, Any] = {}

    def _headers(self) -> dict[str, str]:
        # These mirror the headers that were observed from Kilo Code 4.81.
        # The local enterprise proxy may use some of them as a compatibility shim.
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "HTTP-Referer": "https://kilocode.ai",
            "X-Title": "Kilo Code",
            "X-KiloCode-Version": "4.81.0",
            "User-Agent": "Kilo-Code/4.81.0",
            "X-Stainless-Retry-Count": "0",
            "X-Stainless-Lang": "js",
            "X-Stainless-Package-Version": "5.5.1",
            "X-Stainless-OS": "Windows",
            "X-Stainless-Arch": "x64",
            "X-Stainless-Runtime": "node",
            "X-Stainless-Runtime-Version": "v24.18.1",
        }

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_retries: int = 3,
    ) -> tuple[str, dict[str, Any]]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        if temperature is not None:
            payload["temperature"] = temperature

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last_error: Exception | None = None

        for attempt in range(max_retries):
            req = urllib.request.Request(
                self.endpoint,
                data=data,
                headers=self._headers(),
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=180) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                    if self.debug:
                        print("\n[debug] raw API response:\n" + raw + "\n", file=sys.stderr)
                    obj = json.loads(raw)
                    self.last_usage = obj.get("usage") or {}
                    choices = obj.get("choices") or []
                    if not choices:
                        raise MiniAgentError(f"API response had no choices: {raw[:1000]}")
                    message = choices[0].get("message") or {}
                    return normalize_content(message.get("content")), obj
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                last_error = MiniAgentError(f"HTTP {e.code}: {body}")
                # Retry only typical transient statuses.
                if e.code not in (408, 429, 500, 502, 503, 504) or attempt == max_retries - 1:
                    raise last_error
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                last_error = e
                if attempt == max_retries - 1:
                    raise MiniAgentError(f"API request failed: {e}") from e
            time.sleep(1.5 * (attempt + 1))

        raise MiniAgentError(f"API request failed: {last_error}")


class SessionStore:
    def __init__(self, root: Path, is_git: bool):
        self.root = root
        self.is_git = is_git
        self.agent_dir = root / STATE_DIR_NAME
        self.sessions_dir = self.agent_dir / "sessions"
        self.project_path = self.agent_dir / "project.json"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        ensure_agent_ignored(root, is_git)
        self._ensure_project_metadata()

    def _ensure_project_metadata(self) -> None:
        current = {
            "root": str(self.root),
            "git_repository": self.is_git,
            "remote_origin": get_git_remote(self.root) if self.is_git else None,
            "updated_at": now_iso(),
        }
        if self.project_path.exists():
            try:
                old = json.loads(self.project_path.read_text(encoding="utf-8"))
                current["created_at"] = old.get("created_at", now_iso())
            except Exception:
                current["created_at"] = now_iso()
        else:
            current["created_at"] = now_iso()
        atomic_write_json(self.project_path, current)

    def list_sessions(self) -> list[dict[str, Any]]:
        out = []
        for d in self.sessions_dir.iterdir():
            if not d.is_dir():
                continue
            state_path = d / "state.json"
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            state["session_id"] = d.name
            out.append(state)
        out.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
        return out

    def latest_session_id(self) -> str | None:
        sessions = self.list_sessions()
        return sessions[0]["session_id"] if sessions else None

    def create(self, task: str) -> "Session":
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        session_id = f"{stamp}-{slugify(task)}-{uuid.uuid4().hex[:5]}"
        path = self.sessions_dir / session_id
        path.mkdir(parents=True, exist_ok=False)
        state = {
            "session_id": session_id,
            "task": task,
            "status": "active",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "plan": [],
            "needs_plan": None,
            "modified_files": [],
            "important_files": [],
            "compacted_through": 0,
            "last_prompt_tokens": None,
            "tool_calls": 0,
            "finish_review_ready": False,
            "last_tests": None,
        }
        atomic_write_json(path / "state.json", state)
        atomic_write_text(path / "summary.md", "")
        atomic_write_text(path / "transcript.jsonl", "")
        return Session(path)

    def load(self, session_id: str | None) -> "Session":
        if not session_id or session_id == "latest":
            session_id = self.latest_session_id()
        if not session_id:
            raise MiniAgentError("No session exists for this project.")
        path = self.sessions_dir / session_id
        if not path.is_dir():
            # Permit unique prefix matching.
            matches = [d for d in self.sessions_dir.iterdir() if d.is_dir() and d.name.startswith(session_id)]
            if len(matches) == 1:
                path = matches[0]
            elif len(matches) > 1:
                raise MiniAgentError(f"Session prefix is ambiguous: {session_id}")
            else:
                raise MiniAgentError(f"Session not found: {session_id}")
        return Session(path)


class Session:
    def __init__(self, path: Path):
        self.path = path
        self.state_path = path / "state.json"
        self.summary_path = path / "summary.md"
        self.transcript_path = path / "transcript.jsonl"
        self.state = json.loads(self.state_path.read_text(encoding="utf-8"))

    @property
    def session_id(self) -> str:
        return self.path.name

    def save_state(self) -> None:
        self.state["updated_at"] = now_iso()
        atomic_write_json(self.state_path, self.state)

    def append(self, role: str, content: str, kind: str = "message") -> None:
        record = {
            "ts": now_iso(),
            "role": role,
            "kind": kind,
            "content": content,
        }
        with self.transcript_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self.save_state()

    def records(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not self.transcript_path.exists():
            return out
        with self.transcript_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def summary(self) -> str:
        try:
            return self.summary_path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def set_summary(self, text: str, compacted_through: int) -> None:
        atomic_write_text(self.summary_path, text.strip() + "\n")
        self.state["compacted_through"] = compacted_through
        self.save_state()


class WorkspaceTools:
    def __init__(self, root: Path, session: Session, is_git: bool):
        self.root = root
        self.session = session
        self.is_git = is_git

    def _resolve(self, relative: str | None, *, must_exist: bool = False) -> Path:
        relative = relative or "."
        p = (self.root / relative).resolve()
        try:
            p.relative_to(self.root)
        except ValueError:
            raise MiniAgentError(f"Path escapes workspace: {relative}")
        if must_exist and not p.exists():
            raise MiniAgentError(f"Path not found: {relative}")
        return p

    def _rel(self, p: Path) -> str:
        try:
            return str(p.resolve().relative_to(self.root)).replace("\\", "/")
        except ValueError:
            return str(p)

    def read_file(self, args: dict[str, Any]) -> str:
        path = self._resolve(str(args.get("path", "")), must_exist=True)
        if not path.is_file():
            raise MiniAgentError(f"Not a file: {self._rel(path)}")
        start = max(1, int(args.get("start_line") or 1))
        end_raw = args.get("end_line")
        end = int(end_raw) if end_raw not in (None, "") else None
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        if end is None:
            end = len(lines)
        end = max(start, min(end, len(lines)))
        selected = lines[start - 1:end]
        body = "\n".join(f"{i:6d} | {line}" for i, line in enumerate(selected, start=start))
        if len(body) > MAX_FILE_READ_CHARS:
            body = body[:MAX_FILE_READ_CHARS] + "\n...[truncated by host]"
        self._mark_important(path)
        return f"FILE {self._rel(path)} lines {start}-{end} of {len(lines)}\n{body}"

    def list_files(self, args: dict[str, Any]) -> str:
        base = self._resolve(str(args.get("path") or "."), must_exist=True)
        recursive = bool(args.get("recursive", False))
        pattern = str(args.get("pattern") or "*")
        entries: list[str] = []
        iterator: Iterable[Path]
        if base.is_file():
            iterator = [base]
        elif recursive:
            iterator = base.rglob("*")
        else:
            iterator = base.iterdir()
        for p in iterator:
            try:
                rel = self._rel(p)
            except OSError:
                continue
            if self._skip_path(p):
                continue
            if not fnmatch.fnmatch(p.name, pattern) and not fnmatch.fnmatch(rel, pattern):
                continue
            suffix = "/" if p.is_dir() else ""
            entries.append(rel + suffix)
            if len(entries) >= MAX_LIST_ENTRIES:
                entries.append("...[truncated by host]")
                break
        entries.sort()
        return "\n".join(entries) if entries else "(no matching entries)"

    def search_files(self, args: dict[str, Any]) -> str:
        base = self._resolve(str(args.get("path") or "."), must_exist=True)
        regex = str(args.get("regex") or "")
        if not regex:
            raise MiniAgentError("search_files requires regex")
        pattern = str(args.get("file_pattern") or "*")
        ignore_case = bool(args.get("ignore_case", False))
        context = max(0, min(5, int(args.get("context") or 0)))
        flags = re.IGNORECASE if ignore_case else 0
        try:
            rx = re.compile(regex, flags)
        except re.error as e:
            raise MiniAgentError(f"Invalid regex: {e}")

        files = [base] if base.is_file() else base.rglob("*")
        out: list[str] = []
        matches = 0
        for p in files:
            if matches >= MAX_SEARCH_MATCHES:
                break
            if not p.is_file() or self._skip_path(p):
                continue
            rel = self._rel(p)
            if not fnmatch.fnmatch(p.name, pattern) and not fnmatch.fnmatch(rel, pattern):
                continue
            try:
                if p.stat().st_size > MAX_SEARCH_FILE_BYTES:
                    continue
                data = p.read_bytes()
                if b"\x00" in data[:4096]:
                    continue
                text = data.decode("utf-8", errors="replace")
            except OSError:
                continue
            lines = text.splitlines()
            hit_indices = [i for i, line in enumerate(lines) if rx.search(line)]
            if not hit_indices:
                continue
            self._mark_important(p)
            emitted_ranges: list[tuple[int, int]] = []
            for idx in hit_indices:
                if matches >= MAX_SEARCH_MATCHES:
                    break
                lo = max(0, idx - context)
                hi = min(len(lines), idx + context + 1)
                if emitted_ranges and lo <= emitted_ranges[-1][1]:
                    lo = emitted_ranges[-1][1]
                if lo >= hi:
                    continue
                emitted_ranges.append((lo, hi))
                out.append(f"--- {rel}:{idx + 1} ---")
                for j in range(lo, hi):
                    marker = ">" if j == idx else " "
                    out.append(f"{marker}{j + 1:6d} | {lines[j]}")
                matches += 1
        if matches >= MAX_SEARCH_MATCHES:
            out.append(f"...[stopped after {MAX_SEARCH_MATCHES} matches]")
        return "\n".join(out) if out else "(no matches)"

    def apply_patch(self, args: dict[str, Any]) -> str:
        path = self._resolve(str(args.get("path", "")), must_exist=True)
        if not path.is_file():
            raise MiniAgentError(f"Not a file: {self._rel(path)}")
        old = args.get("old")
        new = args.get("new")
        if not isinstance(old, str) or not isinstance(new, str):
            raise MiniAgentError("apply_patch requires string old and new")
        if old == "":
            raise MiniAgentError("apply_patch old text must not be empty")
        occurrence = max(1, int(args.get("occurrence") or 1))
        text = path.read_text(encoding="utf-8", errors="replace")
        positions = [m.start() for m in re.finditer(re.escape(old), text)]
        if len(positions) < occurrence:
            hint = self._closest_block_hint(text, old)
            raise MiniAgentError(
                f"Exact text not found for occurrence {occurrence}; found {len(positions)} exact occurrence(s)."
                + ("\nClosest nearby block:\n" + hint if hint else "")
            )
        pos = positions[occurrence - 1]
        updated = text[:pos] + new + text[pos + len(old):]
        atomic_write_text(path, updated)
        self._mark_modified(path)
        self.session.state["finish_review_ready"] = False
        self.session.save_state()
        return f"Patched {self._rel(path)} successfully ({len(old)} chars -> {len(new)} chars)."

    def write_file(self, args: dict[str, Any]) -> str:
        path = self._resolve(str(args.get("path", "")), must_exist=False)
        content = args.get("content")
        if not isinstance(content, str):
            raise MiniAgentError("write_file requires string content")
        existed = path.exists()
        atomic_write_text(path, content)
        self._mark_modified(path)
        self.session.state["finish_review_ready"] = False
        self.session.save_state()
        return f"{'Rewrote' if existed else 'Created'} {self._rel(path)} ({len(content)} chars)."

    def execute_command(self, args: dict[str, Any]) -> str:
        command = str(args.get("command") or "").strip()
        if not command:
            raise MiniAgentError("execute_command requires command")
        cwd = self._resolve(str(args.get("cwd") or "."), must_exist=True)
        if not cwd.is_dir():
            raise MiniAgentError("execute_command cwd is not a directory")
        timeout = max(1, min(1800, int(args.get("timeout") or DEFAULT_COMMAND_TIMEOUT)))
        self._reject_dangerous_command(command)

        # shell=True intentionally mirrors a developer terminal. File-oriented tools
        # are strictly workspace-confined; shell commands rely on the model's rules
        # plus the explicit destructive-command block below.
        try:
            cp = subprocess.run(
                command,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                timeout=timeout,
                shell=True,
            )
            output = cp.stdout or ""
        except subprocess.TimeoutExpired as e:
            output = (e.stdout or "") if isinstance(e.stdout, str) else ""
            return f"COMMAND TIMED OUT after {timeout}s\n{output[-MAX_TOOL_OUTPUT_CHARS:]}"

        if len(output) > MAX_TOOL_OUTPUT_CHARS:
            output = output[: MAX_TOOL_OUTPUT_CHARS // 2] + "\n...[truncated by host]...\n" + output[-MAX_TOOL_OUTPUT_CHARS // 2 :]

        self._refresh_git_modified()
        low = command.lower()
        if any(x in low for x in ("pytest", "unittest", "npm test", "cargo test", "go test", "dotnet test", "mvn test", "gradle test")):
            self.session.state["last_tests"] = {
                "command": command,
                "exit_code": cp.returncode,
                "at": now_iso(),
            }
        # A command can mutate files, so force another final review.
        if not self._looks_read_only(command):
            self.session.state["finish_review_ready"] = False
        self.session.save_state()
        return f"EXIT CODE: {cp.returncode}\nCWD: {self._rel(cwd) or '.'}\n{output}"

    def update_plan(self, args: dict[str, Any]) -> str:
        raw_steps = args.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise MiniAgentError("update_plan requires a non-empty steps array")
        steps: list[dict[str, str]] = []
        in_progress = 0
        for item in raw_steps[:10]:
            if isinstance(item, str):
                item = {"text": item, "status": "pending"}
            if not isinstance(item, dict) or not str(item.get("text") or "").strip():
                raise MiniAgentError("Each plan step needs text")
            status = str(item.get("status") or "pending")
            if status not in {"pending", "in_progress", "done"}:
                raise MiniAgentError(f"Invalid plan status: {status}")
            if status == "in_progress":
                in_progress += 1
            steps.append({"text": str(item["text"]).strip(), "status": status})
        if in_progress > 1:
            raise MiniAgentError("At most one plan step may be in_progress")
        self.session.state["plan"] = steps
        self.session.state["needs_plan"] = True
        self.session.save_state()
        return self._format_plan()

    def ask_user(self, args: dict[str, Any]) -> dict[str, Any]:
        question = str(args.get("question") or "").strip()
        if not question:
            raise MiniAgentError("ask_user requires question")
        return {"ask_user": question}

    def finish(self, args: dict[str, Any]) -> dict[str, Any] | str:
        summary = str(args.get("summary") or "").strip()
        tests = str(args.get("tests") or "").strip()
        if not summary:
            raise MiniAgentError("finish requires summary")

        if not self.session.state.get("finish_review_ready"):
            review = self._git_final_review()
            self.session.state["finish_review_ready"] = True
            self.session.save_state()
            return (
                "AUTOMATIC FINAL REVIEW (first finish request)\n"
                "Inspect this before completing. If anything needs correction, fix it. "
                "Then call finish again.\n\n" + review
            )

        self.session.state["status"] = "completed"
        plan = self.session.state.get("plan") or []
        for item in plan:
            item["status"] = "done"
        self.session.state["plan"] = plan
        self.session.save_state()
        return {"finished": True, "summary": summary, "tests": tests}

    def dispatch(self, name: str, args: dict[str, Any]) -> Any:
        fn = getattr(self, name, None)
        if name not in {
            "read_file", "list_files", "search_files", "apply_patch", "write_file",
            "execute_command", "update_plan", "ask_user", "finish"
        } or fn is None:
            raise MiniAgentError(f"Unknown tool: {name}")
        self.session.state["tool_calls"] = int(self.session.state.get("tool_calls") or 0) + 1
        self.session.save_state()
        return fn(args)

    def _skip_path(self, p: Path) -> bool:
        parts = set(p.parts)
        return ".git" in parts or STATE_DIR_NAME in parts or "__pycache__" in parts

    def _mark_important(self, path: Path) -> None:
        rel = self._rel(path)
        files = list(self.session.state.get("important_files") or [])
        if rel not in files:
            files.append(rel)
            self.session.state["important_files"] = files[-100:]
            self.session.save_state()

    def _mark_modified(self, path: Path) -> None:
        rel = self._rel(path)
        files = list(self.session.state.get("modified_files") or [])
        if rel not in files:
            files.append(rel)
            self.session.state["modified_files"] = files[-100:]
        self._mark_important(path)
        self.session.save_state()

    def _closest_block_hint(self, text: str, old: str) -> str:
        old_lines = old.splitlines()
        lines = text.splitlines()
        if not old_lines or not lines:
            return ""
        target = "\n".join(old_lines[: min(8, len(old_lines))])
        window = max(1, min(len(old_lines), 12))
        best_ratio = 0.0
        best: tuple[int, int] | None = None
        for i in range(0, len(lines), max(1, window // 3)):
            chunk = "\n".join(lines[i:i + window])
            ratio = difflib.SequenceMatcher(None, target, chunk).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best = (i, min(len(lines), i + window))
        if not best or best_ratio < 0.20:
            return ""
        lo, hi = best
        return "\n".join(f"{i + 1:6d} | {lines[i]}" for i in range(lo, hi))

    def _reject_dangerous_command(self, command: str) -> None:
        compact = re.sub(r"\s+", " ", command.strip().lower())
        banned = [
            r"git\s+reset\s+--hard",
            r"git\s+clean\s+-[^ ]*f",
            r"git\s+checkout\s+--\s+\.",
            r"git\s+restore\s+\.",
            r"rm\s+-rf\s+[/~]",
            r"format\s+[a-z]:",
            r"shutdown\b",
            r"restart-computer\b",
            r"remove-item\b.*-recurse.*-force",
            r"del\s+/s\s+/q\s+[a-z]:\\",
        ]
        for pat in banned:
            if re.search(pat, compact, flags=re.IGNORECASE):
                raise MiniAgentError(f"Blocked destructive command: {command}")

    def _looks_read_only(self, command: str) -> bool:
        low = command.strip().lower()
        prefixes = (
            "git status", "git diff", "git log", "git show", "git grep", "git ls-files",
            "python --version", "python -v", "where ", "dir", "type ", "findstr ",
        )
        return low.startswith(prefixes)

    def _refresh_git_modified(self) -> None:
        if not self.is_git:
            return
        try:
            cp = run_process(["git", "status", "--porcelain"], self.root, timeout=10)
            files = list(self.session.state.get("modified_files") or [])
            for line in cp.stdout.splitlines():
                if len(line) < 4:
                    continue
                rel = line[3:].strip()
                if " -> " in rel:
                    rel = rel.split(" -> ", 1)[1].strip()
                rel = rel.strip('"').replace("\\", "/")
                if rel.startswith(STATE_DIR_NAME + "/"):
                    continue
                if rel and rel not in files:
                    files.append(rel)
            self.session.state["modified_files"] = files[-100:]
        except Exception:
            pass

    def _format_plan(self) -> str:
        symbols = {"done": "[x]", "in_progress": "[>]", "pending": "[ ]"}
        return "\n".join(f"{symbols.get(s['status'], '[ ]')} {s['text']}" for s in self.session.state.get("plan") or [])

    def _git_final_review(self) -> str:
        if not self.is_git:
            return "Not a Git repository. No Git diff review available."
        chunks = []
        commands = [
            ("git status --short", ["git", "status", "--short"]),
            ("git diff --check", ["git", "diff", "--check"]),
            ("git diff --cached --check", ["git", "diff", "--cached", "--check"]),
            ("git diff --stat HEAD", ["git", "diff", "--stat", "HEAD"]),
            ("git diff HEAD", ["git", "diff", "HEAD", "--no-ext-diff"]),
        ]
        status_text = ""
        for label, cmd in commands:
            try:
                cp = run_process(cmd, self.root, timeout=30)
                text = (cp.stdout or "") + (cp.stderr or "")
                if label == "git status --short":
                    status_text = text
                if label == "git diff HEAD" and len(text) > MAX_DIFF_REVIEW_CHARS:
                    half = MAX_DIFF_REVIEW_CHARS // 2
                    text = text[:half] + "\n...[diff truncated by host]...\n" + text[-half:]
                chunks.append(f"## {label}\nexit={cp.returncode}\n{text or '(no output)'}")
            except Exception as e:
                chunks.append(f"## {label}\nERROR: {e}")

        # git diff does not display untracked file contents. Include small previews so
        # the model can actually review newly created files before finishing.
        untracked: list[str] = []
        for line in status_text.splitlines():
            if line.startswith("?? "):
                untracked.append(line[3:].strip())
        if untracked:
            previews = []
            budget = 20_000
            for rel in untracked[:20]:
                if budget <= 0:
                    break
                try:
                    p = self._resolve(rel, must_exist=True)
                    if not p.is_file() or p.stat().st_size > 200_000:
                        previews.append(f"### {rel}\n(preview skipped: not a small regular file)")
                        continue
                    text = p.read_text(encoding="utf-8", errors="replace")
                    take = min(len(text), budget)
                    previews.append(f"### {rel}\n{text[:take]}")
                    budget -= take
                except Exception as e:
                    previews.append(f"### {rel}\nERROR: {e}")
            chunks.append("## Untracked file previews\n" + "\n\n".join(previews))

        last_tests = self.session.state.get("last_tests")
        if last_tests:
            chunks.append("## Last detected test command\n" + json.dumps(last_tests, ensure_ascii=False, indent=2))
        else:
            chunks.append("## Tests\nNo test command was detected by the harness in this session. Decide whether testing is applicable before finishing.")
        return "\n\n".join(chunks)



class MiniAgent:
    def __init__(
        self,
        root: Path,
        is_git: bool,
        client: APIClient,
        store: SessionStore,
        session: Session,
        *,
        context_budget: int,
        max_steps: int,
        debug: bool,
    ):
        self.root = root
        self.is_git = is_git
        self.client = client
        self.store = store
        self.session = session
        self.context_budget = context_budget
        self.max_steps = max_steps
        self.debug = debug
        self.tools = WorkspaceTools(root, session, is_git)

    def project_context(self) -> str:
        shell_note = "cmd.exe-compatible shell" if os.name == "nt" else "POSIX-compatible shell"
        chunks = [
            f"Workspace root: {self.root}",
            f"Git repository: {self.is_git}",
            f"Host platform: {sys.platform}; execute_command uses a {shell_note}.",
        ]
        if self.is_git:
            try:
                cp = run_process(["git", "status", "--short"], self.root, timeout=10)
                chunks.append("Initial/current git status --short:\n" + (cp.stdout.strip() or "(clean)"))
            except Exception:
                pass
        agents = self._load_agents_md()
        if agents:
            chunks.append("PROJECT INSTRUCTIONS (AGENTS.md):\n" + agents)
        return "\n\n".join(chunks)

    def _load_agents_md(self) -> str:
        for name in ("AGENTS.md", "agents.md"):
            p = self.root / name
            if p.is_file():
                text = p.read_text(encoding="utf-8", errors="replace")
                return text[:50_000]
        return ""
    def _state_context(self) -> str:
        s = self.session.state
        plan = s.get("plan") or []
        plan_text = "\n".join(
            f"- [{ {'done':'x','in_progress':'>','pending':' '}.get(x.get('status'), ' ') }] {x.get('text')}"
            for x in plan
        ) or "(none)"
        return textwrap.dedent(f"""
        SESSION STATE
        Session: {self.session.session_id}
        Task: {s.get('task')}
        Status: {s.get('status')}
        Plan:
        {plan_text}
        Modified files: {', '.join(s.get('modified_files') or []) or '(none recorded)'}
        Important files: {', '.join(s.get('important_files') or []) or '(none recorded)'}
        """).strip()

    def build_messages(self) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "system", "content": self.project_context()},
            {"role": "system", "content": self._state_context()},
        ]
        summary = self.session.summary().strip()
        if summary:
            messages.append({"role": "system", "content": "COMPACTED WORKING MEMORY:\n" + summary})
        records = self.session.records()
        start = int(self.session.state.get("compacted_through") or 0)
        for rec in records[start:]:
            role = rec.get("role")
            if role not in {"user", "assistant", "system"}:
                role = "user"
            messages.append({"role": role, "content": str(rec.get("content") or "")})
        return messages

    def estimate_tokens(self, messages: list[dict[str, str]]) -> int:
        # Deliberately simple and conservative enough for context management.
        chars = sum(len(m.get("content", "")) + 20 for m in messages)
        return max(1, chars // 4)

    def maybe_compact(self, force: bool = False) -> bool:
        messages = self.build_messages()
        estimate = self.estimate_tokens(messages)
        last_prompt = self.session.state.get("last_prompt_tokens")
        over = estimate >= int(self.context_budget * COMPACTION_THRESHOLD)
        if isinstance(last_prompt, int):
            over = over or last_prompt >= int(self.context_budget * COMPACTION_THRESHOLD)
        if not force and not over:
            return False

        records = self.session.records()
        start = int(self.session.state.get("compacted_through") or 0)
        if len(records) - start <= RECENT_MESSAGES_TO_KEEP + 2:
            return False
        cutoff = len(records) - RECENT_MESSAGES_TO_KEEP
        to_compact = records[start:cutoff]
        if not to_compact:
            return False

        existing = self.session.summary().strip()
        chunks = []
        if existing:
            chunks.append("EXISTING SUMMARY:\n" + existing)
        chunks.append("CURRENT SESSION STATE:\n" + self._state_context())
        chunks.append("OLDER TRANSCRIPT TO COMPACT:")
        for rec in to_compact:
            content = str(rec.get("content") or "")
            if len(content) > 24_000:
                content = content[:12_000] + "\n...[middle omitted for compaction]...\n" + content[-12_000:]
            chunks.append(f"\n[{rec.get('role')} / {rec.get('kind')}]\n{content}")

        prompt = "\n".join(chunks)
        text, obj = self.client.chat([
            {"role": "system", "content": COMPACTION_PROMPT},
            {"role": "user", "content": prompt},
        ])
        if not text.strip():
            raise MiniAgentError("Compaction returned an empty summary")
        self.session.set_summary(text, cutoff)
        usage = obj.get("usage") or {}
        if isinstance(usage.get("prompt_tokens"), int):
            self.session.state["last_prompt_tokens"] = usage["prompt_tokens"]
            self.session.save_state()
        print(f"[compacted session through transcript record {cutoff}]", file=sys.stderr)
        return True

    def auto_plan(self, task: str) -> None:
        if self.session.state.get("needs_plan") is not None:
            return
        messages = [
            {"role": "system", "content": PLANNER_PROMPT},
            {"role": "user", "content": task},
        ]
        needs_plan = True
        steps: list[str] = []
        try:
            text, _ = self.client.chat(messages)
            # tolerate accidental prose around the JSON
            m = re.search(r"\{.*\}", text, re.DOTALL)
            obj = json.loads(m.group(0) if m else text)
            needs_plan = bool(obj.get("needs_plan", True))
            raw_steps = obj.get("steps") or []
            steps = [str(x).strip() for x in raw_steps if str(x).strip()][:8]
        except Exception:
            # Reliable fallback: most coding tasks benefit from a short plan.
            words = len(task.split())
            needs_plan = words > 8 or bool(re.search(r"\b(fix|debug|implement|refactor|build|add|migrate|investigate|test)\b", task, re.I))
            if needs_plan:
                steps = ["Inspect relevant code and current state", "Implement the requested change", "Run relevant checks and review the diff"]
        self.session.state["needs_plan"] = needs_plan
        if needs_plan and steps:
            self.session.state["plan"] = [
                {"text": s, "status": "in_progress" if i == 0 else "pending"}
                for i, s in enumerate(steps)
            ]
        self.session.save_state()

    def run_task(self, user_text: str) -> str:
        if self.session.state.get("task") == "Interactive coding session" and not self.session.records():
            self.session.state["task"] = user_text
            self.session.state["needs_plan"] = None
            self.session.state["plan"] = []
        self.session.state["status"] = "active"
        self.session.state["finish_review_ready"] = False
        self.session.save_state()
        self.session.append("user", user_text)
        self.auto_plan(self.session.state.get("task") or user_text)

        for step in range(1, self.max_steps + 1):
            self.maybe_compact()
            messages = self.build_messages()
            if self.debug:
                print(f"[debug] step={step} approx_tokens={self.estimate_tokens(messages)} messages={len(messages)}", file=sys.stderr)
            text, raw = self.client.chat(messages)
            usage = raw.get("usage") or {}
            if isinstance(usage.get("prompt_tokens"), int):
                self.session.state["last_prompt_tokens"] = usage["prompt_tokens"]
                self.session.save_state()
            if not text.strip():
                raise MiniAgentError("Model returned empty content")
            self.session.append("assistant", text)

            call, parse_error = self._parse_tool_call(text)
            if parse_error:
                self.session.append(
                    "user",
                    self._tool_result_text("parser", "ERROR: " + parse_error),
                    kind="tool_result",
                )
                continue
            if call is None:
                return text

            name, tool_args = call["name"], call["arguments"]
            try:
                result = self.tools.dispatch(name, tool_args)
            except Exception as e:
                result = f"ERROR: {type(e).__name__}: {e}"

            if isinstance(result, dict) and result.get("ask_user"):
                question = str(result["ask_user"])
                print(f"\nAgent asks: {question}")
                try:
                    answer = input("You> ").strip()
                except (EOFError, KeyboardInterrupt):
                    answer = ""
                self.session.append("user", self._tool_result_text(name, f"USER ANSWER:\n{answer}"), kind="tool_result")
                continue

            if isinstance(result, dict) and result.get("finished"):
                tests = result.get("tests") or ""
                final = result.get("summary") or "Task completed."
                if tests:
                    final += "\n\nTests/checks: " + tests
                self.session.append("system", "Session marked completed by finish tool.", kind="checkpoint")
                return final

            tool_result = str(result)
            if len(tool_result) > MAX_TOOL_OUTPUT_CHARS:
                half = MAX_TOOL_OUTPUT_CHARS // 2
                tool_result = tool_result[:half] + "\n...[truncated]...\n" + tool_result[-half:]
            self.session.append("user", self._tool_result_text(name, tool_result), kind="tool_result")

        raise MiniAgentError(f"Stopped after {self.max_steps} agent steps. Resume the session to continue.")

    def _parse_tool_call(self, text: str) -> tuple[dict[str, Any] | None, str | None]:
        m = TOOL_CALL_RE.search(text)
        if not m:
            return None, None
        raw = m.group(1)
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            return None, f"malformed tool-call JSON: {e}. Re-emit exactly one valid <tool_call> JSON block."
        name = obj.get("name")
        tool_args = obj.get("arguments", {})
        if not isinstance(name, str) or not isinstance(tool_args, dict):
            return None, "tool call must contain string 'name' and object 'arguments'"
        return {"name": name, "arguments": tool_args}, None

    def _tool_result_text(self, name: str, result: str) -> str:
        return f"<tool_result name={json.dumps(name)}>\n{result}\n</tool_result>"


def print_sessions(store: SessionStore) -> None:
    sessions = store.list_sessions()
    if not sessions:
        print("No sessions for this project.")
        return
    for s in sessions:
        plan = s.get("plan") or []
        done = sum(1 for x in plan if x.get("status") == "done")
        total = len(plan)
        prog = f" plan {done}/{total}" if total else ""
        print(f"{s.get('session_id')}  [{s.get('status','?')}] {s.get('updated_at','')}  {s.get('task','')}{prog}")


def print_resume_banner(session: Session) -> None:
    s = session.state
    print(f"Resuming: {session.session_id}")
    print(f"Task: {s.get('task')}")
    plan = s.get("plan") or []
    if plan:
        print("Plan:")
        symbols = {"done": "x", "in_progress": ">", "pending": " "}
        for item in plan:
            print(f"  [{symbols.get(item.get('status'), ' ')}] {item.get('text')}")
    mods = s.get("modified_files") or []
    if mods:
        print("Modified: " + ", ".join(mods))
    summary = session.summary().strip()
    if summary:
        preview = summary if len(summary) <= 700 else summary[:700] + "..."
        print("Working memory:\n" + textwrap.indent(preview, "  "))


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stdlib-only coding agent harness")
    p.add_argument("prompt", nargs="*", help="Task/prompt. Omit for interactive mode.")
    p.add_argument("--resume", action="store_true", help="Resume the latest session for this project")
    p.add_argument("--session", metavar="SESSION", help="With --resume, resume this specific session ID/prefix")
    p.add_argument("--new", action="store_true", help="Force a new session")
    p.add_argument("--sessions", action="store_true", help="List project sessions and exit")
    p.add_argument("--compact", action="store_true", help="Force compaction before continuing")
    p.add_argument("--base-url", default=os.environ.get("MINIAGENT_BASE_URL", DEFAULT_BASE_URL))
    p.add_argument("--api-key", default=os.environ.get("MINIAGENT_API_KEY", ""))
    p.add_argument("--model", default=os.environ.get("MINIAGENT_MODEL", DEFAULT_MODEL))
    p.add_argument("--context-budget", type=int, default=int(os.environ.get("MINIAGENT_CONTEXT_BUDGET", DEFAULT_CONTEXT_BUDGET)))
    p.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    p.add_argument("--debug", action="store_true")
    return p.parse_args(argv)


def interactive_loop(agent: MiniAgent) -> None:
    print("Interactive mode. Commands: :quit, :status, :compact, :sessions")
    while True:
        try:
            line = input("\nYou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        if line in {":quit", ":q", "quit", "exit"}:
            return
        if line == ":status":
            print_resume_banner(agent.session)
            continue
        if line == ":compact":
            changed = agent.maybe_compact(force=True)
            print("Compacted." if changed else "Nothing old enough to compact.")
            continue
        if line == ":sessions":
            print_sessions(agent.store)
            continue
        try:
            result = agent.run_task(line)
            print("\nAgent> " + result)
        except MiniAgentError as e:
            print(f"\nERROR: {e}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    cwd = Path.cwd()
    root, is_git = detect_project_root(cwd)
    store = SessionStore(root, is_git)

    if args.sessions:
        print(f"Project: {root}")
        print_sessions(store)
        return 0

    task_text = " ".join(args.prompt).strip()

    if args.resume and args.new:
        raise MiniAgentError("Use either --resume or --new, not both.")
    if args.session and not args.resume:
        raise MiniAgentError("--session requires --resume.")

    if args.resume:
        session = store.load(args.session or "latest")
        print_resume_banner(session)
    else:
        initial_task = task_text or "Interactive coding session"
        session = store.create(initial_task)
        print(f"Session: {session.session_id}")

    if not args.base_url:
        raise MiniAgentError("Missing base URL. Set MINIAGENT_BASE_URL or pass --base-url.")
    api_key = args.api_key
    if not api_key:
        api_key = getpass.getpass("API key: ")
    if not api_key:
        raise MiniAgentError("API key is required.")

    client = APIClient(args.base_url, api_key, args.model, debug=args.debug)
    agent = MiniAgent(
        root,
        is_git,
        client,
        store,
        session,
        context_budget=max(8_000, args.context_budget),
        max_steps=max(1, args.max_steps),
        debug=args.debug,
    )

    if args.compact:
        try:
            agent.maybe_compact(force=True)
        except Exception as e:
            print(f"Compaction warning: {e}", file=sys.stderr)

    if task_text:
        if args.resume:
            # Preserve original task; this is a continuation instruction.
            pass
        try:
            result = agent.run_task(task_text)
            print(result)
            return 0
        except MiniAgentError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2

    interactive_loop(agent)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MiniAgentError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)