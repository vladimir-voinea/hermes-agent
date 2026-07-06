# Curator — Verifier Gate (the trust unlock)

Status: design proposal (not yet implemented)
Author: drafted by Claude for Vladimir
Part of: Curator 10x — see sibling docs in docs/design/curator-*.md

## The problem this doc solves

`curator.consolidate` is OFF by default (`agent/curator.py:1499-1504`,
`get_consolidate` at `hermes_cli/config`/`agent/curator.py:190`). The reason
is not that the umbrella-builder produces bad clusters — sibling docs
`curator-semantic-clustering.md` and `curator-mapreduce-forks.md` fix
clustering and scale. The reason is **lever C: nothing verifies a merge was
content-lossless.** A pass can archive a skill after "absorbing" it into an
umbrella, self-report a clean YAML block, pass reconciliation, and have
silently dropped a load-bearing recipe. Until a merge is *proven* lossless and
reversible at per-merge granularity, no operator will flip this to run weekly
unattended. This doc closes that gap.

The gate is: consolidation moves from **"mutate then reconcile"** to
**"PROPOSE → VERIFY → COMMIT-or-ROLLBACK"**, with a separate critic fork that
answers a bounded, structured question about each proposed merge before the
sibling is archived.

## Root cause — why "names reconcile" ≠ "no knowledge lost"

After the builder fork mutates, the only post-run integrity check is the
classification reconciliation pipeline:

- `_parse_structured_summary` (`agent/curator.py:723`) parses the model's
  `consolidations:`/`prunings:` YAML block.
- `_extract_absorbed_into_declarations` (`agent/curator.py:804`) walks the run's
  tool calls and extracts each `skill_manage(action='delete', absorbed_into=…)`
  declaration — the authoritative signal of *intent* at delete time.
- `_reconcile_classification` (`agent/curator.py:858`) merges those signals and
  places every **removed skill name** into exactly one bucket: consolidated
  (into an umbrella that exists in `destinations`) or pruned.

Look closely at what `destinations` is and what the check actually proves. The
authoritative branch (`agent/curator.py:906-918`) accepts a consolidation when
`into_claim and into_claim in destinations` — i.e. when the umbrella **name**
survived or was newly created. That is the entire lossless check. It confirms:

1. the model *claimed* skill X was absorbed into umbrella Y, and
2. a skill named Y **exists** on disk after the run.

It does **not** confirm:

- that Y's SKILL.md actually gained X's content;
- that the content it gained is X's *load-bearing* instructions rather than a
  hand-wavy one-line pointer;
- that any `references/`, `templates/`, `scripts/`, or `assets/` files X
  depended on were re-homed into Y (the prompt *warns* about this at
  `agent/curator.py:483-501` — see below — but nothing enforces it);
- that relative links inside Y's SKILL.md resolve to files that exist.

