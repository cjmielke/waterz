#pragma once

#include "types.hpp"

#include <cstddef>
#include <iostream>
#include <map>

/**
 * Extract the region graph from a segmentation. Edges are annotated with the 
 * maximum affinity between the regions.
 *
 * @param aff [in]
 *              The affinity graph to read the affinities from.
 * @param seg [in]
 *              The segmentation.
 * @param max_segid [in]
 *              The highest ID in the segmentation.
 * @param statisticsProvider [in]
 *              A statistics provider to update on-the-fly.
 * @param region_graph [out]
 *              A reference to a region graph to store the result.
 */
template<typename AG, typename V, typename StatisticsProviderType>
inline
void
get_region_graph(
		const AG& aff,
		const V& seg,
		std::size_t max_segid,
		StatisticsProviderType& statisticsProvider,
		RegionGraph<typename V::element>& rg) {

	typedef typename AG::element F;
	typedef typename V::element ID;
	typedef RegionGraph<ID> RegionGraphType;
	typedef typename RegionGraphType::EdgeIdType EdgeIdType;

	std::ptrdiff_t zdim = aff.shape()[1];
	std::ptrdiff_t ydim = aff.shape()[2];
	std::ptrdiff_t xdim = aff.shape()[3];

	// Stream samples directly into the statistics provider as each is
	// discovered (no per-edge buffering -- Histogram::inc / running-sum
	// style providers are already fully online, so there was never a need
	// to collect the full sample set upfront; this removes O(samples)
	// small heap allocations spread across hundreds of thousands of
	// independent per-edge vectors).
	//
	// Cache the last-resolved (id1,id2) -> edge pair across loop
	// iterations: consecutive voxels along the fast-varying (innermost, x)
	// axis very often sit on the same straight fragment boundary, turning
	// a large fraction of lookups into one branch comparison instead of
	// even the cheap findEdge scan.
	ID lastU = 0, lastV = 0;
	EdgeIdType lastE = RegionGraphType::NoEdge;

	std::size_t p[3];
	for (p[0] = 0; p[0] < zdim; ++p[0])
		for (p[1] = 0; p[1] < ydim; ++p[1])
			for (p[2] = 0; p[2] < xdim; ++p[2]) {

				ID id1 = seg[p[0]][p[1]][p[2]];
				statisticsProvider.addVoxel(id1, p[2], p[1], p[0]);

				for (int d = 0; d < 3; d++) {

					if (p[d] == 0)
						continue;

					ID id2 = seg[p[0]-(d==0)][p[1]-(d==1)][p[2]-(d==2)];

					if (id1 != id2 && id1 != 0 && id2 != 0) {

						EdgeIdType e;
						if (id1 == lastU && id2 == lastV) {
							e = lastE;
						} else {
							e = rg.findEdge(id1, id2);
							if (e == RegionGraphType::NoEdge) {
								e = rg.addEdge(id1, id2);
								statisticsProvider.notifyNewEdge(e);
							}
							lastU = id1;
							lastV = id2;
							lastE = e;
						}
						statisticsProvider.addAffinity(e, aff[d][p[0]][p[1]][p[2]]);
					}
				}
			}

	std::cout << "Region graph number of edges: " << rg.edges().size() << std::endl;
}

/**
 * Like get_region_graph but also outputs the contact count (number of
 * affinity samples) per edge.  edge_counts is resized to rg.numEdges()
 * after construction; element i is the contact area for edge i.
 */
template<typename AG, typename V, typename StatisticsProviderType>
inline
void
get_region_graph(
		const AG& aff,
		const V& seg,
		std::size_t max_segid,
		StatisticsProviderType& statisticsProvider,
		RegionGraph<typename V::element>& rg,
		std::vector<uint64_t>& edge_counts) {

	typedef typename AG::element F;
	typedef typename V::element ID;
	typedef RegionGraph<ID> RegionGraphType;
	typedef typename RegionGraphType::EdgeIdType EdgeIdType;

	std::ptrdiff_t zdim = aff.shape()[1];
	std::ptrdiff_t ydim = aff.shape()[2];
	std::ptrdiff_t xdim = aff.shape()[3];

	// See the non-rich overload above for rationale (streaming +
	// last-resolved-edge cache). edge_counts grows in lockstep with edge
	// creation (both strictly sequential, 0,1,2,... by construction).
	edge_counts.clear();

	ID lastU = 0, lastV = 0;
	EdgeIdType lastE = RegionGraphType::NoEdge;

	std::size_t p[3];
	for (p[0] = 0; p[0] < zdim; ++p[0])
		for (p[1] = 0; p[1] < ydim; ++p[1])
			for (p[2] = 0; p[2] < xdim; ++p[2]) {

				ID id1 = seg[p[0]][p[1]][p[2]];
				statisticsProvider.addVoxel(id1, p[2], p[1], p[0]);

				for (int d = 0; d < 3; d++) {

					if (p[d] == 0)
						continue;

					ID id2 = seg[p[0]-(d==0)][p[1]-(d==1)][p[2]-(d==2)];

					if (id1 != id2 && id1 != 0 && id2 != 0) {

						EdgeIdType e;
						if (id1 == lastU && id2 == lastV) {
							e = lastE;
						} else {
							e = rg.findEdge(id1, id2);
							if (e == RegionGraphType::NoEdge) {
								e = rg.addEdge(id1, id2);
								statisticsProvider.notifyNewEdge(e);
								edge_counts.push_back(0);
							}
							lastU = id1;
							lastV = id2;
							lastE = e;
						}
						statisticsProvider.addAffinity(e, aff[d][p[0]][p[1]][p[2]]);
						edge_counts[e]++;
					}
				}
			}

	std::cout << "Region graph number of edges: " << rg.edges().size() << std::endl;
}
