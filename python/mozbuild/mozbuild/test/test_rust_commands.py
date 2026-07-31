# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import unittest

import mozunit

from mozbuild.rust_commands import (
    CargoCommand,
    applies_library_lto,
    compose_cargo_build_edge_argv,
    compose_env,
    compose_mach_cargo_argv,
    compose_rustflags,
)


def _cmd(**kw):
    kw.setdefault("kind", "library")
    kw.setdefault("subcommand", "rustc")
    kw.setdefault("manifest_path", "/src/toolkit/library/rust/Cargo.toml")
    kw.setdefault("target_triple", "x86_64-unknown-linux-gnu")
    return CargoCommand(**kw)


class TestFromDict(unittest.TestCase):
    def test_roundtrip(self):
        cmd = CargoCommand.from_dict({
            "kind": "library",
            "subcommand": "rustc",
            "manifest_path": "/x/Cargo.toml",
            "features": ["a", "b"],
        })
        self.assertEqual(cmd.kind, "library")
        self.assertEqual(cmd.features, ["a", "b"])

    def test_unknown_key_rejected(self):
        with self.assertRaises(ValueError):
            CargoCommand.from_dict({
                "kind": "library",
                "subcommand": "rustc",
                "manifest_path": "x",
                "bogus": 1,
            })

    def test_bad_kind_rejected(self):
        with self.assertRaises(ValueError):
            CargoCommand.from_dict({
                "kind": "nope",
                "subcommand": "rustc",
                "manifest_path": "x",
            })

    def test_bad_subcommand_rejected(self):
        with self.assertRaises(ValueError):
            CargoCommand.from_dict({
                "kind": "library",
                "subcommand": "nope",
                "manifest_path": "x",
            })


class TestAppliesLibraryLto(unittest.TestCase):
    def test_release_library(self):
        self.assertTrue(applies_library_lto("library", False, {}))

    def test_gkrust_gtest(self):
        self.assertFalse(applies_library_lto("library", True, {}))

    def test_developer_options(self):
        self.assertFalse(
            applies_library_lto("library", False, {"DEVELOPER_OPTIONS": "1"})
        )

    def test_code_coverage(self):
        self.assertFalse(
            applies_library_lto("library", False, {"MOZ_CODE_COVERAGE": "1"})
        )

    def test_lto_rust_cross(self):
        self.assertFalse(
            applies_library_lto("library", False, {"MOZ_LTO_RUST_CROSS": "cross"})
        )

    def test_sancov(self):
        self.assertFalse(
            applies_library_lto("library", False, {"RUST_SANCOV_FLAGS": "-x"})
        )

    def test_host_library(self):
        self.assertFalse(applies_library_lto("host-library", False, {}))

    def test_program(self):
        self.assertFalse(applies_library_lto("program", False, {}))


