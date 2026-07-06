# Curator — Map-Reduce the Fork

Status: design proposal (not yet implemented)
Author: drafted by Claude for Vladimir
Part of: Curator 10x — see sibling docs in docs/design/curator-*.md

## Thesis

Today one forked `AIAgent` with `max_iterations=9999` does *everything* —
clustering, merging, and (nominally) verification — over the entire skill
library in a single conversation. This doc replaces that monolith with a
map-reduce: **MAP** = one *bounded* fork per cluster, each seeing only its
~5–15 skills, run concurrently under the existing delegation caps; **REDUCE**
= a single serialized reducer that merges the per-cluster structured reports
into one run report and is the *only* actor that commits archive/patch
mutations to the shared on-disk store. This unlocks lever B from the shared
thesis (`docs/design/curator-common-context.md`): the monolithic single-shot
fork.

It consumes clusters from `curator-semantic-clustering.md` (or falls back to
today's prefix clusters), and it defers every commit through the verifier from
`curator-verifier-gate.md`.

## Root-cause: why the monolith degrades

The single fork is built at `agent/curator.py:1878-1894`:

```python
review_agent = AIAgent(
    model=_model_name, provider=_resolved_provider, api_key=_api_key,
    base_url=_base_url, api_mode=_api_mode,
    max_iterations=9999,      # <- the whole library in one conversation
    quiet_mode=True, platform="curator",
    skip_context_files=True, skip_memory=True,
)
```

It receives the *entire* candidate list — `_render_candidate_list()`
(`agent/curator.py:1458`) renders one line per agent-created skill with no
size bound — appended to `CURATOR_REVIEW_PROMPT` and runs
`review_agent.run_conversation(...)` once (`agent/curator.py:1915`). Four
structural failures follow from "one conversation, unbounded iterations, whole
library":

| Failure | Mechanism |
| --- | --- |
| **Context saturation** | Hundreds of candidate lines + every tool result (skill_manage patches, `read_file` of SKILL.md bodies, terminal `mv` output) accumulate in *one* message history. As the pass progresses the model reasons over a window increasingly dominated by unrelated skills. |
| **No isolation** | A `/pony` bot cluster and a `git-guardrails` cluster share the same conversation. Cross-talk (the model "remembering" an earlier merge while judging an unrelated one) is a feature of the architecture, not a bug we can prompt away. |
| **Quality decay with N** | The comment at `:1884-1888` concedes "50–100 API calls against hundreds of candidate skills." Merge quality is worst for clusters processed late, when the window is fullest — exactly backwards from what you want. |
| **Zero parallelism / all-or-nothing crash** | `_run_llm_review` is a single `run_conversation`. One provider error, one iteration-budget exhaustion, one malformed tool call aborts the *whole* pass. `_llm_pass` catches it at `agent/curator.py:1667` and records `llm: error (...)` — every cluster's work is lost, including ones already correctly merged in-conversation but not yet reported. |

The `max_iterations=9999` ceiling is the tell: it exists *because* one
conversation must be big enough to sweep the whole library. Shrinking the unit
of work removes the reason for the ceiling.

## Proposed architecture: MAP → REDUCE

### MAP — one bounded fork per cluster

`_llm_pass` (`agent/curator.py:1569`) changes shape. Instead of building one
prompt and calling `_run_llm_review(prompt)` once, it:

1. Obtains clusters. Primary source: the semantic clusterer
   (`curator-semantic-clustering.md`) returns a `List[Cluster]`, each a set of
   skill names known to be semantically adjacent. Fallback (that doc absent or
   the embedder unavailable): derive **prefix clusters** from
   `skill_usage.agent_created_report()` (`tools/skill_usage.py:870`) by first
   name-token — the same lexical grouping the current prompt asks the model to
   find, now computed deterministically in Python so the fork never spends
   iterations discovering it. Singletons (a cluster of one) are dropped: there
   is nothing to consolidate, so they cost a fork for no reason.

2. For each cluster, spawns a **bounded** fork via a new
   `_run_cluster_review(cluster)` — structurally `_run_llm_review` but with
   two changes:
   - **`max_iterations` drops from 9999 to a per-cluster budget.** A cluster
     of ~5–15 skills needs on the order of *tens* of iterations, not
     thousands: read each SKILL.md, decide the umbrella, issue the
     merges/moves, emit the YAML block. Proposed default `max_iterations=40`,
     surfaced as `curator.cluster_max_iterations`. The ceiling now bounds a
     *known-small* task instead of an open-ended sweep.
   - **The prompt carries ONLY this cluster + the umbrella-building rules.**
     The fork sees a `_render_candidate_list()`-shaped slice restricted to the
     cluster's members (plus the `PRUNE-BUILTINS`/dry-run notes already
     assembled at `agent/curator.py:1643-1662`), not the whole library. The
     `CURATOR_REVIEW_PROMPT` (`agent/curator.py:403`) is reused verbatim for
     the merge rules; the "find prefix clusters" instruction becomes dead
     weight (clustering is done for it) and can be trimmed in the per-cluster
     variant.

