# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import json
import unittest
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp
from unittest import mock

import mozunit
from mach.registrar import Registrar

from mozbuild import rust_commands


def _spec(**overrides):
    spec = {
        "kind": "library",
        "subcommand": "rustc",
        "manifest_path": "/src/toolkit/library/rust/Cargo.toml",
        "target_triple": "x86_64-unknown-linux-gnu",
        "working_directory": "/obj/toolkit/library/rust",
        "features": ["gkrust-feature"],
    }
    spec.update(overrides)
    return spec


class TestRunCargoCommand(unittest.TestCase):
    def setUp(self):
        self._categories = []
        for cat in ("build", "post-build", "misc", "testing", "devenv", "build-dev"):
            if cat in Registrar.categories:
                continue
            Registrar.register_category(cat, cat, cat)
            self._categories.append(cat)
        self.tmpdir = Path(mkdtemp())

    def tearDown(self):
        rmtree(self.tmpdir)
        for cat in self._categories:
            del Registrar.categories[cat]
            del Registrar.commands_by_category[cat]

    def _run(
        self,
        kind="library",
        cargo_command="check",
        subcommand_args=None,
        cargo_build_flags=None,
        cargo_extra_flags=None,
        jobs=0,
        continue_on_error=False,
        returncode=0,
        write_spec=True,
        spec_overrides=None,
        verbose=False,
        composed_argv=None,
    ):
        from mozbuild.mach_commands import _run_cargo_command

        directory = "toolkit/library/rust"
        if write_spec:
            spec_dir = self.tmpdir / directory
            spec_dir.mkdir(parents=True, exist_ok=True)
            (spec_dir / rust_commands.CARGO_SPEC_FILES[kind]).write_text(
                json.dumps(_spec(kind=kind, **(spec_overrides or {}))),
                encoding="utf-8",
            )

        command_context = mock.Mock(
            substs={"CARGO": "cargo", "RUST_TARGET": "x86_64-unknown-linux-gnu"},
            topsrcdir="/src",
            topobjdir=str(self.tmpdir),
        )

        seen = {}

        def fake_argv(command, substs, env, subcommand, **kwargs):
            seen["command"] = command
            seen["subcommand"] = subcommand
            seen.update(kwargs)
            if composed_argv is not None:
                return composed_argv
            return ["cargo", subcommand]

        def fake_env(command, substs, current_env, topsrcdir, topobjdir):
            seen["current_env"] = current_env
            return {"COMPOSED_ENV": "1"}

        def fake_run(argv, env=None, cwd=None, check=None):
            seen["run_argv"] = argv
            seen["run_env"] = env
            seen["run_cwd"] = cwd
            return mock.Mock(returncode=returncode)

        with mock.patch.object(
            rust_commands, "compose_env", side_effect=fake_env
        ), mock.patch.object(
            rust_commands, "compose_mach_cargo_argv", side_effect=fake_argv
        ), mock.patch("subprocess.run", side_effect=fake_run):
            rc = _run_cargo_command(
                command_context,
                "gkrust",
                {"directory": directory, "kind": kind},
                directory,
                cargo_command,
                subcommand_args,
                cargo_build_flags,
                cargo_extra_flags,
                False,
                continue_on_error,
                jobs,
                verbose,
            )
        return rc, seen

    def test_reads_library_spec(self):
        rc, seen = self._run(kind="library")
        self.assertEqual(rc, 0)
        self.assertEqual(seen["command"].kind, "library")
        self.assertEqual(seen["subcommand"], "check")

    def test_reads_program_spec(self):
        rc, seen = self._run(
            kind="program",
            cargo_command="clippy",
            spec_overrides={"cargo_subcommand_args": ["--bin", "geckodriver"]},
        )
        self.assertEqual(rc, 0)
        self.assertEqual(seen["command"].kind, "program")

    def test_reads_host_program_spec(self):
        rc, seen = self._run(
            kind="host-program",
            spec_overrides={"cargo_subcommand_args": ["--bin", "geckodriver"]},
        )
        self.assertEqual(rc, 0)
        self.assertEqual(seen["command"].kind, "host-program")

    def test_missing_spec_returns_error(self):
        rc, _ = self._run(write_spec=False)
        self.assertEqual(rc, 1)

    def test_jobs_forwarded(self):
        _, seen = self._run(jobs=3)
        self.assertEqual(seen["jobs"], 3)

    def test_failure_propagates(self):
        rc, _ = self._run(returncode=17)
        self.assertEqual(rc, 17)

    def test_continue_on_error_swallows_failure(self):
        rc, _ = self._run(returncode=17, continue_on_error=True)
        self.assertEqual(rc, 0)

    def test_missing_plugin_suggestion(self):
        with mock.patch("builtins.print") as printed:
            rc, _ = self._run(cargo_command="clippy", returncode=101)
        self.assertEqual(rc, 101)
        joined = " ".join(str(c.args[0]) for c in printed.call_args_list if c.args)
        self.assertIn("cargo install cargo-clippy", joined)

    def test_subcommand_args_substituted(self):
        _, seen = self._run(subcommand_args="-- {crate} {arch}")
        self.assertEqual(
            seen["extra_cli_flags"],
            ("--", "gkrust", "x86_64-unknown-linux-gnu"),
        )

    def test_build_flags_override_sets_no_auto_arg(self):
        _, seen = self._run(cargo_build_flags="check --manifest-path {manifest}")
        self.assertTrue(seen["no_auto_arg"])
        self.assertIn("check", seen["build_flags_override"])
        self.assertIn(
            "/src/toolkit/library/rust/Cargo.toml", seen["build_flags_override"]
        )
        self.assertFalse(seen["command"].uses_ltoable_rustflags)

    def test_ltoable_set_without_build_flags(self):
        _, seen = self._run()
        self.assertTrue(seen["command"].uses_ltoable_rustflags)
        self.assertFalse(seen["no_auto_arg"])

    def test_cargo_extra_flags_reach_env(self):
        _, seen = self._run(cargo_extra_flags="--offline {crate}")
        self.assertEqual(seen["current_env"]["CARGO_EXTRA_FLAGS"], "--offline gkrust")

    def test_verbose_prints_quoted_command(self):
        with mock.patch("builtins.print") as printed:
            self._run(verbose=True, composed_argv=["cargo", "check", "a b"])
        joined = " ".join(str(c.args[0]) for c in printed.call_args_list if c.args)
        self.assertIn("cargo check 'a b'", joined)

    def test_verbose_sets_build_verbose_log(self):
        _, seen = self._run(verbose=True)
        self.assertEqual(seen["current_env"]["BUILD_VERBOSE_LOG"], "1")

    def test_non_verbose_omits_build_verbose_log(self):
        _, seen = self._run()
        self.assertNotIn("BUILD_VERBOSE_LOG", seen["current_env"])

    def test_passes_composed_argv_env_and_cwd_to_subprocess(self):
        _, seen = self._run()
        self.assertEqual(seen["run_argv"], ["cargo", "check"])
        self.assertEqual(seen["run_env"], {"COMPOSED_ENV": "1"})
        self.assertEqual(seen["run_cwd"], "/obj/toolkit/library/rust")

    def _run_cargo(self, substs):
        from buildconfig import topsrcdir

        from mozbuild import mach_commands

        command_context = mock.Mock(
            substs=substs,
            topsrcdir=topsrcdir,
            topobjdir=str(self.tmpdir),
        )
        command_context.resolve_num_jobs.return_value = 1
        command_context._spawn.return_value.build.return_value = 0
        command_context._spawn.return_value.configure.return_value = 0
        command_context._run_make.return_value = 0
        with mock.patch.object(
            mach_commands, "_run_cargo_command", return_value=0
        ) as executor:
            rc = mach_commands.cargo(command_context, "check")
        return rc, command_context, executor

    def test_nonlegacy_dispatches_to_executor(self):
        rc, cc, executor = self._run_cargo({})
        self.assertEqual(rc, 0)
        executor.assert_called()
        cc.ensure_backend_current.assert_called_once()
        cc._run_make.assert_not_called()

    def test_legacy_dispatches_to_run_make(self):
        rc, cc, executor = self._run_cargo({"MOZ_USE_LEGACY_CARGO_INVOCATION": True})
        self.assertEqual(rc, 0)
        cc._run_make.assert_called()
        executor.assert_not_called()
        cc.ensure_backend_current.assert_not_called()


if __name__ == "__main__":
    mozunit.main()
