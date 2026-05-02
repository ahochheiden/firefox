# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Compose the cargo argv and env for a Rust build edge.

Inputs:
  * spec      per-target dict (kind, manifest_path, output_path, features,
              target_triple, is_megazord, is_gkrust_gtest, is_ltoable,
              extra_rustcflags, cargo_extra_cli_flags, output_category,
              relsrcdir, relobjdir)
  * substs    config.status substs (mapping)
  * topsrcdir, topobjdir (absolute, normsep'd)
  * current_env   the env to start from (typically os.environ)

Outputs:
  * argv      list[str], the cargo invocation
  * env       dict[str, str], the env to pass to cargo
"""

import os


def _as_str(v):
    if v is None:
        return ""
    if isinstance(v, list):
        return " ".join(v)
    return str(v)


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, list):
        return list(v)
    if isinstance(v, str):
        return v.split()
    return [str(v)]


def _bool(v):
    if v is None or v == "":
        return False
    if isinstance(v, bool):
        return v
    s = str(v).strip()
    return s not in ("", "0")


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


def compose_rustflags(spec, substs, current_env, topobjdir=""):
    """Build the RUSTFLAGS env value for this recipe."""
    rustflags_override = _as_list(substs.get("MOZ_RUST_DEFAULT_FLAGS"))
    rustflags_override.extend(_compute_neon_flags(spec, substs))

    extra = current_env.get("extra_rustflags", "")
    if extra:
        rustflags_override.extend(extra.split())

    if _bool(substs.get("DEVELOPER_OPTIONS")):
        rustflags_override.append("-Clto=off")

    rustflags_sancov = _compute_sancov_flags(substs)
    pgo_topobjdir = topobjdir

    base = _as_list(substs.get("RUSTFLAGS"))

    if _bool(substs.get("MOZ_TSAN")):
        base.append("-Zsanitizer=thread")

    pgo = _compute_pgo_flags(substs, pgo_topobjdir)

    if spec["is_ltoable"]:
        result = list(rustflags_override) + rustflags_sancov + base + pgo
        if substs.get("MOZ_LTO_RUST_CROSS"):
            result.append("-Clinker-plugin-lto")
    else:
        result = list(rustflags_override) + rustflags_sancov + base

    if not _bool(substs.get("DEVELOPER_OPTIONS")):
        result.append("-C")
        result.append("codegen-units=1")

    if not _bool(substs.get("MOZ_DEBUG_RUST")) and not _bool(
        substs.get("DEVELOPER_OPTIONS")
    ):
        if not substs.get("MOZ_LTO_RUST_CROSS") and not rustflags_sancov:
            if not _bool(substs.get("MOZ_CODE_COVERAGE")):
                result.append("-Cembed-bitcode=yes")

    if substs.get("OS_ARCH") == "WINNT" and substs.get("CC_TYPE") == "clang":
        dlltool = substs.get("LLVM_DLLTOOL")
        if dlltool:
            result.append("-C")
            result.append(f"dlltool={_as_str(dlltool)}")

    target = spec["target_triple"]
    if _bool(substs.get("MOZ_DEBUG_RUST")) and target.startswith("i686-pc-windows-"):
        result.append("-Zmir-enable-passes=-CheckAlignment")

    if substs.get("OS_ARCH") == "Darwin" and not any(
        f.startswith("-Zsanitizer=") for f in base
    ):
        ldflags = _as_list(substs.get("LDFLAGS"))
        if any(f.startswith("-fsanitize=") for f in ldflags):
            if spec["is_ltoable"]:
                result.extend(["-C", "default-linker-libraries=yes"])

    return " ".join(result)


def _compute_neon_flags(spec, substs):
    if substs.get("MOZ_FPU") != "neon":
        return []
    target = spec["target_triple"]
    if target.startswith("thumbv7neon-"):
        return []
    return ["-C", "target_feature=+neon,-d16"]


def _compute_sancov_flags(substs):
    if _bool(substs.get("MOZ_TSAN")) or _bool(substs.get("FUZZING_JS_FUZZILLI")):
        return []
    if _bool(substs.get("LIBFUZZER")):
        return [
            "-Cpasses=sancov-module",
            "-Cllvm-args=-sanitizer-coverage-inline-8bit-counters",
            "-Cllvm-args=-sanitizer-coverage-level=4",
            "-Cllvm-args=-sanitizer-coverage-trace-compares",
            "-Cllvm-args=-sanitizer-coverage-pc-table",
        ]
    if _bool(substs.get("AFLFUZZ")):
        return [
            "-Cpasses=sancov-module",
            "-Cllvm-args=-sanitizer-coverage-level=3",
            "-Cllvm-args=-sanitizer-coverage-pc-table",
            "-Cllvm-args=-sanitizer-coverage-trace-pc-guard",
        ]
    return []


def _compute_pgo_flags(substs, topobjdir=""):
    if not _bool(substs.get("MOZ_PGO_RUST")):
        return []
    if _bool(substs.get("MOZ_PROFILE_GENERATE")):
        flags = ["-C", f"profile-generate={topobjdir}"]
        rustc_llvm = str(substs.get("RUSTC_LLVM_VERSION", ""))
        cc_version = str(substs.get("CC_VERSION", ""))
        old = ("5.", "6.", "7.", "8.", "9.", "10.", "11.")
        rustc_old = any(rustc_llvm.startswith(p) for p in old)
        cc_old = any(cc_version.startswith(p) for p in old)
        if rustc_old != cc_old:
            flags.append("-C")
            flags.append("llvm-args=--disable-vp=true")
        pgflags = _as_list(substs.get("PROFILE_GEN_CFLAGS"))
        i = 0
        while i < len(pgflags):
            if pgflags[i] == "-mllvm" and i + 1 < len(pgflags):
                flags.append("-C")
                flags.append(f"llvm-args={pgflags[i + 1]}")
                i += 2
            else:
                i += 1
        return flags
    if _bool(substs.get("MOZ_PROFILE_USE")):
        path = substs.get("PGO_PROFILE_PATH", "")
        return ["-C", f"profile-use={_as_str(path)}"]
    return []


def _compute_pass_only_base_cflags(substs):
    if not _bool(substs.get("CROSS_COMPILE")):
        if _bool(substs.get("MOZ_TSAN")):
            return True
        if _bool(substs.get("MOZ_ASAN")) or _bool(substs.get("MOZ_UBSAN")):
            if substs.get("OS_ARCH") != "Linux":
                return True
    if substs.get("HOST_OS_ARCH") == "WINNT" and _bool(substs.get("MOZ_CODE_COVERAGE")):
        return True
    return False


def _compute_cargo_build_flags(spec, substs, current_env):
    flags = list(_as_list(substs.get("CARGOFLAGS")))

    is_megazord = spec["is_megazord"]
    if is_megazord:
        if _bool(substs.get("MOZ_DEBUG_RUST")):
            flags.append("--profile")
            flags.append("dev-megazord")
        else:
            flags.append("--profile")
            flags.append("release-megazord")
    elif not _bool(substs.get("MOZ_DEBUG_RUST")):
        flags.append("--release")

    if not _bool(substs.get("JS_STANDALONE")):
        flags.append("--frozen")

    flags.append("--manifest-path")
    flags.append(spec["manifest_path"])

    if _bool(current_env.get("BUILD_VERBOSE_LOG")):
        flags.append("-vv")

    if _bool(current_env.get("USE_CARGO_JSON_MESSAGE_FORMAT")) or _bool(
        substs.get("USE_CARGO_JSON_MESSAGE_FORMAT")
    ):
        flags.append("--message-format=json")

    if _bool(current_env.get("MACH_STDOUT_ISATTY")):
        if not any(f.startswith("--color") for f in flags):
            if _bool(current_env.get("NO_ANSI")):
                flags.append("--color=never")
            else:
                flags.append("--color=always")

    if _bool(substs.get("MOZ_TSAN")):
        flags.append("-Zbuild-std=std,panic_abort")

    return flags


def _compute_rustc_flags(spec, substs):
    flags = list(spec.get("extra_rustcflags", []))

    if substs.get("OS_ARCH") == "WASI" and spec["kind"] == "library":
        flags.append("-C")
        flags.append("target-feature=-crt-static")

    if (
        not _bool(substs.get("DEVELOPER_OPTIONS"))
        and not _bool(substs.get("MOZ_DEBUG_RUST"))
        and not substs.get("MOZ_LTO_RUST_CROSS")
        and not _compute_sancov_flags(substs)
        and not _bool(substs.get("MOZ_CODE_COVERAGE"))
        and not spec["is_gkrust_gtest"]
    ):
        moz_lto_rust_cross = substs.get("MOZ_LTO_RUST_CROSS", "")
        if moz_lto_rust_cross == "full":
            flags.append("-Clto=fat")
        else:
            flags.append("-Clto")

    return flags


def _compute_cargo_wrap_ldflags(spec, substs, topobjdir=""):
    ldflags = _as_list(spec.get("computed_ldflags"))
    if not ldflags:
        ldflags = _as_list(substs.get("LDFLAGS"))
    excluded = [
        "-fsanitize=cfi%",
        "-framework",
        "Cocoa",
        "-lobjc",
        "AudioToolbox",
        "ExceptionHandling",
        "-fprofile-%",
        "-Wl,--build-id=uuid",
    ]
    ldflags = _filter_out(ldflags, excluded)

    rustflags = _as_list(substs.get("RUSTFLAGS"))
    has_sanitizer = any(f.startswith("-Zsanitizer=") for f in rustflags)
    is_tsan = "-Zsanitizer=thread" in rustflags

    if has_sanitizer and (is_tsan or spec["kind"] == "program"):
        ldflags = _filter_out(ldflags, ["-fsanitize=%"])

    if (
        spec["kind"] == "program"
        and substs.get("OS_ARCH") == "WINNT"
        and _is_clang(substs.get("CC_TYPE"))
    ):
        ldflags.append(f"-L{topobjdir}/build/win32")
        ldflags.append("-lunwind")

    return " ".join(ldflags)


def _compute_host_ldflags(substs):
    host_ldflags = _as_list(substs.get("HOST_LDFLAGS"))
    if substs.get("HOST_OS_ARCH") == "WINNT":
        host_libpaths = _as_list(substs.get("HOST_LINKER_LIBPATHS_BAT"))
    else:
        host_libpaths = _as_list(substs.get("HOST_LINKER_LIBPATHS"))
    return " ".join(host_ldflags + host_libpaths)


def _compute_cargo_target_linker_path(substs, topsrcdir, host=False):
    is_winnt_host = substs.get("HOST_OS_ARCH") == "WINNT"
    name = "cargo-host-linker" if host else "cargo-linker"
    ext = ".bat" if is_winnt_host else ""
    return f"{topsrcdir}/build/{name}{ext}"


def _compute_target_cc(substs):
    cc = _as_list(substs.get("CC"))
    base = _as_list(substs.get("CC_BASE_FLAGS"))
    return _filter_out(cc, base)


def _compute_target_cxx(substs):
    cxx = _as_list(substs.get("CXX"))
    base = _as_list(substs.get("CXX_BASE_FLAGS"))
    return _filter_out(cxx, base)


def _compute_host_cc(substs):
    host_cc = _as_list(substs.get("HOST_CC"))
    base = _as_list(substs.get("HOST_CC_BASE_FLAGS"))
    return _filter_out(host_cc, base)


def _compute_host_cxx(substs):
    host_cxx = _as_list(substs.get("HOST_CXX"))
    base = _as_list(substs.get("HOST_CXX_BASE_FLAGS"))
    return _filter_out(host_cxx, base)


def _compute_cflags_for_triple(spec, substs, host=False):
    pass_base_only = _compute_pass_only_base_cflags(substs)

    if host:
        base = list(_as_list(substs.get("HOST_CC_BASE_FLAGS")))
        if substs.get("HOST_OS_ARCH") == "WINNT":
            base.append("-DUNICODE")
        if pass_base_only:
            return base
        computed = _as_list(spec.get("computed_host_cflags"))
        return base + computed

    base = list(_as_list(substs.get("CC_BASE_FLAGS")))
    if substs.get("OS_ARCH") == "WINNT":
        base.append("-DUNICODE")
    if pass_base_only:
        return base
    rust_lto = (
        _as_list(substs.get("MOZ_LTO_CFLAGS"))
        if _is_clang(substs.get("CC_TYPE"))
        else []
    )
    computed = _as_list(spec.get("computed_cflags"))
    pgo = _filter_out(_as_list(substs.get("PGO_CFLAGS")), ["-fprofile-generate%"])
    return base + rust_lto + computed + pgo


def _compute_cxxflags_for_triple(spec, substs, host=False):
    pass_base_only = _compute_pass_only_base_cflags(substs)

    if host:
        base = list(_as_list(substs.get("HOST_CXX_BASE_FLAGS")))
        if substs.get("HOST_OS_ARCH") == "WINNT":
            base.append("-DUNICODE")
        if pass_base_only:
            return base
        computed = _as_list(spec.get("computed_host_cxxflags"))
        return base + computed

    base = list(_as_list(substs.get("CXX_BASE_FLAGS")))
    if substs.get("OS_ARCH") == "WINNT":
        base.append("-DUNICODE")
    if pass_base_only:
        computed = _as_list(spec.get("computed_cxxflags"))
        return base + _filter(computed, ["-fno-aligned-new", "-fno-sized-deallocation"])
    rust_lto = (
        _as_list(substs.get("MOZ_LTO_CFLAGS"))
        if _is_clang(substs.get("CC_TYPE"))
        else []
    )
    computed = _as_list(spec.get("computed_cxxflags"))
    pgo = _filter_out(_as_list(substs.get("PGO_CFLAGS")), ["-fprofile-generate%"])
    return base + rust_lto + computed + pgo


def _compute_wrap_ld(substs, kind):
    if _is_clang_cl(substs.get("CC_TYPE")):
        ld = _as_str(substs.get("LINKER"))
    else:
        ld = _as_str(substs.get("CC"))
    return ld


def _compute_wrap_ld_cxx(substs, kind):
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


def compose_env(spec, substs, current_env, topsrcdir, topobjdir):
    env = dict(current_env)

    target = spec["target_triple"]
    host = substs.get("RUST_HOST_TARGET", "")
    target_suffix = _cc_env_suffix(target)
    host_suffix = _cc_env_suffix(host) if host else target_suffix

    env[f"CC_{host_suffix}"] = _as_str(_compute_host_cc(substs))
    env[f"CXX_{host_suffix}"] = _as_str(_compute_host_cxx(substs))
    env[f"AR_{host_suffix}"] = _as_str(substs.get("HOST_AR"))

    env[f"CC_{target_suffix}"] = _as_str(_compute_target_cc(substs))
    env[f"CXX_{target_suffix}"] = _as_str(_compute_target_cxx(substs))
    env[f"AR_{target_suffix}"] = _as_str(substs.get("AR"))

    env[f"CFLAGS_{host_suffix}"] = _as_str(
        _compute_cflags_for_triple(spec, substs, host=True)
    )
    env[f"CXXFLAGS_{host_suffix}"] = _as_str(
        _compute_cxxflags_for_triple(spec, substs, host=True)
    )
    env[f"CFLAGS_{target_suffix}"] = _as_str(_compute_cflags_for_triple(spec, substs))
    env[f"CXXFLAGS_{target_suffix}"] = _as_str(
        _compute_cxxflags_for_triple(spec, substs)
    )

    if _bool(substs.get("CARGO_INCREMENTAL")):
        env["CARGO_INCREMENTAL"] = _as_str(substs.get("CARGO_INCREMENTAL"))

    using_cache = _bool(substs.get("MOZ_USING_SCCACHE")) or _bool(
        substs.get("MOZ_USING_BUILDCACHE")
    )
    if using_cache:
        env["RUSTC_WRAPPER"] = _as_str(substs.get("CCACHE"))

    env["CARGO_TARGET_DIR"] = topobjdir
    env["RUSTFLAGS"] = compose_rustflags(spec, substs, current_env, topobjdir)
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
    if substs.get("MOZ_FOLD_LIBS"):
        env["MOZ_FOLD_LIBS"] = _as_str(substs.get("MOZ_FOLD_LIBS"))
    env["PYTHON3"] = _as_str(substs.get("PYTHON3"))
    if substs.get("CARGO_PROFILE_RELEASE_OPT_LEVEL") is not None:
        env["CARGO_PROFILE_RELEASE_OPT_LEVEL"] = _as_str(
            substs.get("CARGO_PROFILE_RELEASE_OPT_LEVEL")
        )
    if substs.get("CARGO_PROFILE_DEV_OPT_LEVEL") is not None:
        env["CARGO_PROFILE_DEV_OPT_LEVEL"] = _as_str(
            substs.get("CARGO_PROFILE_DEV_OPT_LEVEL")
        )

    bindgen_clang_args = _filter(
        _as_list(substs.get("BINDGEN_SYSTEM_FLAGS")), ["--target=%"]
    )
    env["BINDGEN_EXTRA_CLANG_ARGS"] = " ".join(bindgen_clang_args)

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
            parts.extend(["encoding_rs", "any_all_workaround"])
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

    if spec["kind"].startswith("host-"):
        env["MOZ_CARGO_WRAP_LD"] = _compute_wrap_host_ld(substs)
        env["MOZ_CARGO_WRAP_LD_CXX"] = _compute_wrap_host_ld_cxx(substs)
        env["MOZ_CARGO_WRAP_LDFLAGS"] = _compute_host_ldflags(substs)
    else:
        env["MOZ_CARGO_WRAP_LD"] = _compute_wrap_ld(substs, spec["kind"])
        env["MOZ_CARGO_WRAP_LD_CXX"] = _compute_wrap_ld_cxx(substs, spec["kind"])
        env["MOZ_CARGO_WRAP_LDFLAGS"] = _compute_cargo_wrap_ldflags(
            spec, substs, topobjdir
        )

    env["MOZ_CARGO_WRAP_HOST_LD"] = _compute_wrap_host_ld(substs)
    env["MOZ_CARGO_WRAP_HOST_LD_CXX"] = _compute_wrap_host_ld_cxx(substs)
    env["MOZ_CARGO_WRAP_HOST_LDFLAGS"] = _compute_host_ldflags(substs)

    _set_sanitizer_intercept_options(env, substs)

    return env


def compose_argv(spec, substs, current_env):
    cargo = _as_str(substs.get("CARGO"))

    subcommand = spec.get("subcommand", "build")
    is_main_build = subcommand == "build"

    argv = [cargo]
    if is_main_build:
        argv.append("rustc")
        argv.append("--timings")
    else:
        argv.append(subcommand)

    argv.extend(_compute_cargo_build_flags(spec, substs, current_env))

    cargo_extra = _as_list(current_env.get("CARGO_EXTRA_FLAGS"))
    argv.extend(cargo_extra)

    argv.extend(spec.get("cargo_extra_cli_flags", []))

    kind = spec["kind"]

    if kind in ("library", "host-library"):
        argv.append("--lib")

    if spec["is_megazord"]:
        argv.extend(["--crate-type", "staticlib"])

    if kind in ("library", "test"):
        argv.append(f"--target={spec['target_triple']}")
    elif kind in ("host-library", "host-program"):
        argv.append(f"--target={substs.get('RUST_HOST_TARGET')}")
    elif kind == "program":
        argv.append(f"--target={spec['target_triple']}")

    features = spec.get("features", [])
    feat_str = (
        ",".join(features)
        + ("," if features else "")
        + "mozilla-central-workspace-hack"
    )
    argv.append("--features")
    argv.append(feat_str)

    if is_main_build and kind in ("library", "host-library", "program"):
        argv.append("--")
        argv.extend(_compute_rustc_flags(spec, substs))

    return argv


def compose(spec, substs, current_env, topsrcdir, topobjdir):
    """Return (argv, env) to invoke cargo for this spec."""
    env = compose_env(spec, substs, current_env, topsrcdir, topobjdir)
    argv = compose_argv(spec, substs, env)
    return argv, env