3. Every per-cluster fork inherits the same hardening the monolith has today
   and it must not lose: `quiet_mode`, `platform="curator"`,
   `skip_context_files`/`skip_memory`, the nudge kills
   (`agent/curator.py:1896-1897`), and crucially
   `_memory_write_origin="background_review"` (`agent/curator.py:1905`) — the
   tag that arms `skill_manage`'s background-review write guard. Any fork
   missing it inherits `assistant_tool` origin and the external/bundled/
   hub-installed guards never fire (`agent/curator.py:1898-1904`).

### PROPOSE, don't commit

The MAP forks run against a **shared, mutable on-disk store**. This is the
load-bearing constraint (see Seams). To make concurrency safe, MAP forks
**propose**; a serialized REDUCE **commits**. Concretely: in the map phase the
per-cluster fork runs in **dry-run posture** — `CURATOR_DRY_RUN_BANNER`
(`agent/curator.py:376`) is prepended so the fork emits its structured
`consolidations:`/`prunings:` YAML block **without** calling the mutating
`skill_manage` archive/patch/delete or terminal `mv`. The fork's *output* is a
merge plan, not a mutation. This sidesteps write contention entirely for the
concurrent phase: N forks reading SKILL.md bodies in parallel is safe;
N forks racing `archive_skill`/`bump_patch` through the same `.usage.json`
lock is not (and even lock-serialized, disjointness is only *probably* true —
see Seams).

### REDUCE — serialized commit + aggregate

After all forks return (or time out), a single-threaded reducer runs in the
curator's own thread:

1. **Verify then commit, per cluster.** Each cluster's proposed merge plan is
   handed to the verifier gate (`curator-verifier-gate.md`) which proves the
   umbrella is content-lossless and `references/`/`templates/`/`scripts/`
   survived. Only plans that pass are committed — the reducer replays the
   proposed operations as real `skill_usage.archive_skill`
   (`tools/skill_usage.py:696`), `set_state`, and the terminal moves, each
   under `_usage_file_lock()`. Because the reducer is single-threaded, there
   is exactly one writer: no cross-cluster write race is *possible*, so the
   disjointness question becomes moot for the commit phase.
2. **Aggregate reports.** The N per-cluster structured summaries feed one
   `_write_run_report` (`agent/curator.py:1079`) — see Report shape.

## Concurrency: worker-pool over clusters

Reuse the existing delegation budget rather than inventing a curator-specific
one. `delegation.max_concurrent_children` (default 3, floor 1, no ceiling —
`hermes_cli/config.py:2130`; the shared context lists it as 5, the deployed
value) caps parallel forks; `max_async_children` is deprecated and folded into
it (`hermes_cli/config.py:5788-5810`), so the pool reads
`max_concurrent_children` only. `child_timeout_seconds` (default 0 = no cap,
floor 30s when set — `hermes_cli/config.py:2123-2127`; the shared context's
600 is the deployed value) becomes each fork's wall-clock cap.

- **Pool.** A bounded `ThreadPoolExecutor(max_workers=max_concurrent_children)`
  over the cluster list. Each worker calls `_run_cluster_review`. This mirrors
  the delegation subsystem's own semantics so a user who tuned delegation for
  their box gets the curator's parallelism tuned for free.
