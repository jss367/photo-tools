# vireo/tests/test_pipeline_locks.py
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))

from pipeline_locks import (
    _GPU_SEMAPHORE,
    acquire_gpu,
    acquire_gpu_if_session_uses_it,
    acquire_photo_mask,
    acquire_workspace_regroup,
    release_archive_destination,
    try_reserve_archive_destination,
)


class _FakeSession:
    def __init__(self, providers):
        self._providers = list(providers)

    def get_providers(self):
        return list(self._providers)


def test_session_cache_lock_wait_honors_bound_cancel_probe():
    import onnx_runtime
    import resource_ledger

    lock = threading.Lock()
    lock.acquire()
    ledger = resource_ledger.ResourceLedger(cpu_capacity=2)
    previous = resource_ledger._set_resource_ledger_for_tests(ledger)
    cancelled = threading.Event()
    finished = threading.Event()
    outcome = []

    def waiter():
        with (
            resource_ledger.bind_resource_owner("model-job"),
            resource_ledger.bind_resource_cancel_check(cancelled.is_set),
        ):
            try:
                with onnx_runtime.acquire_session_cache_lock(lock):
                    pass
            except resource_ledger.ResourceWaitCancelled:
                outcome.append("cancelled")
            finally:
                finished.set()

    thread = threading.Thread(target=waiter)
    thread.start()
    try:
        deadline = time.monotonic() + 1.0
        while ledger.snapshot()["waiters"] == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ledger.snapshot()["waiters"] == 1
        cancelled.set()
        assert finished.wait(timeout=1.0)
        thread.join(timeout=1.0)
        assert not thread.is_alive()
        assert outcome == ["cancelled"]
        assert ledger.owner_timing("model-job")["wait_count"] == 1
    finally:
        lock.release()
        thread.join(timeout=1.0)
        resource_ledger._set_resource_ledger_for_tests(previous)


def test_session_cache_lock_acquire_race_with_cancel_releases_and_raises():
    """Regression: when cancellation becomes true during the 0.2s
    ``lock.acquire(timeout=0.2)`` window inside
    ``acquire_session_cache_lock`` and the previous holder releases in
    the same window, the acquire succeeds and the caller used to
    proceed with the lock held despite the pending cancel. For an
    already-cancelled eye-keypoint participant waiting on
    ``_download_locks``, that meant a fresh multi-hundred-megabyte
    download would start if the previous holder exited with only one
    of the two required files on disk. Recheck the probe after
    acquire; release the lock and raise if cancel won the race.
    Matches the same fix already applied to the GPU semaphore's
    ``_GpuLockContext``.
    """
    import onnx_runtime
    import resource_ledger

    lock = threading.Lock()
    lock.acquire()
    holder_released = False
    ledger = resource_ledger.ResourceLedger(cpu_capacity=2)
    previous = resource_ledger._set_resource_ledger_for_tests(ledger)
    outcome = []
    call_count = {"n": 0}
    cancel = threading.Event()

    def counting_cancel_check():
        call_count["n"] += 1
        # Call #1 is the entry probe (line 34). Call #2 is inside the
        # while loop (line 48). From call #2 onward, honour the flag —
        # so the test can flip cancel after the flag-observing probe
        # ran but before the timed acquire returns, guaranteeing the
        # only remaining probe to fire is the post-acquire recheck.
        if call_count["n"] >= 2:
            return cancel.is_set()
        return False

    def waiter():
        with resource_ledger.bind_resource_cancel_check(
            counting_cancel_check,
        ):
            try:
                with onnx_runtime.acquire_session_cache_lock(lock):
                    outcome.append("acquired")
            except resource_ledger.ResourceWaitCancelled:
                outcome.append("cancelled")

    thread = threading.Thread(target=waiter)
    thread.start()
    try:
        _wait_until(lambda: call_count["n"] >= 2, timeout=2.0)
        cancel.set()
        lock.release()
        holder_released = True

        thread.join(timeout=2.0)
        assert not thread.is_alive(), (
            "waiter did not finish — check the timed-acquire loop"
        )
        assert outcome == ["cancelled"], (
            f"Expected cancelled outcome (post-acquire cancel recheck), "
            f"got {outcome!r}"
        )
        # The lock must be free — the recheck released it before raising.
        assert lock.acquire(blocking=False), (
            "Lock was not released on the cancel-race path — leaked"
        )
        lock.release()
    finally:
        if not holder_released:
            lock.release()
        thread.join(timeout=1.0)
        resource_ledger._set_resource_ledger_for_tests(previous)


