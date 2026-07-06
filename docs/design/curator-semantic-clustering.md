# Curator — Semantic Clustering with the Existing Embedder

Status: design proposal (not yet implemented)
Author: drafted by Claude for Vladimir
Part of: Curator 10x — see sibling docs in docs/design/curator-*.md

## Thesis (this doc unlocks lever A)

The curator's consolidation pass is OFF by default (`curator.consolidate: false`)
because three coupled weaknesses make it untrustworthy: (A) it clusters skills
**lexically**, (B) it does all the work in one saturating single-shot fork
(`curator-mapreduce-forks.md`), and (C) it mutates without proving merges are
lossless (`curator-verifier-gate.md`). This doc kills lever **A**: replace the
prompt's name-prefix heuristic with deterministic embedding-based clustering
computed *before* any LLM runs.

Semantic clustering is the prerequisite fix. The map-reduce redesign
(`curator-mapreduce-forks.md`) needs a **candidate structure to map over** — a
cluster becomes one map unit — and the verifier gate (`curator-verifier-gate.md`)
is only worth building once clusters are drawn on meaning rather than spelling.
So sequence this first.

## Root cause: clustering is lexical, discoverability is semantic

The fork is told, verbatim, how to find merge candidates
(`agent/curator.py:450`–`454`):

> "1. Scan the full candidate list. Identify PREFIX CLUSTERS (skills
> sharing a first word or domain keyword). Examples you are likely to
> find: hermes-config-*, hermes-dashboard-*, gateway-*, codex-*,
> ollama-*, anthropic-*, gemini-*, mcp-*, salvage-*, pr-*,
> competitor-*, python-*, security-*, etc. Expect 10-25 clusters."

That is a **first-word groupby on the skill name**. It clusters
`gateway-timeout-fix` with `gateway-restart-loop` because both start with
`gateway`. It cannot cluster `gateway-timeout-fix` with
`hermes-config-max-tokens-death-loop` even when both are, in substance, "the
gateway hangs on long sessions and the fix is a `max_tokens` reserve" — because
their name prefixes (`gateway-` vs `hermes-config-`) never collide.

This is fatal, and the prompt itself states why, four lines up
(`agent/curator.py:410`–`413`):

> "An agent searching skills matches on descriptions, not on exact names;
> one broad umbrella skill with labeled subsections beats five narrow
> siblings for discoverability, not the other way around."

So the collection's stated goal is **description-matched discoverability**. The
clustering signal the curator actually uses is the one axis the goal says
*doesn't matter*: the name prefix. A skill is discovered by the semantic
proximity of a query to its **description**; it is grouped for consolidation by
the lexical proximity of its **name**. Those two spaces are unrelated. Concretely:

- **False negatives (the expensive failure).** Semantically-equal skills with
  different name lineages never enter the same cluster, so they never get
  offered to the fork as a merge candidate, so they survive as narrow siblings
  forever. The library accretes exactly the "hundreds of narrow skills"
  degenerate case the prompt calls "a FAILURE of the library"
  (`agent/curator.py:408`–`410`).
- **False positives.** `python-repl-probe` and `python-packaging-notes` share a
  prefix and get proposed as one umbrella despite serving disjoint intents,
  wasting fork iterations arguing itself out of a bad merge.
- **Naming is author mood, not taxonomy.** Agent-created skill names are minted
  ad hoc at authoring time (a PR number, an error string, a codename — the
  prompt itself flags these at `agent/curator.py:502`–`505`). Prefix clustering
  inherits every naming inconsistency as a taxonomy gap.

The name is a *label*, not a *coordinate*. To cluster by meaning we need a
coordinate, and the collection already computes exactly that coordinate for
retrieval elsewhere: a dense embedding. This design computes it for the curator
too, deterministically, before the fork sees anything.

## Proposed mechanism

Insert a deterministic clustering stage between `apply_automatic_transitions`
(`agent/curator.py:291`) and the LLM fork. The fork stops *finding* clusters and
starts *acting on* pre-computed ones.

### 1. What text to embed

For each agent-created candidate (the rows from
`skill_usage.agent_created_report()` at `tools/skill_usage.py:870`), build one
embedding input document per skill:

```
<name>
<SKILL.md description / frontmatter summary>

<SKILL.md body, truncated to ~2–3k chars>
```

Order matters and is deliberate:

- **Description first, weighted heaviest.** The prompt says discovery matches on
  descriptions (`agent/curator.py:410`). Cluster on the same signal the target
  goal optimizes. If a description is empty, fall back to the first paragraph of
  the body.
- **Name included but not dominant.** The name still carries real signal
  (`gateway-` really does often mean gateway); include it so lineage siblings
  still tend to co-cluster, but do not let it dominate a rich body.
