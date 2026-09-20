# Any copyright is dedicated to the Public Domain.
# https://creativecommons.org/publicdomain/zero/1.0/
"""
Tests for the 'tests.py' transforms
"""

import hashlib
from functools import partial
from pprint import pprint
from types import SimpleNamespace

import mozunit
import pytest
from taskgraph.util import json

from gecko_taskgraph.test.conftest import FakeParameters
from gecko_taskgraph.transforms import test as test_transforms
from gecko_taskgraph.transforms.test import build_fetches, chunk, other
from gecko_taskgraph.transforms.test.chunk import DYNAMIC_CHUNK_DURATION
from gecko_taskgraph.util import chunking


@pytest.fixture
def make_test_task():
    """Create a test task definition with required default values."""

    def inner(**extra):
        task = {
            "attributes": {"unittest_suite": "task"},
            "build-platform": "linux64",
            "mozharness": {"extra-options": []},
            "test-platform": "linux64",
            "treeherder-symbol": "g(t)",
            "test-name": "task",
            "try-name": "task",
        }
        task.update(extra)
        return task

    return inner


def test_split_variants(monkeypatch, run_full_config_transform, make_test_task):
    # mock out variant definitions
    monkeypatch.setattr(
        test_transforms.variant,
        "TEST_VARIANTS",
        {
            "foo": {
                "description": "foo variant",
                "suffix": "foo",
                "mozinfo": "foo",
                "component": "foo bar",
                "expiration": "never",
                "merge": {
                    "mozharness": {
                        "extra-options": [
                            "--setpref=foo=1",
                        ],
                    },
                },
            },
            "bar": {
                "description": "bar variant",
                "suffix": "bar",
                "mozinfo": "bar",
                "component": "foo bar",
                "expiration": "never",
                "when": {
                    "$eval": "task['test-platform'][:5] == 'linux'",
                },
                "merge": {
                    "mozharness": {
                        "extra-options": [
                            "--setpref=bar=1",
                        ],
                    },
                },
                "replace": {"tier": 2},
            },
        },
    )

    def make_expected(variant):
        """Helper to generate expected tasks."""
        return make_test_task(**{
            "attributes": {"unittest_suite": "task", "unittest_variant": variant},
            "description": f"{variant} variant",
            "mozharness": {
                "extra-options": [f"--setpref={variant}=1"],
            },
            "treeherder-symbol": f"g-{variant}(t)",
            "variant-suffix": f"-{variant}",
        })

    run_split_variants = partial(
        run_full_config_transform, test_transforms.variant.split_variants
    )

    # test no variants
    input_task = make_test_task(**{
        "run-without-variant": True,
    })
    tasks = list(run_split_variants(input_task))
    assert len(tasks) == 1

    expected = input_task
    expected["attributes"]["unittest_variant"] = None
    assert tasks[0] == expected

    # test variants are split into expected tasks
    input_task = make_test_task(**{
        "run-without-variant": True,
        "variants": ["foo", "bar"],
    })
    tasks = list(run_split_variants(input_task))
    assert len(tasks) == 3

    expected = make_test_task()
    expected["attributes"]["unittest_variant"] = None
    assert tasks[0] == expected
    assert tasks[1] == make_expected("foo")

    expected = make_expected("bar")
    expected["tier"] = 2
    assert tasks[2] == expected

    # test composite variants
    input_task = make_test_task(**{
        "run-without-variant": True,
        "variants": ["foo+bar"],
    })
    tasks = list(run_split_variants(input_task))
    assert len(tasks) == 2
    assert tasks[1]["attributes"]["unittest_variant"] == "foo+bar"
    assert tasks[1]["mozharness"]["extra-options"] == [
        "--setpref=foo=1",
        "--setpref=bar=1",
    ]
    assert tasks[1]["treeherder-symbol"] == "g-foo-bar(t)"

    # test 'when' filter
    input_task = make_test_task(**{
        "run-without-variant": True,
        # this should cause task to be filtered out of 'bar' and 'foo+bar' variants
        "test-platform": "windows",
        "variants": ["foo", "bar", "foo+bar"],
    })
    tasks = list(run_split_variants(input_task))
    assert len(tasks) == 2
    assert tasks[0]["attributes"]["unittest_variant"] is None
    assert tasks[1]["attributes"]["unittest_variant"] == "foo"

    # test 'run-without-variants=False'
    input_task = make_test_task(**{
        "run-without-variant": False,
        "variants": ["foo"],
    })
    tasks = list(run_split_variants(input_task))
    assert len(tasks) == 1
    assert tasks[0]["attributes"]["unittest_variant"] == "foo"


