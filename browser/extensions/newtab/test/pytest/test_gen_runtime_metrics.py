# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import io
import json
import sys
from pathlib import Path

import mozunit
import yaml

TOPSRCDIR = Path(__file__).resolve().parents[5]
sys.path.append(
    str(TOPSRCDIR / "browser" / "extensions" / "newtab" / "webext-glue" / "metrics")
)
import gen_runtime_metrics

NEWTAB_COMPONENT_PATH = TOPSRCDIR / "browser" / "components" / "newtab"


def load_names(yaml_path):
    with open(yaml_path, encoding="utf-8") as f:
        return {key: value for key, value in yaml.safe_load(f).items() if key[0] != "$"}


def test_runtime_metrics_all_definitions():
    metrics_yaml = NEWTAB_COMPONENT_PATH / "metrics.yaml"
    pings_yaml = NEWTAB_COMPONENT_PATH / "pings.yaml"
    output = io.StringIO()

    gen_runtime_metrics.generate(output, str(metrics_yaml), str(pings_yaml))

    payload = json.loads(output.getvalue())
    assert {
        category: sorted(metrics) for category, metrics in payload["metrics"].items()
    } == {
        category: sorted(metrics)
        for category, metrics in load_names(metrics_yaml).items()
    }
    assert sorted(payload["pings"]) == sorted(load_names(pings_yaml))


if __name__ == "__main__":
    mozunit.main()
