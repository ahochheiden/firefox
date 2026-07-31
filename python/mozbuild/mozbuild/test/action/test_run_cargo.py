# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import json
import sys
import unittest
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp
from unittest import mock

import mozunit

from mozbuild.action import run_cargo


class TestRunCargo(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(mkdtemp())

    def tearDown(self):
        rmtree(self.tmpdir)

    def _spec(self, **overrides):
        spec = {
            "kind": "library",
            "subcommand": "rustc",
            "manifest_path": "/src/Cargo.toml",
        }
        spec.update(overrides)
        path = self.tmpdir / "spec.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        return str(path)

    def _run(self, argv, cargo_argv=None, env=None, returncode=0):
        cargo_argv = ["cargo", "rustc"] if cargo_argv is None else cargo_argv
        env = {"E": "1"} if env is None else env
        buildconfig = mock.Mock(substs={}, topsrcdir="/src", topobjdir="/obj")
        seen = {}

        def fake_run(argv_, env=None, cwd=None, close_fds=None, check=None):
            seen.update(argv=argv_, env=env, cwd=cwd, close_fds=close_fds)
            return mock.Mock(returncode=returncode)

        with mock.patch.dict(
            sys.modules, {"buildconfig": buildconfig}
        ), mock.patch.object(
            run_cargo, "compose_env", return_value=env
        ), mock.patch.object(
            run_cargo, "compose_cargo_build_edge_argv", return_value=cargo_argv
        ) as compose, mock.patch.object(
            run_cargo.subprocess, "run", side_effect=fake_run
        ):
            rc = run_cargo.main(argv)
        return rc, compose, seen

    def test_runs_the_composed_command(self):
        spec = self._spec(working_directory="/obj/toolkit/library/rust")
        rc, _, seen = self._run(["--spec", spec])
        self.assertEqual(rc, 0)
        self.assertEqual(seen["argv"], ["cargo", "rustc"])
        self.assertEqual(seen["env"], {"E": "1"})
        self.assertEqual(seen["cwd"], "/obj/toolkit/library/rust")
        self.assertFalse(seen["close_fds"])

    def test_composes_from_the_spec_and_buildconfig(self):
        spec = self._spec(
            kind="program", cargo_subcommand_args=["--bin", "geckodriver"]
        )
        _, compose, _ = self._run(["--spec", spec])
        command = compose.call_args[0][0]
        self.assertEqual(command.kind, "program")
        self.assertEqual(command.cargo_subcommand_args, ["--bin", "geckodriver"])

    def test_forwards_runtime_signals(self):
        spec = self._spec()
        _, compose, _ = self._run([
            "--spec",
            spec,
            "--single-job",
            "--timings",
            "--keep-going",
        ])
        self.assertEqual(compose.call_args.kwargs["single_job"], True)
        self.assertEqual(compose.call_args.kwargs["timings"], True)
        self.assertEqual(compose.call_args.kwargs["keep_going"], True)

    def test_runtime_signals_default_off(self):
        spec = self._spec()
        _, compose, _ = self._run(["--spec", spec])
        self.assertEqual(compose.call_args.kwargs["single_job"], False)
        self.assertEqual(compose.call_args.kwargs["timings"], False)
        self.assertEqual(compose.call_args.kwargs["keep_going"], False)

    def test_empty_working_directory_means_inherit_cwd(self):
        rc, _, seen = self._run(["--spec", self._spec()])
        self.assertIsNone(seen["cwd"])

    def test_returncode_propagates(self):
        rc, _, _ = self._run(["--spec", self._spec()], returncode=17)
        self.assertEqual(rc, 17)

    def test_env_rustflags_not_recomposed(self):
        from mozbuild.backend.configenvironment import PartialConfigEnvironment

        substs_dir = self.tmpdir / "config.statusd" / "substs"
        substs_dir.mkdir(parents=True)
        configured = {
            "MOZ_RUST_DEFAULT_FLAGS": "-C debuginfo=2 -Dwarnings",
            "RUST_SANCOV_FLAGS": "-Cpasses=sancov-module -Cllvm-args=-x",
            "RUSTFLAGS": "",
            "RUST_PGO_FLAGS": "",
            "CARGO": "cargo",
            "RUST_TARGET": "i686-unknown-linux-gnu",
            "RUST_HOST_TARGET": "x86_64-unknown-linux-gnu",
            "OS_ARCH": "Linux",
        }
        for key, value in configured.items():
            (substs_dir / key).write_text(json.dumps(value), encoding="utf-8")

        spec = self._spec(uses_ltoable_rustflags=True)
        composed = (
            "-C debuginfo=2 -Dwarnings -Cpasses=sancov-module "
            "-Cllvm-args=-x -C codegen-units=1"
        )
        buildconfig = mock.Mock(
            substs=PartialConfigEnvironment(str(self.tmpdir)).substs,
            topsrcdir="/src",
            topobjdir=str(self.tmpdir),
        )
        seen = {}

        def fake_run(argv_, env=None, cwd=None, close_fds=None, check=None):
            seen["env"] = env
            return mock.Mock(returncode=0)

        with mock.patch.dict(
            sys.modules, {"buildconfig": buildconfig}
        ), mock.patch.dict(
            run_cargo.os.environ, {"RUSTFLAGS": composed}, clear=False
        ), mock.patch.object(run_cargo.subprocess, "run", side_effect=fake_run):
            rc = run_cargo.main(["--spec", spec])

        self.assertEqual(rc, 0)
        rustflags = seen["env"]["RUSTFLAGS"]
        self.assertEqual(rustflags.count("-Cpasses=sancov-module"), 1)
        self.assertEqual(rustflags.count("codegen-units=1"), 1)


if __name__ == "__main__":
    mozunit.main()