class TestComposeArgv(unittest.TestCase):
    def test_library(self):
        substs = {"CARGO": "cargo"}
        cmd = _cmd(features=("gkrust-shared/foo",))
        argv = compose_cargo_build_edge_argv(cmd, substs, {})
        self.assertEqual(argv[:2], ["cargo", "rustc"])
        self.assertIn("--lib", argv)
        self.assertIn("--target=x86_64-unknown-linux-gnu", argv)
        i = argv.index("--features")
        self.assertEqual(
            argv[i + 1], "gkrust-shared/foo,mozilla-central-workspace-hack"
        )
        self.assertEqual(argv[argv.index("--") + 1 :], ["-Clto"])

    def test_features_empty(self):
        argv = compose_cargo_build_edge_argv(_cmd(features=()), {"CARGO": "cargo"}, {})
        self.assertEqual(
            argv[argv.index("--features") + 1], "mozilla-central-workspace-hack"
        )

    def test_host_library_targets_host_and_has_no_rustc_tail(self):
        substs = {"CARGO": "cargo", "RUST_HOST_TARGET": "x86_64-pc-windows-msvc"}
        argv = compose_cargo_build_edge_argv(_cmd(kind="host-library"), substs, {})
        self.assertIn("--target=x86_64-pc-windows-msvc", argv)
        self.assertNotIn("--", argv)

    def test_test_subcommand_has_no_rustc_tail(self):
        cmd = _cmd(
            kind="test",
            subcommand="test",
            cargo_subcommand_args=("--no-fail-fast", "-p", "style"),
        )
        argv = compose_cargo_build_edge_argv(cmd, {"CARGO": "cargo"}, {})
        self.assertEqual(argv[1], "test")
        self.assertNotIn("--", argv)
        for arg in ("--no-fail-fast", "-p", "style"):
            self.assertIn(arg, argv)

    def test_program_bin_resfile_and_mingw_tail(self):
        substs = {"CARGO": "cargo", "OS_ARCH": "WINNT", "CC_TYPE": "clang"}
        cmd = _cmd(
            kind="program",
            target_triple="x86_64-pc-windows-gnu",
            cargo_subcommand_args=("--bin", "nmhproxy"),
            extra_rustcflags=("-C", "link-arg=/obj/module.res"),
        )
        argv = compose_cargo_build_edge_argv(cmd, substs, {})
        self.assertEqual(argv[argv.index("--bin") + 1], "nmhproxy")
        self.assertEqual(
            argv[argv.index("--") + 1 :],
            ["-C", "link-arg=/obj/module.res", "-C", "default-linker-libraries=yes"],
        )

    def test_cargoflags_come_from_env_not_substs(self):
        substs = {"CARGO": "cargo", "CARGOFLAGS": "--ignored"}
        argv = compose_cargo_build_edge_argv(
            _cmd(), substs, {"CARGOFLAGS": "--offline"}
        )
        self.assertIn("--offline", argv)
        self.assertNotIn("--ignored", argv)

    def test_cargoflags_honor_shell_quoting(self):
        argv = compose_cargo_build_edge_argv(
            _cmd(), {"CARGO": "cargo"}, {"CARGOFLAGS": "--config 'k = \"v\"'"}
        )
        self.assertEqual(argv[argv.index("--config") + 1], 'k = "v"')

    def test_forced_j1_wins_over_cargoflags_j(self):
        # The explicit single-job override must follow and override CARGOFLAGS.
        argv = compose_cargo_build_edge_argv(
            _cmd(), {"CARGO": "cargo"}, {"CARGOFLAGS": "-j8"}, single_job=True
        )
        self.assertLess(argv.index("-j8"), argv.index("-j1"))

    def test_runtime_signals_positions(self):
        argv = compose_cargo_build_edge_argv(
            _cmd(),
            {"CARGO": "cargo"},
            {},
            timings=True,
            keep_going=True,
            single_job=True,
        )
        self.assertEqual(argv[1:4], ["rustc", "--timings", "--keep-going"])
        self.assertLess(argv.index("-j1"), argv.index("--"))

    def test_megazord_library_gets_staticlib_crate_type(self):
        argv = compose_cargo_build_edge_argv(
            _cmd(is_megazord=True), {"CARGO": "cargo"}, {}
        )
        self.assertEqual(argv[argv.index("--profile") + 1], "release-megazord")
        self.assertEqual(argv[argv.index("--crate-type") + 1], "staticlib")

    def test_megazord_test_uses_profile_but_omits_crate_type(self):
        # A colocated test edge uses the megazord profile but must not receive
        # --crate-type staticlib, which `cargo test` rejects.
        cmd = _cmd(kind="test", subcommand="test", is_megazord=True)
        argv = compose_cargo_build_edge_argv(cmd, {"CARGO": "cargo"}, {})
        self.assertEqual(argv[1], "test")
        self.assertEqual(argv[argv.index("--profile") + 1], "release-megazord")
        self.assertNotIn("--crate-type", argv)

    def test_test_edge_exact_argv(self):
        # Later CARGO_EXTRA_FLAGS override shared test build flags.
        cmd = _cmd(
            kind="test",
            subcommand="test",
            target_triple="t",
            features=("f",),
            cargo_subcommand_args=("--no-fail-fast", "-p", "style"),
        )
        argv = compose_cargo_build_edge_argv(
            cmd,
            {"CARGO": "cargo"},
            {"CARGOFLAGS": "--offline", "CARGO_EXTRA_FLAGS": "--extra"},
        )
        self.assertEqual(
            argv,
            [
                "cargo",
                "test",
                "--target=t",
                "--no-fail-fast",
                "-p",
                "style",
                "--features",
                "f,mozilla-central-workspace-hack",
                "--offline",
                "--release",
                "--frozen",
                "--manifest-path",
                cmd.manifest_path,
                "--extra",
            ],
        )

    def test_unquoted_metacharacter_rejected(self):
        from mozshellutil import MetaCharacterException

        with self.assertRaises(MetaCharacterException):
            compose_cargo_build_edge_argv(
                _cmd(), {"CARGO": "cargo"}, {"CARGOFLAGS": "--x; --y"}
            )