def test_acquire_gpu_if_session_uses_it_takes_lock_for_cuda_session():
    sess = _FakeSession(["CUDAExecutionProvider", "CPUExecutionProvider"])
    before = _GPU_SEMAPHORE._value
    with acquire_gpu_if_session_uses_it(sess):
        held = _GPU_SEMAPHORE._value
    after = _GPU_SEMAPHORE._value
    assert before == 1
    assert held == 0, "semaphore should be acquired for GPU sessions"
    assert after == 1


def test_acquire_gpu_if_session_uses_it_takes_lock_for_coreml_session():
    sess = _FakeSession(["CoreMLExecutionProvider", "CPUExecutionProvider"])
    with acquire_gpu_if_session_uses_it(sess):
        assert _GPU_SEMAPHORE._value == 0


def test_acquire_gpu_if_session_uses_it_skips_lock_for_cpu_only_session():
    import resource_ledger

    sess = _FakeSession(["CPUExecutionProvider"])
    ledger = resource_ledger.ResourceLedger(cpu_capacity=2)
    previous = resource_ledger._set_resource_ledger_for_tests(ledger)
    before = _GPU_SEMAPHORE._value
    try:
        with acquire_gpu_if_session_uses_it(sess):
            held = _GPU_SEMAPHORE._value
            snapshot = ledger.snapshot()
            assert snapshot["cpu"]["allocated"] == 2
            assert snapshot["lanes"]["cpu_ml"]["allocated"] == 1
        after = _GPU_SEMAPHORE._value
    finally:
        resource_ledger._set_resource_ledger_for_tests(previous)
    assert before == 1
    assert held == 1, "CPU-only session must not take the GPU semaphore"
    assert after == 1
    assert ledger.snapshot()["cpu"]["allocated"] == 0


def test_acquire_inference_resources_wakes_on_bound_cancel_check():
    """A queued CPU-inference claim wakes when the bound job probe fires.

    Without this, a cancelled classify/detect/mask/embed worker would
    block on ``cpu_ml`` until the current holder released — and if the
    native inference stalls, the cancelled worker could outlive
    ``JobRunner.shutdown()``.
    """
    import resource_ledger
    from pipeline_locks import acquire_inference_resources
    from resource_ledger import (
        CpuRequest,
        ResourceLedger,
        ResourceRequest,
        ResourceWaitCancelled,
        bind_resource_cancel_check,
    )

    sess = _FakeSession(["CPUExecutionProvider"])
    ledger = ResourceLedger(cpu_capacity=1)
    previous = resource_ledger._set_resource_ledger_for_tests(ledger)
    holder = ledger.acquire(ResourceRequest(
        cpu=CpuRequest(1, 1, 1), lanes=("cpu_ml",),
    ))
    cancelled_flag = threading.Event()
    finished = threading.Event()
    outcome = []
    try:
        def waiter():
            with bind_resource_cancel_check(cancelled_flag.is_set):
                try:
                    with acquire_inference_resources(sess):
                        pass
                except ResourceWaitCancelled:
                    outcome.append("cancelled")
                finally:
                    finished.set()

        thread = threading.Thread(target=waiter)
        thread.start()
        # Give the waiter time to enter the ledger wait loop.
        time.sleep(0.05)
        cancelled_flag.set()
        # The ledger uses a bounded 200ms poll so the waiter observes the
        # flag on the next tick; no need to release the holder.
        assert finished.wait(timeout=1.0), "waiter never observed cancel"
        thread.join(timeout=1.0)
        assert not thread.is_alive()
        assert outcome == ["cancelled"]
    finally:
        holder.release()
        resource_ledger._set_resource_ledger_for_tests(previous)


