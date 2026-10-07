# Optimization plan: `get_region_graph` / `get_region_graph_rich`

## Update: found and fixed a much bigger bug first (2026-10-06)

Before touching anything below, investigating *why* `build_rg` was so
slow turned up a real scaling bug, not just a constant-factor
inefficiency: `large_decode.py`'s `handle_build_rg_chunk` pre-offsets
`seg` into its final global id range *before* calling
`get_region_graph_rich`. `frontend_agglomerate.cpp`'s
`buildRegionGraphRich` sizes several per-call structures --
`sizes`, `RegionGraph`'s incidence lists (`_incEdges`), and
`region_graph.hpp`'s per-node `affinities` vector -- by `seg.max()+1`.
With pre-offset ids, that max is the *cumulative* fragment count of
every chunk processed before this one, not this chunk's own local
count (~150-270K fragments). On this run, by chunk ~1670/7200 the
global offset was already ~330 million, and growing without bound as
the run progresses -- explaining the 20-40GB+ per-chunk memory spikes
and repeated cgroup kills we'd been fighting all night, previously
(wrongly) attributed to "spatially dense chunk regions."

Fix (`handle_build_rg_chunk`, committed): build the region graph on the
chunk's own local ids (don't pre-offset `seg`), then add the global
offset only to the returned `id1`/`id2` edge-endpoint arrays (O(edges),
~1M elements, not O(volume)). Verified bit-identical against 5 real
already-completed production chunks from this run (including the
worst-offset ones) -- see `validate_rg_fix.py`. Measured on the worst
tested chunk (`z0_y28_x10`, offset ~144M): 2.37GB peak RSS / 3.83s wall
(including Python/import startup), vs. the 20-30GB+ / repeated-crash
behavior the same chunk caused before the fix.

This is now fixed on `large_decode.py`'s overlap pipeline. The
`region_graph.hpp` optimizations below are still a valid, smaller,
constant-factor follow-on (now safe to attempt without betting a live
multi-hour run on it) -- not done yet.

## Original plan (std::map -> array-backed edge lookup)

Context: `region_graph.hpp`'s two `get_region_graph` overloads (the plain
one, and the `_rich` one with `edge_counts` used by the large_decode
overlap pipeline we're actually running) both build a per-node
`std::map<ID, std::vector<F>>` during the voxel scan, buffering every raw
affinity sample per candidate edge before ever touching the histogram.
Two independent inefficiencies, to be fixed and benchmarked as two
separate commits so each one's effect is isolated and attributable.

## Benchmark methodology (same for both commits)

- Test volume: the same 300^3 sub-region smoke test used earlier this
  session (`--region 300 600 500 800 500 800` via
  `adapter/dump_adapter_affinity_fov005.py`), decoded via
  `waterz_decode_large` with `chunk_shape=[100,100,100]`,
  `overlap=[16,16,16]` (27 chunks) -- small enough to iterate on
  quickly, big enough to exercise real multi-chunk stitching.
- **Step 0 (baseline, before any change)**: rebuild-from-clean not
  needed yet (current code is already built and running); once safe to
  touch, do a baseline timing run on unmodified code first and save its
  output segmentation for diffing.
- After each commit: rebuild the extension (`pip install -e .` in the
  `pytc` conda env, editable install already points at this checkout),
  rerun the identical smoke test, record wall-clock time for the
  `build_rg` stage specifically (the stage these changes touch) and for
  the full pipeline.
- Correctness check each time: diff the resulting decoded segmentation
  against the previous step's output (bit-identical `main` array
  expected, since neither change alters the actual algorithm -- only how
  cheaply it arrives at the same edges/histograms). Also compare
  `n_segments` and the mega-blob %, as a coarse second check.
- Commit message for each: absolute wall-clock time before -> after on
  this test volume, and the fold (speedup factor).

## Commit 1: replace `std::map` edge lookup with `RegionGraph`'s existing `findEdge`/`addEdge`

Scope: `region_graph.hpp`, both overloads. Keep the overall two-pass
shape (accumulate samples per edge, then feed the statistics provider in
a second pass) -- change ONLY how a candidate edge is found-or-created
during the voxel scan.

- Replace `std::vector<std::map<ID, std::vector<F>>> affinities(...)`
  with: resolve `EdgeIdType e = rg.findEdge(id1, id2)` inline during the
  single voxel scan; if `NoEdge`, call `rg.addEdge(id1, id2)` and
  `statisticsProvider.notifyNewEdge(e)` right there.