class TestComposeRustflags(unittest.TestCase):
    def test_host_edge_gets_override_only(self):
        substs = {
            "MOZ_RUST_DEFAULT_FLAGS": "-Cdebuginfo=2",
            "RUSTFLAGS": "-Cbase",
            "RUST_PGO_FLAGS": "-Cpgo",
        }
        out = compose_rustflags(_cmd(kind="host-library"), substs, {})
        self.assertEqual(out, "-Cdebuginfo=2 -C codegen-units=1")

    def test_ltoable_adds_pgo_and_cross_lto(self):
        substs = {
            "RUSTFLAGS": "-Cbase",
            "RUST_PGO_FLAGS": "-Cpgo",
            "MOZ_LTO_RUST_CROSS": "cross",
        }
        out = compose_rustflags(_cmd(uses_ltoable_rustflags=True), substs, {}).split()
        self.assertIn("-Cpgo", out)
        self.assertIn("-Clinker-plugin-lto", out)

    def test_nonltoable_edge_omits_pgo(self):
        substs = {"RUSTFLAGS": "-Cbase", "RUST_PGO_FLAGS": "-Cpgo"}
        out = compose_rustflags(
            _cmd(kind="test", subcommand="test", uses_ltoable_rustflags=False),
            substs,
            {},
        ).split()
        self.assertNotIn("-Cpgo", out)

    def test_tsan_injects_sanitizer(self):
        out = compose_rustflags(
            _cmd(uses_ltoable_rustflags=True), {"MOZ_TSAN": "1"}, {}
        ).split()
        self.assertIn("-Zsanitizer=thread", out)

    def test_darwin_default_linker_libraries_uses_assembled_ldflags(self):
        # Sanitizer flags in global PGO or LTO flags also affect this decision.
        substs = {"OS_ARCH": "Darwin", "RUST_PGO_LDFLAGS": "-fsanitize=address"}
        out = compose_rustflags(_cmd(kind="program"), substs, {}).split()
        self.assertIn("default-linker-libraries=yes", out)

    def test_rustflags_exact_order(self):
        substs = {
            "MOZ_RUST_DEFAULT_FLAGS": "-Coverride",
            "RUST_SANCOV_FLAGS": "-Csancov",
            "RUSTFLAGS": "-Cbase",
            "RUST_PGO_FLAGS": "-Cpgo",
            "MOZ_LTO_RUST_CROSS": "cross",
            "OS_ARCH": "Darwin",
        }
        cmd = _cmd(
            kind="library",
            uses_ltoable_rustflags=True,
            link_flags=("-fsanitize=address",),
            extra_rustflags=("-Cextra",),
        )
        out = compose_rustflags(cmd, substs, {})
        self.assertEqual(
            out,
            "-Coverride -Csancov -Cbase -Cpgo -Clinker-plugin-lto"
            " -C codegen-units=1 -C default-linker-libraries=yes -Cextra",
        )

    def test_extra_rustflags_appended_last(self):
        cmd = _cmd(
            kind="test",
            subcommand="test",
            extra_rustflags=("-C", "link-arg=-Wl,-rpath,/obj/dist/bin"),
        )
        out = compose_rustflags(cmd, {}, {})
        self.assertTrue(out.endswith("-C link-arg=-Wl,-rpath,/obj/dist/bin"))


