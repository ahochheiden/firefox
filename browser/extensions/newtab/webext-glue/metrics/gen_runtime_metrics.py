# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Generate New Tab's runtime Glean metrics/pings JSON for train-hopping.

Metrics and pings that exist in Nightly but not yet in an older Firefox can't be
compiled into the build that an XPI train-hops onto, so they are registered at
runtime from this payload. It holds every metric and ping in New Tab's local
metrics.yaml and pings.yaml, and registration skips the ones the host build
already has compiled in.

This is the build-time entry point for the GENERATED_FILES rules in ../moz.build;
`mach newtab channel-metrics-diff` writes the diff against an older version for
inspection.
"""

import io
import sys
import tempfile
from pathlib import Path
from urllib.request import urlopen

import yaml

HERE = Path(__file__).resolve().parent
TOPSRCDIR = HERE.parents[4]

sys.path.append(
    str(
        TOPSRCDIR
        / "toolkit"
        / "components"
        / "glean"
        / "build_scripts"
        / "glean_parser_ext"
    )
)
sys.path.append(str(HERE))

import glean_utils
from run_glean_parser import parse_with_options

# Diffs from `mach newtab channel-metrics-diff` are pinned to
# FIREFOX_BETA_<version>_BASE, the tag created when that version branched from
# central. Every build of the version - its betas, the RC,
# the release and its dot releases - descends from that tag, so the diff is a
# superset of whatever any of them is missing, and registration skips what is
# already compiled in. Branch heads are deliberately not used: refs/heads/beta
# and refs/heads/release move days before the builds in the field do, so a
# payload diffed against them can disagree with the version in its name.
VERSION_YAML_URL_TEMPLATE = (
    "https://raw.githubusercontent.com/mozilla-firefox/firefox/"
    "refs/tags/FIREFOX_BETA_{version}_BASE/browser/components/newtab/{yaml}"
)


def fetch_version_yaml(version, yaml_name):
    """Fetch New Tab's metrics.yaml or pings.yaml as of Firefox `version`."""
    url = VERSION_YAML_URL_TEMPLATE.format(version=version, yaml=yaml_name)
    with urlopen(url) as response:
        return yaml.safe_load(response.read())


def get_new_metrics(main_yaml, compare_yaml):
    """Return categories/metrics present in main_yaml but not compare_yaml.

    main_yaml is New Tab's local (Nightly) definitions; compare_yaml is the
    target channel's (Beta/Release). The result is what needs runtime
    registration on that channel.
    """
    new_metrics_yaml = {}
    for category in main_yaml:
        if category.startswith("$"):
            new_metrics_yaml[category] = main_yaml[category]
            continue
        if category not in compare_yaml:
            new_metrics_yaml[category] = main_yaml[category]
            continue
        new_metrics = {}
        for metric in main_yaml[category]:
            if metric not in compare_yaml[category]:
                new_metrics[metric] = main_yaml[category][metric]
        if new_metrics:
            new_metrics_yaml[category] = new_metrics
    return new_metrics_yaml


def get_new_pings(main_yaml, compare_yaml):
    """Return pings present in main_yaml but not compare_yaml (see get_new_metrics)."""
    new_pings_yaml = {}
    for ping in main_yaml:
        if ping.startswith("$"):
            new_pings_yaml[ping] = main_yaml[ping]
            continue
        if ping not in compare_yaml:
            new_pings_yaml[ping] = main_yaml[ping]
    return new_pings_yaml


def _write_yaml(path, definitions):
    # $tags is stripped to avoid an invalid-tag lint error when re-parsing.
    if "$tags" in definitions:
        del definitions["$tags"]
    path.write_text(yaml.dump(definitions, sort_keys=False))


def _render_runtime_metrics(metrics_yaml, pings_yaml):
    with open(metrics_yaml) as f:
        main_metrics = yaml.safe_load(f)
    with open(pings_yaml) as f:
        main_pings = yaml.safe_load(f)

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_dir = Path(temp_dir)
        metrics_path = temp_dir / "newtab_metrics.yaml"
        pings_path = temp_dir / "newtab_pings.yaml"
        _write_yaml(metrics_path, main_metrics)
        _write_yaml(pings_path, main_pings)

        all_objs, options = parse_with_options(
            [metrics_path, pings_path], {"allow_reserved": False}
        )
        buffer = io.StringIO()
        glean_utils.output_file_with_key(all_objs, buffer, options)
        return buffer.getvalue()


def generate(output, metrics_yaml, pings_yaml):
    """GENERATED_FILES entry point: write the runtime metrics JSON to `output`.

    The build system calls this with the target file, then the declared inputs,
    New Tab's local metrics.yaml and pings.yaml.
    """
    output.write(_render_runtime_metrics(metrics_yaml, pings_yaml))