- **Body truncated.** Session-specific tails (`references/`, long repro logs)
  add noise and token cost without moving the class-level centroid. Cap the body
  contribution so a 6-KB skill and a 600-char skill embed at comparable weight.

Do **not** embed `references/`, `templates/`, `scripts/`, or `assets/`
sub-files. Those are the *contents* of a skill package, not new roots (the
prompt is explicit about this distinction at `agent/curator.py:483`–`489`); a
per-skill document is the clustering unit.

### 2. Where the embedder comes from (injected seam, never hard-coded)

Vladimir already runs an OpenAI-compatible embeddings endpoint, aliased
`lmstudio-embed-auto`, and the `smart_memory` plugin already talks to it. The
client is `EmbeddingClient` in `plugins/smart_memory/embeddings.py:26`:

- `embed(texts) -> list | None` POSTs `{"model", "input": [...]}` to
  `{base_url}/embeddings`, returns **unit-normalized** float32 vectors, and
  **returns `None` on any failure** (`embeddings.py:32`–`55`). Graceful
  degradation is already the contract — reuse it, don't reinvent it.
- `cosine(a, b)` is a plain dot product because vectors are pre-normalized, with
  a dimension-mismatch guard that returns `-1.0` (`embeddings.py:81`–`88`). That
  guard is load-bearing here: a re-embed under a different model can never
  spuriously match a stale vector.
- The plugin resolves the endpoint from an `embedder:` config block
  (`base_url`, `model`, `timeout`) at `plugins/smart_memory/__init__.py:289`–
  `296`, defaulting `base_url` to `http://localhost:1234/v1`.

**Do not import the plugin.** The curator lives in `agent/`, the embedder in
`plugins/smart_memory/`; a hard import couples the core curator to an optional
plugin. Instead define a tiny structural seam — an `Embedder` protocol with the
one method the curator needs:

```python
class Embedder(Protocol):
    def embed(self, texts: list[str]) -> "list | None": ...
```

Resolve a concrete instance through the **same aux-slot config plumbing the
review fork already uses** — `auxiliary.curator.*` is resolved by
`_resolve_review_runtime` (`agent/curator.py:1744`). Add a parallel
`auxiliary.curator.embedder.{base_url,model,timeout}` block and a
`_resolve_curator_embedder(cfg)` helper that returns either a duck-typed
`EmbeddingClient` (constructed with `base_url`/`model` from config, pointing at
`lmstudio-embed-auto`) or `None` when unconfigured/unreachable. The clustering
function takes the embedder as a **parameter**, so tests inject a fake and prod
injects the LM Studio client. This is the exact injection style the sibling
docs assume.

### 3. Caching keyed on content hash (incremental re-embedding)

Embedding hundreds of skills weekly is wasteful when almost nothing changed
between runs. Cache per skill, keyed on a content hash:

```
key   = sha256(name + "\0" + skill_md_bytes)[:16]
value = { "dim": D, "model": <embed-model>, "vec": <float32 bytes> }
```

Store at `~/.hermes/logs/curator/.embed_cache.json` (the curator already owns
`~/.hermes/logs/curator/` — `_reports_root()` at `agent/curator.py:561`), or a
sidecar `.npy`/binary blob if numpy is present (mirror
`EmbeddingClient.to_bytes`/`from_bytes` at `embeddings.py:73`–`79`). On each
run:

1. Hash every candidate's `SKILL.md`.
2. Cache **hit** (hash unchanged **and** cached `model` == current embed model)
   → reuse the vector, zero endpoint calls.
3. Cache **miss** (new skill, edited `SKILL.md`, or model changed) → collect
   into a batch, call `embed()` once for the whole batch, store results.
4. Garbage-collect entries whose skill no longer appears in
   `agent_created_report()`.

The model-in-key check enforces the same vector-space invariant `cosine()`
guards (`embeddings.py:83`–`88`): switch embed models and every entry misses and
re-embeds, so you never mix spaces. Steady-state, a weekly run with a handful of
new/edited skills issues one small batch call.

### 4. Clustering algorithm: threshold agglomerative / connected-components

Build the cosine similarity graph over the N candidate vectors, add an edge for
every pair with `cosine >= τ`, and take **connected components** as clusters
(equivalently: single-linkage agglomerative cut at distance `1 − τ`).

Why this and not k-means:

| | connected-components @ τ | k-means |
|---|---|---|
| Needs a preset cluster count | no — emerges from the data | yes — `k` unknown; the whole point is we don't know how many umbrellas exist |
| Handles singletons | naturally (a skill with no edge is its own component) | forces every point into a cluster; a genuinely unique skill gets absorbed into the nearest centroid |
| Determinism | fully deterministic given τ | seed-dependent centroid init; two runs cluster differently → non-reproducible curator |
| Cluster shape | arbitrary (chains of related skills) | spherical, equal-variance assumption — wrong for skill topics |