class TestComposeEnv(unittest.TestCase):
    def _env(self, cmd, substs, current_env=None):
        return compose_env(cmd, substs, current_env or {}, "/src", "/obj")

    def test_cross_build_keeps_host_and_target_toolchains_distinct(self):
        substs = {
            "RUST_TARGET": "aarch64-linux-android",
            "RUST_HOST_TARGET": "x86_64-unknown-linux-gnu",
            "CC": "target-clang",
            "AR": "target-ar",
            "HOST_CC": "host-clang",
            "HOST_AR": "host-ar",
        }
        # Host edges still populate both host and target tool variables.
        env = self._env(_cmd(kind="host-library"), substs)
        self.assertEqual(env["CC_x86_64_unknown_linux_gnu"], "host-clang")
        self.assertEqual(env["CC_aarch64_linux_android"], "target-clang")
        self.assertEqual(env["AR_x86_64_unknown_linux_gnu"], "host-ar")
        self.assertEqual(env["AR_aarch64_linux_android"], "target-ar")

    def test_known_wrapper_custom_is_only_set_when_configured(self):
        substs = {"RUST_TARGET": "t", "RUST_HOST_TARGET": "t"}
        env = self._env(_cmd(), substs)
        self.assertNotIn("CC_KNOWN_WRAPPER_CUSTOM", env)

        substs["CC_KNOWN_WRAPPER_CUSTOM"] = "kache"
        env = self._env(_cmd(), substs)
        self.assertEqual(env["CC_KNOWN_WRAPPER_CUSTOM"], "kache")

    def test_wrap_ldflags_wraps_lto_and_pgo_around_link_flags(self):
        substs = {
            "RUST_TARGET": "t",
            "RUST_HOST_TARGET": "t",
            "MOZ_LTO_LDFLAGS": "-flto",
            "RUST_PGO_LDFLAGS": "-Cpgo-ld",
        }
        env = self._env(_cmd(link_flags=("-Wl,-z,relro",)), substs)
        self.assertEqual(env["MOZ_CARGO_WRAP_LDFLAGS"], "-flto -Wl,-z,relro -Cpgo-ld")

    def test_wrap_ldflags_drops_cfi_and_fprofile(self):
        substs = {"RUST_TARGET": "t", "RUST_HOST_TARGET": "t"}
        cmd = _cmd(link_flags=("-fsanitize=cfi", "-fprofile-generate", "-Wl,-z,relro"))
        env = self._env(cmd, substs)
        self.assertEqual(env["MOZ_CARGO_WRAP_LDFLAGS"], "-Wl,-z,relro")

    def test_program_keeps_fsanitize_without_rust_sanitizer(self):
        # Preserve native sanitizer flags unless Rust sanitizer instrumentation is active.
        substs = {"RUST_TARGET": "t", "RUST_HOST_TARGET": "t"}
        cmd = _cmd(kind="program", link_flags=("-fsanitize=address",))
        env = self._env(cmd, substs)
        self.assertIn("-fsanitize=address", env["MOZ_CARGO_WRAP_LDFLAGS"])

    def test_program_strips_fsanitize_with_rust_sanitizer(self):
        substs = {
            "RUST_TARGET": "t",
            "RUST_HOST_TARGET": "t",
            "RUSTFLAGS": [" -Zsanitizer=address"],
        }
        cmd = _cmd(kind="program", link_flags=("-fsanitize=address",))
        env = self._env(cmd, substs)
        self.assertIn("-Zsanitizer=address", env["RUSTFLAGS"].split())
        self.assertNotIn("-fsanitize=address", env["MOZ_CARGO_WRAP_LDFLAGS"].split())

    def test_library_strips_fsanitize_only_under_tsan(self):
        base = {"RUST_TARGET": "t", "RUST_HOST_TARGET": "t"}
        cmd = _cmd(kind="library", link_flags=("-fsanitize=address",))
        asan = self._env(cmd, {**base, "RUSTFLAGS": "-Zsanitizer=address"})
        self.assertIn("-fsanitize=address", asan["MOZ_CARGO_WRAP_LDFLAGS"])
        tsan = self._env(
            cmd, {**base, "MOZ_TSAN": "1", "RUSTFLAGS": "-Zsanitizer=thread"}
        )
        self.assertNotIn("-fsanitize=address", tsan["MOZ_CARGO_WRAP_LDFLAGS"])

    def test_darwin_lto_stages_objects_per_binary(self):
        substs = {
            "RUST_TARGET": "t",
            "RUST_HOST_TARGET": "t",
            "OS_TARGET": "Darwin",
            "MOZ_LTO": "1",
        }
        cmd = _cmd(kind="program", lto_object_stem="geckodriver")
        env = self._env(cmd, substs)
        self.assertIn(
            "-Wl,-object_path_lto,geckodriver.lto.o/", env["MOZ_CARGO_WRAP_LDFLAGS"]
        )
        linux = self._env(cmd, {**substs, "OS_TARGET": "Linux"})
        self.assertNotIn("object_path_lto", linux["MOZ_CARGO_WRAP_LDFLAGS"])
        no_lto = self._env(cmd, {k: v for k, v in substs.items() if k != "MOZ_LTO"})
        self.assertNotIn("object_path_lto", no_lto["MOZ_CARGO_WRAP_LDFLAGS"])

    def test_cflags_assemble_base_lto_computed_pgo(self):
        substs = {
            "RUST_TARGET": "t",
            "RUST_HOST_TARGET": "t",
            "CC_BASE_FLAGS": "-base",
            "CC_TYPE": "clang",
            "MOZ_LTO_CFLAGS": "-flto",
            "RUST_PGO_CFLAGS": "-fprofile-use=x",
        }
        env = self._env(_cmd(computed_cflags=("-Icomputed",)), substs)
        self.assertEqual(env["CFLAGS_t"], "-base -flto -Icomputed -fprofile-use=x")


