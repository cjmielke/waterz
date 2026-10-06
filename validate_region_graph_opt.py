"""Validate region_graph.hpp's std::map->findEdge + streaming optimization
against real, already-completed production output (saved before this
change existed) -- confirms bit-identical edges/scores/contact-areas.

Usage: point WORKFLOW_ROOT at any large_decode overlap-pipeline workflow
directory that still has its rg/<chunk_key>.npz files on disk.
"""
import sys
import time

import numpy as np

sys.path.insert(0, "/home/cosmo/segmentation/waterz/src")
from waterz.large_decode import LargeDecodeRunner
from waterz._merge import get_region_graph_rich

WORKFLOW_ROOT = "/home/cosmo/segmentation/adapter/tile2x3_waterz_fragments_workflow"
TEST_CHUNKS = [
    "z0_y0_x0", "z0_y0_x10", "z0_y28_x0", "z0_y28_x10", "z0_y29_x0",
    "z2_y25_x10", "z0_y22_x16", "z0_y6_x19", "z1_y9_x8", "z1_y0_x15",
    "z1_y32_x6", "z0_y10_x8", "z0_y0_x1", "z0_y0_x18", "z0_y6_x23",
]


def canon(affs_, i1, i2, areas_):
    order = np.lexsort((i2, i1))
    return affs_[order], i1[order], i2[order], areas_[order]


runner = LargeDecodeRunner.load(WORKFLOW_ROOT)
offsets = runner._read_json(runner._offsets_path())

all_match = True
total_t = 0.0
for chunk_key in TEST_CHUNKS:
    ov_chunk = runner.overlap_chunk_map[chunk_key]
    affs = runner._read_affinity_chunk(ov_chunk)
    if affs.dtype != np.uint8:
        affs = affs.astype(np.float32, copy=False)
    seg_local = runner._read_chunk_seg(runner._raw_chunk_path(chunk_key))
    offset = int(offsets["chunk_offsets"][chunk_key])

    t0 = time.time()
    rg_affs, id1, id2, contact_areas = get_region_graph_rich(
        seg_local, affs, scoring_function=runner.config.affinity_scoring_function,
    )
    dt = time.time() - t0
    total_t += dt
    if offset:
        id1 = id1 + offset
        id2 = id2 + offset

    saved = np.load(f"{WORKFLOW_ROOT}/rg/{chunk_key}.npz")
    fa, fi1, fi2, fareas = canon(rg_affs, id1, id2, contact_areas)
    ga, gi1, gi2, gareas = canon(saved["rg_affs"], saved["id1"], saved["id2"], saved["contact_areas"])

    match = (
        fi1.shape == gi1.shape
        and np.array_equal(fi1, gi1) and np.array_equal(fi2, gi2)
        and np.allclose(fa, ga, atol=1e-6) and np.array_equal(fareas, gareas)
    )
    all_match = all_match and match
    print(f"{chunk_key}: n_edges={len(fi1)} match={match} time={dt:.2f}s")

print(f"\nAll {len(TEST_CHUNKS)} chunks bit-identical: {all_match}")
print(f"Total time: {total_t:.1f}s")
