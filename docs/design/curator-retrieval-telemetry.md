# Curator — Retrieval Telemetry (Close the Loop)

Status: design proposal (not yet implemented)
Author: drafted by Claude for Vladimir
Part of: Curator 10x — see sibling docs in docs/design/curator-*.md

## The blind spot, root-caused

The curator's stated north star is **discoverability**. The review prompt says
it plainly (`agent/curator.py:407-413`):

> "The goal of the skill collection is a LIBRARY OF CLASS-LEVEL INSTRUCTIONS AND
> EXPERIENTIAL KNOWLEDGE. […] An agent searching skills **matches on
> descriptions, not on exact names**; one broad umbrella skill with labeled
> subsections beats five narrow siblings **for discoverability**, not the other
> way around."

So the curator's entire justification for merging N narrow skills into one
umbrella is: *the agent will find the umbrella more reliably when it searches.*
Yet the curator has **zero signal about whether search actually surfaces the
right skill**. It cannot see a single retrieval event. It ages skills by
inactivity timestamps (`apply_automatic_transitions` `agent/curator.py:291`,
keying off `last_used_at` / `last_activity_at`) and merges them by content
overlap judged inside a fork. Neither mechanism ever observes a *search that
should have matched an umbrella and didn't*. The lever it pulls (description
quality → findability) is invisible to the loop that pulls it.

The one telemetry signal it does have is a red herring. `bump_use`
(`tools/skill_usage.py:623`) is fired from `_skill_view_with_bump`
(`tools/skills_tool.py:1630`) **after** the agent has already chosen and loaded a
skill. `use_count` records *that a skill ran* — never *whether the right skill
was found* or *whether a better one existed and was missed*. Two failure modes
are indistinguishable from `use_count` alone:

- The agent searched, the umbrella ranked #1, the agent picked it. Good.
- The agent searched, the umbrella was buried under a stale narrow sibling with
  a keyword-stuffed description, the agent picked the sibling. `use_count` on the
  sibling goes up — which the ager reads as "keep it alive," the exact opposite
  of what happened.

`use_count` is a *post-selection* counter. Discoverability is a *pre-selection*
property. The curator is optimizing a variable it never measures.

## The retrieval path (what we can actually instrument)

Skill retrieval in Hermes is a two-tier progressive-disclosure flow, both tiers
in `tools/skills_tool.py`:

| Tier | Tool | What it exposes | Signal it carries |
| --- | --- | --- | --- |
| 1 (browse) | `skills_list` `:689` | name + description for every skill (the **match surface**) | the candidate set the agent saw |
| 2/3 (load) | `skill_view` `:864` via `_skill_view_with_bump` `:1630` | full SKILL.md + linked files | the **choice** the agent made |

`skills_list` returns "only name + description to minimize token usage"
(`:693`). That means **the description frontmatter is the entire ranking
surface** an agent (or a model doing tool-selection) sees before it commits to a
`skill_view`. This is exactly what the prompt at `:411` means by "matches on
descriptions." The seam we want is: *between `skills_list` returning candidates
and `skill_view` loading one*, capture what happened.

`_skill_view_with_bump` (`:1630`) is the natural instrumentation point — it is
already the single funnel where a selection is committed, and it already does
best-effort telemetry (`bump_view` + `bump_use` at `:1644-1649`, wrapped in a
`try/except` so a telemetry failure never breaks the tool call). We extend that
same funnel to also record a **retrieval event**, and we add a lightweight hook
at `skills_list` to stamp the candidate set the selection is drawn from.

## Proposed instrumentation

### Event schema

A retrieval event captures one search→choice episode. Stored as append-only
JSONL in a new file analogous to `.usage.json`, guarded by the same
cross-process lock discipline (`_usage_file_lock` `tools/skill_usage.py:90`).