- Buffer samples per edge via `RegionGraph<ID>::EdgeMap<std::vector<F>>`
  (the same auto-growing edge-map primitle `IterativeRegionMerging`
  already uses for `_edgeScores`/`_stale`/`_deleted`) instead of the
  per-node `std::map`. This keeps the change minimal and isolated to
  "swap the lookup structure," nothing else.
- Second pass over `rg.edges()` (0..numEdges()) feeds
  `statisticsProvider.addAffinity(e, sample)` for each buffered sample,
  and fills `edge_counts[e]` from the buffer's size -- same as today,
  just reading from the new per-edge buffer instead of the old
  per-node-map-of-vectors.
- Rationale for expected speedup: `findEdge` scans a short, contiguous,
  cache-friendly array (bounded by real node degree, ~10-30 in this
  domain) instead of two nested `std::map` lookups, each doing
  pointer-chased red-black-tree traversal with no useful prefetch.

## Commit 2: stream affinity samples directly into the histogram, drop per-edge buffering

Scope: same two overloads, building on commit 1's inline
find-or-create-edge structure.

- Remove the `EdgeMap<std::vector<F>>` buffer entirely. Call
  `statisticsProvider.addAffinity(e, affinity)` immediately at the point
  each sample is discovered in the single voxel scan (no second pass
  needed for this part -- `Histogram::inc(int)` is already a single
  array increment, fully online, never needed the full sample set
  upfront).
- Increment `edge_counts[e]` (now itself just a plain growing
  `EdgeMap<uint64_t>` or `std::vector<uint64_t>`) at the same point,
  same reasoning.
- Small same-commit addition, natural once this line is being touched
  anyway: cache the last-resolved `(id1, id2) -> e` pair across loop
  iterations. Consecutive voxels along the fast-varying (innermost, x)
  axis very often sit on the same straight fragment boundary, so this
  turns a large fraction of `findEdge` calls into a single branch
  comparison instead of even the cheap array scan. Not benchmarked as
  its own commit -- it's small enough that isolating it wouldn't be
  meaningful, but noted here so it's not a silent, unexplained addition
  if someone reads the diff later.
- Rationale for expected speedup: removes O(samples) small heap
  allocations/reallocations spread across hundreds of thousands of
  independent per-edge vectors, replaced with zero extra allocation
  beyond the graph/histogram structures that need to exist anyway.

## After both commits

- Report the two commits' individual and combined speedup on the 300^3
  smoke test.
- Only then consider a real, full-scale rerun -- not before, since the
  whole point of doing this in two isolated, benchmarked, diffed steps
  is to trust the result before betting a multi-hour run on it.

## Result (2026-10-06): both commits done, combined and deployed

Implemented in an isolated checkout (not the 300^3 smoke test -- used
real already-completed chunks from the live Tile_2x3 run instead, via
`validate_region_graph_opt.py`), benchmarked against the current
(unmodified) live checkout directly rather than a separate baseline
commit, since both commits are small enough that isolating each one's
effect wasn't worth a second full validation pass.

Found and fixed one real bug along the way: commit 1's inline
`findEdge`/`addEdge` has no built-in reason to skip background (id 0)
the way the old code's emission loop did (`for (ID id1 = 1; ...)`
implicitly dropped any edge touching background) -- without an
explicit `id1 != 0 && id2 != 0` guard, background-adjacent voxel pairs
leaked through as extra, spurious edges (caught by diffing against
real saved output: 2 of 5 initial test chunks had slightly more edges
than the ground truth). Fixed by adding the explicit guard; reran and
all chunks matched.

First timing pass showed no real gain (0.84x, i.e. slightly *slower*)
-- traced to the first call into a freshly-JIT-compiled module paying
a one-time compile/link/page-fault-in cost that has nothing to do with
the algorithm change; a warm-up call before timing anything fixed the
measurement. With that corrected, combined commits 1+2 measured
**1.29x average speedup (range 1.18x-1.41x) across 15 real production
chunks, spanning both the hot region (chunks with ~141-148M global
offset) and ordinary ones, 100% bit-identical** (edges, scores, and
contact areas, compared via `validate_region_graph_opt.py`'s
canonical id1/id2 sort -- internal edge creation order differs from
the old code's since edges are now created in voxel-scan order rather
than sorted-(id1,id2) order, but that's irrelevant since the
Python-level `get_region_graph_rich` wrapper already re-sorts the
final output by score before returning it).