Name-reconciliation is a **referential** check ("does the forwarding target
exist?"), not a **content** check ("did the knowledge survive the move?").
Those are different questions and the second is the one that matters.

### Concrete failure that passes reconciliation today

`skill_usage.agent_created_report()` (`tools/skill_usage.py:870`) lists:

- `gb10-lora-training-ops` — umbrella, broad SKILL.md.
- `gb10-lora-oom-137-fix` — narrow sibling whose SKILL.md carries the
  load-bearing recipe: *training + Holo simultaneously → OOM kill (137); keep
  Holo STOPPED during training runs*.

The builder fork decides `oom-137-fix` belongs under the training umbrella. It
patches the umbrella with a one-liner — *"See also: watch for OOM during
training"* — then calls `skill_manage(action='delete',
name='gb10-lora-oom-137-fix', absorbed_into='gb10-lora-training-ops')`.

Now trace the reconciliation:

- `_extract_absorbed_into_declarations` records
  `{'gb10-lora-oom-137-fix': {'into': 'gb10-lora-training-ops', 'declared': True}}`.
- `gb10-lora-training-ops` still exists, so it is in `destinations`.
- `_reconcile_classification` hits `agent/curator.py:906-918`, sees
  `into_claim in destinations`, and files it as a clean **consolidation**.

Reconciliation passes. The report shows "1 consolidated, 0 lost." But the
actual load-bearing content — *keep Holo STOPPED, the specific 137 signature* —
was **never written into the umbrella**. The sibling is archived. The knowledge
is gone from the live library; the only copy is in the pre-run whole-library
snapshot (`agent/curator.py:1537`), which no one will think to consult because
no signal says anything was lost. This is the trust gap in one example: the
merge was lossy and the system reported it as clean.

## Proposed mechanism — the verifier (critic) fork

Introduce a **verifier fork**, structurally separate from the builder fork, run
*per proposed consolidation* after the umbrella is built but **before the
sibling is archived**. It is a critic: it does not mutate; it renders a verdict.

The verifier is a second `AIAgent`, spawned by the same machinery as the
builder — reuse `_run_llm_review`'s provider-resolution path
(`agent/curator.py:1854-1874`, via `_resolve_review_runtime` at `:1744`) so the
critic inherits the same `auxiliary.curator.*` binding. It must be spawned with
a **read-only toolset** (`skills_list`, `skill_view`, `terminal` restricted to
read commands — no `skill_manage`, no `mv`/`rm`). Concretely, drop
`skill_manage` from its tool registry and keep `max_iterations` low (a bounded
per-merge question needs ~5–15 calls, not the builder's `9999` at
`agent/curator.py:1889`).

### What the verifier is shown

For a single proposed consolidation `{from: X, into: Y}`:

| Input | Source |
|---|---|
| Old skill package contents of X | The staged pre-archive copy of `<X>/` (SKILL.md + `references/`/`templates/`/`scripts/`/`assets/`) |
| New umbrella contents of Y | Live `<Y>/` after the builder's patch |
| Claimed `into:` mapping | The `absorbed_into` declaration from `_extract_absorbed_into_declarations` (`agent/curator.py:804`) |
| The builder's stated rationale | `reason` field from the parsed YAML (`_parse_structured_summary`, `agent/curator.py:723`) |

Because verification happens **before** the archive, X's package still exists at
its original path — this is the crux of the per-merge staging change below. The
verifier can `skill_view` both packages directly.

### The bounded question

The verifier answers exactly two things, and nothing else:

1. **Content coverage.** Is every *load-bearing* instruction from X present —
   semantically, not verbatim — somewhere in Y (SKILL.md or a re-homed support
   file)? "Load-bearing" = a specific recipe, command, parameter value,
   failure signature, or gotcha whose loss would change behavior. Prose fluff
   and duplicated boilerplate do not count.
2. **Link integrity.** Do all relative links in Y's SKILL.md
   (`references/…`, `templates/…`, `scripts/…`, `assets/…`) resolve to files
   that exist under `<Y>/`?

### Structured verdict

The verifier must emit a fenced YAML block, parsed by a sibling of
`_parse_structured_summary`:

```yaml
verdict: pass            # pass | fail
from: gb10-lora-oom-137-fix
into: gb10-lora-training-ops
missing:                 # empty on pass; load-bearing content NOT found in Y
  - "OOM-137 signature + 'keep Holo STOPPED during training' recipe"