def test_gpu_inference_wait_wakes_on_bound_cancel_check():
    """Queued accelerator work observes the same bound job probe as CPU."""
    from pipeline_locks import acquire_inference_resources
    from resource_ledger import (
        ResourceWaitCancelled,
        bind_resource_cancel_check,
    )

    sess = _FakeSession(["CUDAExecutionProvider", "CPUExecutionProvider"])
    holder = acquire_gpu()
    holder.__enter__()
    cancelled_flag = threading.Event()
    finished = threading.Event()
    outcome = []
    try:
        def waiter():
            with bind_resource_cancel_check(cancelled_flag.is_set):
                try:
                    with acquire_inference_resources(sess):
                        pass
                except ResourceWaitCancelled:
                    outcome.append("cancelled")
                finally:
                    finished.set()

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.05)
        cancelled_flag.set()
        assert finished.wait(timeout=1.0), "GPU waiter never observed cancel"
        thread.join(timeout=1.0)
        assert not thread.is_alive()
        assert outcome == ["cancelled"]
    finally:
        holder.__exit__(None, None, None)


def test_gpu_inference_contention_records_live_and_completed_owner_timing():
    """Accelerator semaphore waits feed the shared job diagnostics."""
    import resource_ledger
    from pipeline_locks import acquire_inference_resources
    from resource_ledger import ResourceLedger, bind_resource_owner

    sess = _FakeSession(["CUDAExecutionProvider", "CPUExecutionProvider"])
    ledger = ResourceLedger(cpu_capacity=2)
    previous = resource_ledger._set_resource_ledger_for_tests(ledger)
    holder = acquire_gpu()
    holder.__enter__()
    holder_released = False
    waiting = threading.Event()
    finished = threading.Event()

    def waiter():
        with bind_resource_owner("gpu-job"):
            waiting.set()
            with acquire_inference_resources(sess):
                pass
        finished.set()

    thread = threading.Thread(target=waiter)
    thread.start()
    try:
        assert waiting.wait(timeout=1.0)
        _wait_until(
            lambda: ledger.owner_timing("gpu-job")["wait_count"] == 1,
        )
        active = ledger.owner_timing("gpu-job")
        assert active["wait_seconds"] >= 0
        assert ledger.snapshot()["waiters"] == 1

        holder.__exit__(None, None, None)
        holder_released = True
        assert finished.wait(timeout=1.0)
        thread.join(timeout=1.0)
        assert not thread.is_alive()
        completed = ledger.owner_timing("gpu-job")
        assert completed["wait_count"] == 1
        assert completed["wait_seconds"] >= active["wait_seconds"]
        assert ledger.snapshot()["waiters"] == 0
    finally:
        if not holder_released:
            holder.__exit__(None, None, None)
        thread.join(timeout=1.0)
        resource_ledger._set_resource_ledger_for_tests(previous)


