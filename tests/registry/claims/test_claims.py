"""Tests for mbtools.registry.claims -- the cross-instance, same-host
board claim (sprint 007, ticket 002).

Every real-filesystem test here uses ``tmp_path`` as its own claims
directory, never the real, host-shared
``mbtools.registry.paths.claims_dir_path()`` location -- see that
function's own docstring for why a test should never touch it directly
(cross-test pollution within one pytest process). No test touches a real
board or probe, matching sprint.md's Test Strategy for this module.
"""

from __future__ import annotations

import multiprocessing as mp
import stat
import threading
import time
from pathlib import Path

import pytest

from mbtools.registry import claims


def _uid(tag: str) -> str:
    return f"uid-{tag}"


# ---------------------------------------------------------------------------
# try_claim / release -- real flock semantics, no real board
# ---------------------------------------------------------------------------


def test_try_claim_succeeds_and_creates_lock_file(tmp_path):
    claims_dir = tmp_path / "claims"
    handle = claims.try_claim(_uid("a"), claims_dir=claims_dir)

    assert handle is not None
    assert (claims_dir / f"{_uid('a')}.lock").exists()
    handle.release()


def test_second_claim_attempt_on_same_uid_fails_while_first_holds_it(tmp_path):
    claims_dir = tmp_path / "claims"
    uid = _uid("b")

    first = claims.try_claim(uid, claims_dir=claims_dir)
    assert first is not None

    second = claims.try_claim(uid, claims_dir=claims_dir)
    assert second is None

    first.release()


def test_claim_on_different_uids_both_succeed(tmp_path):
    claims_dir = tmp_path / "claims"

    a = claims.try_claim(_uid("c1"), claims_dir=claims_dir)
    b = claims.try_claim(_uid("c2"), claims_dir=claims_dir)

    assert a is not None
    assert b is not None
    a.release()
    b.release()


def test_release_allows_a_later_claim_attempt_to_succeed(tmp_path):
    claims_dir = tmp_path / "claims"
    uid = _uid("d")

    handle = claims.try_claim(uid, claims_dir=claims_dir)
    assert handle is not None
    assert claims.try_claim(uid, claims_dir=claims_dir) is None

    handle.release()

    second = claims.try_claim(uid, claims_dir=claims_dir)
    assert second is not None
    second.release()


def test_release_is_idempotent(tmp_path):
    claims_dir = tmp_path / "claims"
    handle = claims.try_claim(_uid("e"), claims_dir=claims_dir)
    assert handle is not None

    handle.release()
    handle.release()  # must not raise


def test_release_free_function_is_a_no_op_for_a_failed_claim(tmp_path):
    claims_dir = tmp_path / "claims"
    uid = _uid("f")
    first = claims.try_claim(uid, claims_dir=claims_dir)
    failed = claims.try_claim(uid, claims_dir=claims_dir)

    assert failed is None
    claims.release(failed)  # must not raise on None

    claims.release(first)


def test_claim_handle_is_a_context_manager_that_releases_on_exit(tmp_path):
    claims_dir = tmp_path / "claims"
    uid = _uid("g")

    handle = claims.try_claim(uid, claims_dir=claims_dir)
    assert handle is not None

    with handle:
        assert claims.try_claim(uid, claims_dir=claims_dir) is None

    # released on __exit__ -- a fresh attempt now succeeds
    reclaimed = claims.try_claim(uid, claims_dir=claims_dir)
    assert reclaimed is not None
    reclaimed.release()


def test_claims_dir_is_created_when_missing(tmp_path):
    claims_dir = tmp_path / "does" / "not" / "exist" / "yet"
    assert not claims_dir.exists()

    handle = claims.try_claim(_uid("h"), claims_dir=claims_dir)

    assert handle is not None
    assert claims_dir.is_dir()
    handle.release()


def test_claims_dir_is_world_writable_sticky(tmp_path):
    claims_dir = tmp_path / "claims"
    handle = claims.try_claim(_uid("i"), claims_dir=claims_dir)
    assert handle is not None

    mode = stat.S_IMODE(claims_dir.stat().st_mode)
    assert mode == 0o1777

    handle.release()


# ---------------------------------------------------------------------------
# two threads racing for the same uid -- only one ever wins
# ---------------------------------------------------------------------------


def test_two_threads_racing_for_the_same_uid_only_one_wins(tmp_path):
    claims_dir = tmp_path / "claims"
    uid = _uid("race")
    results: list[claims.ClaimHandle | None] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def attempt() -> None:
        barrier.wait()
        handle = claims.try_claim(uid, claims_dir=claims_dir)
        with lock:
            results.append(handle)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [h for h in results if h is not None]
    assert len(winners) == 1
    for h in winners:
        h.release()


# ---------------------------------------------------------------------------
# a crashed/killed holder's claim becomes available with no manual cleanup
# ---------------------------------------------------------------------------


def _child_hold_claim(claims_dir: str, uid: str, ready: "mp.synchronize.Event") -> None:
    """Run in a real, separate OS process: claim ``uid`` and then just
    sit there (never releasing) until the parent kills this process --
    simulating a crashed holder. Imports locally since ``spawn`` (this
    test's own multiprocessing context) re-imports the target's module in
    the child rather than inheriting the parent's already-imported state.
    """
    from mbtools.registry import claims as _claims

    handle = _claims.try_claim(uid, claims_dir=Path(claims_dir))
    if handle is None:  # pragma: no cover -- would fail the test's own assert
        return
    ready.set()
    time.sleep(60)