broken_links: []         # relative links in Y that don't resolve
notes: "umbrella gained only a one-line pointer; recipe dropped"
```

`verdict: fail` with a populated `missing` list is the exact signal
reconciliation lacks today. The gate consumes it deterministically — the
verdict, not the prose, decides commit vs rollback.

## The gate — PROPOSE → VERIFY → COMMIT-or-ROLLBACK

Today the flow is: builder patches umbrella, builder calls
`skill_manage(delete)` (which archives X immediately via
`skill_usage.archive_skill`, `tools/skill_usage.py:696`), *then*
`_reconcile_classification` runs on names. The archive happens **inside** the
builder's turn, before any verification is possible.

The gate reorders this into three explicit phases per merge:

1. **PROPOSE.** The builder builds the umbrella (patch/create/write_file,
   re-home support files via `terminal mv`) but is **forbidden from archiving**.
   The `skill_manage(action='delete')` call is deferred out of the builder's
   hands. The builder's output is a *proposal*: the `absorbed_into` mapping plus
   the mutated umbrella on disk, with X still present.
2. **VERIFY.** Deterministic pre-checks (next section) run first; only surviving
   proposals reach the verifier fork. The verifier renders `pass`/`fail`.
3. **COMMIT-or-ROLLBACK.**
   - On `pass`: archive X via `skill_usage.archive_skill(X)`
     (`tools/skill_usage.py:696`). The umbrella patch stays. This is the only
     path that archives.
   - On `fail`: **roll back this one merge.** Restore the umbrella to its
     pre-merge SKILL.md (see staging below) and leave X untouched and live.
     Both skills survive; the operator sees the `missing`/`broken_links`
     verdict in the report.

### Per-merge reversibility

The safety infra today is coarse: `snapshot_skills(reason="pre-curator-run")`
(`agent/curator_backup.py:211`, called at `agent/curator.py:1537`) tars the
**whole** `~/.hermes/skills/` tree once, before the run. Rollback
(`curator_backup.rollback`, `agent/curator_backup.py:539`) is all-or-nothing:
it restores the entire library to the pre-run state, discarding *every* merge
in the run — good ones with the bad. That is unusable as a per-merge gate: one
lossy merge in a batch of twelve should not force twelve rollbacks.

The gate needs **per-merge staging** built from primitives that already exist:

- **Keep the sibling until verify passes.** Since archiving is deferred to the
  COMMIT phase, X's directory is untouched during PROPOSE and VERIFY. Rolling
  back a failed merge means simply *not calling* `archive_skill(X)`. No restore
  needed for the sibling — its live copy never moved.
- **Stage the umbrella's pre-merge SKILL.md.** Before the builder patches Y,
  copy `<Y>/SKILL.md` (and any support dir the builder is about to touch) into a
  per-merge staging area, e.g.
  `~/.hermes/skills/.curator_backups/.merge-staging-<stamp>/<Y>/`. On `fail`,
  restore that copy over the live umbrella; on `pass`, discard the stage. This
  mirrors the staging dance `rollback()` already does at
  `agent/curator_backup.py:591-603` (`.rollback-staging-*`), reusing the same
  `.curator_backups/` root and the same "move current aside, then reconcile"
  discipline.
- **`restore_skill` is the escape hatch.** If a merge slips past the gate and is
  archived in error (e.g. a bug in the COMMIT ordering), `restore_skill(X)`
  (`tools/skill_usage.py:757`) moves X back from `.archive/` to live — the exact
  inverse of `archive_skill`. The gate should treat this pair as its
  per-merge commit/rollback primitives: `archive_skill` = commit-forget-sibling,
  `restore_skill` = undo-a-bad-commit.

The whole-library `snapshot_skills` pre-run backup **stays** as a belt: if the
per-merge machinery itself faults, the operator still has `hermes curator
rollback` to the pre-run state. Per-merge staging is the fine-grained layer;
the pre-run snapshot is the coarse backstop.

## Deterministic pre-checks — run before spending the verifier LLM

The verifier is an aux-model call; do not spend one on a merge a cheap check can
already reject or clear. Run these first, in order:

1. **Broken-link scan (deterministic, hard gate).** Grep Y's SKILL.md for
   relative links matching `(references|templates|scripts|assets)/\S+`, then
   `stat` each target under `<Y>/`. Any link whose target does not exist →
   `fail` immediately, no verifier call, `broken_links` populated from the scan.
   This is a pure filesystem check with zero false positives; it catches the
   most common lossy-merge signature (the prompt's "instructions pointing at
   files that were left behind under the old skill directory",
   `agent/curator.py:500-501`) for free.
2. **Package-integrity check (deterministic, hard gate).** If X's package
   contains any support file (`<X>/references|templates|scripts|assets/`) OR
   X's SKILL.md contains relative links, then Y must contain a corresponding
   re-homed file for each. If X had support files and Y gained none, the merge
   flattened only SKILL.md and dropped the package → `fail` before the verifier.
   This is precisely the rule the prompt states at `agent/curator.py:483-501`:

   > Package integrity — not optional: Before demoting or archiving a skill,
   > inspect it as a COMPLETE directory package, not just SKILL.md. […] If the
   > source skill has support files OR SKILL.md contains relative links […],
   > DO NOT flatten only SKILL.md […]. Never leave archived/demoted
   > instructions pointing at files that were left behind under the old skill
   > directory.

   Today that is **an instruction to the model with no enforcement** — a
   suggestion the builder can ignore with no consequence. The pre-check turns
   the same rule into a machine-enforced gate.
3. **Content-shrink heuristic (deterministic, soft signal).** Compare the
   umbrella's growth against the absorbed body size. If `len(Y_after) −
   len(Y_before)` is `<<` `len(X_body)` (e.g. umbrella grew by 40 bytes while
   X's SKILL.md was 3 KB of recipe), the merge is *suspicious*: the builder
   likely wrote a pointer, not the content. This does not hard-fail (legitimate
   dedup of overlapping content shrinks too), but it **forces the verifier
   call** and is surfaced in the report so a human can spot-check. It is the
   trigger that would have caught the `gb10-lora-oom-137-fix` example above.

Only proposals that clear checks 1–2 and are either cleared or flagged by check
3 reach the verifier fork. Deterministic first, LLM last.

## Why this is the unlock

Once merges are **verified lossless** (content coverage + link integrity) and
**per-merge reversible** (bad merge rolls back alone, good merges commit), the
reason `curator.consolidate` is OFF disappears. The pass is no longer "mutate
and hope the YAML is honest"; it is "propose, prove, commit only what proved
out." At that point `curator.consolidate` can flip to `true` and the pass can
run **weekly, unattended**, exactly as `maybe_run_curator`
(`agent/curator.py:1958`) + `should_run_now` (`agent/curator.py:219`) already
schedule it. That is the concrete payoff of lever C: it converts consolidation
from a scary opt-in into a trusted default. State it plainly — this doc is the
precondition for turning the feature on.

## Config / CLI surface

New config under the existing `curator:` block (`~/.hermes/config.yaml`):

```yaml
curator:
  consolidate: true         # can now default true once verify is on
  verify: true              # NEW — gate each merge on the verifier fork
