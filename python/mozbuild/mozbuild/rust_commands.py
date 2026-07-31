# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Compose Cargo commands and environments for Rust build edges."""

import os
from dataclasses import dataclass, fields

from mozshellutil import split as shell_split

_VALID_KINDS = frozenset(("library", "host-library", "program", "host-program", "test"))
_VALID_SUBCOMMANDS = frozenset(("rustc", "test", "check", "clippy", "udeps"))

# Serialized command filename for each Rust edge kind.
CARGO_SPEC_FILES = {
    "library": ".cargo-library-spec.json",
    "host-library": ".cargo-host-library-spec.json",
    "program": ".cargo-program-spec.json",
    "host-program": ".cargo-host-program-spec.json",
    "test": ".cargo-tests-spec.json",
}


@dataclass
class CargoCommand:
    """Declarative metadata for one Cargo build edge.

    ``extra_rustcflags`` are passed after the ``cargo rustc`` separator.
    ``extra_rustflags`` are appended to RUSTFLAGS. ``lto_object_stem`` selects
    where retained macOS LTO objects are stored.
    """

    kind: str
    subcommand: str
    manifest_path: str
    target_triple: str = ""
    features: tuple = ()
    is_megazord: bool = False
    is_gkrust_gtest: bool = False
    uses_ltoable_rustflags: bool = False
    extra_rustcflags: tuple = ()
    cargo_subcommand_args: tuple = ()
    working_directory: str = ""
    computed_cflags: tuple = ()
    computed_cxxflags: tuple = ()
    computed_host_cflags: tuple = ()
    computed_host_cxxflags: tuple = ()
    link_flags: tuple = ()
    extra_rustflags: tuple = ()
    lto_object_stem: str = ""

    @classmethod
    def from_dict(cls, data):
        unknown = set(data) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"Unknown CargoCommand keys: {sorted(unknown)}")
        cmd = cls(**data)
        if cmd.kind not in _VALID_KINDS:
            raise ValueError(f"Unknown Rust build kind: {cmd.kind!r}")
        if cmd.subcommand not in _VALID_SUBCOMMANDS:
            raise ValueError(f"Unknown Cargo subcommand: {cmd.subcommand!r}")
        return cmd


def _as_str(v):
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return " ".join(v)
    return str(v)


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return list(v)
    if isinstance(v, str):
        return v.split()
    return [str(v)]


def _split_command_fragment(value):
    """Split a shell-quoted command fragment into arguments."""
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return shell_split(str(value))


def _bool(v):
    if v is None or v == "":
        return False
    if isinstance(v, bool):
        return v
    return str(v).strip() not in ("", "0")


def _filter_out(seq, excludes):
    out = []
    for item in seq:
        skip = False
        for ex in excludes:
            if ex.endswith("%"):
                if item.startswith(ex[:-1]):
                    skip = True
                    break
            elif item == ex:
                skip = True
                break
        if not skip:
            out.append(item)
    return out


def _filter(seq, patterns):
    out = []
    for item in seq:
        for pat in patterns:
            if pat.endswith("%") and item.startswith(pat[:-1]):
                out.append(item)
                break
            elif item == pat:
                out.append(item)
                break
    return out


def _varize(triple):
    return triple.upper().replace("-", "_")


def _cc_env_suffix(triple):
    return triple.replace("-", "_")


def _is_clang(cc_type):
    return cc_type and cc_type.startswith("clang")


def _is_clang_cl(cc_type):
    return cc_type == "clang-cl"


def _compute_neon_flags(cmd, substs):
    if substs.get("MOZ_FPU") != "neon":
        return []
    if cmd.target_triple == "thumbv7neon-":
        return ["-C", "target_feature=+neon,-d16"]
    return []


def _embed_bitcode(substs):
    return (
        not _bool(substs.get("DEVELOPER_OPTIONS"))
        and not _bool(substs.get("MOZ_DEBUG_RUST"))
        and not substs.get("MOZ_LTO_RUST_CROSS")
        and not _as_list(substs.get("RUST_SANCOV_FLAGS"))
        and not _bool(substs.get("MOZ_CODE_COVERAGE"))
    )