def test_gpu_acquire_race_with_cancel_releases_semaphore_and_raises():
    """Regression: when cancellation becomes true during the
    ``_GPU_SEMAPHORE.acquire(timeout=0.2)`` window and a release wins
    the same window, the successful acquire path used to return without
    rechecking cancellation. The caller then held the semaphore and
    proceeded into ``session.run`` despite the pending cancel — for
    interactive text search past its 5-second deadline, that means the
    UI stalls on inference the user has already cancelled and the
    semaphore stays held by a request whose result will be discarded.
    Recheck after acquire; release and raise if cancel won the race.
    """
    from pipeline_locks import _GpuLockContext
    from resource_ledger import ResourceWaitCancelled

    # Occupy the semaphore so the pre-acquire non-blocking probe at
    # ``_GpuLockContext.__enter__`` fails and the waiter enters the
    # timed-polling loop.
    _GPU_SEMAPHORE.acquire()
    holder_released = False
    outcome = []
    call_count = {"n": 0}
    cancel = threading.Event()

    def counting_cancel_check():
        call_count["n"] += 1
        # Call #1 is the entry probe (line 61). Call #2 is inside the
        # while loop (line 73). From call #2 onward, honour the flag —
        # so the test can flip cancel AFTER the flag-observing probe
        # ran but BEFORE the timed acquire returns, guaranteeing the
        # only remaining probe to fire is the post-acquire recheck.
        if call_count["n"] >= 2:
            return cancel.is_set()
        return False

    def waiter():
        try:
            with _GpuLockContext(cancel_check=counting_cancel_check):
                outcome.append("acquired")
        except ResourceWaitCancelled:
            outcome.append("cancelled")

    thread = threading.Thread(target=waiter)
    thread.start()
    try:
        # Wait until the waiter has called cancel_check at least twice —
        # meaning it is now inside ``_GPU_SEMAPHORE.acquire(timeout=0.2)``
        # (or about to enter it, with the flag observed as False on the
        # loop probe).
        _wait_until(lambda: call_count["n"] >= 2, timeout=2.0)

        # Fire the race: cancel becomes true THEN the semaphore is
        # released. The waiter's timed acquire will succeed shortly.
        # Without the post-acquire recheck the waiter would return with
        # the semaphore held; with the fix, the recheck sees cancel and
        # unwinds.
        cancel.set()
        _GPU_SEMAPHORE.release()
        holder_released = True

        thread.join(timeout=2.0)
        assert not thread.is_alive(), (
            "waiter did not finish — check the timed-acquire loop"
        )
        assert outcome == ["cancelled"], (
            f"Expected cancelled outcome (post-acquire cancel recheck), "
            f"got {outcome!r}"
        )
        # Semaphore must be back to full value 1 — the released holder
        # slot returned to it, the waiter's briefly-acquired slot was
        # released before the raise.
        assert _GPU_SEMAPHORE._value == 1, (
            f"Semaphore leaked; value={_GPU_SEMAPHORE._value}"
        )
    finally:
        # If the fix regressed and the waiter returned "acquired", it
        # would hold the semaphore on the way out of its with-block —
        # already released by ``__exit__``. But if the test aborted
        # before joining, the waiter thread may still be blocked in
        # ``acquire``: release once more so the module-level semaphore
        # doesn't leak across tests.
        if not holder_released:
            _GPU_SEMAPHORE.release()
        thread.join(timeout=1.0)


def test_acquire_gpu_if_session_uses_it_defaults_to_lock_when_providers_missing():
    """A session that doesn't expose get_providers (or raises) must
    conservatively take the lock — same behavior as before this check existed.
    """
    class _NoProviders:
        pass

    with acquire_gpu_if_session_uses_it(_NoProviders()):
        assert _GPU_SEMAPHORE._value == 0


def _wait_until(predicate, timeout=1.0, interval=0.005):
    """Poll ``predicate`` until true or timeout; assert on timeout.

    Replaces unbounded ``while not <cond>: time.sleep(...)`` loops that
    would otherwise hang the suite if a thread stalls.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


def test_gpu_lock_serialises_two_threads():
    """Only one thread holds the GPU lock at a time."""
    held = []
    release_first = threading.Event()
    second_started = threading.Event()

    def first():
        with acquire_gpu():
            held.append("first-in")
            second_started.wait(timeout=2.0)
            time.sleep(0.05)  # ensure second is blocked, not racing
            held.append("first-out")
        # released
        time.sleep(0.05)

    def second():
        second_started.set()
        with acquire_gpu():
            held.append("second-in")

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    _wait_until(lambda: "first-in" in held)
    t2.start()
    t1.join(timeout=3.0)
    t2.join(timeout=3.0)
    assert not t1.is_alive(), "first thread did not finish"
    assert not t2.is_alive(), "second thread did not finish"

    assert held == ["first-in", "first-out", "second-in"], (
        f"second must wait for first to release; got {held}"
    )


def test_gpu_lock_released_after_with_block():
    """Sequential `with acquire_gpu()` calls don't deadlock — release works."""
    for _ in range(3):
        with acquire_gpu():
            pass
    # If release was broken, the second iteration would deadlock and the
    # test timeout would fire. Reaching here is the assertion.


def test_workspace_regroup_lock_serialises_same_workspace():
    """Two threads regrouping the same workspace take turns."""
    held = []
    second_started = threading.Event()

    def first():
        with acquire_workspace_regroup(42):
            held.append("first-in")
            second_started.wait(timeout=2.0)
            time.sleep(0.05)
            held.append("first-out")

    def second():
        second_started.set()
        with acquire_workspace_regroup(42):
            held.append("second-in")

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    _wait_until(lambda: "first-in" in held)
    t2.start()
    t1.join(timeout=3.0)
    t2.join(timeout=3.0)
    assert not t1.is_alive(), "first thread did not finish"
    assert not t2.is_alive(), "second thread did not finish"

    assert held == ["first-in", "first-out", "second-in"], (
        f"second on same workspace must wait; got {held}"
    )


