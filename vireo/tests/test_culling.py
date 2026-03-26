"""Tests for the culling engine (vireo/culling.py).

Tests the pure algorithmic functions: embedding clustering, pHash merging,
scene grouping, keep/reject decisions, and scene labels.
"""

from datetime import datetime

import imagehash
import numpy as np
import pytest

from culling import (
    _build_scene_groups,
    _cluster_photos,
    _group_into_scenes,
    _merge_buckets_by_phash,
    _phash_merge,
    _scene_label,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_embedding(values):
    """Create a normalized float32 embedding from a list of values."""
    arr = np.array(values, dtype=np.float32)
    norm = np.linalg.norm(arr)
    if norm > 0:
        arr = arr / norm
    return arr


def _make_phash(hex_str):
    """Create an imagehash from a hex string."""
    return imagehash.hex_to_hash(hex_str)


def _ts(hour, minute=0, second=0):
    """Shorthand for a datetime on an arbitrary fixed date."""
    return datetime(2025, 6, 15, hour, minute, second)


# ---------------------------------------------------------------------------
# _cluster_photos
# ---------------------------------------------------------------------------

class TestClusterPhotos:
    def test_single_photo(self):
        emb = {1: _make_embedding([1, 0, 0, 0])}
        result = _cluster_photos(emb, 0.88)
        assert result == [[1]]

    def test_identical_embeddings_same_cluster(self):
        e = _make_embedding([1, 0, 0, 0])
        emb = {1: e, 2: e.copy(), 3: e.copy()}
        result = _cluster_photos(emb, 0.88)
        assert len(result) == 1
        assert set(result[0]) == {1, 2, 3}

    def test_orthogonal_embeddings_separate_clusters(self):
        emb = {
            1: _make_embedding([1, 0, 0, 0]),
            2: _make_embedding([0, 1, 0, 0]),
            3: _make_embedding([0, 0, 1, 0]),
        }
        result = _cluster_photos(emb, 0.88)
        assert len(result) == 3

    def test_threshold_boundary(self):
        """Two embeddings with similarity just above/below threshold."""
        a = _make_embedding([1, 0, 0, 0])
        # b is slightly rotated — similarity ~0.95
        b = _make_embedding([1, 0.3, 0, 0])
        sim = float(np.dot(a, b))
        assert sim > 0.88

        emb = {1: a, 2: b}
        result = _cluster_photos(emb, 0.88)
        assert len(result) == 1

    def test_threshold_boundary_below(self):
        """Two embeddings below threshold stay separate."""
        a = _make_embedding([1, 0, 0, 0])
        b = _make_embedding([1, 2, 0, 0])
        sim = float(np.dot(a, b))
        assert sim < 0.88

        emb = {1: a, 2: b}
        result = _cluster_photos(emb, 0.88)
        assert len(result) == 2

    def test_single_linkage_chaining(self):
        """A-B similar, B-C similar, but A-C not — all end up in one cluster."""
        a = _make_embedding([1, 0, 0, 0])
        b = _make_embedding([1, 0.35, 0, 0])  # similar to a
        c = _make_embedding([1, 0.7, 0, 0])   # similar to b, less to a

        # Verify chain: a~b, b~c, but a!~c (at a strict threshold)
        ab = float(np.dot(a, b))
        bc = float(np.dot(b, c))
        ac = float(np.dot(a, c))
        # Use a threshold that a-b and b-c pass but a-c doesn't
        threshold = min(ab, bc) - 0.01
        assert ab >= threshold
        assert bc >= threshold

        emb = {1: a, 2: b, 3: c}
        result = _cluster_photos(emb, threshold)
        # Single-linkage: 2 joins cluster of 1, then 3 joins via 2
        assert len(result) == 1
        assert set(result[0]) == {1, 2, 3}

    def test_empty_map(self):
        result = _cluster_photos({}, 0.88)
        assert result == [[]]

    def test_two_distinct_clusters(self):
        emb = {
            1: _make_embedding([1, 0, 0, 0]),
            2: _make_embedding([1, 0.1, 0, 0]),  # near 1
            3: _make_embedding([0, 0, 1, 0]),
            4: _make_embedding([0, 0, 1, 0.1]),  # near 3
        }
        result = _cluster_photos(emb, 0.88)
        assert len(result) == 2
        cluster_sets = [set(c) for c in result]
        assert {1, 2} in cluster_sets
        assert {3, 4} in cluster_sets


# ---------------------------------------------------------------------------
# _phash_merge
# ---------------------------------------------------------------------------

class TestPhashMerge:
    def test_single_photo(self):
        result = _phash_merge([1], {}, 10)
        assert result == [[1]]

    def test_empty_list(self):
        result = _phash_merge([], {}, 10)
        assert result == [[]]

    def test_identical_hashes_merge(self):
        h = _make_phash("a" * 16)
        phashes = {1: h, 2: h, 3: h}
        result = _phash_merge([1, 2, 3], phashes, 10)
        assert len(result) == 1
        assert set(result[0]) == {1, 2, 3}

    def test_distant_hashes_separate(self):
        phashes = {
            1: _make_phash("0" * 16),
            2: _make_phash("f" * 16),
        }
        result = _phash_merge([1, 2], phashes, 5)
        assert len(result) == 2

    def test_missing_phash_gets_own_cluster(self):
        h = _make_phash("a" * 16)
        phashes = {1: h, 2: h}  # 3 has no phash
        result = _phash_merge([1, 2, 3], phashes, 10)
        assert len(result) == 2
        # 1 and 2 together, 3 alone
        cluster_sets = [set(c) for c in result]
        assert {1, 2} in cluster_sets
        assert {3} in cluster_sets

    def test_threshold_zero_requires_exact_match(self):
        phashes = {
            1: _make_phash("a" * 16),
            2: _make_phash("a" * 16),
            3: _make_phash("a" * 15 + "b"),
        }
        result = _phash_merge([1, 2, 3], phashes, 0)
        # 1 and 2 identical, 3 differs by at least 1
        assert any(set(c) == {1, 2} for c in result)


# ---------------------------------------------------------------------------
# _merge_buckets_by_phash
# ---------------------------------------------------------------------------

class TestMergeBucketsByPhash:
    def test_similar_buckets_merge(self):
        h = _make_phash("a" * 16)
        phashes = {1: h, 2: h}
        buckets = [[1], [2]]
        result = _merge_buckets_by_phash(buckets, phashes, 10)
        assert len(result) == 1
        assert set(result[0]) == {1, 2}

    def test_dissimilar_buckets_stay_separate(self):
        phashes = {
            1: _make_phash("0" * 16),
            2: _make_phash("f" * 16),
        }
        buckets = [[1], [2]]
        result = _merge_buckets_by_phash(buckets, phashes, 5)
        assert len(result) == 2

    def test_transitive_merge(self):
        """A~B and B~C should result in one merged bucket."""
        h1 = _make_phash("a" * 16)
        h2 = _make_phash("a" * 16)  # same as h1
        h3 = _make_phash("a" * 16)
        phashes = {1: h1, 2: h2, 3: h3}
        buckets = [[1], [2], [3]]
        result = _merge_buckets_by_phash(buckets, phashes, 10)
        assert len(result) == 1
        assert set(result[0]) == {1, 2, 3}

    def test_no_phashes_no_merge(self):
        buckets = [[1], [2], [3]]
        result = _merge_buckets_by_phash(buckets, {}, 10)
        assert len(result) == 3

    def test_single_bucket_unchanged(self):
        h = _make_phash("a" * 16)
        result = _merge_buckets_by_phash([[1, 2]], {1: h, 2: h}, 10)
        assert len(result) == 1
        assert set(result[0]) == {1, 2}


# ---------------------------------------------------------------------------
# _group_into_scenes
# ---------------------------------------------------------------------------

class TestGroupIntoScenes:
    def test_photos_within_time_window_same_scene(self):
        """Time proximity + similar pHash = same scene."""
        h = _make_phash("a" * 16)
        pids = [1, 2, 3]
        timestamps = {
            1: _ts(10, 0, 0),
            2: _ts(10, 0, 30),  # 30s after 1
            3: _ts(10, 0, 50),  # 20s after 2
        }
        phashes = {1: h, 2: h, 3: h}
        result = _group_into_scenes(pids, timestamps, phashes, 60, 10, False)
        assert len(result) == 1
        assert set(result[0]) == {1, 2, 3}

    def test_photos_beyond_time_window_different_scenes(self):
        pids = [1, 2]
        timestamps = {
            1: _ts(10, 0, 0),
            2: _ts(10, 5, 0),  # 5 min later
        }
        result = _group_into_scenes(pids, timestamps, {}, 60, 10, False)
        assert len(result) == 2

    def test_no_timestamps_each_photo_own_scene(self):
        pids = [1, 2, 3]
        result = _group_into_scenes(pids, {}, {}, 60, 10, False)
        assert len(result) == 3

    def test_mixed_with_and_without_timestamps(self):
        h = _make_phash("a" * 16)
        pids = [1, 2, 3]
        timestamps = {
            1: _ts(10, 0, 0),
            2: _ts(10, 0, 30),
            # 3 has no timestamp
        }
        phashes = {1: h, 2: h, 3: h}
        result = _group_into_scenes(pids, timestamps, phashes, 60, 10, False)
        # 1,2 in one scene (time+phash), 3 alone (no timestamp)
        assert len(result) == 2

    def test_cross_bucket_merge_by_phash(self):
        """Two time-separated groups with similar pHash merge when enabled."""
        pids = [1, 2]
        timestamps = {
            1: _ts(10, 0, 0),
            2: _ts(10, 5, 0),  # 5 min gap — separate time buckets
        }
        h = _make_phash("a" * 16)
        phashes = {1: h, 2: h}

        without_merge = _group_into_scenes(pids, timestamps, phashes, 60, 19, False)
        assert len(without_merge) == 2

        with_merge = _group_into_scenes(pids, timestamps, phashes, 60, 19, True)
        assert len(with_merge) == 1
        assert set(with_merge[0]) == {1, 2}

    def test_no_phash_splits_within_time_bucket(self):
        """Without pHashes, each photo becomes its own scene even if time-close."""
        pids = [1, 2, 3]
        timestamps = {
            1: _ts(10, 0, 0),
            2: _ts(10, 0, 30),
            3: _ts(10, 0, 50),
        }
        result = _group_into_scenes(pids, timestamps, {}, 60, 10, False)
        assert len(result) == 3

    def test_single_linkage_time_bucketing(self):
        """Photos chain together: A->B within window, B->C within window."""
        h = _make_phash("a" * 16)
        pids = [1, 2, 3]
        timestamps = {
            1: _ts(10, 0, 0),
            2: _ts(10, 0, 50),   # 50s from 1
            3: _ts(10, 1, 30),   # 40s from 2, 90s from 1
        }
        phashes = {1: h, 2: h, 3: h}
        result = _group_into_scenes(pids, timestamps, phashes, 60, 10, False)
        # Single-linkage: 3 is within 60s of 2, so all chain together
        assert len(result) == 1
        assert set(result[0]) == {1, 2, 3}


# ---------------------------------------------------------------------------
# _build_scene_groups
# ---------------------------------------------------------------------------

class TestBuildSceneGroups:
    def _photo_data(self, pids_qualities):
        """Build photo_data list from [(pid, quality), ...]."""
        return [
            {"photo_id": pid, "quality": q, "filename": f"IMG_{pid}.jpg", "timestamp": None, "phash": None}
            for pid, q in pids_qualities
        ]

    def test_single_photo_is_keeper(self):
        photo_data = self._photo_data([(1, 0.9)])
        scene_clusters = [[1]]
        redundancy_clusters = [[1]]

        groups, keepers, rejects = _build_scene_groups(
            scene_clusters, redundancy_clusters, photo_data, {}, {}
        )
        assert keepers == 1
        assert rejects == 0
        assert groups[0]["photos"][0]["action"] == "keep"

    def test_redundant_pair_best_quality_kept(self):
        photo_data = self._photo_data([(1, 0.5), (2, 0.9)])
        scene_clusters = [[1, 2]]
        redundancy_clusters = [[1, 2]]  # same redundancy cluster

        groups, keepers, rejects = _build_scene_groups(
            scene_clusters, redundancy_clusters, photo_data, {}, {}
        )
        assert keepers == 1
        assert rejects == 1
        photos = {p["photo_id"]: p for p in groups[0]["photos"]}
        assert photos[2]["action"] == "keep"    # higher quality
        assert photos[1]["action"] == "reject"
        assert photos[1]["redundant_with"] == 2

    def test_different_redundancy_clusters_both_kept(self):
        photo_data = self._photo_data([(1, 0.5), (2, 0.9)])
        scene_clusters = [[1, 2]]
        redundancy_clusters = [[1], [2]]  # different clusters

        groups, keepers, rejects = _build_scene_groups(
            scene_clusters, redundancy_clusters, photo_data, {}, {}
        )
        assert keepers == 2
        assert rejects == 0

    def test_multiple_scenes(self):
        photo_data = self._photo_data([(1, 0.5), (2, 0.9), (3, 0.7), (4, 0.3)])
        scene_clusters = [[1, 2], [3, 4]]
        redundancy_clusters = [[1, 2], [3, 4]]

        groups, keepers, rejects = _build_scene_groups(
            scene_clusters, redundancy_clusters, photo_data, {}, {}
        )
        assert len(groups) == 2
        assert keepers == 2
        assert rejects == 2

    def test_photo_not_in_redundancy_cluster_kept(self):
        """A photo not present in any redundancy cluster should be kept."""
        photo_data = self._photo_data([(1, 0.5), (2, 0.9)])
        scene_clusters = [[1, 2]]
        redundancy_clusters = [[1]]  # 2 not in any cluster

        groups, keepers, rejects = _build_scene_groups(
            scene_clusters, redundancy_clusters, photo_data, {}, {}
        )
        # Both should be kept — they're in different (effective) clusters
        assert keepers == 2
        assert rejects == 0

    def test_three_way_redundancy_one_keeper(self):
        photo_data = self._photo_data([(1, 0.3), (2, 0.8), (3, 0.5)])
        scene_clusters = [[1, 2, 3]]
        redundancy_clusters = [[1, 2, 3]]

        groups, keepers, rejects = _build_scene_groups(
            scene_clusters, redundancy_clusters, photo_data, {}, {}
        )
        assert keepers == 1
        assert rejects == 2
        photos = {p["photo_id"]: p for p in groups[0]["photos"]}
        assert photos[2]["action"] == "keep"


# ---------------------------------------------------------------------------
# _scene_label
# ---------------------------------------------------------------------------

class TestSceneLabel:
    def test_with_timestamps_range(self):
        timestamps = {
            1: _ts(10, 0, 0),
            2: _ts(10, 5, 30),
        }
        label = _scene_label(0, [1, 2], timestamps, {})
        assert "Scene 1" in label
        assert "10:00:00" in label
        assert "10:05:30" in label
        assert "2 photos" in label

    def test_with_single_timestamp(self):
        timestamps = {1: _ts(14, 30, 0)}
        label = _scene_label(2, [1], timestamps, {})
        assert "Scene 3" in label
        assert "14:30:00" in label
        assert "1 photos" in label

    def test_same_start_end_time(self):
        t = _ts(12, 0, 0)
        timestamps = {1: t, 2: t}
        label = _scene_label(0, [1, 2], timestamps, {})
        # Should show single time, not a range with " to "
        assert " to " not in label
        assert "12:00:00" in label

    def test_no_timestamps(self):
        label = _scene_label(4, [1, 2, 3], {}, {})
        assert "Scene 5" in label
        assert "3 photos" in label
        assert ":" not in label  # no time info

    def test_scene_id_is_one_indexed(self):
        label = _scene_label(0, [1], {}, {})
        assert "Scene 1" in label
