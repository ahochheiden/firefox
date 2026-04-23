# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Ingest cargo's `--unit-graph` for a Rust edge into the unit graph the Ninja
backend turns into explicit per-crate rustc edges.

We build the same cargo argv the edge would normally run (via
`_rust_env.compose_argv`, so features/profile/target match exactly), turn it
into a `--unit-graph` dump, run it under `RUSTC_BOOTSTRAP=1` (cargo's `-Z`
gate), and parse the JSON. This runs at backend-generation time, once per edge.
"""

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mozfile import json

from mozbuild.action._rust_env import compose_argv, compose_env


def _to_unit_graph_argv(argv):
    """Turn a `cargo rustc --timings <...> -- <rustcflags>` argv into a
    `cargo build --unit-graph -Z unstable-options <...>` dump: use the `build`
    subcommand (which accepts `--unit-graph`, unlike the single-target `rustc`
    passthrough), drop `--timings` and the trailing `-- <rustcflags>` (which
    `--unit-graph` ignores), and keep the selection flags
    (--lib/--bin/--target/--features/--frozen)."""
    out = [argv[0], "build", "--unit-graph", "-Z", "unstable-options"]
    for a in argv[2:]:
        if a == "--timings":
            continue
        if a == "--":
            break
        out.append(a)
    return out


def vendored_cargo_config(topsrcdir, topobjdir):
    """Write an absolute-path copy of `.cargo/config.toml.in` (the vendored
    source-replacement config) into the objdir and return its path.

    The build's own `$topobjdir/.cargo/config.toml` is only generated at build
    time, so at backend-generation time the unit-graph call has no source
    replacement and would try to fetch git deps (which `--frozen` forbids).
    config.toml.in is usable as-is; we only rewrite the vendored directory to
    an absolute path so it does not depend on cargo's --config cwd resolution."""
    src = Path(topsrcdir) / ".cargo" / "config.toml.in"
    if not src.exists():
        return None
    content = src.read_text(encoding="utf-8")
    vendored = (Path(topsrcdir) / "third_party" / "rust").as_posix()
    content = content.replace(
        'directory = "third_party/rust"', f'directory = "{vendored}"'
    )
    out = Path(topobjdir) / ".cargo-unit-graph-config.toml"
    with out.open("w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    return out.as_posix()


def unit_graph_for_spec(
    spec, substs, topsrcdir, topobjdir, cargo_home=None, config_path=None
):
    """Run `cargo ... --unit-graph` for one edge's cargo `spec` and return the
    parsed JSON graph. When `cargo_home` is set it is used as CARGO_HOME, so
    concurrent calls don't serialize on cargo's shared `.package-cache` lock.
    When `config_path` is None the vendored source-replacement config is written
    here; callers running many in parallel pass a shared pre-written one to
    avoid racing on that file. Raises if the call fails or the schema version is
    not 1 (the version tripwire: a bump means cargo changed the format and this
    parser needs updating)."""
    env = compose_env(spec, substs, dict(os.environ), topsrcdir, topobjdir)
    env["RUSTC_BOOTSTRAP"] = "1"
    if cargo_home:
        Path(cargo_home).mkdir(parents=True, exist_ok=True)
        env["CARGO_HOME"] = cargo_home
    argv = _to_unit_graph_argv(compose_argv(spec, substs, env))
    if config_path is None:
        config_path = vendored_cargo_config(topsrcdir, topobjdir)
    if config_path:
        argv += ["--config", config_path]
    proc = subprocess.run(
        argv, env=env, cwd=topsrcdir, capture_output=True, check=False
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace")
        raise RuntimeError(
            f"cargo --unit-graph failed for {spec.get('manifest_path')}:\n{stderr}"
        )
    graph = json.loads(proc.stdout.decode("utf-8"))
    version = graph.get("version")
    if version != 1:
        raise RuntimeError(
            f"cargo --unit-graph version {version!r} is not the supported 1; the "
            "schema changed and the explicit-rustc-edges parser needs updating"
        )
    return graph


def unit_graphs_for_specs(specs, substs, topsrcdir, topobjdir, config_path=None):
    """Run `cargo --unit-graph` for each edge spec concurrently -- each with its
    own CARGO_HOME so they don't serialize on cargo's package-cache lock -- and
    return the parsed graphs in `specs` order. Turns N serial ~6s calls into
    roughly one call's wall time. Cargo stays the resolver, so there is no
    correctness risk versus running them one at a time. A caller running this
    alongside other cargo work (e.g. a metadata call) passes a pre-written
    `config_path` so they don't race to write the shared vendored config."""
    if not specs:
        return []
    if config_path is None:
        config_path = vendored_cargo_config(topsrcdir, topobjdir)
    homes = os.path.join(topobjdir, "rust-edges", "cargo-homes")

    def run(i):
        return unit_graph_for_spec(
            specs[i],
            substs,
            topsrcdir,
            topobjdir,
            cargo_home=os.path.join(homes, str(i)),
            config_path=config_path,
        )

    with ThreadPoolExecutor(max_workers=min(len(specs), os.cpu_count() or 4)) as ex:
        return list(ex.map(run, range(len(specs))))