def _compose_rustflags(cmd, substs, current_env):
    """The composed RUSTFLAGS tokens for this edge."""
    override = _as_list(substs.get("MOZ_RUST_DEFAULT_FLAGS"))
    override.extend(_compute_neon_flags(cmd, substs))

    extra = current_env.get("extra_rustflags", "")
    if extra:
        override.extend(extra.split())

    if _bool(substs.get("DEVELOPER_OPTIONS")):
        override.append("-Clto=off")

    if cmd.kind.startswith("host-"):
        result = list(override)
        if not _bool(substs.get("DEVELOPER_OPTIONS")):
            result += ["-C", "codegen-units=1"]
        return result

    # Keep configured RUSTFLAGS before the derived target flags.
    base = shell_split(_as_str(substs.get("RUSTFLAGS")))
    if _bool(substs.get("MOZ_TSAN")):
        base.append("-Zsanitizer=thread")
    if _embed_bitcode(substs):
        base.append("-Cembed-bitcode=yes")
    target = cmd.target_triple
    if _bool(substs.get("MOZ_DEBUG_RUST")) and target.startswith("i686-pc-windows-"):
        base.append("-Zmir-enable-passes=-CheckAlignment")
    if substs.get("OS_ARCH") == "WINNT" and substs.get("CC_TYPE") == "clang":
        dlltool = substs.get("LLVM_DLLTOOL")
        if dlltool:
            base += ["-C", f"dlltool={_as_str(dlltool)}"]

    sancov = _as_list(substs.get("RUST_SANCOV_FLAGS"))
    pgo = _as_list(substs.get("RUST_PGO_FLAGS"))

    if cmd.uses_ltoable_rustflags:
        result = list(override) + sancov + base + pgo
        if substs.get("MOZ_LTO_RUST_CROSS"):
            result.append("-Clinker-plugin-lto")
    else:
        result = list(override) + sancov + base

    if not _bool(substs.get("DEVELOPER_OPTIONS")):
        result += ["-C", "codegen-units=1"]

    if (
        substs.get("OS_ARCH") == "Darwin"
        and not any(f.startswith("-Zsanitizer=") for f in result)
        and any(
            f.startswith("-fsanitize=") for f in _assembled_target_ldflags(cmd, substs)
        )
    ):
        result += ["-C", "default-linker-libraries=yes"]

    result.extend(cmd.extra_rustflags)

    return result


def compose_rustflags(cmd, substs, current_env):
    """The RUSTFLAGS env value for this edge."""
    return " ".join(_compose_rustflags(cmd, substs, current_env))


def _compute_cargo_build_flags(cmd, substs, current_env, single_job=False):
    flags = _split_command_fragment(current_env.get("CARGOFLAGS"))

    if cmd.is_megazord:
        if _bool(substs.get("MOZ_DEBUG_RUST")):
            flags += ["--profile", "dev-megazord"]
        else:
            flags += ["--profile", "release-megazord"]
    elif not _bool(substs.get("MOZ_DEBUG_RUST")):
        flags.append("--release")

    if not _bool(substs.get("JS_STANDALONE")):
        flags.append("--frozen")

    flags += ["--manifest-path", cmd.manifest_path]

    if _bool(current_env.get("BUILD_VERBOSE_LOG")):
        flags.append("-vv")

    if _bool(current_env.get("USE_CARGO_JSON_MESSAGE_FORMAT")) or _bool(
        substs.get("USE_CARGO_JSON_MESSAGE_FORMAT")
    ):
        flags.append("--message-format=json")

    if _bool(current_env.get("MACH_STDOUT_ISATTY")):
        if not any(f.startswith("--color") for f in flags):
            flags.append(
                "--color=never"
                if _bool(current_env.get("NO_ANSI"))
                else "--color=always"
            )

    # A jobserver with one job is not propagated to Cargo, so pass -j1 explicitly.
    if single_job:
        flags.append("-j1")

    if _bool(substs.get("MOZ_TSAN")):
        flags.append("-Zbuild-std=std,panic_abort")

    return flags


def applies_library_lto(kind, is_gkrust_gtest, substs):
    """Return whether a Rust library edge uses LTO."""
    return (
        kind == "library"
        and not _bool(substs.get("DEVELOPER_OPTIONS"))
        and not _bool(substs.get("MOZ_DEBUG_RUST"))
        and not substs.get("MOZ_LTO_RUST_CROSS")
        and not _as_list(substs.get("RUST_SANCOV_FLAGS"))
        and not _bool(substs.get("MOZ_CODE_COVERAGE"))
        and not is_gkrust_gtest
    )


