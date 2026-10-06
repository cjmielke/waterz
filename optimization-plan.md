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
larger remaining task count.