- **Ordering / determinism.** Submit clusters in a **stable sort** (by cluster
  key: lowest member name). Forks complete out of order, but the reducer
  processes results in the submitted (sorted) order, so the committed sequence
  and the report are deterministic given the same inputs — independent of
  which fork happened to finish first. This matters because merges are not
  commutative when two clusters both propose the *same* umbrella name (rare
  but possible); deterministic reduce order makes the conflict resolution
  reproducible.
- **Timeout.** A fork exceeding `child_timeout_seconds` is abandoned; its
  cluster is recorded `status=timeout` and contributes nothing to the commit.
  Its skills are untouched (they were only *read*), so a slow cluster degrades
  to "not consolidated this run," never to a corrupt half-merge.

### The shared-store contention question, answered

`skill_manage` mutates `~/.hermes/skills/` and every read-modify-write of
`.usage.json` goes through `_usage_file_lock()` (`tools/skill_usage.py:90`), an
`fcntl.flock` exclusive lock (`:105`). The lock makes concurrent writes *safe*
but *serialized* — it does not make them *correct*: two forks whose clusters
share a skill (semantic clustering can put one skill in overlapping neighbor
sets) could both archive it, or one could archive a skill the other is
patching into an umbrella. Rather than reason about whether clusters are
disjoint *enough* to mutate concurrently, the design removes the question:
**forks never mutate.** Only the serialized reducer commits, one operation at
a time under the same lock. Concurrency buys us the expensive part (N LLM
passes in parallel); serialization protects the cheap part (the actual
filesystem commit).

## Reconciliation: per-cluster, then aggregate

`_reconcile_classification` (`agent/curator.py:858`) today reconciles the
model's claimed consolidations/prunings against what actually disappeared,
once, over the whole pass. It becomes per-cluster and then aggregate:

- Each committed cluster produces its own `removed` set (the before/after diff
  restricted to that cluster's members) and its own model YAML block. Run
  `_reconcile_classification(removed, heuristic, model_block, destinations,
  absorbed_declarations)` **per cluster**, over that cluster's tool-call
  evidence only. This is strictly *more* accurate than the monolith: the
  heuristic's `destinations` set (umbrellas that survived or were created)
  is scoped to the cluster, so a hallucinated cross-cluster umbrella can no
  longer be laundered into a real one by coincidence of name.
- The reducer concatenates the per-cluster reconciliation buckets
  (`consolidated`, `pruned`) into the run-level buckets. Because commit is
  serialized and clusters are processed in sorted order, the aggregate is a
  deterministic union.

## Report shape: N sub-reports → one run report

`_write_run_report` (`agent/curator.py:1079`) already takes `before_report`,
`before_names`, `after_report`, and `llm_meta` and diffs the whole pass. Under
map-reduce:

- **`llm_meta` becomes a list.** Each cluster fork returns the same
  `_run_llm_review`-shaped dict (`final`, `summary`, `model`, `provider`,
  `tool_calls`, `error` — `agent/curator.py:1812-1819`). The reducer collects
  them into `cluster_metas: List[Dict]` plus a rolled-up `llm_meta` whose
  `tool_calls` is the concatenation (so the existing `tc_counts` histogram at
  `agent/curator.py:1131-1134` still works) and whose `summary` is
  `"N clusters: M consolidated, K pruned, F failed"`.
- **`before_report`/`after_report` stay whole-library.** They are single
  `skill_usage.agent_created_report()` snapshots taken *before the map phase*
  and *after the reduce commit* — the diff (`removed`/`added`/`transitions` at
  `agent/curator.py:1118-1128`) is unchanged and correctly reflects the net
  effect of all committed clusters.
- **New per-cluster section in REPORT.md.** `_render_report_markdown`
  (`agent/curator.py:1271`) gains a table: cluster key, size, status
  (`committed`/`failed`/`timeout`/`rejected-by-verifier`), umbrella created,
  members absorbed. This is where partial failure becomes *visible* instead of
  swallowed.

## Failure modes & determinism

| Mode | Old (monolith) | New (map-reduce) |
| --- | --- | --- |
| One cluster's fork errors | Whole pass aborts (`agent/curator.py:1667`) | That cluster → `status=failed`; others commit. Isolated. |
| Fork times out | No per-unit timeout; the 9999 ceiling was the only bound | `child_timeout_seconds` caps it; cluster skipped, skills untouched |
| Verifier rejects a merge | N/A (no verifier) | Cluster not committed; recorded `rejected`; skills stay put |
| Two clusters propose same umbrella | Silent last-writer-wins inside one conversation | Deterministic: sorted reduce order resolves it, logged |
| Crash mid-run | Whole pass lost | Clusters committed before the crash are durable (each commit is its own locked write); `last_run_at` already persisted pre-pass (`agent/curator.py:1562-1567`) |

Determinism holds end-to-end given the same skill set + same clustering: MAP is
order-independent (read-only), REDUCE processes a sorted list single-threaded.
The only nondeterminism is the LLM itself inside each fork — unchanged from
today, but now confined to a small window per cluster.

## Composition with the verifier gate

The map-reduce is the *mechanism*; the verifier
(`curator-verifier-gate.md`) is the *policy* that decides which proposed
merges commit. The seam is clean: MAP produces plans, the verifier judges each
plan, REDUCE commits only the survivors. Without the verifier the reducer would
commit every plan (equivalent to today's trust level, but now parallel and
isolated); with it, each cluster's merges are proven lossless before a single
`archive_skill` fires. The two docs are independently shippable but designed to
snap together at the propose/verify/commit boundary.

## Change surface (concrete)

- `agent/curator.py`: refactor `_run_llm_review` (`:1809`) into
  `_run_cluster_review(cluster, dry_run=True, max_iterations=...)`; rewrite
  `_llm_pass` (`:1569`) to cluster → pool-map → reduce; add the reducer commit
  loop; extend `_write_run_report` (`:1079`) / `_render_report_markdown`
  (`:1271`) for per-cluster sections; call `_reconcile_classification`
  (`:858`) per cluster.
- `hermes_cli/config.py`: add `curator.cluster_max_iterations` (default 40);
  reuse `delegation.max_concurrent_children` / `child_timeout_seconds`.
- No change to `tools/skill_usage.py`: the reducer uses the existing
  `archive_skill`/`set_state` under `_usage_file_lock()` as-is.

## Seams found while grounding this design

- **The shared-store mutation + lock is the load-bearing seam.**
  `skill_manage` writes live directories under `~/.hermes/skills/`, and every
  `.usage.json` read-modify-write serializes through `_usage_file_lock()`
  (`tools/skill_usage.py:90`), an `fcntl.flock(LOCK_EX)` (`:105`). The lock
  guarantees *safe* concurrent writes, **not correct** ones — it cannot detect
  two forks archiving overlapping cluster members. This is the entire reason
  the design forbids concurrent mutation and routes all commits through a
  serialized reducer.
- The monolith's `max_iterations=9999` (`agent/curator.py:1889`) is
  explicitly justified by "hundreds of candidate skills" in one conversation
  (`:1884-1888`). Shrinking the work unit is what makes a small ceiling
  correct.
- `_memory_write_origin="background_review"` (`agent/curator.py:1905`) is
  load-bearing for safety: it arms `skill_manage`'s background-review write
  guard (`:1898-1904`). Every per-cluster fork MUST set it, or the
  external/bundled/hub-installed protections silently disappear.
- `_render_candidate_list()` (`agent/curator.py:1458`) has no size bound — it
  emits one line per skill. It is the natural place to add a `names=` filter so
  a fork's prompt carries only its cluster.
- `delegation.max_concurrent_children` (`hermes_cli/config.py:2130`) is the
  *unified* concurrency cap; `max_async_children` is deprecated and folded into
  it (`hermes_cli/config.py:5788-5810`). Read the unified key only.
  `child_timeout_seconds` default is 0 = no timeout (`:2123-2127`); the
  curator should set an explicit cap.
- State is persisted *before* the LLM pass (`agent/curator.py:1562-1567`) so a
  crash doesn't re-trigger immediately — this already gives us
  crash-durability for the pre-map bookkeeping; per-cluster commits extend the
  same property to the actual mutations.
- The pre-run snapshot (`agent/curator.py:1537`,
  `curator_backup.snapshot_skills`) still runs once before the whole map-reduce
  — one restore point covers the entire pass, unchanged.