```
# ~/.hermes/skills/.retrieval.jsonl   (one JSON object per line)
{
  "ts":          "2026-07-07T18:22:04+00:00",   # _now_iso()
  "session":     "s_9f21…",                      # correlate w/ a task, not a user
  "query":       "flux lora strength sweep",     # search text / task context
  "candidates":  [                               # what skills_list surfaced
    {"name": "flux-lora-sweep",  "rank": 1, "score": 0.71},
    {"name": "flux-lora-train",  "rank": 2, "score": 0.55},
    {"name": "rapid-image",      "rank": 3, "score": 0.31}
  ],
  "chosen":      "flux-lora-train",              # what skill_view actually loaded
  "chosen_rank": 2,                              # rank of the pick in candidates
  "outcome":     "near_miss"                     # good | near_miss | miss | unknown
}
```

Field derivation:

- `query` / `candidates` are captured at `skills_list` (`:689`) time and cached
  per-session in memory. `score` is the retrieval ranker's score; with the
  current lexical list it is `null` (position-only), and it becomes a real
  cosine similarity once `curator-semantic-clustering.md` lands the embedder on
  the browse path — the schema is forward-compatible either way.
- `chosen` / `chosen_rank` are filled at `skill_view` (`:1630`) by matching the
  loaded skill against the last cached candidate set for that session.
- `outcome` is derived, not asked of the model:
  - `good` — `chosen_rank == 1`.
  - `near_miss` — `chosen_rank > 1` (a better-scoring candidate existed but the
    agent skipped it; the top result's description under-sold it, or the pick's
    over-sold itself).
  - `miss` — `skill_view` loads a skill that was **not** in the candidate set
    (agent bypassed search / found it by memory), OR a `skills_list` with no
    `skill_view` follow-up inside the session window (searched, found nothing
    worth loading).
  - `unknown` — no candidate set cached (e.g. direct `skill_view` from a slash
    command).

### Storage & write path

- New helper `record_retrieval(event: dict)` in `tools/skill_usage.py`,
  appending one line under `_usage_file_lock()`. Append-only JSONL avoids the
  read-modify-write of the map store (`load_usage` `:500` / `save_usage`
  `:520`) — a retrieval log is high-volume and never mutated in place, so atomic
  append is both cheaper and lock-friendlier than rewriting `.usage.json`.
- The write is best-effort in the exact spirit of the existing bump helpers:
  wrapped so a logging failure never breaks `skills_list`/`skill_view`.
- Reads for analysis go through `retrieval_report(window_days=N)` — streams the
  JSONL, filters by `ts`, returns aggregate metrics (below). Keeping analysis
  behind one function means the curator never parses raw lines itself.

## What this unlocks that the curator CANNOT do today

### (1) Description optimization (a new mandate + mechanism)

Today the curator only **archives** and **merges** — it moves *bodies* around.
It never edits the `description:` frontmatter of the survivor, even though that
frontmatter is the *entire* ranking surface (`skills_list` `:693`). So a merge
can be content-lossless and still hurt findability if the umbrella's description
doesn't contain the query words the absorbed siblings used to match on.

Retrieval telemetry makes description optimization tractable, because it names
the exact queries that *should* have matched a skill and didn't:

- **Collision detection** — group `near_miss` events by `(top_candidate,
  chosen)` pairs. A recurring pair like `(flux-lora-sweep, flux-lora-train)`
  means two descriptions overlap on the query terms and the ranker can't
  separate them. Feed both descriptions + the colliding queries to the review
  fork with a mandate: **rewrite the survivor's `description:` to own its query
  space and disambiguate from its neighbor.**
- **Coverage gaps** — group `miss` events (searched, nothing loaded) by query.
  Cluster the query text; if a cluster maps to an existing umbrella whose
  description lacks those terms, that's a concrete "this query should have
  matched X" edit target.
- **Mechanism** — the fork already has `skill_manage action=patch`
  (`agent/curator.py:511`) and the package can edit frontmatter. We add a
  fourth consolidation move to the prompt's list at `agent/curator.py:460`:
  **(d) RE-DESCRIBE** — patch an umbrella's `description:` to absorb the query
  vocabulary of the queries that missed it, without touching its body. This is
  strictly safer than a merge (no archive, no body change) and is the highest-
  leverage edit per token, since description is the only thing `skills_list`
  shows. It should run even when `consolidate: false`, gated on its own flag
  `curator.optimize_descriptions` so findability tuning is available without
  the scarier merge pass.

### (2) Self-measurement (the curator finally grades itself)