def _compute_rustc_flags(cmd, substs):
    flags = list(cmd.extra_rustcflags)

    if substs.get("OS_ARCH") == "WASI" and cmd.kind == "library":
        flags += ["-C", "target-feature=-crt-static"]

    if applies_library_lto(cmd.kind, cmd.is_gkrust_gtest, substs):
        if substs.get("MOZ_LTO_RUST_CROSS") == "full":
            flags.append("-Clto=fat")
        else:
            flags.append("-Clto")

    if (
        cmd.kind == "program"
        and substs.get("OS_ARCH") == "WINNT"
        and substs.get("CC_TYPE") == "clang"
    ):
        flags += ["-C", "default-linker-libraries=yes"]

    return flags


def _compute_target_cc(substs):
    return _filter_out(
        _as_list(substs.get("CC")), _as_list(substs.get("CC_BASE_FLAGS"))
    )


def _compute_target_cxx(substs):
    return _filter_out(
        _as_list(substs.get("CXX")), _as_list(substs.get("CXX_BASE_FLAGS"))
    )


def _compute_host_cc(substs):
    return _filter_out(
        _as_list(substs.get("HOST_CC")), _as_list(substs.get("HOST_CC_BASE_FLAGS"))
    )


def _compute_host_cxx(substs):
    return _filter_out(
        _as_list(substs.get("HOST_CXX")), _as_list(substs.get("HOST_CXX_BASE_FLAGS"))
    )


def _compute_cflags(cmd, substs, host=False):
    pass_base_only = _bool(substs.get("PASS_ONLY_BASE_CFLAGS_TO_RUST"))

    if host:
        base = _as_list(substs.get("HOST_CC_BASE_FLAGS"))
        if substs.get("HOST_OS_ARCH") == "WINNT":
            base.append("-DUNICODE")
        if pass_base_only:
            return base
        return base + list(cmd.computed_host_cflags)

    base = _as_list(substs.get("CC_BASE_FLAGS"))
    if substs.get("OS_ARCH") == "WINNT":
        base.append("-DUNICODE")
    if pass_base_only:
        return base
    rust_lto = (
        _as_list(substs.get("MOZ_LTO_CFLAGS"))
        if _is_clang(substs.get("CC_TYPE"))
        else []
    )
    pgo = _as_list(substs.get("RUST_PGO_CFLAGS"))
    return base + rust_lto + list(cmd.computed_cflags) + pgo


def _compute_cxxflags(cmd, substs, host=False):
    pass_base_only = _bool(substs.get("PASS_ONLY_BASE_CFLAGS_TO_RUST"))

    if host:
        base = _as_list(substs.get("HOST_CXX_BASE_FLAGS"))
        if substs.get("HOST_OS_ARCH") == "WINNT":
            base.append("-DUNICODE")
        if pass_base_only:
            return base
        return base + list(cmd.computed_host_cxxflags)

    base = _as_list(substs.get("CXX_BASE_FLAGS"))
    if substs.get("OS_ARCH") == "WINNT":
        base.append("-DUNICODE")
    if pass_base_only:
        return base + _filter(
            list(cmd.computed_cxxflags), ["-fno-aligned-new", "-fno-sized-deallocation"]
        )
    rust_lto = (
        _as_list(substs.get("MOZ_LTO_CFLAGS"))
        if _is_clang(substs.get("CC_TYPE"))
        else []
    )
    pgo = _as_list(substs.get("RUST_PGO_CFLAGS"))
    return base + rust_lto + list(cmd.computed_cxxflags) + pgo


def _assembled_target_ldflags(cmd, substs):
    # Assemble global LTO and PGO flags around the per-directory linker flags.
    ldflags = (
        _as_list(substs.get("MOZ_LTO_LDFLAGS"))
        + _as_list(cmd.link_flags)
        + _as_list(substs.get("RUST_PGO_LDFLAGS"))
    )
    # Keep macOS LTO objects in a separate directory for each binary so dsymutil
    # can read them without collisions.
    if (
        cmd.lto_object_stem
        and _bool(substs.get("MOZ_LTO"))
        and substs.get("OS_TARGET") == "Darwin"
    ):
        ldflags.append(f"-Wl,-object_path_lto,{cmd.lto_object_stem}.lto.o/")
    return ldflags