def test_workspace_regroup_lock_does_not_block_other_workspaces():
    """Different workspace IDs use independent locks; second runs immediately."""
    held = []
    first_holding = threading.Event()
    let_first_go = threading.Event()

    def first():
        with acquire_workspace_regroup(1):
            held.append("first-in")
            first_holding.set()
            let_first_go.wait(timeout=2.0)
            held.append("first-out")

    def second():
        with acquire_workspace_regroup(2):
            held.append("second-in")

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    assert first_holding.wait(timeout=1.0)
    t2.start()
    t2.join(timeout=1.0)
    assert not t2.is_alive(), "second thread should not be blocked by a different workspace"
    assert "second-in" in held, "different workspace must not be blocked"
    let_first_go.set()
    t1.join(timeout=2.0)
    assert not t1.is_alive(), "first thread did not finish"


def test_workspace_regroup_lock_reentrant_keys_share_one_lock():
    """The lock object for a given workspace_id is stable across calls."""
    from pipeline_locks import _workspace_regroup_lock_for_tests
    lock1 = _workspace_regroup_lock_for_tests(7)
    lock2 = _workspace_regroup_lock_for_tests(7)
    assert lock1 is lock2


def test_photo_mask_lock_serialises_same_photo():
    """Two threads writing the same photo's mask take turns."""
    held = []

    def first():
        with acquire_photo_mask(42):
            held.append("first-in")
            time.sleep(0.05)
            held.append("first-out")

    def second():
        with acquire_photo_mask(42):
            held.append("second-in")

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    _wait_until(lambda: "first-in" in held)
    t2.start()
    t1.join(timeout=3.0)
    t2.join(timeout=3.0)
    assert not t1.is_alive() and not t2.is_alive()
    assert held == ["first-in", "first-out", "second-in"], held


def test_photo_mask_lock_does_not_block_different_photo():
    """Different photo IDs don't contend — common case."""
    held = []
    first_holding = threading.Event()
    let_first_go = threading.Event()

    def first():
        with acquire_photo_mask(1):
            held.append("first-in")
            first_holding.set()
            let_first_go.wait(timeout=2.0)

    def second():
        with acquire_photo_mask(2):
            held.append("second-in")

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    assert first_holding.wait(timeout=1.0)
    t2.start()
    t2.join(timeout=1.0)
    assert not t2.is_alive(), "different photo must not be blocked"
    assert "second-in" in held
    let_first_go.set()
    t1.join(timeout=2.0)


def test_photo_mask_lock_serialises_same_photo_across_variants():
    """Same photo, DIFFERENT SAM variants still take turns.

    Cross-variant serialisation is load-bearing: ``set_active_mask_variant``
    and ``update_photo_embeddings`` denormalise into the same ``photos``
    row regardless of variant, so interleaved writes between two
    pipelines on the same photo with different variants would corrupt
    the row (e.g. active_mask_variant=large but dino embedding cropped
    from small's mask). The lock keys on photo_id only — variant is
    intentionally NOT part of the key.

    The pipeline_job call site passes only photo_id; this test exercises
    the lock primitive directly to lock in the cross-variant guarantee.
    """
    held = []
    first_holding = threading.Event()
    let_first_go = threading.Event()

    def first():
        # Caller treats lock as photo-scoped — variant is irrelevant.
        with acquire_photo_mask(42):
            held.append("first-in")
            first_holding.set()
            let_first_go.wait(timeout=2.0)
            held.append("first-out")

    def second():
        with acquire_photo_mask(42):
            held.append("second-in")

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    assert first_holding.wait(timeout=1.0)
    t2.start()
    # Second must block until first releases — even though semantically
    # the two pipelines might be working different variants.
    assert not held.count("second-in"), (
        "second thread ran while first held the photo lock"
    )
    let_first_go.set()
    t1.join(timeout=2.0)
    t2.join(timeout=2.0)
    assert held == ["first-in", "first-out", "second-in"], held


