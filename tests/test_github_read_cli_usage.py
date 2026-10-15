#!/usr/bin/env python3
"""Offline test: a command line gh itself rejects never halts the run.

On 2026-10-15 two waves halted because lane agents ran
`gh api ... --paginate --slurp --jq ...`; gh refuses that combination locally,
the helper retried it as an outage and wrote the run-wide halt. The read must
exit 64 on the first attempt instead, and only strict scripts may halt. Run:
    python3 tests/test_github_read_cli_usage.py
"""
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

# One file, two homes: the canonical pack (tests/ beside scripts/) and an
# installed repo (tests/ beside scripts/).
_here = Path(__file__).resolve()
HELPER = next(p for p in (_here.parents[1] / "scripts" / "super-board-github-read.py",
                          _here.parents[2] / "bin" / "super-board-github-read.py") if p.exists())


def run(tmp, stderr, strict=False):
    fake = Path(tmp) / "gh"
    calls = Path(tmp) / "calls"
    fake.write_text(f"#!/bin/sh\necho x >> '{calls}'\nprintf '%s\\n' '{stderr}' >&2\nexit 1\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    halt = Path(tmp) / "halt.json"
    env = dict(os.environ, PATH=f"{tmp}:{os.environ['PATH']}", SB_GITHUB_HALT_FILE=str(halt),
               SB_GITHUB_RETRY_DELAY="0")
    env.pop("SB_GITHUB_READ_STRICT", None)
    if strict:
        env["SB_GITHUB_READ_STRICT"] = "1"
    proc = subprocess.run([sys.executable, str(HELPER), "--kind", "json", "--", "api",
                           "repos/o/r/pulls/1/comments", "--paginate", "--slurp", "--jq", "."],
                          capture_output=True, text=True, env=env)
    n = len(calls.read_text().splitlines()) if calls.exists() else 0
    calls.unlink(missing_ok=True)
    existed = halt.exists()
    halt.unlink(missing_ok=True)
    return proc, n, existed


def main():
    usage = "the `--slurp` option is not supported with `--jq` or `--template`"
    with tempfile.TemporaryDirectory() as tmp:
        proc, n, halted = run(tmp, usage)
        assert proc.returncode == 64, (proc.returncode, proc.stderr)
        assert n == 1, f"expected one attempt, got {n}"
        assert not halted, "usage error must not write the halt file"
        assert "the run continues" in proc.stderr, proc.stderr

        proc, n, halted = run(tmp, "unknown flag: --bogus")
        assert proc.returncode == 64 and n == 1 and not halted, (proc.returncode, n, halted)

        proc, n, halted = run(tmp, usage, strict=True)
        assert proc.returncode == 79 and n == 1 and halted, (proc.returncode, n, halted)

        # A real outage still retries three times and halts.
        proc, n, halted = run(tmp, "HTTP 502: Bad Gateway")
        assert proc.returncode == 79 and n == 3 and halted, (proc.returncode, n, halted)
    print("ok: gh usage errors exit 64 without a halt; strict and outages still halt")


if __name__ == "__main__":
    main()