Applied to the live checkout's `region_graph.hpp`. Not yet deployed
to the live Tile_2x3 run as of this commit -- unlike the pure-Python
unbounded-growth fix, this changes the compiled C++ extension, so
picking it up requires the same pause/relaunch cycle (a fresh process
import), and that's a separate decision about whether it's worth
interrupting the currently-running job again for a ~1.3x gain on one
stage (`build_rg`) when `stitch` -- untouched by this change -- is a
larger remaining task count. (Later deployed successfully.)

## Update: merge_rg memory explosion, and the sort that didn't need to exist (2026-10-06)

After `build_rg` finished, `merge_rg` (loads all 7200 chunks' region
graphs into one process to concatenate) got OOM-killed twice -- once
silently under the regular pool's uniform 8GB cgroup cap (orphaned the
task for 27 minutes with nobody actually working it, since the kill
was abrupt enough that the orchestrator never got told), then again
under a dedicated 100GB cap (real usage hit ~99GB and was still
climbing with swap spiking to 6.9GB before the cgroup killed it --
killed manually just as this was noticed, system survived both times
without the full-session crashes from earlier in this project).

Investigating *why* 100GB wasn't enough found that `merge_region_graphs`
(the `assume_disjoint_ids=True` path, the only path `handle_merge_rg`
uses) holds several full-size copies simultaneously by construction:
the caller's own `rg_list` (every chunk, ~90GB post dtype-narrowing),
then four `np.concatenate` calls (another ~90GB, while `rg_list` is
still alive), then `lo`/`hi` (two more full-size arrays), then
`np.argsort(-all_affs)` (an index array -- 8 bytes/edge at full scale,
since N here is ~7.46 billion edges, which itself exceeds uint32's
range and forces int64 indices, ~60GB just for the index), then four
more full-size copies from the `[order]` reindex. Several multiples of
the final ~90GB dataset, not one copy -- consistent with real usage
blowing past both caps.

Before building a fix for this (a streaming/preallocate rewrite was
drafted, and a true external k-way-merge-of-already-sorted-chunks
design was discussed, since each chunk's own rg file is already
sorted by score from `get_region_graph_rich`'s own `argsort` -- a
proper disk-resident/mmap'd version of that would be the right fix
*if* global sortedness is ever actually required), checked whether
`handle_agglomerate` -- the only consumer of `merge_rg`'s output --
actually needs any of this. It doesn't, in two independent ways:

1. **This run's config**: `handle_agglomerate` filters with
   `qualify = rg_affs >= threshold`. This run's `threshold` is `2.0`,
   intentionally set (per the user) to isolate raw stitched fragments
   for direct comparison against ScalableMinds' own fragments-level
   numbers. Since affinity scores are mathematically bounded to
   `[0, 1]`, `rg_affs >= 2.0` is provably false for every edge, in
   every chunk, always -- not just empirically on a sample. Confirmed
   empirically anyway on 36 sampled chunks (35.8M edges, 0 qualify) as
   a sanity check before relying on the mathematical argument alone.
   `handle_merge_rg` now short-circuits this case: if
   `max(self.config.thresholds) > 1.0`, skip the full concatenation
   entirely and write an empty placeholder `merged_rg` file, since no
   edge could ever qualify downstream regardless of what the real
   merge would have produced. Fixed this run in 1.42s (was stuck/dead
   for 27+ minutes before).

2. **The general case, for when a real threshold is used later**: a
   boolean mask (`rg_affs >= threshold`) produces the same result
   regardless of input order, for *any* threshold value -- not just
   `2.0`. So the sort inside `merge_region_graphs` was never actually
   needed by this pipeline's one and only caller, independent of the
   threshold-value special case above. Searched the entire
   `/home/cosmo/segmentation` tree (not just this package) for any
   other caller of `merge_region_graphs` or consumer of its sorted
   contract -- there is none; `handle_merge_rg` is the only caller
   anywhere. Added a `sort: bool = True` parameter (default preserves
   the documented contract for any future/external caller), so when
   real thresholds are used, `handle_merge_rg` should pass
   `sort=False` *and* use the preallocate-and-stream rewrite (still
   not implemented -- not needed yet, since today's run hits the
   `threshold > 1.0` short-circuit instead). That combination handles
   the general/real-threshold case without ever needing the heavier
   external-merge/mmap design -- that design would only become
   necessary if some future caller genuinely needs global sort order,
   which, as of this writing, none does.

## Update: replaced the special case with the general fix (2026-10-06)