Determinism is non-negotiable: a curator that proposes different merges on
identical input each week is untrustworthy by construction. Connected-components
over a fixed τ is a pure function of (vectors, τ).

For hundreds of skills the pairwise similarity matrix is trivial (a few hundred
squared is well under a million dot products of normalized vectors — sub-second
with numpy). No approximate-NN index is warranted at this scale; revisit only if
the library reaches low thousands.

**Choosing τ.** Cosine thresholds are model-specific, so pin τ empirically
against the actual embed model, not by folklore. Procedure: embed the current
library, sort all pairwise cosines descending, and eyeball where genuinely
"same-class" pairs stop and "same-domain-but-distinct" pairs begin — that knee
is τ. Start at **τ ≈ 0.82** for Qwen3-Embedding-family models (smart_memory's
own semantic floor for a *looser* "related" judgement is 0.35 per MEMORY.md, so
a *merge-candidate* bar must be markedly higher). Expose τ as
`curator.cluster_threshold` (config-tunable, defaulted). Bias **high**: an
over-tight τ under-clusters (some merges missed, caught next run or by the
event-driven pass in `curator-event-driven-prevention.md`), whereas a loose τ
over-clusters and hands the fork bad candidates — and a bad merge is far more
expensive than a missed one.

### 5. Singletons

A skill with no edge above τ is a **singleton cluster of size 1**. Singletons
are legitimate output: a genuinely unique class-level skill *should not* be
merged, and the prompt already sanctions "keep" for exactly that case
(`agent/curator.py:524`–`528`). Singletons are simply omitted from the map units
the fork acts on (nothing to consolidate), while still being counted in the
report so the run shows "N candidates, M clustered, K singletons".

## How clusters feed the existing pass — the change surface

Today `_render_candidate_list()` (`agent/curator.py:1458`) emits a flat bulleted
list and the fork is told to *find* prefix clusters in it
(`agent/curator.py:450`). The change:

**`run_curator_review` / `_llm_pass`** (`agent/curator.py:1480`, nested pass
starts `:1569`): after `apply_automatic_transitions` and before building the
prompt (`agent/curator.py:1627`), compute clusters:

```python
embedder = _resolve_curator_embedder(cfg)          # may be None
clusters = compute_semantic_clusters(              # pure fn, embedder injected
    rows=skill_usage.agent_created_report(),
    embedder=embedder,
    threshold=get_cluster_threshold(),
)
candidate_list = _render_candidate_list(clusters=clusters)
```

**`_render_candidate_list`** (`agent/curator.py:1458`): accept an optional
`clusters` arg. When present, render skills **grouped by cluster** with a
similarity hint, e.g.:

```
Semantic cluster 3 (cohesion ~0.88) — likely one umbrella:
  - gateway-timeout-fix          state=active  use=2  ...
  - hermes-config-max-tokens...  state=stale   use=0  ...
Singletons (no strong semantic sibling):
  - poco-f1-pmos-kernel          state=active  ...
```

Preserve every existing per-row field (`state`/`pinned`/`cron`/`use`/
`last_activity`, `agent/curator.py:1466`–`1476`) — clustering *adds* grouping,
it doesn't remove the fields the hard rules depend on (pinned/cron gating at
`agent/curator.py:425`–`435` must still be visible per row).

**`CURATOR_REVIEW_PROMPT`** (`agent/curator.py:403`): replace step 1
(`:450`–`454`). New text: *"The candidate list below is ALREADY grouped into
semantic clusters computed from skill descriptions and bodies. For each cluster
with 2+ members, decide the umbrella per the three consolidation modes below.
Do not re-derive clusters from names. You MAY split a cluster you judge
over-merged, or note a cross-cluster merge you'd make — but the provided
clustering is your starting structure."* Steps 2–5 (the umbrella modes,
package-integrity rules, structured-YAML output) are unchanged — the fork's job
narrows from *discovery + judgement* to *judgement*, which is exactly the token
budget relief `curator-mapreduce-forks.md` then exploits by handing one cluster
per fork.

Nothing downstream changes: the fork still emits the same
`consolidations:`/`prunings:` YAML (`agent/curator.py:536`–`553`), still gets
reconciled by `_parse_structured_summary`/`_reconcile_classification`
(`agent/curator.py:723`/`:858`), still reported via `_write_run_report`
(`agent/curator.py:1079`). The clustering is a pre-pass; the whole existing
mutation/reporting spine is untouched.

## Failure modes

- **Embedder offline — must never block the ager.** If
  `_resolve_curator_embedder` returns `None`, or `embed()` returns `None`
  (`embeddings.py:52`–`55`), `compute_semantic_clusters` **falls back to today's
  lexical prefix grouping** and the prompt reverts to its current step-1 text.
  The deterministic ager (`apply_automatic_transitions`,
  `agent/curator.py:291`) runs unconditionally and first, so a down embedder
  degrades semantic → lexical but never stops stale/archive transitions. This
  matches smart_memory's own contract: the embedder is optional, its absence
  silently downgrades quality, nothing hard-depends on it.
