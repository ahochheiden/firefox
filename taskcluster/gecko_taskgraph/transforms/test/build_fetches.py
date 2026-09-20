# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""
Deliver the installer, the test archives and the crash reporter symbols of
the build under test through fetches.
"""

import functools
import importlib.util
import os
from types import SimpleNamespace

from taskgraph.transforms.base import TransformSequence
from taskgraph.util.taskcluster import get_artifact_path

from gecko_taskgraph import GECKO
from gecko_taskgraph.transforms.job.mozharness_test import get_variant
from gecko_taskgraph.util.perftest import is_external_browser

transforms = TransformSequence()

TESTS_DEST = "tests"
SYMBOLS_DEST = "symbols"
COMMON_ARCHIVE = "target.common.tests.tar.zst"
JSSHELL_ARCHIVE = "target.jsshell.zip"
SYMBOLS_ARCHIVE = "target.crashreporter-symbols.zip"
MULTI_LOCALE_VARIANTS = (
    "shippable",
    "shippable-qr",
    "shippable-lite",
    "shippable-lite-qr",
)

SUITE_CATEGORY_ALIASES = {
    "mochitest-chrome": "mochitest",
    "mochitest-media": "mochitest",
    "mochitest-plain": "mochitest",
    "mochitest-plain-gpu": "mochitest",
    "mochitest-webgl1-core": "mochitest",
    "mochitest-webgl1-ext": "mochitest",
    "mochitest-webgl2-core": "mochitest",
    "mochitest-webgl2-ext": "mochitest",
    "mochitest-webgl2-deqp": "mochitest",
    "mochitest-webgpu": "mochitest",
    "geckoview": "mochitest",
    "geckoview-junit": "mochitest",
    "reftest-qr": "reftest",
    "crashtest": "reftest",
    "crashtest-qr": "reftest",
    "reftest-debug": "reftest",
    "crashtest-debug": "reftest",
}

PER_TEST_CATEGORIES = ["mochitest", "reftest", "xpcshell"]
SCRIPT_CATEGORIES = {
    "web_platform_tests.py": ["web-platform"],
    "awsy_script.py": ["common", "awsy"],
    "raptor_script.py": ["common", "condprof", "raptor"],
    "talos_script.py": ["common", "talos"],
}
UNITTEST_SCRIPTS = {
    "desktop_unittest.py": "category",
    "devtools_compat.py": "category",
    "android_emulator_unittest.py": "suite",
    "android_hardware_unittest.py": "suite",
}


@functools.cache
def test_packages():
    path = os.path.join(GECKO, "build", "gen_test_packages_manifest.py")
    spec = importlib.util.spec_from_file_location("gen_test_packages_manifest", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    harnesses = module.PACKAGE_SPECIFIED_HARNESSES + module.OPTIONAL_PACKAGES
    args = SimpleNamespace(
        tests_common=COMMON_ARCHIVE,
        jsshell=JSSHELL_ARCHIVE,
        **{harness: f"target.{harness}.tests.tar.zst" for harness in harnesses},
    )
    return module.generate_package_data(args)


def suite_categories(task):
    script = task["mozharness"]["script"]
    if script in SCRIPT_CATEGORIES:
        return SCRIPT_CATEGORIES[script]
    if script not in UNITTEST_SCRIPTS:
        return ["common"]
    suite = task["attributes"]["unittest_suite"]
    if suite.startswith(("test-verify", "test-coverage")):
        return PER_TEST_CATEGORIES
    if UNITTEST_SCRIPTS[script] == "suite" or suite in test_packages():
        return [suite]
    return [task["attributes"]["unittest_category"]]


def test_archives(task):
    packages = test_packages()
    archives = set()
    for category in suite_categories(task):
        harness = SUITE_CATEGORY_ALIASES.get(category, category)
        archives.update(packages.get(harness, packages["common"]))
    return sorted(archives)


INDEX_DEPENDENCY = "indexed-installer"
INSTALLER_SUFFIXES = (
    ".apk",
    ".tar.bz2",
    ".tar.xz",
    ".tar.gz",
    ".dmg",
    ".installer-stub.exe",
    ".installer.exe",
    ".exe",
    ".zip",
)


def index_search_label(kind, index):
    return f"{kind}-index-search-{index}"


def symbols_next_to_installer(path):
    for suffix in INSTALLER_SUFFIXES:
        if path.endswith(suffix):
            return path[: -len(suffix)] + ".crashreporter-symbols.zip"
    return path + ".crashreporter-symbols.zip"


def installer(config, task):
    target = task.get("target")
    if isinstance(target, dict):
        if "index" in target:
            label = index_search_label(config.kind, target["index"])
            task.setdefault("dependencies", {})[INDEX_DEPENDENCY] = label
            return INDEX_DEPENDENCY, f"public/{target['name']}"
        return target["upstream-task"], target["name"]
    if task["mozharness"]["requires-signed-builds"]:
        return "build-signing", task["mozharness"]["build-artifact-name"]
    return "build", task["mozharness"]["build-artifact-name"]


def build_artifact_dir(task):
    test_platform = task["test-platform"]
    if (
        "android" in test_platform
        and get_variant(test_platform) in MULTI_LOCALE_VARIANTS
        and not is_external_browser(task.get("try-name", ""))
    ):
        return "en-US/"
    return ""


@transforms.add
def add_build_fetches(config, tasks):
    for task in tasks:
        if not task["use-build-fetches"]:
            yield task
            continue

        upstream, path = installer(config, task)
        fetches = task.setdefault("fetches", {})
        fetches.setdefault(upstream, []).append({
            "artifact": f"/{path}",
            "extract": False,
        })

        artifact_dir = build_artifact_dir(task)
        build_fetches = fetches.setdefault("build", [])
        for archive in test_archives(task):
            build_fetches.append({
                "artifact": f"/{get_artifact_path(task, artifact_dir + archive)}",
                "dest": (
                    f"{TESTS_DEST}/bin" if archive == JSSHELL_ARCHIVE else TESTS_DEST
                ),
            })

        env = task.setdefault("worker", {}).setdefault("env", {})
        env["MOZHARNESS_USE_FETCHES"] = "1"

        if upstream == INDEX_DEPENDENCY:
            symbols_source = INDEX_DEPENDENCY
            symbols = f"/{symbols_next_to_installer(path)}"
        else:
            symbols_source = "build"
            symbols = f"/{get_artifact_path(task, artifact_dir + SYMBOLS_ARCHIVE)}"
        download_symbols = task["mozharness"].get("download-symbols")
        if download_symbols:
            env["MOZ_SYMBOLS_URL"] = {
                "artifact-reference": f"<{symbols_source}{symbols}>"
            }
        if download_symbols is True:
            fetches["build"] = [
                fetch
                for fetch in fetches["build"]
                if fetch["artifact"] != SYMBOLS_ARCHIVE
            ]
            fetches.setdefault(symbols_source, []).append({
                "artifact": symbols,
                "dest": SYMBOLS_DEST,
            })

        yield task
