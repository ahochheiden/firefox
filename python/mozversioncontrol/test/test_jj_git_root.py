# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import mozunit
import pytest

from mozversioncontrol import get_repository_object
from mozversioncontrol.repo.jj import _git_root


def test_jj_git_root_cached(repo):
    if repo.vcs != "jj":
        pytest.skip("jj only")

    _git_root.cache_clear()
    first = get_repository_object(repo.dir)
    second = get_repository_object(repo.dir)

    assert _git_root.cache_info().misses == 1
    assert _git_root.cache_info().hits == 1
    assert first._git._env["GIT_DIR"] == second._git._env["GIT_DIR"]


if __name__ == "__main__":
    mozunit.main()