def _compute_cargo_wrap_ldflags(cmd, substs, topobjdir, rustflags):
    ldflags = _filter_out(
        _assembled_target_ldflags(cmd, substs),
        [
            "-fsanitize=cfi%",
            "-framework",
            "Cocoa",
            "-lobjc",
            "AudioToolbox",
            "ExceptionHandling",
            "-fprofile-%",
            "-Wl,--build-id=uuid",
        ],
    )

    # Remove native sanitizer flags only when Rust sanitizer instrumentation is
    # active. TSan applies to every target edge. Other sanitizers apply to programs.
    has_rust_sanitizer = any(f.startswith("-Zsanitizer=") for f in rustflags)
    is_tsan = "-Zsanitizer=thread" in rustflags
    if has_rust_sanitizer and (cmd.kind == "program" or is_tsan):
        ldflags = _filter_out(ldflags, ["-fsanitize=%"])

    if (
        cmd.kind == "program"
        and substs.get("OS_ARCH") == "WINNT"
        and substs.get("CC_TYPE") == "clang"
    ):
        ldflags += [f"-L{topobjdir}/build/win32", "-lunwind"]

    return " ".join(ldflags)


def _compute_host_ldflags(substs):
    if substs.get("HOST_OS_ARCH") == "WINNT":
        libpaths = _as_list(substs.get("HOST_LINKER_LIBPATHS_BAT"))
    else:
        libpaths = _as_list(substs.get("HOST_LINKER_LIBPATHS"))
    return " ".join(_as_list(substs.get("HOST_LDFLAGS")) + libpaths)


def _compute_cargo_target_linker_path(substs, topsrcdir, host=False):
    name = "cargo-host-linker" if host else "cargo-linker"
    ext = ".bat" if substs.get("HOST_OS_ARCH") == "WINNT" else ""
    return f"{topsrcdir}/build/{name}{ext}"


def _compute_wrap_ld(substs):
    if _is_clang_cl(substs.get("CC_TYPE")):
        return _as_str(substs.get("LINKER"))
    return _as_str(substs.get("CC"))


def _compute_wrap_ld_cxx(substs):
    if _is_clang_cl(substs.get("CC_TYPE")):
        return _as_str(substs.get("LINKER"))
    return _as_str(substs.get("CXX"))


def _compute_wrap_host_ld(substs):
    if _is_clang_cl(substs.get("HOST_CC_TYPE")):
        return _as_str(substs.get("HOST_LINKER"))
    return _as_str(substs.get("HOST_CC"))


def _compute_wrap_host_ld_cxx(substs):
    if _is_clang_cl(substs.get("HOST_CC_TYPE")):
        return _as_str(substs.get("HOST_LINKER"))
    return _as_str(substs.get("HOST_CXX"))


def _set_sanitizer_intercept_options(env, substs):
    if substs.get("RUST_TARGET") != substs.get("RUST_HOST_TARGET"):
        return
    for san in ("ASAN", "TSAN", "UBSAN"):
        if not _bool(substs.get(f"MOZ_{san}")):
            continue
        var = f"{san}_OPTIONS"
        existing = env.get(var, "")
        prefix = f"{existing}:" if existing else ""
        env[var] = f"{prefix}intercept_tls_get_addr=0"