The curator's reconciliation (`_reconcile_classification` `agent/curator.py:858`)
checks **names only** — it verifies a claimed merge's skills actually
disappeared, never whether the collection got *easier to search*. So nobody can
answer the only question that matters: *did this consolidation pass improve
findability or hurt it?*

Define a rolling-window retrieval-quality metric from the log:

| Metric | Definition | Reads as |
| --- | --- | --- |
| **precision@1** | fraction of events with `outcome == good` | "search's #1 was the pick" |
| **MRR** | mean of `1/chosen_rank` over resolved events | "how deep did agents dig" |
| **miss-rate** | fraction of events with `outcome == miss` | "search returned nothing usable" |

Compute the metric over the trailing `metric_window_days` (default 14)
**immediately before** the run (from `before_report` time) and **again after**
the run settles — the run itself is the intervention. The window boundary is the
run's `started_at`, so "before" and "after" are the same rolling window measured
on either side of the mutation.

Wire the delta into two existing surfaces:

- `_write_run_report` (`agent/curator.py:1079`) already takes `before_report` /
  `after_report` and diffs states. Add a `retrieval` block to its `run.json` /
  `REPORT.md`: `{precision_at_1_before, precision_at_1_after, mrr_before,
  mrr_after, miss_rate_before, miss_rate_after, n_events, verdict}` where
  `verdict ∈ {improved, regressed, flat, insufficient_data}`. This is the first
  time a run report will say whether the run was *net-positive for the thing the
  curator exists to improve*.
- `hermes curator status` (`hermes_cli/curator.py:39`) already prints
  `last summary` and `last report`. Add one line:
  `  findability:    p@1 0.62→0.71 (last run), 41 events/14d`.

Because self-measurement is downstream of the "after" window filling up, the
"after" number is necessarily lagged (you need post-run searches to grade the
run). Two honest options, both in-schema: report the "before" number at run time
and backfill the "after"/verdict on the *next* status read (recompute lazily
from the log), or defer the verdict to a small follow-up pass. The report should
label an un-settled verdict `insufficient_data` rather than fabricate one.

## Privacy & volume

Retrieval logs grow with every search and contain **query text**, which can
carry task-specific and potentially sensitive strings. Constraints:

- **Local only.** The log lives under `~/.hermes/skills/.retrieval.jsonl` and is
  never uploaded, mirrored, or included in any fork's memory. This is consistent
  with the review fork already running `skip_memory=True` /
  `_memory_write_origin="background_review"` (`agent/curator.py:1809` context) —
  curator internals stay out of the memory substrate by design.
- **Retention / rotation.** Cap by age (`retrieval_retain_days`, default 30) and
  size (rotate `.retrieval.jsonl` → `.retrieval.1.jsonl` at e.g. 5 MB, keep 3).
  The metric window (14d) is comfortably inside the retention window, so
  rotation never starves self-measurement. Rotation happens best-effort at
  `record_retrieval` append time — cheap, no separate timer.
- **Query redaction option.** A `retrieval_log_queries: false` config drops the
  `query` field entirely and keeps only candidate names + ranks + outcome. That
  degrades description-optimization (no query vocabulary to mine) but preserves
  all three self-measurement metrics — a clean privacy/utility knob.

## Failure modes

- **Cold start / sparse data.** A fresh library sees few searches; a 14-day
  window may hold single-digit events. Every metric must gate on a minimum
  `n_events` (e.g. 20) and report `insufficient_data` below it rather than a
  noisy ratio. The description-optimization pass must likewise require a
  collision/gap to recur (≥ K events) before it edits a `description:` — one
  near-miss is noise, not signal.
- **Gaming the metric.** precision@1 is trivially inflatable by archiving every
  low-ranking skill until only one candidate ever returns. Guardrail: the metric
  is a *diagnostic reported to the human*, never an objective the fork optimizes
  directly, and it is paired with `miss-rate` (which archiving-to-win pushes
  *up*). A run that improves p@1 while raising miss-rate is flagged, not
  celebrated. The verifier gate in `curator-verifier-gate.md` remains the
  authority on whether a mutation was legitimate; telemetry grades *findability*,
  not *correctness*.