def test_crashed_holder_claim_is_available_to_another_claimant_with_no_manual_cleanup(
    tmp_path,
):
    claims_dir = tmp_path / "claims"
    uid = _uid("crash")

    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    proc = ctx.Process(target=_child_hold_claim, args=(str(claims_dir), uid, ready))
    proc.start()
    try:
        assert ready.wait(timeout=10), "child process never signaled it claimed the uid"

        # Held by the (still-alive) child -- this process's own attempt
        # must fail.
        assert claims.try_claim(uid, claims_dir=claims_dir) is None
    finally:
        proc.kill()  # SIGKILL -- no graceful shutdown, no explicit release
        proc.join(timeout=10)

    assert proc.exitcode is not None  # the child really did exit

    # No manual cleanup performed here at all -- the OS released the
    # flock the moment the killed process's fd closed.
    handle = claims.try_claim(uid, claims_dir=claims_dir)
    assert handle is not None
    handle.release()


# ---------------------------------------------------------------------------
# Windows -- an explicit, documented no-op that always succeeds
# ---------------------------------------------------------------------------


def test_windows_try_claim_always_succeeds_and_touches_no_filesystem(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(claims.sys, "platform", "win32")

    # Deliberately do NOT create tmp_path/claims and deliberately do NOT
    # pass claims_dir -- proves the win32 branch never touches the
    # filesystem at all (a real Windows host has no equivalent of
    # claims_dir_path()'s /tmp-style location to depend on).
    first = claims.try_claim(_uid("win-a"))
    second = claims.try_claim(_uid("win-a"))  # "another instance" -- still succeeds

    assert first is not None
    assert second is not None
    first.release()
    second.release()


def test_windows_claim_handle_release_is_a_safe_no_op(monkeypatch):
    monkeypatch.setattr(claims.sys, "platform", "win32")
    handle = claims.try_claim(_uid("win-b"))
    assert handle is not None
    handle.release()
    handle.release()  # idempotent, same as the Unix path


# ---------------------------------------------------------------------------
# protect_fd -- best-effort TIOCEXCL, never raises
# ---------------------------------------------------------------------------


def test_protect_fd_returns_false_on_windows(monkeypatch):
    monkeypatch.setattr(claims.sys, "platform", "win32")
    assert claims.protect_fd(0) is False


def test_protect_fd_returns_false_for_an_invalid_fd():
    # A wildly out-of-range fd is never a valid tty -- ioctl() fails
    # cleanly (EBADF), and protect_fd swallows it rather than raising.
    assert claims.protect_fd(999999) is False


def test_protect_fd_succeeds_on_a_real_pty(tmp_path):
    pytest.importorskip("pty")
    import os
    import pty

    controller_fd, follower_fd = pty.openpty()
    try:
        assert claims.protect_fd(follower_fd) is True
    finally:
        os.close(controller_fd)
        os.close(follower_fd)


# ---------------------------------------------------------------------------
# is_claimable -- the pure --only-uid/--exclude-uid predicate
# ---------------------------------------------------------------------------


def test_is_claimable_true_with_no_restrictions():
    assert claims.is_claimable("any-uid") is True


def test_is_claimable_false_when_excluded():
    assert claims.is_claimable("x", exclude_uids={"x", "y"}) is False


def test_is_claimable_true_when_not_excluded():
    assert claims.is_claimable("z", exclude_uids={"x", "y"}) is True


def test_is_claimable_false_when_only_uids_given_and_uid_not_in_it():
    assert claims.is_claimable("z", only_uids={"x", "y"}) is False


def test_is_claimable_true_when_only_uids_given_and_uid_is_in_it():
    assert claims.is_claimable("x", only_uids={"x", "y"}) is True


def test_is_claimable_exclude_wins_over_only_uids_for_the_same_uid():
    assert claims.is_claimable("x", only_uids={"x"}, exclude_uids={"x"}) is False


def test_is_claimable_true_with_empty_only_uids_treated_as_no_restriction():
    assert claims.is_claimable("z", only_uids=set()) is True


# ---------------------------------------------------------------------------
# build_claim_fn -- --only-uid/--exclude-uid wired ahead of try_claim
# ---------------------------------------------------------------------------


def test_build_claim_fn_with_no_restrictions_delegates_to_try_claim(tmp_path):
    claims_dir = tmp_path / "claims"
    claim_fn = claims.build_claim_fn(claims_dir=claims_dir)

    handle = claim_fn(_uid("k"))
    assert handle is not None
    handle.release()


def test_build_claim_fn_excluded_uid_never_calls_try_claim(tmp_path):
    claims_dir = tmp_path / "claims"
    uid = _uid("excluded")
    claim_fn = claims.build_claim_fn(exclude_uids=[uid], claims_dir=claims_dir)

    assert claim_fn(uid) is None
    # try_claim was never even attempted -- no lock file exists.
    assert not (claims_dir / f"{uid}.lock").exists()


def test_build_claim_fn_only_uids_restricts_to_the_given_set(tmp_path):
    claims_dir = tmp_path / "claims"
    allowed = _uid("allowed")
    other = _uid("other")
    claim_fn = claims.build_claim_fn(only_uids=[allowed], claims_dir=claims_dir)

    handle = claim_fn(allowed)
    assert handle is not None
    handle.release()

    assert claim_fn(other) is None
    assert not (claims_dir / f"{other}.lock").exists()


def test_build_claim_fn_still_respects_real_contention_for_allowed_uids(tmp_path):
    claims_dir = tmp_path / "claims"
    uid = _uid("contended")
    claim_fn = claims.build_claim_fn(claims_dir=claims_dir)

    first = claim_fn(uid)
    assert first is not None
    assert claim_fn(uid) is None  # still contended, not just filtered

    first.release()