```

A `get_verify()` getter mirrors `get_consolidate` (`agent/curator.py:190`). The
review fork's model already resolves from `auxiliary.curator.*`
(`_resolve_review_runtime`, `agent/curator.py:1744`); the verifier reuses the
same binding — no new provider slot. (If a distinct, cheaper critic model is
wanted later, add `auxiliary.curator_verify.*` with fallback to
`auxiliary.curator.*`; not required for v1.)

**Verifier-unavailable → fail-closed.** If the verifier fork cannot be spawned
or resolve a model — the same failure `_run_llm_review` guards against at
`agent/curator.py:1833-1836` and `1870-1871` — the gate must **skip the merge,
keeping both skills**. A merge that cannot be verified is not committed. This is
the opposite of the pre-run snapshot's fail-*open* posture
(`agent/curator.py:1530-1544`, "a failed snapshot logs at debug and
continues"): a backup failure is tolerable because it only weakens the safety
net, but a verification failure must never let an unverified archive through.
Fail-open on backup, fail-closed on verify.

**Dry-run interaction.** `--dry-run` (`CURATOR_DRY_RUN_BANNER`,
`agent/curator.py:376`; report-only, no mutations) composes naturally: the
builder still produces proposals, the deterministic pre-checks still run
(they are read-only), and the verifier still renders verdicts — but the COMMIT
phase is a no-op. The REPORT.md then shows, per proposed merge, the verdict and
any `missing`/`broken_links` the *real* run would have gated on. Dry-run becomes
a genuine preview of what would commit vs roll back, not just a list of
intended deletions. When `verify: false`, dry-run degrades to today's behavior
(report the intended consolidations without a verdict).

## Seams found while grounding this design

These are load-bearing implementation facts discovered while reading the code;
they change the change-surface, not just the framing.

- **Reconciliation is name-only by construction.** `_reconcile_classification`
  (`agent/curator.py:858`) files a skill as consolidated on
  `into_claim in destinations` (`:908`, `:935`) — the umbrella *name* existing.
  `destinations` is a set of surviving/created **names**; there is no content
  comparison anywhere in `_parse_structured_summary` (`:723`),
  `_extract_absorbed_into_declarations` (`:804`), or the reconciler. The
  "lossless" property is asserted by the builder's YAML, never checked.

- **Package-integrity is an unenforced prompt instruction.** The rules at
  `agent/curator.py:483-501` ("Package integrity — not optional", "Never leave
  archived/demoted instructions pointing at files that were left behind") are
  prose in `CURATOR_REVIEW_PROMPT`. Nothing in the post-run pipeline `stat`s a
  single relative link or checks a single re-homed file. The builder is on its
  honor. The deterministic pre-checks (broken-link scan, package-integrity
  check) are the enforcement that string was always missing.

- **Snapshots are whole-library, pre-run, all-or-nothing.**
  `snapshot_skills(reason="pre-curator-run")` (`agent/curator_backup.py:211`) is
  called exactly once at `agent/curator.py:1537`, tars the entire `skills/`
  tree, and `rollback` (`agent/curator_backup.py:539`) restores the whole tree.
  There is no per-merge granularity today. Per-merge staging must be built; the
  `.rollback-staging-*` pattern (`agent/curator_backup.py:591-603`) and the
  `archive_skill`/`restore_skill` pair (`tools/skill_usage.py:696`/`:757`) are
  the primitives to build it from.

- **Archiving happens inside the builder's turn.** `skill_manage(action=
  'delete', absorbed_into=…)` archives X the moment the builder calls it, via
  `archive_skill` (`tools/skill_usage.py:696`). To gate archive on verify, that
  delete must be **deferred out of the builder** into the COMMIT phase — the
  single most structurally invasive change this doc requires. The builder's
  `absorbed_into` declaration (still captured by
  `_extract_absorbed_into_declarations`, `agent/curator.py:804`) becomes a
  *proposal record* rather than an already-executed action.

- **The provider-resolution path is reusable as-is.** The verifier fork does
  not need new plumbing: `_resolve_review_runtime`/`_resolve_review_model`
  (`agent/curator.py:1744`/`:1790`) and the `AIAgent(...)` construction in
  `_run_llm_review` (`agent/curator.py:1878-1894`) already resolve the
  `auxiliary.curator.*` binding and handle OAuth/pool-backed credentials. The
  verifier is that same construction with `skill_manage` removed from its tools
  and a small `max_iterations`.

## Cross-references

- **`curator-mapreduce-forks.md`** — the verifier gate consumes the per-cluster
  proposals that doc's bounded builder forks emit. Each cluster fork's proposed
  merges flow through PROPOSE → VERIFY → COMMIT-or-ROLLBACK independently, which
  also means verification parallelizes per cluster alongside building.
- **`curator-semantic-clustering.md`** — semantic clustering (lever A) improves
  *which* skills are proposed for merge; this gate proves each proposed merge is
  safe. They are complementary: better proposals still need lossless
  verification, and lossless verification is worthless without proposals worth
  merging.
- **`curator-retrieval-telemetry.md`** — telemetry measures curator precision
  *after* the fact; the verifier gate prevents the specific regression (dropped
  load-bearing content) that would otherwise show up as a retrieval miss weeks
  later.