- **Bad / low-quality embeddings.** A weak model produces a flat similarity
  distribution (no clear knee), so τ can't separate classes. Mitigations: pin τ
  empirically against the *actual* model (§4); the model-in-cache-key invariant
  prevents mixing spaces; and the downstream verifier gate
  (`curator-verifier-gate.md`) is the real backstop — a semantically-plausible
  but content-wrong cluster still can't produce a lossy merge, because the merge
  must be *proven* lossless before archive.
- **Over-clustering** (τ too low): the fork receives sprawling clusters and is
  asked to umbrella-ify unrelated skills. The prompt already licenses "keep"
  and now explicitly permits splitting a provided cluster, so the fork can
  reject; but bias τ high (§4) to avoid relying on the fork to undo bad grouping.
- **Under-clustering** (τ too high): real siblings land in separate clusters and
  a merge is missed. This is the *safe* direction — the miss is caught on a
  later run (descriptions drift closer, or a new sibling bridges them) or by the
  event-driven pass at creation time
  (`curator-event-driven-prevention.md`). A missed merge costs one narrow skill;
  a bad merge costs correctness.
- **Cost of embedding hundreds of skills.** First run embeds all N in one
  batched `embed()` call; every subsequent run embeds only the delta (§3 cache),
  typically a handful. Endpoint load is bounded and local (`lmstudio-embed-auto`
  on Vladimir's box). If the batch is large, chunk it — `EmbeddingClient.embed`
  takes a list and preserves input order via the OpenAI `index` field
  (`embeddings.py:44`–`45`), so chunking is safe.

## Seams found while grounding this design

- **The clustering instruction is a literal first-word groupby.**
  `agent/curator.py:450`–`454` tells the fork to find "PREFIX CLUSTERS (skills
  sharing a first word or domain keyword)" — pure name-lexical, on the one axis
  the discoverability goal (`agent/curator.py:410`) says is irrelevant. This is
  lever A, quoted verbatim.
- **Candidates arrive as a flat, field-rich list.** `_render_candidate_list`
  (`agent/curator.py:1458`) emits one bullet per skill with
  `state`/`pinned`/`cron`/`use`/`view`/`patches`/`last_activity`
  (`:1466`–`:1476`). Grouping can wrap this render without disturbing the fields
  the hard rules gate on.
- **A working, degrade-safe embeddings client already exists in-tree.**
  `plugins/smart_memory/embeddings.py:26` — `embed()` returns `None` on failure
  (`:52`), vectors are unit-normalized (`:48`–`50`), `cosine()` is a guarded dot
  product (`:81`–`88`). Reuse the contract; inject the client via a protocol, do
  not import the plugin.
- **The embedder is already config-resolved from an aux block.**
  `plugins/smart_memory/__init__.py:289`–`296` reads `embedder.{base_url,
  model,timeout}` and constructs the client lazily. The curator has the parallel
  plumbing: `_resolve_review_runtime` (`agent/curator.py:1744`) already resolves
  `auxiliary.curator.*`; add `auxiliary.curator.embedder.*` alongside it.
- **The curator owns a writable, profile-aware dir for the vector cache.**
  `_reports_root()` (`agent/curator.py:561`) → `~/.hermes/logs/curator/`,
  mkdir-guarded. `.embed_cache.json` belongs here, next to the run reports.
- **The ager is fully decoupled from the fork.**
  `apply_automatic_transitions` (`agent/curator.py:291`) runs before and
  independent of the LLM pass (`run_curator_review`, `:1545` then `_llm_pass`,
  `:1569`), so clustering can fail closed to lexical without ever endangering the
  deterministic stale/archive transitions — the required graceful-degradation
  property.
- **Reconciliation is name-only.** `_reconcile_classification`
  (`agent/curator.py:858`) checks *which names disappeared*, never whether a
  merge was content-lossless. Better clusters improve *which* merges get
  proposed but do nothing for merge *correctness* — that's lever C
  (`curator-verifier-gate.md`), which this doc is a prerequisite for.

## Cross-references

- `curator-mapreduce-forks.md` — the clusters produced here are the **map unit**:
  one bounded fork per cluster, run concurrently. Build clustering first.
- `curator-verifier-gate.md` — better candidates still need lossless-merge
  proof; semantic clustering is prerequisite-ish (cluster on meaning, *then*
  gate the merge).
- `curator-retrieval-telemetry.md` — measures whether these merges actually
  improved description-matched discovery (precision before/after).
- `curator-event-driven-prevention.md` — reuses the same embedder + cache to
  catch a near-duplicate at skill-creation time instead of the weekly pass.
