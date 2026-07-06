# Curator — Event-Driven Prevention (Lever 5)

Status: design proposal (not yet implemented)
Author: drafted by Claude for Vladimir
Part of: Curator 10x — see sibling docs in docs/design/curator-*.md

## Root cause: the curator is reactive and coarse-grained

Everything the curator does is post-hoc bulk cleanup on a slow clock. The only
trigger is inactivity plus an elapsed interval:

- `should_run_now()` (`agent/curator.py:219`) gates on `is_enabled()`,
  `not is_paused()`, and `last_run_at` being older than `get_interval_hours()`
  (default **168 = weekly**, `curator-common-context.md`).
- `maybe_run_curator()` (`agent/curator.py:1958`) is what the session-start /
  idle hook calls; it additionally enforces `min_idle_hours` at the call site
  (`agent/curator.py:1969-1972`) and then delegates to `run_curator_review()`.

So the shortest latency between "the agent births a near-duplicate skill" and
"the curator even looks at it" is one full interval of *inactivity* — in
practice, up to a week, and only if the box then sits idle long enough. During
that window sprawl accumulates unopposed, and it is cleaned in **one bulk
map-reduce pass** (`curator-mapreduce-forks.md`) that has to re-derive, from
scratch, the very clustering that was obvious at the moment each sibling was
created.