@pytest.mark.parametrize(
    "task,expected",
    (
        pytest.param(
            {
                "attributes": {"unittest_variant": "webrender-sw+1proc"},
                "test-platform": "linux2404-64-clang-trunk/opt",
            },
            {
                "platform": {
                    "arch": "64",
                    "os": {
                        "name": "linux",
                        "version": "2404",
                    },
                },
                "build": {
                    "type": "opt",
                    "clang-trunk": True,
                },
                "runtime": {
                    "1proc": True,
                    "webrender-sw": True,
                },
            },
            id="linux",
        ),
        pytest.param(
            {
                "attributes": {},
                "test-platform": "linux2204-64-wayland-shippable/opt",
            },
            {
                "platform": {
                    "arch": "64",
                    "display": "wayland",
                    "os": {
                        "name": "linux",
                        "version": "2204",
                    },
                },
                "build": {
                    "type": "opt",
                    "shippable": True,
                },
                "runtime": {},
            },
            id="linux wayland shippable",
        ),
        pytest.param(
            {
                "attributes": {},
                "test-platform": "android-hw-a51-11-0-arm7-shippable-qr/opt",
            },
            {
                "platform": {
                    "arch": "arm7",
                    "device": "a51",
                    "os": {
                        "name": "android",
                        "version": "11.0",
                    },
                },
                "build": {
                    "type": "opt",
                    "shippable": True,
                },
                "runtime": {},
            },
            id="android",
        ),
        pytest.param(
            {
                "attributes": {},
                "test-platform": "windows11-64-2009-hw-ref-ccov/debug",
            },
            {
                "platform": {
                    "arch": "64",
                    "machine": "hw-ref",
                    "os": {
                        "build": "2009",
                        "name": "windows",
                        "version": "11",
                    },
                },
                "build": {
                    "type": "debug",
                    "ccov": True,
                },
                "runtime": {},
            },
            id="windows",
        ),
    ),
)
def test_set_test_setting(run_transform, task, expected):
    # add hash to 'expected'
    expected["_hash"] = hashlib.sha256(
        json.dumps(expected, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]

    task = list(run_transform(test_transforms.other.set_test_setting, task))[0]
    assert "test-setting" in task
    assert task["test-setting"] == expected


def assert_spi_not_disabled(task):
    extra_options = task["mozharness"]["extra-options"]
    # The pref to enable this gets set outside of this transform, so only
    # bother asserting that the pref to disable does not exist.
    assert (
        "--setpref=media.peerconnection.mtransport_process=false" not in extra_options
    )
    assert "--setpref=network.process.enabled=false" not in extra_options


def assert_spi_disabled(task):
    extra_options = task["mozharness"]["extra-options"]
    assert "--setpref=media.peerconnection.mtransport_process=false" in extra_options
    assert "--setpref=media.peerconnection.mtransport_process=true" not in extra_options
    assert "--setpref=network.process.enabled=false" in extra_options
    assert "--setpref=network.process.enabled=true" not in extra_options


@pytest.mark.parametrize(
    "task,callback",
    (
        pytest.param(
            {"attributes": {"unittest_variant": "socketprocess"}},
            assert_spi_not_disabled,
            id="socketprocess",
        ),
        pytest.param(
            {
                "attributes": {"unittest_variant": "socketprocess_networking"},
            },
            assert_spi_not_disabled,
            id="socketprocess_networking",
        ),
        pytest.param({}, assert_spi_disabled, id="no variant"),
        pytest.param(
            {"suite": "cppunit", "attributes": {"unittest_variant": "socketprocess"}},
            assert_spi_not_disabled,
            id="excluded suite",
        ),
        pytest.param(
            {"attributes": {"unittest_variant": "no-fission+socketprocess"}},
            assert_spi_not_disabled,
            id="composite variant",
        ),
    ),
)
def test_ensure_spi_disabled_on_all_but_spi(
    make_test_task, run_transform, task, callback
):
    task.setdefault("suite", "mochitest-plain")
    task = make_test_task(**task)
    task = list(
        run_transform(test_transforms.other.ensure_spi_disabled_on_all_but_spi, task)
    )[0]
    pprint(task)
    callback(task)


def test_resolve_dynamic_chunks_uses_variant_suffix(
    monkeypatch, run_transform, make_test_task
):
    """resolve_dynamic_chunks should include variant-suffix in the suite name
    passed to get_runtimes."""
    calls = []

    def fake_get_runtimes(platform, suite_name):
        calls.append((platform, suite_name))
        if suite_name == "task-swr":
            return {"manifest.toml": 600}
        return {}

    monkeypatch.setattr(
        "gecko_taskgraph.transforms.test.chunk.get_runtimes", fake_get_runtimes
    )
    monkeypatch.setattr(
        "gecko_taskgraph.transforms.test.chunk.resolve_manifest_runtimes",
        lambda runtimes, manifests: {
            m: runtimes[m] for m in manifests if m in runtimes
        },
    )

    task = make_test_task(**{
        "chunks": "dynamic",
        "default-chunks": 10,
        "variant-suffix": "-swr",
        "test-manifests": {"active": ["manifest.toml"], "skipped": []},
    })
    tasks = list(run_transform(test_transforms.chunk.resolve_dynamic_chunks, task))
    assert len(tasks) == 1
    assert ("linux64", "task-swr") in calls
    assert tasks[0]["chunks"] == 1


def test_resolve_dynamic_chunks_falls_back_without_runtimes(
    monkeypatch, run_transform, make_test_task
):
    """resolve_dynamic_chunks should fall back to default-chunks when
    get_runtimes returns no data."""
    monkeypatch.setattr(
        "gecko_taskgraph.transforms.test.chunk.get_runtimes", lambda p, s: {}
    )

    task = make_test_task(**{
        "chunks": "dynamic",
        "default-chunks": 10,
        "variant-suffix": "-swr",
        "test-manifests": {"active": ["manifest.toml"], "skipped": []},
    })
    tasks = list(run_transform(test_transforms.chunk.resolve_dynamic_chunks, task))
    assert tasks[0]["chunks"] == 10


def test_split_chunks_uses_variant_suffix(monkeypatch, run_transform, make_test_task):
    """split_chunks should pass the variant-suffixed suite name to
    chunk_manifests so manifests are distributed using variant-specific
    runtime data."""
    calls = []

    def fake_chunk_manifests(suite, platform, chunks, manifests):
        calls.append(suite)
        return [manifests]

    monkeypatch.setattr(
        "gecko_taskgraph.transforms.test.chunk.chunk_manifests",
        fake_chunk_manifests,
    )

    task = make_test_task(**{
        "chunks": 1,
        "variant-suffix": "-swr",
        "treeherder-symbol": "M-swr(bc)",
        "test-manifests": {"active": ["manifest.toml"], "skipped": []},
    })
    tasks = list(
        run_transform(
            test_transforms.chunk.split_chunks,
            task,
            params=FakeParameters({"backstop": False, "try_task_config": {}}),
        )
    )
    assert len(tasks) == 1
    assert "task-swr" in calls


def test_split_chunks_base_task_no_variant_suffix(
    monkeypatch, run_transform, make_test_task
):
    """split_chunks should pass the plain test-name when there is no variant."""
    calls = []

    def fake_chunk_manifests(suite, platform, chunks, manifests):
        calls.append(suite)
        return [manifests]

    monkeypatch.setattr(
        "gecko_taskgraph.transforms.test.chunk.chunk_manifests",
        fake_chunk_manifests,
    )

    task = make_test_task(**{
        "chunks": 1,
        "treeherder-symbol": "M(bc)",
        "test-manifests": {"active": ["manifest.toml"], "skipped": []},
    })
    tasks = list(
        run_transform(
            test_transforms.chunk.split_chunks,
            task,
            params=FakeParameters({"backstop": False, "try_task_config": {}}),
        )
    )
    assert len(tasks) == 1
    assert "task" in calls


_TESTS_ROOT = "testing/web-platform/tests/"
_MOZ_TESTS_ROOT = "testing/web-platform/mozilla/tests/"


@pytest.mark.parametrize(
    "test_name,input_paths,expected",
    [
        # A subsuite task runs when at least one path is under its prefix...
        ("web-platform-tests-webrtc-1", [_TESTS_ROOT + "webrtc/a.html"], True),
        # ...and does not when none are.
        ("web-platform-tests-webrtc-1", [_TESTS_ROOT + "dom/a.html"], False),
        # Prefix matching is deliberately loose: "webrtc" covers the webrtc-*
        # sibling directories too.
        (
            "web-platform-tests-webrtc-1",
            [_TESTS_ROOT + "webrtc-encoded-transform/a.html"],
            True,
        ),
        # Every input path is considered, not just the first.
        (
            "web-platform-tests-webrtc-1",
            [_TESTS_ROOT + "dom/a.html", _TESTS_ROOT + "webrtc/b.html"],
            True,
        ),
        # The mozilla-specific wpt root is matched too.
        ("web-platform-tests-webrtc-1", [_MOZ_TESTS_ROOT + "webrtc/a.html"], True),
        # A nested subsuite prefix only matches its specific subdir.
        (
            "web-platform-tests-webcodecs-1",
            [_TESTS_ROOT + "media-source/mse-for-webcodecs/a.html"],
            True,
        ),
        # A general (non-subsuite) task runs when at least one path is outside
        # every subsuite prefix.
        ("web-platform-tests-1", [_TESTS_ROOT + "dom/a.html"], True),
        ("web-platform-tests-1", [_MOZ_TESTS_ROOT + "dom/a.html"], True),
        # media-source (but not mse-for-webcodecs) belongs to the general task.
        ("web-platform-tests-1", [_TESTS_ROOT + "media-source/a.html"], True),
        # A path that merely contains a subsuite name as a substring rather than
        # a path prefix belongs to the general task.
        ("web-platform-tests-1", [_TESTS_ROOT + "css/foo-webgpu/a.html"], True),
        # A general task does not run when every path belongs to a subsuite.
        ("web-platform-tests-1", [_TESTS_ROOT + "webrtc/a.html"], False),
        (
            "web-platform-tests-1",
            [_TESTS_ROOT + "webrtc/a.html", _TESTS_ROOT + "webgpu/b.html"],
            False,
        ),
        # ...but does when at least one path is non-subsuite.
        (
            "web-platform-tests-1",
            [_TESTS_ROOT + "webrtc/a.html", _TESTS_ROOT + "dom/b.html"],
            True,
        ),
        # A non-wpt (e.g. mochitest) path mixed into a wpt task's scheduled
        # paths is ignored; the subsuite task still runs on its wpt path.
        (
            "web-platform-tests-webrtc-1",
            [_TESTS_ROOT + "webrtc/a.html", "dom/media/webrtc/tests/mochitest/a.html"],
            True,
        ),
        # ...and a subsuite task with only a non-wpt path does not run.
        (
            "web-platform-tests-webrtc-1",
            ["dom/media/webrtc/tests/mochitest/a.html"],
            False,
        ),
        # A non-wpt path that merely contains a subsuite name as a substring must
        # not trigger the matching wpt subsuite task. webgpu exists as both a wpt
        # and a mochitest subsuite, but only paths under the wpt roots count.
        ("web-platform-tests-webgpu-1", ["dom/webgpu/mochitest/a.html"], False),
        # No scheduled paths means there is nothing for the task to run.
        ("web-platform-tests-webrtc-1", [], False),
        ("web-platform-tests-1", [], False),
    ],
)
def test_wpt_task_should_run(test_name, input_paths, expected):
    assert chunk._wpt_task_should_run(test_name, input_paths) == expected


def test_test_paths_do_not_drop_no_manifest_loader_tasks(run_transform):
    """MOZHARNESS_TEST_PATHS must not drop tasks that opt out of taskgraph-time
    manifest resolution (test-manifest-loader=None), such as gtest/cppunittest.
    """
    tasks = [
        {
            "test-name": "gtest",
            "attributes": {"unittest_suite": "gtest"},
            "test-manifest-loader": None,
        },
        {
            "test-name": "cppunittest",
            "attributes": {"unittest_suite": "cppunittest"},
            "test-manifest-loader": None,
        },
    ]
    params = FakeParameters({
        "try_task_config": {
            "env": {
                "MOZHARNESS_TEST_PATHS": json.dumps({
                    "web-platform-tests": [_TESTS_ROOT + "webrtc/a.html"],
                    "mochitest-plain": ["dom/media/test/a.html"],
                })
            }
        },
        "test_manifest_loader": "default",
    })

    result = list(run_transform(chunk.set_test_manifests, tasks, params=params))
    assert sorted(t["test-name"] for t in result) == ["cppunittest", "gtest"]

    # Their manifests were not restricted, so their chunks would all run the
    # same tests and only the first is worth scheduling.
    assert not any(t["attributes"].get("test-manifests-restricted") for t in result)


def _make_test_paths_params(paths, suite="mochitest-browser-chrome"):
    return FakeParameters({
        "try_task_config": {
            "env": {"MOZHARNESS_TEST_PATHS": json.dumps({suite: paths})},
        },
        "test_manifest_loader": "default",
        "head_repository": "",
        "app_version": "",
        "backstop": False,
    })


@pytest.fixture
def path_scoped_task(monkeypatch):
    """Set up set_test_manifests for a suite of 10 manifests, of which the
    requested paths hold the first four."""
    suite_manifests = [f"dir/manifest{i}.toml" for i in range(10)]

    def inner(matched):
        monkeypatch.setattr(chunk, "guess_mozinfo_from_task", lambda *args, **kw: {})
        monkeypatch.setattr(
            chunk,
            "get_manifest_loader",
            lambda name, params: SimpleNamespace(
                get_manifests=lambda suite, mozinfo: {
                    "active": list(suite_manifests),
                    "skipped": ["dir/skipped.toml"],
                    "other_dirs": {},
                }
            ),
        )
        monkeypatch.setattr(
            chunk.resolver, "get_test_paths_by_manifest", lambda suite, paths: matched
        )

        return {
            "attributes": {"unittest_suite": "mochitest-browser-chrome"},
            "suite": "mochitest-browser-chrome",
            "test-name": "mochitest-browser-chrome",
            "test-platform": "linux64",
            "test-setting": {},
            "treeherder-symbol": "M(bc)",
            "chunks": "dynamic",
            "default-chunks": 10,
        }

    inner.suite_manifests = suite_manifests
    return inner


def test_set_test_manifests_restricts_to_test_paths(run_transform, path_scoped_task):
    """A multi-manifest MOZHARNESS_TEST_PATHS keeps only the manifests holding
    tests under the requested paths, and scales the chunk counts down to that
    share of the suite."""
    matched = {m: [m] for m in path_scoped_task.suite_manifests[:4]}
    task = path_scoped_task(matched)

    tasks = list(
        run_transform(
            chunk.set_test_manifests, task, params=_make_test_paths_params(["dir"])
        )
    )

    assert len(tasks) == 1
    assert tasks[0]["test-manifests"] == {"active": sorted(matched), "skipped": []}
    assert tasks[0]["default-chunks"] == 4
    assert tasks[0]["attributes"]["test-manifests-restricted"] is True


def test_set_test_manifests_accepts_a_single_test_path_string(
    run_transform, path_scoped_task
):
    """A suite's paths can be given as a string rather than as a list."""
    matched = {m: [m] for m in path_scoped_task.suite_manifests[:4]}
    task = path_scoped_task(matched)

    tasks = list(
        run_transform(
            chunk.set_test_manifests, task, params=_make_test_paths_params("dir")
        )
    )

    assert len(tasks) == 1
    assert tasks[0]["test-manifests"]["active"] == sorted(matched)


def test_set_test_manifests_leaves_chunking_alone_for_test_tags(
    run_transform, path_scoped_task
):
    """A test tag runs only part of each manifest, which the per-manifest runtime
    data can't account for, so a tagged task is not marked as restricted and
    keeps being chunked by the harness."""
    matched = {m: [m] for m in path_scoped_task.suite_manifests[:4]}
    task = path_scoped_task(matched)
    params = _make_test_paths_params(["dir"])
    params["try_task_config"]["env"]["MOZHARNESS_TEST_TAG"] = json.dumps(["a_tag"])

    tasks = list(run_transform(chunk.set_test_manifests, task, params=params))

    assert len(tasks) == 1
    assert "test-manifests-restricted" not in tasks[0]["attributes"]


def test_set_test_manifests_keeps_whole_suite_for_wpt(run_transform, path_scoped_task):
    """web-platform-tests manifest names hold namespaces rather than source
    paths, so the task keeps the whole suite for the harness to filter, and is
    not marked as restricted."""
    task = path_scoped_task({})
    task["test-name"] = "web-platform-tests"
    task["attributes"]["unittest_suite"] = "web-platform-tests"
    params = _make_test_paths_params(
        [_TESTS_ROOT + "dom/events"], suite="web-platform-tests"
    )

    tasks = list(run_transform(chunk.set_test_manifests, task, params=params))

    assert len(tasks) == 1
    assert tasks[0]["test-manifests"]["active"] == path_scoped_task.suite_manifests
    assert "test-manifests-restricted" not in tasks[0]["attributes"]


def test_set_test_manifests_keeps_narrower_test_paths(run_transform, path_scoped_task):
    """A path narrower than a manifest, e.g. a single test file, is kept as is so
    that the task doesn't widen to the whole manifest, and stays at one chunk."""
    task = path_scoped_task({"dir/manifest0.toml": ["dir/test_one.js"]})

    tasks = list(
        run_transform(
            chunk.set_test_manifests,
            task,
            params=_make_test_paths_params(["dir/test_one.js"]),
        )
    )

    assert len(tasks) == 1
    assert tasks[0]["test-manifests"] == {"active": ["dir/test_one.js"], "skipped": []}
    assert tasks[0]["default-chunks"] == 1


def test_set_test_manifests_drops_unrelated_tasks(run_transform, path_scoped_task):
    """A task with no active manifest under the requested paths is dropped."""
    task = path_scoped_task({})

    tasks = list(
        run_transform(
            chunk.set_test_manifests, task, params=_make_test_paths_params(["dir"])
        )
    )

    assert tasks == []


def test_test_paths_are_split_into_chunks(monkeypatch, run_transform, path_scoped_task):
    """The manifests matching the requested paths are spread over several
    chunks, rather than all landing in a single one."""
    matched = {m: [m] for m in path_scoped_task.suite_manifests[:4]}
    task = path_scoped_task(matched)
    params = _make_test_paths_params(["dir"])

    # Each manifest is worth half a chunk, so the four of them need two chunks.
    runtimes = {m: DYNAMIC_CHUNK_DURATION / 2 for m in matched}
    monkeypatch.setattr(chunk, "get_runtimes", lambda platform, suite: runtimes)
    monkeypatch.setattr(chunking, "get_runtimes", lambda platform, suite: runtimes)

    tasks = list(run_transform(chunk.set_test_manifests, task, params=params))
    tasks = list(run_transform(chunk.resolve_dynamic_chunks, tasks, params=params))
    assert tasks[0]["chunks"] == 2

    tasks = list(run_transform(chunk.split_chunks, tasks, params=params))
    assert len(tasks) == 2
    assert all(t["test-manifests"] for t in tasks)
    assert sorted(m for t in tasks for m in t["test-manifests"]) == sorted(matched)


@pytest.fixture
def task_with_zero_runtimes(monkeypatch, make_test_task):
    """A task whose runtime data covers a single one of its ten manifests, the
    nine others being at 0."""

    def inner(restricted):
        manifests = [f"manifest{i}.toml" for i in range(10)]
        runtimes = dict.fromkeys(manifests, 0)
        runtimes["manifest0.toml"] = DYNAMIC_CHUNK_DURATION
        monkeypatch.setattr(chunk, "get_runtimes", lambda platform, suite: runtimes)

        return make_test_task(**{
            "attributes": {
                "unittest_suite": "task",
                "test-manifests-restricted": restricted,
            },
            "chunks": "dynamic",
            "default-chunks": 1,
            "test-manifests": {"active": manifests, "skipped": []},
        })

    return inner


def test_resolve_dynamic_chunks_ignores_zero_runtimes_when_restricted(
    run_transform, task_with_zero_runtimes
):
    """For a restricted task, a manifest at 0 means the runtime data doesn't
    cover this configuration, so it is filled in with the average of the
    manifests that do have data rather than counted as instant."""
    tasks = list(
        run_transform(chunk.resolve_dynamic_chunks, task_with_zero_runtimes(True))
    )

    assert tasks[0]["chunks"] == 10


def test_resolve_dynamic_chunks_keeps_zero_runtimes_when_unrestricted(
    run_transform, task_with_zero_runtimes
):
    """An unrestricted task runs the whole suite, where the manifests that do
    have runtime data are representative enough to be used as they are."""
    tasks = list(
        run_transform(chunk.resolve_dynamic_chunks, task_with_zero_runtimes(False))
    )

    assert tasks[0]["chunks"] == 1


@pytest.fixture
def task_with_partial_chunk_runtimes(monkeypatch, make_test_task):
    """A task whose manifests add up to 1.4 times the target chunk duration."""

    def inner(restricted):
        manifests = [f"manifest{i}.toml" for i in range(4)]
        runtimes = dict.fromkeys(manifests, DYNAMIC_CHUNK_DURATION * 0.35)
        monkeypatch.setattr(chunk, "get_runtimes", lambda platform, suite: runtimes)

        return make_test_task(**{
            "attributes": {
                "unittest_suite": "task",
                "test-manifests-restricted": restricted,
            },
            "chunks": "dynamic",
            "default-chunks": 1,
            "test-manifests": {"active": manifests, "skipped": []},
        })

    return inner


def test_resolve_dynamic_chunks_rounds_up_when_restricted(
    run_transform, task_with_partial_chunk_runtimes
):
    """A restricted task gets a chunk for the remainder, rather than a single
    chunk running half again the target duration."""
    tasks = list(
        run_transform(
            chunk.resolve_dynamic_chunks, task_with_partial_chunk_runtimes(True)
        )
    )

    assert tasks[0]["chunks"] == 2


def test_resolve_dynamic_chunks_rounds_to_nearest_when_unrestricted(
    run_transform, task_with_partial_chunk_runtimes
):
    """For an unrestricted task the remainder is spread over the many chunks the
    whole suite needs, so the count is rounded to the nearest."""
    tasks = list(
        run_transform(
            chunk.resolve_dynamic_chunks, task_with_partial_chunk_runtimes(False)
        )
    )

    assert tasks[0]["chunks"] == 1


@pytest.mark.parametrize(
    "use_artifact_builds,supports_artifact_builds,expected_kept",
    (
        pytest.param(False, False, True, id="non_artifact_push_keeps_unsupported"),
        pytest.param(True, True, True, id="artifact_push_keeps_supported"),
        pytest.param(True, False, False, id="artifact_push_drops_unsupported"),
    ),
)
def test_drop_artifact_build_unsupported(
    run_transform,
    make_test_task,
    use_artifact_builds,
    supports_artifact_builds,
    expected_kept,
):
    task = make_test_task(**{"supports-artifact-builds": supports_artifact_builds})
    params = FakeParameters({
        "try_task_config": {"use-artifact-builds": use_artifact_builds}
    })
    tasks = list(
        run_transform(
            test_transforms.drop_artifact_build_unsupported, task, params=params
        )
    )
    assert bool(tasks) is expected_kept


@pytest.fixture
def build_fetches_task(make_test_task):
    def inner(
        script="desktop_unittest.py",
        suite="mochitest-plain",
        category="mochitest",
        test_platform="linux64/debug",
        download_symbols=True,
        **extra,
    ):
        mozharness = {
            "script": script,
            "extra-options": [],
            "requires-signed-builds": False,
            "build-artifact-name": "public/build/target.tar.xz",
        }
        if download_symbols is not None:
            mozharness["download-symbols"] = download_symbols
        return make_test_task(**{
            "attributes": {"unittest_suite": suite, "unittest_category": category},
            "mozharness": mozharness,
            "test-platform": test_platform,
            "try-name": suite,
            **extra,
        })

    return inner


def run_build_fetches(run_transform, task):
    return list(run_transform(build_fetches.add_build_fetches, task))[0]


def archive_fetches(task):
    return [
        (fetch["artifact"], fetch["dest"])
        for fetch in task["fetches"]["build"]
        if fetch.get("dest", "").startswith("tests")
    ]


def test_build_fetches_desktop_debug(run_transform, build_fetches_task):
    task = run_build_fetches(run_transform, build_fetches_task())
    assert task["fetches"] == {
        "build": [
            {"artifact": "/public/build/target.tar.xz", "extract": False},
            {"artifact": "/public/build/target.common.tests.tar.zst", "dest": "tests"},
            {
                "artifact": "/public/build/target.condprof.tests.tar.zst",
                "dest": "tests",
            },
            {
                "artifact": "/public/build/target.mochitest.tests.tar.zst",
                "dest": "tests",
            },
            {
                "artifact": "/public/build/target.crashreporter-symbols.zip",
                "dest": "symbols",
            },
        ]
    }
    assert task["worker"]["env"] == {
        "MOZ_SYMBOLS_URL": {
            "artifact-reference": (
                "<build/public/build/target.crashreporter-symbols.zip>"
            )
        },
    }


def test_build_fetches_jittest_ondemand(run_transform, build_fetches_task):
    task = run_build_fetches(
        run_transform,
        build_fetches_task(
            suite="jittest",
            category="jittest",
            test_platform="linux64/opt",
            download_symbols="ondemand",
        ),
    )
    assert archive_fetches(task) == [
        ("/public/build/target.common.tests.tar.zst", "tests"),
        ("/public/build/target.condprof.tests.tar.zst", "tests"),
        ("/public/build/target.jittest.tests.tar.zst", "tests"),
        ("/public/build/target.jsshell.zip", "tests/bin"),
    ]
    assert not any(f.get("dest") == "symbols" for f in task["fetches"]["build"])
    assert task["worker"]["env"]["MOZ_SYMBOLS_URL"] == {
        "artifact-reference": "<build/public/build/target.crashreporter-symbols.zip>"
    }


def test_build_fetches_jsreftest_suite_archive(run_transform, build_fetches_task):
    task = run_build_fetches(
        run_transform,
        build_fetches_task(
            suite="jsreftest",
            category="reftest",
            test_platform="linux64/opt",
            download_symbols="ondemand",
        ),
    )
    assert [a for a, _ in archive_fetches(task)] == [
        "/public/build/target.common.tests.tar.zst",
        "/public/build/target.condprof.tests.tar.zst",
        "/public/build/target.jsreftest.tests.tar.zst",
        "/public/build/target.reftest.tests.tar.zst",
    ]


def test_build_fetches_android_shippable_per_test(run_transform, build_fetches_task):
    task = run_build_fetches(
        run_transform,
        build_fetches_task(
            script="android_emulator_unittest.py",
            suite="test-verify",
            category="test-verify",
            test_platform="android-em-14-x86_64-shippable/opt",
            download_symbols="ondemand",
            mozharness={
                "script": "android_emulator_unittest.py",
                "extra-options": [],
                "requires-signed-builds": False,
                "build-artifact-name": "public/build/target.apk",
                "download-symbols": "ondemand",
            },
        ),
    )
    assert task["fetches"]["build"][0] == {
        "artifact": "/public/build/target.apk",
        "extract": False,
    }
    assert archive_fetches(task) == [
        ("/public/build/en-US/target.common.tests.tar.zst", "tests"),
        ("/public/build/en-US/target.condprof.tests.tar.zst", "tests"),
        ("/public/build/en-US/target.mochitest.tests.tar.zst", "tests"),
        ("/public/build/en-US/target.reftest.tests.tar.zst", "tests"),
        ("/public/build/en-US/target.xpcshell.tests.tar.zst", "tests"),
    ]
    assert task["worker"]["env"]["MOZ_SYMBOLS_URL"] == {
        "artifact-reference": (
            "<build/public/build/en-US/target.crashreporter-symbols.zip>"
        )
    }


def test_build_fetches_android_suite_alias(run_transform, build_fetches_task):
    task = run_build_fetches(
        run_transform,
        build_fetches_task(
            script="android_hardware_unittest.py",
            suite="crashtest",
            category="reftest",
            test_platform="android-hw-p6-13-0-android-aarch64/debug",
        ),
    )
    assert [a for a, _ in archive_fetches(task)] == [
        "/public/build/target.common.tests.tar.zst",
        "/public/build/target.condprof.tests.tar.zst",
        "/public/build/target.reftest.tests.tar.zst",
    ]


@pytest.mark.parametrize(
    "script,expected",
    (
        pytest.param(
            "web_platform_tests.py",
            ["common", "condprof", "web-platform"],
            id="wpt",
        ),
        pytest.param("awsy_script.py", ["awsy", "common", "condprof"], id="awsy"),
        pytest.param("raptor_script.py", ["common", "condprof", "raptor"], id="raptor"),
        pytest.param("talos_script.py", ["common", "condprof", "talos"], id="talos"),
        pytest.param("marionette.py", ["common", "condprof"], id="common-only"),
    ),
)
def test_build_fetches_script_categories(
    run_transform, build_fetches_task, script, expected
):
    task = run_build_fetches(
        run_transform,
        build_fetches_task(script=script, suite="task", category="task"),
    )
    assert [a for a, _ in archive_fetches(task)] == [
        f"/public/build/target.{name}.tests.tar.zst" for name in expected
    ]


@pytest.mark.parametrize(
    "variant,expected",
    (
        pytest.param(None, False, id="no-variant"),
        pytest.param("xorig", False, id="other-variant"),
        pytest.param("trainhop-release", True, id="trainhop-release"),
        pytest.param("trainhop-beta+xorig", True, id="trainhop-beta-composite"),
    ),
)
def test_build_fetches_trainhop_variants(
    run_transform, build_fetches_task, variant, expected
):
    task = run_build_fetches(
        run_transform,
        build_fetches_task(
            attributes={
                "unittest_suite": "mochitest-browser-chrome",
                "unittest_category": "mochitest",
                "unittest_variant": variant,
            }
        ),
    )
    archives = [a for a, _ in archive_fetches(task)]
    assert ("/public/build/target.trainhop.tests.tar.zst" in archives) == expected


def test_build_fetches_signed_installer(run_transform, build_fetches_task):
    task = build_fetches_task(test_platform="windows11-64-24h2/opt")
    task["mozharness"]["requires-signed-builds"] = True
    task["mozharness"]["build-artifact-name"] = "public/build/target.installer.msix"
    task = run_build_fetches(run_transform, task)
    assert task["fetches"]["build-signing"] == [
        {"artifact": "/public/build/target.installer.msix", "extract": False}
    ]
    assert all("extract" not in f for f in task["fetches"]["build"])


def test_build_fetches_upstream_target(run_transform, build_fetches_task):
    task = run_build_fetches(
        run_transform,
        build_fetches_task(
            script="raptor_script.py",
            test_platform="android-hw-p6-13-0-android-aarch64-shippable/opt",
            target={
                "upstream-task": "signing-apk",
                "name": "public/build/target.arm64-v8a.apk",
            },
        ),
    )
    assert task["fetches"]["signing-apk"] == [
        {"artifact": "/public/build/target.arm64-v8a.apk", "extract": False}
    ]


@pytest.mark.parametrize("download_symbols", ("ondemand", True), ids=("opt", "debug"))
def test_build_fetches_index_target(
    run_transform, build_fetches_task, download_symbols
):
    task = run_build_fetches(
        run_transform,
        build_fetches_task(
            script="raptor_script.py",
            download_symbols=download_symbols,
            target={
                "index": "mobile.v2.reference-browser",
                "name": "target.apk",
                "symbols": False,
            },
        ),
    )
    label = "fake-kind-index-search-mobile.v2.reference-browser"
    assert task["dependencies"] == {"indexed-installer": label}
    assert task["fetches"]["indexed-installer"] == [
        {"artifact": "/public/target.apk", "extract": False}
    ]
    assert [a for a, _ in archive_fetches(task)] == [
        "/public/build/target.common.tests.tar.zst",
        "/public/build/target.condprof.tests.tar.zst",
        "/public/build/target.raptor.tests.tar.zst",
    ]
    assert "worker" not in task
    assert "download-symbols" not in task["mozharness"]


def test_build_fetches_index_target_symbols(run_transform, build_fetches_task):
    index = "gecko.v2.mozilla-beta.shippable.latest.firefox.linux64-opt"
    task = run_build_fetches(
        run_transform,
        build_fetches_task(target={"index": index, "name": "build/target.tar.xz"}),
    )
    symbols = "/public/build/target.crashreporter-symbols.zip"
    assert task["fetches"]["indexed-installer"] == [
        {"artifact": "/public/build/target.tar.xz", "extract": False},
        {"artifact": symbols, "dest": "symbols"},
    ]
    assert not any(f.get("dest") == "symbols" for f in task["fetches"]["build"])
    assert task["worker"]["env"]["MOZ_SYMBOLS_URL"] == {
        "artifact-reference": f"<indexed-installer{symbols}>"
    }


def test_add_index_search_tasks(run_transform):
    label = "fake-kind-index-search-mobile.v2.reference-browser"
    jobs = [
        {
            "label": "test-a",
            "dependencies": {"build": "build-a", "indexed-installer": label},
        },
        {
            "label": "test-b",
            "dependencies": {"build": "build-b", "indexed-installer": label},
        },
        {"label": "test-c", "dependencies": {"build": "build-c"}},
    ]
    out = list(run_transform(test_transforms.add_index_search_tasks, jobs))
    assert [job["label"] for job in out] == ["test-a", label, "test-b", "test-c"]
    assert out[1] == {
        "label": label,
        "description": "Task found through the mobile.v2.reference-browser index",
        "worker-type": "invalid/always-optimized",
        "run": {"using": "always-optimized"},
        "optimization": {"index-search": ["mobile.v2.reference-browser"]},
    }


def test_build_fetches_without_symbols(run_transform, build_fetches_task):
    task = run_build_fetches(
        run_transform,
        build_fetches_task(test_platform="linux64-asan/opt", download_symbols=None),
    )
    assert not any(f.get("dest") == "symbols" for f in task["fetches"]["build"])
    assert "worker" not in task


def test_perfherder_extra_options(run_transform, make_test_task):
    task = make_test_task(
        attributes={"unittest_suite": "mochitest-plain", "unittest_variant": "xorig"}
    )
    task = list(run_transform(other.set_perfherder_extra_options, task))[0]
    assert task["worker"]["env"]["PERFHERDER_EXTRA_OPTIONS"] == "mochitest-plain xorig"

    task = make_test_task(attributes={"unittest_suite": "xpcshell"})
    task = list(run_transform(other.set_perfherder_extra_options, task))[0]
    assert task["worker"]["env"]["PERFHERDER_EXTRA_OPTIONS"] == "xpcshell"


if __name__ == "__main__":
    mozunit.main()