- **Confounding run effect with query drift.** If the mix of tasks changes
  between the before- and after-windows, the metric delta reflects *what people
  searched for*, not *what the curator did*. Mitigations: (a) report `n_events`
  and a query-overlap coefficient alongside the delta so a low-overlap verdict is
  visibly untrustworthy; (b) prefer a **held-query replay** where feasible —
  re-run the pre-run window's queries against the post-run `skills_list` surface
  and measure rank change on the *same* queries, isolating the curator's effect
  from drift. Replay is the honest A/B; the live-window delta is the cheap proxy.

## Where this sits in Curator 10x

This doc is **lever 4 — the proof lever**. The other four change *what* the
curator does; this one lets you know *whether it worked*:

- `curator-semantic-clustering.md` (lever 1) replaces lexical prefix clustering
  with the embedder. Retrieval telemetry is how you **prove** the embedder found
  merges the prefix heuristic missed — and, once it shares the browse path,
  `score` in the event schema stops being `null` and becomes real similarity.
- `curator-mapreduce-forks.md` (lever 2) and `curator-verifier-gate.md`
  (lever 3) make consolidation trustworthy and scalable; the before/after
  findability metric is the acceptance test that a trusted merge was also a
  *findable* one — content-lossless (lever 3's job) is necessary but not
  sufficient, since a lossless merge with a bad `description:` still hurts p@1.
- `curator-event-driven-prevention.md` (lever 5) catches near-duplicate skills
  at *creation* time. The `near_miss` / collision detection here is the exact
  signal that pass consumes — a query that keeps landing between two skills at
  search time is the same collision lever 5 wants to prevent at author time.
  Retrieval telemetry is the shared substrate; lever 5 acts on it early, the
  curator acts on it weekly.

## Seams found while grounding this design

- **The match surface is description-only.** `skills_list`
  (`tools/skills_tool.py:689`) returns "only name + description to minimize token
  usage" (`:693`). Frontmatter `description:` is the entire ranking surface an
  agent sees before loading — confirming the prompt's premise at
  `agent/curator.py:411` and making description edits the highest-leverage
  findability lever.
- **Selection funnels through one wrapper.** `_skill_view_with_bump`
  (`tools/skills_tool.py:1630`) is the single point where a choice is committed;
  it already bumps `view` + `use` best-effort at `:1644-1649`. The retrieval
  event write bolts onto this existing funnel — no new call site in the hot path.
- **`use_count` is post-selection.** `bump_use` (`tools/skill_usage.py:623`) is
  called *after* `skill_view` succeeds — it can never encode "the wrong skill was
  chosen," which is precisely the near-miss signal the curator needs.
- **Reconciliation checks names, not findability.** `_reconcile_classification`
  (`agent/curator.py:858`) verifies claimed skills actually disappeared; nothing
  measures whether the survivor is easier to search. The self-measurement metric
  fills this exact gap.
- **The report writer already diffs before/after.** `_write_run_report`
  (`agent/curator.py:1079`) receives `before_report` / `after_report` and
  computes state transitions (`:1122-1128`). Adding a `retrieval` metric block is
  additive to a structure that already thinks in before/after snapshots.
- **Cross-process write discipline exists.** `_usage_file_lock`
  (`tools/skill_usage.py:90`) serializes `.usage.json` read-modify-write across
  processes via `fcntl`/`msvcrt`; `save_usage` (`:520`) writes atomically via
  tempfile + `os.replace`. The retrieval log reuses the lock but favors
  append-only JSONL over the map rewrite because it is high-volume and never
  mutated in place.
- **Reports live in logs, not skills.** `_reports_root` (`agent/curator.py:561`)
  deliberately puts run reports under `~/.hermes/logs/curator/` "so it's found by
  anyone looking for operational telemetry, not mixed in with the user's authored
  skill data." The metric belongs in that report; the raw retrieval log stays
  under `~/.hermes/skills/.retrieval.jsonl` next to `.usage.json` because it is
  per-skill telemetry that the curator reads, not a human-facing report.
- **Curator internals already stay out of memory.** The review fork runs with
  `skip_memory=True` (`agent/curator.py:1809` region), establishing the precedent
  that retrieval logs remain local and never enter the memory substrate.
