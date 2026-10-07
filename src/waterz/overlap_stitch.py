"""Fragment stitching in overlap zones between adjacent chunks.

Provides majority-vote matching to unify fragment IDs across chunk
boundaries when using overlapping chunks for large-volume segmentation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from numpy.typing import NDArray

__all__ = [
    "apply_overlap_remap",
    "build_overlap_remap",
]


def build_overlap_remap(
    overlap_src: NDArray[np.uint64],
    overlap_dst: NDArray[np.uint64],
) -> dict[int, int]:
    """Map dst fragment IDs to src fragment IDs via majority-vote in overlap.

    For each nonzero dst fragment that overlaps a nonzero src fragment,
    the dst ID is mapped to the src ID that it overlaps most (by voxel
    count).  Dst fragments that don't overlap any src fragment, or that
    only overlap background (0), are not included in the mapping.

    Parameters
    ----------
    overlap_src : ndarray, uint64
        Segmentation from the source (lower-index) chunk in the overlap zone.
    overlap_dst : ndarray, uint64
        Segmentation from the destination (higher-index) chunk in the overlap zone.

    Returns
    -------
    dict[int, int]
        Mapping from dst fragment ID to src fragment ID.
    """
    overlap_src = np.asarray(overlap_src, dtype=np.uint64).ravel()
    overlap_dst = np.asarray(overlap_dst, dtype=np.uint64).ravel()

    # Only consider voxels where both src and dst are nonzero
    mask = (overlap_src > 0) & (overlap_dst > 0)
    src_vals = overlap_src[mask]
    dst_vals = overlap_dst[mask]

    if len(src_vals) == 0:
        return {}

    # Count occurrences of each (dst, src) pair. If both fit in 32 bits
    # (checked against the real data, not assumed -- large_decode's
    # chunked ids can exceed that at full volume scale, though not at
    # this dataset's size), pack them into a single scalar uint64 key
    # instead of a structured dtype. np.unique on a structured dtype
    # falls back to slow generic element-wise comparison; a scalar key
    # gets numpy's fast sort path. Measured ~48x faster on real overlap
    # data (814ms -> 17ms) and this unique() call was the actual dominant
    # cost of the whole stitch task (~96% of per-task time, see
    # optimization-plan.md) -- not the HDF5 reads it sits next to, which
    # is why optimizing those alone barely moved live throughput.
    max_val = int(max(src_vals.max(), dst_vals.max()))
    if max_val <= 0xFFFFFFFF:
        key = (dst_vals << np.uint64(32)) | src_vals
        unique_keys, pair_counts = np.unique(key, return_counts=True)
        dst_arr = (unique_keys >> np.uint64(32)).astype(np.uint64)
        src_arr = (unique_keys & np.uint64(0xFFFFFFFF)).astype(np.uint64)
    else:
        pairs = np.empty(len(dst_vals), dtype=[("dst", np.uint64), ("src", np.uint64)])
        pairs["dst"] = dst_vals
        pairs["src"] = src_vals
        unique_pairs, pair_counts = np.unique(pairs, return_counts=True)
        dst_arr = unique_pairs["dst"]
        src_arr = unique_pairs["src"]

    # For each dst ID, find the src ID with the highest count. dst_arr is
    # already sorted ascending, with src ascending as the tiebreak within
    # each dst group (true for both the packed-key and structured-dtype
    # paths above). Re-sort by (dst ascending, count descending) -- a
    # stable sort preserves each dst-group's original src-ascending order
    # among count ties, so the first row of each dst group is the
    # max-count winner, with ties broken toward the smaller src id --
    # replaces an equivalent O(U) Python loop with per-iteration dict
    # lookups (U = number of unique pairs) that was here before.
    order = np.lexsort((-pair_counts.astype(np.int64), dst_arr))
    dst_sorted = dst_arr[order]
    src_sorted = src_arr[order]
    _, first_idx = np.unique(dst_sorted, return_index=True)

    remap = dict(zip(dst_sorted[first_idx].tolist(), src_sorted[first_idx].tolist()))
    return remap


def apply_overlap_remap(
    seg: NDArray[np.uint64],
    remap: dict[int, int],
) -> NDArray[np.uint64]:
    """Apply fragment ID remapping to a segmentation volume.

    Remaps segment IDs in *seg* according to *remap*.  IDs not in
    *remap* are left unchanged.  Operates in-place when possible
    via vectorized indexing.

    Parameters
    ----------
    seg : ndarray, uint64
        Segmentation volume (modified in-place).
    remap : dict[int, int]
        Mapping from old ID to new ID.

    Returns
    -------
    ndarray, uint64
        The same array, modified in-place.
    """
    if not remap:
        return seg

    seg = np.asarray(seg, dtype=np.uint64)

    # Build a lookup table for fast vectorized remapping
    max_id = int(seg.max())
    remap_max = max(max(remap.keys()), max(remap.values())) if remap else 0
    lut_size = max(max_id, remap_max) + 1

    lut = np.arange(lut_size, dtype=np.uint64)
    for old_id, new_id in remap.items():
        if old_id < lut_size:
            lut[old_id] = new_id

    # Apply via indexing
    flat = seg.ravel()
    flat[:] = lut[flat]
    return seg