The `threshold > 1.0` short-circuit above worked but was a special
case of a better general idea (user's insight, sharper than the
preallocate-and-stream plan this update originally proposed): since
`handle_agglomerate`'s `rg_affs >= threshold` filter is distributive
over concatenation, filter *before* concatenating instead of after.
`handle_merge_rg` now loads each of the 7200 chunks' (small) edge
lists one at a time, filters immediately, and only concatenates the
survivors -- never materializing the full ~90GB unfiltered dataset at
all, for any threshold value, not just the degenerate one. No sort,
no preallocate-the-full-thing step, no list-of-everything. Removed
the now-unused `merge_region_graphs` import and the `sort` parameter
added earlier to it is consequently also unused by this package again
(left in place -- still correct, still useful if a future caller
needs actual sorted output). `handle_agglomerate` simplified to match:
since `merged_rg` now only ever contains survivors, it no longer
re-filters, just consumes `id1`/`id2` directly.

Validated two ways before deploying: (1) per-chunk-filter vs.
concat-then-filter produce bit-identical survivor sets on 200 real
chunks at a realistic, selective threshold (0.85 -> 50.3% survival,
confirming equivalence isn't just trivially true at the degenerate
threshold); (2) re-ran the updated `handle_merge_rg` against the
*entire* real dataset (all 7200 chunks, this run's actual
`threshold=2.0` config) -- completed in 27.7s with 0 survivors out of
7,717,380,483 total edges considered, matching the earlier
special-cased result's outcome (same `num_edges=0`) but via the
general mechanism instead of a threshold-specific bypass.

This is now the permanent implementation, not a deferred TODO --
nothing left to revisit here when a real threshold is eventually
used; the general per-chunk filter already handles that case
correctly today. (The one case this doesn't rescue: a threshold so
permissive that nearly all edges qualify -- then the survivor set
really is close to the full dataset, and no clever filtering order
changes that. Not a flaw in the approach, just an inherent limit of
how much data a very permissive threshold actually needs to keep.)

## Update: stitch optimization, round 1 (I/O) wasn't the bottleneck (2026-10-06/07)