def test_photo_mask_lock_reentrant_keys_share_one_lock():
    """The lock object for a given photo_id is stable across calls."""
    from pipeline_locks import _photo_mask_lock_for_tests
    lock1 = _photo_mask_lock_for_tests(7)
    lock2 = _photo_mask_lock_for_tests(7)
    assert lock1 is lock2


def test_archive_destination_reserve_rejects_exact_match():
    """Two pipelines targeting the same destination cannot both reserve."""
    try:
        assert try_reserve_archive_destination("/Photos/Shoot") is True
        assert try_reserve_archive_destination("/Photos/Shoot") is False
    finally:
        release_archive_destination("/Photos/Shoot")


def test_archive_destination_reserve_rejects_descendant():
    """A child of an already-reserved root is rejected before processing starts.

    Without this the parent run can create a tracked folder row that the
    child later tries to archive into, leaving overlapping folder roots in
    the catalog after the second job moves staging into the already-claimed
    tree.
    """
    try:
        assert try_reserve_archive_destination("/Photos/Shoot") is True
        assert try_reserve_archive_destination("/Photos/Shoot/Subset") is False
    finally:
        release_archive_destination("/Photos/Shoot")


def test_archive_destination_reserve_rejects_ancestor():
    """A parent of an already-reserved leaf is rejected too.

    Symmetry matters because either run can land first; once the leaf is
    in flight, a second pipeline aiming at the enclosing root would later
    have to reparent the in-flight subtree, which ``move_folder_path``
    doesn't do.
    """
    try:
        assert try_reserve_archive_destination("/Photos/Shoot/Subset") is True
        assert try_reserve_archive_destination("/Photos/Shoot") is False
    finally:
        release_archive_destination("/Photos/Shoot/Subset")


def test_archive_destination_reserve_allows_sibling_prefix_lookalike():
    """``/Photos/Shoot`` and ``/Photos/ShootSubset`` are siblings, not nested.

    A naive ``startswith`` overlap check would false-positive here and
    block an unrelated archive. ``commonpath`` correctly returns
    ``/Photos`` for both, which equals neither leaf.
    """
    try:
        assert try_reserve_archive_destination("/Photos/Shoot") is True
        assert try_reserve_archive_destination("/Photos/ShootSubset") is True
    finally:
        release_archive_destination("/Photos/Shoot")
        release_archive_destination("/Photos/ShootSubset")


def test_archive_destination_release_allows_reacquire():
    """After release, the same destination (or a nested one) can be claimed."""
    assert try_reserve_archive_destination("/Photos/Shoot") is True
    release_archive_destination("/Photos/Shoot")
    try:
        # Same path is reusable.
        assert try_reserve_archive_destination("/Photos/Shoot") is True
        release_archive_destination("/Photos/Shoot")
        # And a previously-blocked nested path is also reusable.
        assert try_reserve_archive_destination("/Photos/Shoot/Subset") is True
    finally:
        release_archive_destination("/Photos/Shoot/Subset")


def test_archive_destination_reserve_normalises_relative_paths():
    """Relative and absolute spellings of the same target collide."""
    abs_path = os.path.abspath("Photos/Shoot")
    try:
        assert try_reserve_archive_destination("Photos/Shoot") is True
        assert try_reserve_archive_destination(abs_path) is False
    finally:
        release_archive_destination("Photos/Shoot")


def test_archive_destination_reserve_rejects_symlinked_alias(tmp_path):
    """A symlink and its real path resolve to the same archive root.

    Without realpath in the normalisation, two jobs choosing the same
    physical destination via different spellings (a real path and a
    symlink to it) could both pass the reservation check; with two
    pipeline slots they would then race into the same archive root before
    ``move_folder``'s catalog-side guards run.
    """
    real_target = tmp_path / "real_dest"
    real_target.mkdir()
    symlink = tmp_path / "link_dest"
    try:
        symlink.symlink_to(real_target)
    except OSError as exc:
        pytest.skip(f"symlinks not supported on this filesystem: {exc}")

    real_child = str(real_target / "Shoot")
    aliased_child = str(symlink / "Shoot")

    try:
        assert try_reserve_archive_destination(real_child) is True
        # Aliased path resolves through realpath to the same physical
        # directory — the second reservation must collide.
        assert try_reserve_archive_destination(aliased_child) is False
    finally:
        release_archive_destination(real_child)
