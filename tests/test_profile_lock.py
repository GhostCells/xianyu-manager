from __future__ import annotations

import pytest

from xianyu_manager.profile_lock import ProfileOwnerError, ProfileOwnerLock


def test_profile_lock_allows_one_owner_and_rejects_second(tmp_path):
    profile = tmp_path / "profiles" / "account-1"
    first = ProfileOwnerLock(profile)
    second = ProfileOwnerLock(profile)

    first.acquire()
    try:
        with pytest.raises(ProfileOwnerError, match="另一个进程"):
            second.acquire()
    finally:
        first.release()


def test_profile_lock_can_be_reacquired_after_release(tmp_path):
    profile = tmp_path / "profiles" / "account-1"
    first = ProfileOwnerLock(profile)
    first.acquire()
    first.release()

    second = ProfileOwnerLock(profile)
    second.acquire()
    second.release()