Separately from the merge_rg work above, `stitch` was identified as
the largest remaining task count (20212, vs. build_rg's 7200) and its
`handle_stitch_overlap` had an obvious inefficiency: it read each
*entire* chunk (`_read_chunk_seg`, ~176x192x176) via HDF5 just to
slice out a thin ~16-32-voxel-thick overlap region afterward, and did
this for both chunks on every one of a chunk's up-to-6 border tasks.
Fixed by reading the needed hyperslab directly via HDF5 slicing
(`_read_chunk_region`, generalizing the single-index `_read_chunk_face`
pattern already used elsewhere in this file to an arbitrary slice
range) instead of loading-then-slicing in numpy. Measured ~4.1x-6.35x
faster per read on real chunk files (the wider range reflects two
measurements: an initial same-file-sequential comparison at 4.1x, and
a corrected disjoint-cold-files comparison at 6.35x that ruled out
page-cache contamination between the two reads as the explanation).
Validated bit-identical against 20 real border pairs' saved ground
truth. Deployed to the live run.

Live throughput barely moved after deploying (0.998 tasks/sec vs.
~0.97 tasks/sec pre-fix) -- suspicious given the measured read
speedup, so profiled the real `handle_stitch_overlap` call end-to-end
instead of trusting the isolated read benchmark. Found the read step
really did drop to ~28ms (consistent with the fix working), but
`build_overlap_remap` -- a separate function this task also calls,
untouched by the read fix -- took ~758ms, **96% of total per-task
time**. The I/O fix was real but optimized a part of the task that
was never the bottleneck.

## Update: stitch optimization, round 2 (the actual bottleneck) (2026-10-07)

`build_overlap_remap` (`overlap_stitch.py`) does majority-vote
matching: for each dst fragment touching a src fragment in the
overlap zone, map it to whichever src fragment it shares the most
voxels with. It built a structured-dtype array of `(dst, src)` pairs,
called `np.unique(pairs, return_counts=True)` to count occurrences,
then found the max-count src per dst via a **pure Python for-loop**
over every unique pair with two dict lookups per iteration.

First fix attempt: vectorized the Python loop into a stable
`lexsort`-based groupby-argmax (sort by dst ascending/count
descending, take each dst group's first row). Correct (validated
bit-identical against the original on 25 real border pairs), but
**~1.0x speedup** -- the loop was never the bottleneck either.

Profiled inside the function to find the real cost: `np.unique` on
the **structured dtype** itself took 813.8ms on real overlap data.
The same count-occurrences operation using a **packed scalar uint64
key** (`(dst << 32) | src`, when both fit in 32 bits -- checked
against the real data, same `max_val <= 0xFFFFFFFF` pattern already
used in `_merge.py`'s non-disjoint path, falling back to the
structured dtype otherwise) took 16.8ms for the same data -- **48x
faster**. `np.unique` on a structured/record dtype falls back to slow
generic element-wise comparison instead of numpy's fast scalar-sort
path; this is the exact same class of inefficiency as the
`std::map`-vs-array-lookup fix from the `region_graph.hpp` work, and
the same packed-key trick as `merge_region_graphs`, just not yet
applied here.

Combined fix (packed key + the vectorized groupby from the first
attempt): validated bit-identical (exact dict equality) against the
original pure-Python-loop implementation on 25 real border pairs.
**17.9x combined speedup** (18.0s -> 1.0s total across the 25 test
borders; individual borders ranged 15.0x-32.2x). Combined with the
read fix, projected per-task time drops from ~793ms to roughly
~80ms -- about a 10x overall task speedup, not the ~1.0x the I/O fix
alone delivered live.

## Update: stitch optimization, round 3 (the orchestrator itself) (2026-10-07)

Deployed round 2 and measured live throughput again: still barely
moved (6346 succeeded at a baseline of 6064, 282 tasks over 263s =
1.07 tasks/sec -- essentially flat versus the ~0.97-1.0 tasks/sec
from before *any* of this round's fixes). Three independent task-time
fixes in a row (read, remap, each individually validated and real)
and live throughput never moved -- meant something outside the task's
own algorithmic work was now the floor.

Found it in `orchestrator.py`: `claim_ready_task` calls
`list_records()`, which globs *every* task file across the whole
workflow (41,816 of them at this point: fragment 7200 + stitch 20212
+ build_rg 7200 + apply 7200 + a handful of singletons), loads and
JSON-parses every single one, sorts the result, and only *then* does
`claim_ready_task` start filtering for one pending+ready task. Timed
directly on the live run: ~1.0-1.2s per call. Every worker calls this
on every claim (every ~80ms of real work, now). This was the actual
floor on throughput, and it predates every fix in this document --
it was just never visible before, because build_rg/stitch's own
per-task algorithmic cost (seconds, pre-fix) was always larger than
this constant ~1s overhead, so fixing the algorithmic side kept
helping until it didn't.

Two layered fixes, in order of how much each bought:

1. Added an optional `stages` filter to `list_records()` so the glob
   itself can be narrowed (`f"{stage}_*.json"` instead of `"*.json"`)
   when a caller restricts `allowed_stages` -- safe because
   `_task_filename` slugifies `"{stage}:{key}"` as one string, and
   every stage name in this package is plain alphanumeric/underscore,
   so the stage name always survives verbatim as the filename prefix
   (the colon is the only character the slugify regex ever touches).
   `_deps_satisfied` is unaffected either way -- it looks up each dep
   directly by task_id (O(1) file read), never via this list, so
   narrowing what *this* call loads can't break dependency checking
   on tasks from other stages. Validated the narrowed result exactly
   matches the stitch subset of a full unnarrowed scan. Only ~1.9x
   though (0.55s vs 1.0s) -- stitch alone is still 20212 files, half
   the total.
2. The bigger fix: `list_records()` (and `claim_ready_task`, which
   built on it) always *eagerly* loaded and sorted the entire
   (possibly stage-narrowed) list before any pending/ready filtering
   began -- so even when the very first matching candidate was near
   the front, the call still paid to parse everything else first.
   Added `_iter_records_lazy()`, a generator that yields records one
   at a time as they're loaded, and switched `claim_ready_task` to
   use it instead of `list_records()` -- same filtering logic, just
   genuinely lazy, returning as soon as one claimable task is found
   instead of after loading all of them. No sort needed either, since
   claim order never had to be deterministic. Measured on the live
   run: **~22ms average per claim, down from ~1.0-1.2s -- 24-54x
   faster**, now comparable to or cheaper than the ~80ms of real
   decode work it precedes, instead of ~15x more expensive than it.

`list_records()` itself (now also accepting the `stages` filter)
stays eager/sorted for its other, far-less-frequent callers (progress
reporting, stage-completion checks) where getting the whole list back
is actually wanted -- only the hot claiming path needed to change.

Deploying both rounds (2 and 3) together, since round 2 alone wasn't
enough to show live benefit -- round 3 is what actually unblocks it.