class TestComposeMachCargo(unittest.TestCase):
    def test_library_auto_args(self):
        cmd = _cmd(features=("f",))
        argv = compose_mach_cargo_argv(cmd, {"CARGO": "cargo"}, {}, "check")
        self.assertEqual(
            argv,
            [
                "cargo",
                "check",
                "--release",
                "--frozen",
                "--manifest-path",
                cmd.manifest_path,
                "--lib",
                "--target=x86_64-unknown-linux-gnu",
                "--features",
                "f,mozilla-central-workspace-hack",
            ],
        )

    def test_program_auto_args(self):
        cmd = _cmd(
            kind="program",
            target_triple="t",
            cargo_subcommand_args=("--bin", "geckodriver"),
        )
        argv = compose_mach_cargo_argv(cmd, {"CARGO": "cargo"}, {}, "clippy")
        self.assertEqual(
            argv,
            [
                "cargo",
                "clippy",
                "--release",
                "--frozen",
                "--manifest-path",
                cmd.manifest_path,
                "--bin",
                "geckodriver",
                "--target=t",
                "--features",
                "mozilla-central-workspace-hack",
            ],
        )

    def test_host_program_auto_args_target_host(self):
        cmd = _cmd(
            kind="host-program",
            cargo_subcommand_args=("--bin", "geckodriver"),
        )
        argv = compose_mach_cargo_argv(
            cmd,
            {"CARGO": "cargo", "RUST_HOST_TARGET": "x86_64-pc-host"},
            {},
            "check",
        )
        self.assertIn("--bin", argv)
        self.assertIn("--target=x86_64-pc-host", argv)
        self.assertNotIn("--lib", argv)

    def test_build_verbose_log_adds_vv(self):
        cmd = _cmd()
        argv = compose_mach_cargo_argv(
            cmd, {"CARGO": "cargo"}, {"BUILD_VERBOSE_LOG": "1"}, "check"
        )
        self.assertIn("-vv", argv)

    def test_no_auto_arg_omits_auto_args(self):
        cmd = _cmd(features=("f",))
        argv = compose_mach_cargo_argv(
            cmd, {"CARGO": "cargo"}, {}, "audit", no_auto_arg=True
        )
        self.assertNotIn("--lib", argv)
        self.assertNotIn("--features", argv)
        self.assertTrue(all(not a.startswith("--target=") for a in argv))

    def test_build_flags_override_replaces_computed(self):
        cmd = _cmd()
        argv = compose_mach_cargo_argv(
            cmd,
            {"CARGO": "cargo"},
            {},
            "deny",
            build_flags_override=("check", "--hide-inclusion-graph"),
            no_auto_arg=True,
        )
        self.assertEqual(argv, ["cargo", "deny", "check", "--hide-inclusion-graph"])

    def test_extra_cli_flags_after_extra_flags(self):
        cmd = _cmd(features=())
        argv = compose_mach_cargo_argv(
            cmd,
            {"CARGO": "cargo"},
            {"CARGO_EXTRA_FLAGS": "--offline"},
            "check",
            extra_cli_flags=("--verbose",),
        )
        self.assertLess(argv.index("--offline"), argv.index("--verbose"))
        self.assertLess(argv.index("--verbose"), argv.index("--lib"))

    def test_jobs_added_for_build_like_only(self):
        cmd = _cmd(features=())
        argv = compose_mach_cargo_argv(cmd, {"CARGO": "cargo"}, {}, "check", jobs=4)
        self.assertEqual(argv[argv.index("-j") + 1], "4")
        plugin = compose_mach_cargo_argv(
            cmd,
            {"CARGO": "cargo"},
            {},
            "audit",
            build_flags_override=("check",),
            no_auto_arg=True,
            jobs=4,
        )
        self.assertNotIn("-j", plugin)


if __name__ == "__main__":
    mozunit.main()
