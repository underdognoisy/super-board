#!/usr/bin/env python3
"""Offline test: the planner's sweep frees only cards whose ISSUE blockers closed.

On 2026-10-15 the sweep listed #74-#77 (blocked:decision, blocked:needs-access,
`## Blocked by: - None.`) as "all blockers closed" because an empty blocker list
is vacuously all-closed. Moving them would have handed an unruled decision and
production-access work to builders. Run:
    python3 tests/test_wave_plan_sweep.py
"""
import json
import os
import subprocess
import tempfile
from pathlib import Path

# One file, two homes: the canonical pack (tests/ beside scripts/) and an
# installed repo (tests/ beside scripts/).
_here = Path(__file__).resolve()
PLAN = next(p for p in (_here.parents[1] / "scripts" / "super-board-wave-plan.sh",
                        _here.parents[2] / "bin" / "super-board-wave-plan.sh") if p.exists())


def item(n, status, labels):
    return {"id": f"I{n}", "status": status, "labels": labels,
            "content": {"type": "Issue", "number": n, "title": f"card {n}", "assignees": []}}


def dep(n, blockers, open_blockers):
    return {"number": n, "title": f"card {n}", "state": "OPEN", "blockers": blockers,
            "openBlockers": open_blockers, "parseable": True, "humanGated": False,
            "runnable": not open_blockers, "why": "", "needsYou": False, "needsYouDone": False,
            "approvalRefresh": False, "changesRequested": None, "classify": [], "staleDone": None}


def main():
    items = {"items": [
        item(74, "Blocked", ["blocked:decision"]),         # waits on a person, no issue blockers
        item(76, "Blocked", ["blocked:needs-access"]),     # waits on access, no issue blockers
        item(80, "Blocked", ["blocked:other"]),            # legacy ⏳: blocker #12 closed
        item(81, "Blocked", ["blocked:other"]),            # blocker #13 still open
    ]}
    deps = {"74": dep(74, [], []), "76": dep(76, [], []),
            "80": dep(80, [12], []), "81": dep(81, [13], [13])}
    config = {"project": {"owner": "o", "number": 1}, "repo": {"remote": "https://github.com/o/r"},
              "max_workers": 0}
    with tempfile.TemporaryDirectory() as tmp:
        paths = {}
        for name, data in (("items", items), ("deps", deps), ("config", config)):
            paths[name] = Path(tmp) / f"{name}.json"
            paths[name].write_text(json.dumps(data))
        env = dict(os.environ, SB_GITHUB_HALT_FILE=str(Path(tmp) / "halt.json"))
        proc = subprocess.run(["bash", str(PLAN), "--config", str(paths["config"]),
                               "--items", str(paths["items"]), "--deps", str(paths["deps"])],
                              capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    swept = [s["number"] for s in json.loads(proc.stdout)["sweep"]]
    assert swept == [80], f"only the card whose issue blocker closed sweeps, got {swept}"
    print("ok: sweep frees #80 (blocker closed); #74/#76 (person-blocked) and #81 (open) stay")
    ready_after_drag()


def ready_after_drag():
    """A person-gated card a person dragged to Ready is picked (#74, 2026-10-16).

    The builder's `blocked-by: -` stays the newest line, so humanGated stays true
    after the drag; the planner must read the Ready column as the gate cleared.
    """
    gated = dep(74, [], [])
    gated.update(humanGated=True, runnable=False)
    still_open = dep(82, [13], [13])
    still_open.update(humanGated=False, runnable=False)
    items = {"items": [
        item(74, "Ready", ["feature"]),                  # dragged back after the ruling
        item(82, "Ready", ["feature"]),                  # Ready but issue blocker #13 open
        item(83, "Ready", ["feature"]),                  # unknown to the graph
        item(76, "Blocked", ["blocked:needs-access"]),   # gated, still in Blocked
    ]}
    deps = {"74": gated, "82": still_open, "76": dict(gated, number=76)}
    config = {"project": {"owner": "o", "number": 1}, "repo": {"remote": "https://github.com/o/r"},
              "max_workers": 0}
    with tempfile.TemporaryDirectory() as tmp:
        paths = {}
        for name, data in (("items", items), ("deps", deps), ("config", config)):
            paths[name] = Path(tmp) / f"{name}.json"
            paths[name].write_text(json.dumps(data))
        env = dict(os.environ, SB_GITHUB_HALT_FILE=str(Path(tmp) / "halt.json"))
        proc = subprocess.run(["bash", str(PLAN), "--config", str(paths["config"]),
                               "--items", str(paths["items"]), "--deps", str(paths["deps"])],
                              capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    plan = json.loads(proc.stdout)
    picked = [c["number"] for c in plan["cards"]]
    assert picked == [74], f"only the dragged person-gated card runs, got {picked}"
    assert [s["number"] for s in plan["sweep"]] == [], plan["sweep"]
    print("ok: #74 dragged to Ready runs; #82 (open blocker), #83 (unknown), #76 (Blocked) stay")


if __name__ == "__main__":
    main()
