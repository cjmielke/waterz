"""Validate the local-id build_rg fix against real, already-completed
production output from the (unfixed) live run -- using the exact same
inputs (raw per-chunk fragment seg, affinity window, global offset)."""
import sys
import time

sys.path.insert(0, "/home/cosmo/segmentation/waterz/src")
import numpy as np
from waterz.large_decode import LargeDecodeRunner
from waterz._merge import get_region_graph_rich

WORKFLOW_ROOT = "/home/cosmo/segmentation/adapter/tile2x3_waterz_fragments_workflow"
TEST_CHUNKS = ["z0_y0_x0", "z0_y0_x10", "z0_y28_x0", "z0_y28_x10", "z0_y29_x0"]

runner = LargeDecodeRunner.load(WORKFLOW_ROOT)
offsets = runner._read_json(runner._offsets_path())

for chunk_key in TEST_CHUNKS:
    ov_chunk = runner.overlap_chunk_map[chunk_key]
    affs = runner._read_affinity_chunk(ov_chunk)
    if affs.dtype != np.uint8:
        affs = affs.astype(np.float32, copy=False)
    seg_local = runner._read_chunk_seg(runner._raw_chunk_path(chunk_key))
    offset = int(offsets["chunk_offsets"][chunk_key])

    local_max = int(seg_local.max(initial=0))
    print(f"\n{chunk_key}: local_max_id={local_max:,} global_offset={offset:,} "
          f"(global_max_id_would_be={local_max+offset:,})")

    t0 = time.time()
    rg_affs, id1, id2, contact_areas = get_region_graph_rich(
        seg_local, affs, scoring_function=runner.config.affinity_scoring_function,
    )
    dt_fixed = time.time() - t0
    if offset:
        id1 = id1 + offset
        id2 = id2 + offset

    # Load the ground-truth (unfixed, pre-offset) saved result
    saved = np.load(f"{WORKFLOW_ROOT}/rg/{chunk_key}.npz")
    gt_affs, gt_id1, gt_id2, gt_areas = saved["rg_affs"], saved["id1"], saved["id2"], saved["contact_areas"]

    # Canonical sort for comparison (by id1, id2) since argsort(-rg_affs) order
    # can differ on ties
    def canon(affs_, i1, i2, areas_):
        order = np.lexsort((i2, i1))
        return affs_[order], i1[order], i2[order], areas_[order]

    fa, fi1, fi2, fareas = canon(rg_affs, id1, id2, contact_areas)
    ga, gi1, gi2, gareas = canon(gt_affs, gt_id1, gt_id2, gt_areas)

    match_shape = fi1.shape == gi1.shape
    match_ids = match_shape and np.array_equal(fi1, gi1) and np.array_equal(fi2, gi2)
    match_affs = match_shape and match_ids and np.allclose(fa, ga, atol=1e-6)
    match_areas = match_shape and match_ids and np.array_equal(fareas, gareas)

    print(f"  n_edges: fixed={len(fi1)} ground_truth={len(gi1)}")
    print(f"  match: shape={match_shape} ids={match_ids} affs={match_affs} areas={match_areas}")
    print(f"  fixed-code time: {dt_fixed:.2f}s")
    if not (match_shape and match_ids and match_affs and match_areas):
        print("  *** MISMATCH -- investigate before trusting this fix ***")