def compose_env(cmd, substs, current_env, topsrcdir, topobjdir):
    env = dict(current_env)

    # Set both host and target tool variables for every edge. Only Cargo's
    # --target argument is edge specific.
    target = substs.get("RUST_TARGET", "")
    host = substs.get("RUST_HOST_TARGET", "")
    target_suffix = _cc_env_suffix(target)
    host_suffix = _cc_env_suffix(host) if host else target_suffix

    env[f"CC_{host_suffix}"] = _as_str(_compute_host_cc(substs))
    env[f"CXX_{host_suffix}"] = _as_str(_compute_host_cxx(substs))
    env[f"AR_{host_suffix}"] = _as_str(substs.get("HOST_AR"))
    env[f"CC_{target_suffix}"] = _as_str(_compute_target_cc(substs))
    env[f"CXX_{target_suffix}"] = _as_str(_compute_target_cxx(substs))
    env[f"AR_{target_suffix}"] = _as_str(substs.get("AR"))

    # cc-rs may not know whether we are using a compiler wrapper, so explicitly
    # tell it that we do.
    known_wrapper = _as_str(substs.get("CC_KNOWN_WRAPPER_CUSTOM"))
    if known_wrapper:
        env["CC_KNOWN_WRAPPER_CUSTOM"] = known_wrapper

    env[f"CFLAGS_{host_suffix}"] = _as_str(_compute_cflags(cmd, substs, host=True))
    env[f"CXXFLAGS_{host_suffix}"] = _as_str(_compute_cxxflags(cmd, substs, host=True))
    env[f"CFLAGS_{target_suffix}"] = _as_str(_compute_cflags(cmd, substs))
    env[f"CXXFLAGS_{target_suffix}"] = _as_str(_compute_cxxflags(cmd, substs))

    incremental = _as_str(substs.get("CARGO_INCREMENTAL"))
    if incremental:
        env["CARGO_INCREMENTAL"] = incremental

    if _bool(substs.get("MOZ_USING_SCCACHE")) or _bool(
        substs.get("MOZ_USING_BUILDCACHE")
    ):
        env["RUSTC_WRAPPER"] = _as_str(substs.get("CCACHE"))

    env["CARGO_TARGET_DIR"] = topobjdir
    rustflags = _compose_rustflags(cmd, substs, current_env)
    env["RUSTFLAGS"] = " ".join(rustflags)
    env["RUSTC"] = _as_str(substs.get("RUSTC"))
    env["RUSTDOC"] = _as_str(substs.get("RUSTDOC"))
    env["RUSTDOCFLAGS"] = _as_str(substs.get("RUSTDOCFLAGS"))
    env["RUSTFMT"] = _as_str(substs.get("RUSTFMT"))
    env["LIBCLANG_PATH"] = _as_str(substs.get("MOZ_LIBCLANG_PATH"))
    env["CLANG_PATH"] = _as_str(substs.get("MOZ_CLANG_PATH"))
    env["PKG_CONFIG"] = _as_str(substs.get("PKG_CONFIG"))
    env["PKG_CONFIG_ALLOW_CROSS"] = "1"
    env["PKG_CONFIG_PATH"] = _as_str(substs.get("PKG_CONFIG_PATH"))
    if substs.get("PKG_CONFIG_SYSROOT_DIR"):
        env["PKG_CONFIG_SYSROOT_DIR"] = _as_str(substs.get("PKG_CONFIG_SYSROOT_DIR"))
    if substs.get("PKG_CONFIG_LIBDIR"):
        env["PKG_CONFIG_LIBDIR"] = _as_str(substs.get("PKG_CONFIG_LIBDIR"))
    env["RUST_BACKTRACE"] = "full"
    env["MOZ_TOPOBJDIR"] = topobjdir
    env["MOZ_FOLD_LIBS"] = _as_str(substs.get("MOZ_FOLD_LIBS"))
    env["GLEAN_PYTHON_VENV_DIR"] = _as_str(substs.get("GRADLE_GLEAN_PARSER_VENV"))
    env["PYTHON3"] = _as_str(substs.get("PYTHON3"))
    env["CARGO_PROFILE_RELEASE_OPT_LEVEL"] = _as_str(
        substs.get("CARGO_PROFILE_RELEASE_OPT_LEVEL")
    )
    env["CARGO_PROFILE_DEV_OPT_LEVEL"] = _as_str(
        substs.get("CARGO_PROFILE_DEV_OPT_LEVEL")
    )

    env["BINDGEN_EXTRA_CLANG_ARGS"] = " ".join(
        _filter(_as_list(substs.get("BINDGEN_SYSTEM_FLAGS")), ["--target=%"])
    )

    if substs.get("OS_ARCH") == "Darwin":
        if substs.get("MACOS_SDK_DIR"):
            env["COREAUDIO_SDK_PATH"] = _as_str(substs.get("MACOS_SDK_DIR"))
        if substs.get("IPHONEOS_SDK_DIR"):
            env["COREAUDIO_SDK_PATH"] = _as_str(substs.get("IPHONEOS_SDK_DIR"))
            env["IPHONEOS_SDK_DIR"] = _as_str(substs.get("IPHONEOS_SDK_DIR"))
            env["PATH"] = (
                os.path.join(topsrcdir, "build", "macosx")
                + os.pathsep
                + env.get("PATH", "")
            )

    env["LIBZ_RS_SYS_PREFIX"] = "MOZ_Z_"

    bootstrap = current_env.get("RUSTC_BOOTSTRAP")
    if not bootstrap:
        parts = ["mozglue_static", "qcms"]
        if _bool(substs.get("MOZ_RUST_SIMD")):
            parts += ["encoding_rs", "any_all_workaround"]
        bootstrap = ",".join(parts)
    if _bool(substs.get("MOZ_DEBUG_RUST")) and target.startswith("i686-pc-windows-"):
        bootstrap = "1"
    env["RUSTC_BOOTSTRAP"] = bootstrap

    env["MOZ_CLANG_NEWER_THAN_RUSTC_LLVM"] = _as_str(
        substs.get("MOZ_CLANG_NEWER_THAN_RUSTC_LLVM")
    )

    target_var = _varize(target)
    host_var = _varize(host) if host else target_var
    env[f"CARGO_TARGET_{host_var}_LINKER"] = _compute_cargo_target_linker_path(
        substs, topsrcdir, host=True
    )
    env[f"CARGO_TARGET_{target_var}_LINKER"] = _compute_cargo_target_linker_path(
        substs, topsrcdir, host=False
    )

    if cmd.kind.startswith("host-"):
        env["MOZ_CARGO_WRAP_LD"] = _compute_wrap_host_ld(substs)
        env["MOZ_CARGO_WRAP_LD_CXX"] = _compute_wrap_host_ld_cxx(substs)
        env["MOZ_CARGO_WRAP_LDFLAGS"] = _compute_host_ldflags(substs)
    else:
        env["MOZ_CARGO_WRAP_LD"] = _compute_wrap_ld(substs)
        env["MOZ_CARGO_WRAP_LD_CXX"] = _compute_wrap_ld_cxx(substs)
        env["MOZ_CARGO_WRAP_LDFLAGS"] = _compute_cargo_wrap_ldflags(
            cmd, substs, topobjdir, rustflags
        )

    env["MOZ_CARGO_WRAP_HOST_LD"] = _compute_wrap_host_ld(substs)
    env["MOZ_CARGO_WRAP_HOST_LD_CXX"] = _compute_wrap_host_ld_cxx(substs)
    env["MOZ_CARGO_WRAP_HOST_LDFLAGS"] = _compute_host_ldflags(substs)

    _set_sanitizer_intercept_options(env, substs)

    return env