That is backwards. The curator prompt's stated goal is a **LIBRARY OF
CLASS-LEVEL INSTRUCTIONS** where "one broad umbrella skill with labeled
subsections beats five narrow siblings for discoverability"
(`agent/curator.py:407-413`). Preventing one narrow sibling *at birth* is
strictly cheaper than merging ten of them a week later: at creation time we have
exactly one candidate to embed, the agent that wrote it is still in the loop to
redirect, and there is no lossless-merge burden (nothing to merge yet — see the
verifier's job in `curator-verifier-gate.md`). Steering creation serves the
library goal better than post-hoc consolidation can.

## The real creation seam this hook attaches to

The curator only manages skills it *autonomously* created. That distinction is
enforced at exactly one place:

```
tools/skill_manager_tool.py:1387   if action == "create":
tools/skill_manager_tool.py:1388       if is_background_review():
tools/skill_manager_tool.py:1389           mark_agent_created(name)
```

`skill_manage(action="create")` (`tools/skill_manager_tool.py:1303`, dispatch at
`:1337` → `_create_skill`) runs for *both* foreground user-directed writes and
the background self-improvement review fork. Only when
`skill_provenance.is_background_review()` is true
(`tools/skill_provenance.py:75`, reads the `background_review` ContextVar set on
the review fork) does the tool call `mark_agent_created()`
(`tools/skill_usage.py:646`), which stamps `created_by="agent"` and makes the
skill curator-eligible (`is_agent_created` `:419`, `is_curation_eligible`
`:447`).

**This is the exact hook point.** A prevention check that fires on *every*
`skill_manage(create)` would nag the user about their own hand-authored skills —
which the curator is explicitly forbidden to touch. The check must fire on the
same condition as `mark_agent_created`: `action == "create" and
is_background_review()`. Any skill the curator would later be responsible for
consolidating is a skill it should have had a chance to prevent.

## Proposed mechanism: an at-creation similarity nudge

Insert a check that runs when the background-review fork is about to create a new
agent-managed skill, *before* the write commits inside `_create_skill`:

1. **Embed the candidate.** Take the new skill's frontmatter `name` +
   `description` + first ~N lines of body and embed it with the *same* embedder
   the semantic-clustering lever standardizes on
   (`curator-semantic-clustering.md`). One embedding call, cached by content hash
   (see "Reuse" below).
2. **Compare against the existing library.** Cosine-compare the candidate vector
   against the cached vectors of existing curator-managed skills — the set from
   `agent_created_report()` (`tools/skill_usage.py:870`) — and against any
   umbrella skills already built by prior consolidation passes.
3. **Decide on the top match.** Let `s* = max cosine` and `X = argmax` skill.
   - `s* ≥ prevent_cutoff` (proposed default **0.85**): surface a nudge.
   - `s* < prevent_cutoff`: silent pass — creation proceeds untouched.

The nudge is **advisory and never blocks**. It is returned *in the tool result*
of `skill_manage(create)` as a soft field the review fork sees on its next turn,
alongside the normal success payload:

```json
{
  "success": true,
  "name": "fix-flaky-pytest-fixture",
  "curator_nudge": {
    "kind": "near_duplicate",
    "similar_to": "testing-python",
    "cosine": 0.89,
    "message": "This looks like it extends 'testing-python' (cosine 0.89). Prefer extending that umbrella (skill_manage write_file into references/) over creating a narrow sibling — unless this is a genuinely distinct class.",
    "extend_hint": {
      "target": "testing-python",
      "suggested_action": "write_file",
      "suggested_path": "references/flaky-fixtures.md"
    }
  }
}
```

The fork then has a real decision to take on its **next iteration** (it runs
`max_iterations=9999`, so it always gets one):

- **Extend** — call `skill_manage(action="write_file", name="<X>",
  file_path="references/…")` to fold the new knowledge into the existing umbrella
  as a labeled subfile (exactly the target shape the curator prompt wants,
  `agent/curator.py:414-416`), then `skill_manage(action="delete")` the
  just-created sibling (a recoverable archive, `skill_usage.archive_skill`
  `:696`).
- **Keep** — decide the class really is distinct and do nothing; the sibling
  stands.

Crucially the create already **succeeded** before the nudge is read, so the
worst case is a redundant skill that the weekly pass will still catch. Prevention
degrades to today's behavior; it never regresses below it.

### Auto-extend vs. suggest-only

Two rungs, gated by config:

- **suggest-only** (default, opt-in phase): return the nudge, let the fork act.
  Zero behavior change to the write itself.
- **auto-extend** (later, once trusted): above a *second, higher* cutoff
  (`prevent_autoextend_cutoff`, e.g. 0.93) the tool can itself route the create
  into a `write_file` on `X` and skip the sibling. This is the same
  content-preserving move the verifier gate validates
  (`curator-verifier-gate.md`); do **not** enable auto-extend before that gate
  exists, or you reintroduce unverified mutation (thesis weakness **C**).

## Reuse, don't rebuild

This lever owns *no* new embedding or storage machinery. It is a thin consumer of
two siblings:

| Borrowed capability | From sibling | How it's used here |
|---|---|---|
| Embedder + cosine + content-hash vector cache | `curator-semantic-clustering.md` | Embed the one new skill; compare to cached library vectors. Same model, same cache — a create warms the cache the weekly pass then reuses. |
| Near-miss retrieval signal | `curator-retrieval-telemetry.md` | A skill search that returned *nothing usable* immediately before a create is an independent, embedding-free "about to duplicate" signal. |

The content-hash cache is the same idea already proven in-repo:
`tools/skills_hub.py` content-addresses skills with `content_hash(...)`
(`skills_hub.py:38`, e.g. `:3527`). The clustering lever generalizes that to an
embedding cache keyed by skill content hash; the prevention check reads it and,
on a miss, computes and *writes* one entry — so at-creation work is not wasted,
it front-loads the weekly pass.

### Composing the two signals

The retrieval-telemetry near-miss and the embedding similarity are
complementary, and either alone is enough to nudge:

- **Retrieval near-miss** fires when the fork *searched, found nothing good, and
  created anyway* — high precision on intent, needs telemetry wired.
- **Embedding similarity** fires when the new skill *lands near an existing one*
  regardless of whether a search happened — high recall, needs the embedder.

Policy: nudge if `s* ≥ prevent_cutoff` OR a logged near-miss search preceded this
create within the same fork turn. When both fire and disagree on the target,
prefer the embedding's `argmax` for `extend_hint` (it names a concrete skill);
carry the near-miss as corroboration in the message.

## Where the hook lives without slowing the hot path

Creation inside the review fork is interactive-ish (a live `AIAgent` turn), so
the check must be cheap and must fail open:

- **One embed, cache-first.** Content-hash the candidate; on cache hit, zero
  model calls. On miss, exactly one embed. Library vectors are already cached by
  the clustering lever.
- **Bounded comparison.** Cosine against ≤ (# curator-managed skills) vectors —
  a pure-numpy dot product over a few hundred rows at most; sub-millisecond.
- **Degrade gracefully.** If the embedder is offline / errors / times out, the
  check is *skipped* and creation proceeds. Wrap the whole thing in the same
  best-effort `try/except` that already guards the post-create telemetry block
  (`tools/skill_manager_tool.py:1384-1399` swallows telemetry failures) — a
  prevention failure must never break `skill_manage`.

### Sync vs. async

Run it **synchronously but only in the suggest path**, because the nudge is only
useful if it rides back on the *same* tool result the fork reads next. The cost
is bounded (one cached embed + a dot product) and the fork is not latency-
sensitive the way a foreground chat turn is. Two guards keep it honest: a hard
timeout (reuse `delegation.child_timeout_seconds` semantics conceptually; here a
small local deadline) after which we skip, and the embedder-offline fail-open
above. Auto-extend, if ever enabled, stays synchronous too — it changes the write
target, which cannot be deferred.

Do **not** move the check off-thread to a queue: an async nudge that arrives
after the fork has moved on is just a slower, worse version of the weekly pass.

## Division of labor with the weekly pass

Prevention **reduces** consolidation load; it does not replace it.

| | Event-driven prevention (this doc) | Weekly consolidation (`curator-mapreduce-forks.md`) |
|---|---|---|
| Trigger | at `skill_manage(create)` in the review fork | inactivity + `interval_hours` (`agent/curator.py:219`) |
| Scope | one new skill vs. existing library | whole library, clustered |
| Cost | 1 cached embed + dot product | N forks over N clusters |
| Failure mode if skipped | one redundant sibling survives to the weekly pass | sprawl persists another interval |

Prevention catches the *obvious* births (near-dup of an existing umbrella) at the
cheapest possible moment. The weekly map-reduce still owns everything prevention
can't see from a single create: **drift** (siblings that were distinct at birth
but converged as they were patched), **cross-cluster** merges, and any create
that happened while the embedder was offline. Fewer siblings reach the weekly
pass, so each cluster fork it spins up (thesis weakness **B**) has less to read
and merge — prevention shrinks the map-reduce fan-out rather than competing with
it.

## Config / CLI

New keys under the existing `curator:` block (`~/.hermes/config.yaml`), all
opt-in so the first ship changes nothing until a user turns it on:

```yaml
curator:
  prevent_duplicates: false        # master toggle — OFF by default
  prevent_cutoff: 0.85             # cosine ≥ this ⇒ nudge
  prevent_autoextend: false        # OFF until the verifier gate exists
  prevent_autoextend_cutoff: 0.93  # cosine ≥ this ⇒ auto-route into write_file
  prevent_use_retrieval_nearmiss: true  # also nudge on a logged search near-miss
```

Add getters mirroring the existing pattern (`get_interval_hours` `:146`,
`get_consolidate` `:190`, `get_stale_after_days` `:162`): `is_prevent_enabled`,
`get_prevent_cutoff`, `get_prevent_autoextend`, etc. No new CLI verb is required
to *use* it — the hook is inline in `skill_manage`. For observability, extend
`hermes curator status` to print a rolling count of nudges surfaced / accepted
(extend taken) / ignored, sourced from a lightweight log line written next to the
existing curator logs (`~/.hermes/logs/curator/`, `agent/curator.py:1079` region)
— this is the precision signal `curator-retrieval-telemetry.md` consumes.

## Failure modes

- **False positives on legitimately-distinct skills.** Two skills can be
  semantically adjacent yet correctly separate (`testing-python` vs.
  `testing-python-async`). Mitigation: the nudge is *advisory* — the fork can
  keep the sibling — and the default cutoff is deliberately high (0.85). Never
  block; a hard block would suppress real new classes.
- **Cutoff tuning.** 0.85 is a starting guess, embedder-dependent. Ship
  suggest-only, log every `(cosine, decision)` pair, and tune the cutoff against
  the accepted/ignored ratio before anyone enables `prevent_autoextend`. The
  retrieval-telemetry precision numbers (`curator-retrieval-telemetry.md`) are
  the ground truth for whether nudges are helping.
- **Cold start — no umbrellas yet.** On a fresh install the library has no
  agent-created skills, so `argmax` is undefined and nothing to compare against.
  The check must no-op cleanly on an empty candidate set (0 nudges), never error.
- **Do not punish the first skill in a new class.** By construction, the first
  member of any class has `s* < cutoff` (nothing near it) and passes silently.
  The nudge only ever fires against something that *already exists* — the first
  member of a class is exactly the umbrella we *want* built. This is the whole
  point: prevent sibling #2..#10, welcome member #1.
- **Embedder flapping.** Intermittent embedder availability means inconsistent
  nudging. Acceptable: a missed nudge just defers to the weekly pass. Log skips
  so status can show "N creates unchecked (embedder unavailable)".

## Seams found while grounding this design

- **The provenance gate is the only correct hook point.**
  `skill_manage(create)` calls `mark_agent_created` *only* when
  `is_background_review()` is true (`tools/skill_manager_tool.py:1387-1389`;
  provenance ContextVar at `tools/skill_provenance.py:75`). Foreground
  user-directed creates are deliberately excluded and must stay excluded — the
  prevention check has to gate on the identical condition or it will nag users
  about skills the curator may not touch.
- **Post-create work is already a best-effort island.** The telemetry block at
  `tools/skill_manager_tool.py:1384-1399` runs after `result.get("success")` and
  swallows every exception. The prevention check slots into the same failure
  discipline — it can never break `skill_manage`.
- **The candidate universe is one call.** `agent_created_report()`
  (`tools/skill_usage.py:870`) already backfills defaults and returns exactly the
  curator-managed set — the correct comparison population, no new enumeration
  logic needed.
- **The trigger really is inactivity-only.** `should_run_now()`
  (`agent/curator.py:219`) has no cron path and no event path; it is purely
  `last_run_at + interval_hours`, and fresh installs *defer* the first run by a
  full interval (`agent/curator.py:247-262`). There is currently **nothing** that
  reacts to a create — this lever adds the first event-driven path into the
  curator.
- **Content-addressing precedent exists.** `content_hash(...)` in
  `tools/skills_hub.py` (`:38`, `:3527`) already hashes skill directories; the
  embedding cache the clustering lever introduces is the same key discipline, so
  the at-creation embed and the weekly pass share one cache.

## Cross-references

- **Depends on** `curator-semantic-clustering.md` — the embedder, cosine, and
  content-hash vector cache this check consumes.
- **Feeds off** `curator-retrieval-telemetry.md` — the search near-miss signal
  and the precision numbers used to tune the cutoff.
- **Reduces load on** `curator-mapreduce-forks.md` — fewer siblings survive to
  the weekly map-reduce, shrinking its fan-out.
- **Gated by** `curator-verifier-gate.md` — `prevent_autoextend` must not ship
  until merges are proven lossless.
