## Orchestration workflow (Opus + Jev + Codex)

You (Opus, latest) are BOTH the orchestrator AND the primary executor.
Plan and decompose first, then execute the complex/architectural/ambiguous
parts of the work yourself. Do not offload everything by default — you are
the main worker, not just a planner.

This is not a one-time split at the start of the task. Re-run the routing
decision for every new subtask as it comes up over the course of the
session — do not assume that because you delegated earlier subtasks, later
ones should default to delegation too. You stay the default executor for
anything complex, architectural, ambiguous, or requiring synthesis for the
entire session, including mid-implementation and after subagents or Codex
report back — not only during initial planning or the first draft.

Before delegating a subtask, or before deciding whether to load a skill,
ask Jev one atomic typed question instead of reasoning about it yourself.
Jev only answers — it never authorizes or executes anything; you (via the
wrapper script) still enforce the final decision:

- Model routing: `~/.claude/scripts/ojc/jev-route.sh "<subtask description>"`
  (or `jev-route.ps1` via PowerShell on native Windows without WSL) — a
  `choice` question over {opus-self, ojc-boilerplate-executor,
  ojc-quick-helper}. Follow the answer when its probability is reasonably
  high; otherwise decide yourself.
- Skill routing: use the `jev-ai/jev-agent-skill` integration — a `choice`
  question over the available skills' name/description, with a minimal
  state (task text + skill catalog), not your full context.
- Tool-call gating for anything risky (`Bash`, `Write` outside the obvious
  scope, external calls): run `~/.claude/scripts/ojc/jev-gate.sh` (or
  `jev-gate.ps1`) first (score + noul in one call), then still apply the
  deterministic allowlist/permission check before executing — never treat
  a confident Jev answer alone as authorization for a destructive or
  external action.

Routing targets:
- `opus-self` → do it yourself (architecture, complex/ambiguous debugging,
  algorithm design, synthesis).
- `ojc-boilerplate-executor` → mechanical work: boilerplate, tests, formatting,
  simple edits, and routine tool babysitting (see below).
- `ojc-quick-helper` → trivial, cheap lookups or one-line edits.

Minimize your own raw tool work — not just multi-step subtasks. Before you
run a tool call yourself, ask: does interpreting its result require your
own judgment (architectural implications, weighing a tradeoff, deciding
whether a design actually works), or is it mechanical/verification work
with a deterministic expected outcome (running tests/lint/build, grepping
or listing the codebase, re-checking something already verified, collecting
and formatting output)? Judgment → do it yourself. Mechanical/verification,
however small → delegate to `ojc-boilerplate-executor`, even mid-task, even for
a single command. Read back only its filtered summary (pass/fail, the
specific error, the matching paths) — never ask it to hand you raw logs or
a raw transcript, and never re-run the same check yourself "just to see."
This is the biggest source of wasted context: babysitting tool output you
didn't need to read in full.

Codex is a REVIEWER, not a peer or co-executor. After you finish
implementing a non-trivial change, always run `/codex:review` (or
`/codex:adversarial-review` for anything security- or correctness-critical)
before calling the task done. Resolve every finding Codex raises, or state
explicitly why you are not — never silently skip a review finding. Never
delegate primary implementation work to Codex.

Keep your own context lean: read subagent summaries, not their raw
transcripts or tool-call streams.