def _features_arg(cmd):
    features = list(cmd.features)
    feat = (
        ",".join(features)
        + ("," if features else "")
        + "mozilla-central-workspace-hack"
    )
    return ["--features", feat]


def compose_cargo_build_edge_argv(
    cmd, substs, current_env, timings=False, keep_going=False, single_job=False
):
    cargo = _as_str(substs.get("CARGO"))
    build_flags = _compute_cargo_build_flags(cmd, substs, current_env, single_job)
    extra = _split_command_fragment(current_env.get("CARGO_EXTRA_FLAGS"))

    if cmd.subcommand != "rustc":
        # Keep test arguments before shared build flags so CARGO_EXTRA_FLAGS can
        # override them.
        argv = [cargo, cmd.subcommand, f"--target={cmd.target_triple}"]
        argv.extend(cmd.cargo_subcommand_args)
        argv.extend(_features_arg(cmd))
        argv.extend(build_flags)
        argv.extend(extra)
        return argv

    argv = [cargo, cmd.subcommand]

    if timings:
        argv.append("--timings")
    if keep_going:
        argv.append("--keep-going")

    argv.extend(build_flags)
    argv.extend(extra)
    argv.extend(cmd.cargo_subcommand_args)

    if cmd.kind in ("library", "host-library"):
        argv.append("--lib")

    if cmd.kind == "library" and cmd.is_megazord:
        argv += ["--crate-type", "staticlib"]

    if cmd.kind in ("host-library", "host-program"):
        argv.append(f"--target={substs.get('RUST_HOST_TARGET')}")
    else:
        argv.append(f"--target={cmd.target_triple}")

    argv.extend(_features_arg(cmd))

    if cmd.kind in ("library", "program"):
        argv.append("--")
        argv.extend(_compute_rustc_flags(cmd, substs))

    return argv


def compose_mach_cargo_argv(
    cmd,
    substs,
    current_env,
    subcommand,
    build_flags_override=(),
    extra_cli_flags=(),
    no_auto_arg=False,
    jobs=0,
):
    argv = [_as_str(substs.get("CARGO")), subcommand]

    if build_flags_override:
        argv.extend(build_flags_override)
    else:
        argv.extend(_compute_cargo_build_flags(cmd, substs, current_env))

    # Plugin commands may not accept Cargo's -j option.
    if jobs and not no_auto_arg:
        argv += ["-j", str(jobs)]

    argv.extend(_split_command_fragment(current_env.get("CARGO_EXTRA_FLAGS")))
    argv.extend(extra_cli_flags)

    if not no_auto_arg:
        if cmd.kind in ("library", "host-library"):
            argv.append("--lib")
        else:
            argv.extend(cmd.cargo_subcommand_args)
        if cmd.kind in ("host-library", "host-program"):
            argv.append(f"--target={substs.get('RUST_HOST_TARGET')}")
        else:
            argv.append(f"--target={cmd.target_triple}")
        argv.extend(_features_arg(cmd))

    return argv
