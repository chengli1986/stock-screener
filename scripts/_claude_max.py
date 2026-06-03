#!/usr/bin/env python3
"""Shared helper — call Claude via Claude Code CLI on Max Plan (zero API billing).

研报模块的 LLM 调用统一走 Max 订阅：strip ANTHROPIC_API_KEY 让 `claude` CLI
回落到 Max OAuth（与 wrapper-fetcher-synthesis / narrative wrapper 同一模式）。
用户明确要求：有 API key 也不使用按量计费，全部走 Max 订阅。
"""
import os
import subprocess

_CLAUDE_BIN_DIR = os.path.expanduser("~/.npm-global/bin")


def call_claude_max(prompt: str, *, model: str | None = None, timeout: int = 120) -> str:
    """Run `claude --print` on Max Plan and return stdout text.

    ANTHROPIC_API_KEY / CLAUDECODE are stripped so the CLI uses Max OAuth (no API
    billing). `--allowedTools ""` disables tool use (pure text completion).
    Raises RuntimeError on timeout or non-zero exit.
    """
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("CLAUDECODE", None)
    env["PATH"] = _CLAUDE_BIN_DIR + os.pathsep + env.get("PATH", "")

    cmd = ["claude", "--print", "--allowedTools", ""]
    if model:
        cmd += ["--model", model]
    try:
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            env=env, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"claude CLI timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude CLI failed (exit {proc.returncode}): {proc.stderr.strip()[:300]}"
        )
    return proc.stdout.strip()
