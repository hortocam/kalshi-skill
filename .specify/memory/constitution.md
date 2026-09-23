# Kalshi Skill Constitution
<!-- Ratified: 2026-09-22 | Supreme operating charter for the kalshi-skill repo. Amendments EXTEND; they never replace prior content. Full history below. -->

## Core Principles

### I. Spec-First (NON-NEGOTIABLE)
Every feature, phase, or change beyond a trivial fix begins with a specification written against
this project's Spec Kit flow. The spec defines the *what* and *why* (user journeys, success
criteria) before any code is written. No implementation work starts until a spec exists and has
been accepted. The spec is the shared source of truth; when code and spec disagree, the spec
wins and the code is corrected.

**Why**: this repo's first build (`research_store.py`, 2,129 lines) was driven by a design
attachment rather than a Spec Kit feature directory, which meant `speckit-converge` could not run
at all — there were no `spec.md`/`plan.md`/`tasks.md` to converge against. The review gate rested
on a worker's prose summary instead of a runnable command. Spec-first makes the gate real.

### II. Test-Driven Development (NON-NEGOTIABLE)
Red-Green-Refactor is the enforced cycle: write a failing test that expresses the intended
behavior; confirm it fails for the right reason; implement the minimum to pass; refactor while
green. A card or PR is not complete until its tests are green. There is no "implementation first,
tests later".

**Why**: the store's 24 stdlib `unittest` tests caught real defects during the first build (a
trailing-hyphen bug in reviewer derivation, and idempotency regressions). Tests are the cheapest
independent check that survives into the merge.

### III. GitHub Flow Branching
`main` must always be deployable and green. Every change goes on a short-lived feature branch
(prefix `wt/` for worktree tasks). Work is integrated through a Pull Request against `main`.
Branches are deleted after merge.

### IV. Independent Review (NON-NEGOTIABLE)
Every PR is reviewed by a model from a **different lineage** than the engineer who wrote it. The
implementer must NEVER approve their own work. The reviewer runs `speckit-converge` against the
spec, plan, and tasks; the loop implement → converge repeats until converge returns clean. Only on
a clean converge does the reviewer open the PR.

**Why**: the first build's `diff_ols` "singular design matrix" finding was correctly triaged as a
collinear-regressor diagnostic only because a *different* model lineage re-ran it. A same-lineage
reviewer would have shared the blind spot.

### V. Merge Authority (NON-NEGOTIABLE)
Engineers push `wt/*` branches and request review. Reviewers converge, then open the PR. **Only
Jarvis merges.** No worker, engineer, or reviewer merges a PR. Jarvis is the coordinator and sole
merge authority for `main`.

### VI. Read-Only Public Data Is the Only Default
The skill ships read-only, keyless public market reads. Any endpoint requiring RSA-PSS signed
authentication is out of scope unless a spec explicitly admits it. No code path may place an
order, and none may state an account balance it cannot actually read.

**Why**: the account holds $27 with a $5 per-trade risk cap. A run that inferred a $500
allocation from an example figure in a prompt is the failure mode this principle prevents.

### VII. Artifacts Never In Git
Generated data, research stores, caches, and probe output do not belong in the repository. The
skill is portable: `SKILL.md`, `references/`, and `scripts/` only. Runtime state lives under the
owning profile's directory.

### VIII. Evidence Over Assertion
Every claim of completion carries reproducible evidence: a command, its real output, and the
artifact it produced. A "done" without a PR number, a clean converge, and a green test run is a
hollow done and must be treated as unbuilt.

## Quality Gates & Tooling

- **Tests**: `python3 -m unittest discover -s skills/kalshi/scripts/tests` — stdlib only.
- **Runtime dependencies**: stdlib only. No numpy/pandas/scipy/statsmodels — the host's `python3`
  has none, and the store must run inside an unattended cron session.
- **Portability**: no absolute host paths in shipped code; resolve the profile home at runtime.
- **Data hygiene**: never print, echo, log, or write secret values; resolve secrets from the
  environment, never from argv.

## Governance

- **Amendments EXTEND; they never replace.** The full history stays present in every version.
- Every amendment records a `Why`, so the document carries its own rationale forward.
- Versioning: MAJOR = principle removal/redefinition; MINOR = new principle or expanded guidance;
  PATCH = wording.
- A change is adopted when the human approves it.

**Version**: 1.0.0 | **Ratified**: 2026-09-22 | **Last Amended**: 2026-09-22

## Amendment History

| Version | Date | Change | Why |
|---|---|---|---|
| 1.0.0 | 2026-09-22 | Initial ratification | The repo gained its first Spec Kit feature directory; the converge gate needed a charter to converge against, since the first build had none and its review gate was prose-only. |
