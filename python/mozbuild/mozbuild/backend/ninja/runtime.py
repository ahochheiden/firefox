# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path

import mozpack.path as mozpath
from mozfile import json

from mozbuild.util import cpu_count


class RuntimeMixin:
    def build(self, config, output, jobs, verbose, what=None):
        """Invoke ninja for `mach build`. Targets default to all.

        `./mach build clean` is special-cased: invokes `ninja -t clean`
        first to remove every output ninja knows about, then continues
        with a full build. (ninja's `-t clean` is not a build target;
        passing "clean" as a regular target would fail.) Other targets
        passed alongside "clean" still build after the clean.

        After ninja completes, replays `.ninja_log` through the resource
        monitor as per-edge markers so the build profile gets per-target
        timing (richer than mozmake's link-only markers)."""
        ninja = config.substs.get("NINJA", "ninja")

        # Mirror mozmake's `export INCLUDE` / `export LIB` (config/config.mk):
        # cl/ml64/link rely on these env vars to find SDK headers and libs.
        env = os.environ.copy()
        for var in ("INCLUDE", "LIB"):
            val = config.substs.get(var)
            if val:
                env[var] = val

        # Apply the mozconfig's `mk_add_options "export X=Y"` to the build
        # environment, the way `.mozconfig.mk` does for mozmake (see
        # `_mozconfig_exports`). Every command ninja spawns inherits this,
        # matching the make backend; e.g. the cross-macOS mozconfig's PATH
        # addition that puts clang's `dsymutil` (and cctools) on PATH for
        # rustc, without which linking rust programs fails.
        env.update(self._mozconfig_exports(config))

        env.setdefault("NINJA_STATUS", "[%f/%t %e %E %r] ")

        def _run(cmd, env_override=None):
            """Run ninja, piping combined stdout/stderr through `output`."""
            proc = subprocess.Popen(
                cmd,
                env=env_override if env_override is not None else env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                universal_newlines=True,
            )
            try:
                for line in proc.stdout:
                    output.on_stdout_line(line.rstrip())
                return proc.wait()
            except KeyboardInterrupt:
                try:
                    proc.terminate()
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                return 130  # conventional "interrupted" exit code

        targets = list(what) if what else []
        if "clean" in targets:
            targets = [t for t in targets if t != "clean"]
            output.write_line("ninja: cleaning all outputs (`-t clean`)")
            rc = _run([ninja, "-C", config.topobjdir, "-t", "clean"])
            if rc != 0:
                return rc

        # Automation tier targets (`automation/<tier>` and the bare tier
        # names) are make targets, not ninja targets. Strip them from the
        # ninja invocation; the post-build dispatch below routes them to
        # mozmake. Without this strip, ninja would fail unknown-target
        # for them.
        automation_target_names = (
            "package",
            "package-tests",
            "package-generated-sources",
            "buildsymbols",
            "uploadsymbols",
            "upload",
            "check",
        )
        targets = [
            t
            for t in targets
            if not (t.startswith("automation/") or t in automation_target_names)
        ]

        output.start_progress()

        cmd = [ninja, "-C", config.topobjdir, "--jobserver-pool"]
        if jobs == 0:
            jobs = (cpu_count() or 1) + 2
        if jobs:
            cmd += ["-j", str(jobs)]
        if verbose:
            cmd.append("-v")
        if targets:
            cmd += targets
        # Capture wall-clock just before invoking ninja so .ninja_log's
        # "ms since build start" timestamps can be anchored.
        ninja_start_wall = time.time()
        # Run ninja under a try/except so a ctrl+c during the build
        # still replays whatever made it into .ninja_log into the build
        # profile. Useful for benchmarking the early phase: let ninja
        # run for ~1m, ctrl+c, inspect the partial profile.
        rc = _run(cmd)
        if rc == 130:
            output.write_line(
                "ninja: interrupted; replaying partial .ninja_log into profile"
            )
        self._record_ninja_log_markers(config, output, ninja_start_wall)
        self._upload_ninja_log(config)

        if rc == 0:
            self._dump_sccache_stats(config, output, env)

        # A full `mach build` under the make backend runs the regular build
        # tiers (export, compile, misc, libs, tools); ninja only covers the
        # moz.build-derived graph (compile/link/generated/staging). The
        # `libs` and `tools` tiers still carry hand-written `Makefile.in`
        # recipes ninja doesn't run -- notably the macOS `.app` bundle
        # assembly (tools tier: browser/app, ipc/app, the updater) and the
        # Windows NSIS installer chain (the libs tier stages
        # `TOOLKIT_NSIS_FILES` such as `locales.nsi` into `instgen`, which
        # the tools tier's `test_stub_installer` then consumes). Delegate
        # both tiers to mozmake after the compile, in order. Neither relinks:
        # linking is the `compile` tier (config/rules.mk), which ninja owns;
        # `libs`/`tools` only install files and run those recipes
        # (config/recurse.mk runs `recurse_<tier>`, not a recompile). This
        # delegation goes away as the remaining recipes migrate to moz.build
        # primitives (Bug 2038789).
        if rc == 0 and not what:
            make, make_env = self._make_invocation(config, env)
            for tier in ("libs", "tools"):
                output.write_line(f"ninja: running `{tier}` tier via {make}")
                # `-s` (silent) + `--no-print-directory` to match the quiet
                # output of a normal `mach build`: ninja pipes this straight
                # through, so without these make echoes every recipe and each
                # sub-make's enter/leave-directory banner.
                rc = _run(
                    [
                        make,
                        "-C",
                        config.topobjdir,
                        "-j",
                        str(jobs) if jobs else "1",
                        "-s",
                        "--no-print-directory",
                        tier,
                    ],
                    env_override=make_env,
                )
                if rc != 0:
                    break

        # Automation-tier dispatch. When `MOZ_AUTOMATION` is set in the
        # environment, mozharness expects the build step to also produce
        # package artifacts (`dist/target.*`), symbols, etc. — its
        # post-build `_get_package_metrics` step (testing/mozharness/
        # mozharness/mozilla/building/buildbase.py:1067-1073) fails with
        # `could not determine packageName` if `dist/target.{tar.xz,tar.bz2,
        # zip,dmg,apk}` is missing. Under the make backend, `client.mk`
        # routes `mach build` through `automation/build` which depends on
        # `automation/<tier>` for each enabled `MOZ_AUTOMATION_<TIER>=1`
        # (see `build/moz-automation.mk:73`). Ninja doesn't know about
        # those tiers, so we delegate to mozmake for the remainder.
        # mozmake's `automation/build` target is a no-op when no
        # `MOZ_AUTOMATION_<TIER>=1` vars are set, so it's safe to invoke
        # whenever `MOZ_AUTOMATION` is set, regardless of which tiers CI
        # has enabled for this job.
        #
        # Skipped when `what` is non-empty: explicit targets indicate a
        # developer-driven partial build (e.g. `./mach build firefox`),
        # not the full automation flow.
        if rc == 0 and not what and os.environ.get("MOZ_AUTOMATION"):
            make, make_env = self._make_invocation(config, env)
            output.write_line(
                f"ninja: dispatching automation tiers via {make} automation/build"
            )
            # `make_env` carries the mozconfig `mk_add_options "export X=Y"`
            # applied to `env` above -- notably the automation tier defaults
            # from `build/mozconfig.automation`; without them
            # `automation/build` would be a no-op except for
            # `MOZ_AUTOMATION_PACKAGE_TESTS` (which CI sets in env).
            rc = _run(
                [
                    make,
                    "-C",
                    config.topobjdir,
                    "-j",
                    str(jobs) if jobs else "1",
                    "automation/build",
                ],
                env_override=make_env,
            )

        # Explicit `automation/<tier>` (or bare tier names like `package`)
        # passed as targets: run ninja for everything else, then dispatch
        # the remaining targets through mozmake. Mirrors what `client.mk`
        # would do under the make backend.
        if what:
            automation_targets = [
                t
                for t in what
                if t.startswith("automation/") or t in automation_target_names
            ]
            if automation_targets and rc == 0:
                make, make_env = self._make_invocation(config, env)
                output.write_line(
                    f"ninja: dispatching {' '.join(automation_targets)} via {make}"
                )
                rc = _run(
                    [
                        make,
                        "-C",
                        config.topobjdir,
                        "-j",
                        str(jobs) if jobs else "1",
                    ]
                    + automation_targets,
                    env_override=make_env,
                )

        return rc

    def _make_invocation(self, config, env):
        """Return ``(make, make_env)`` for delegating a tier to the make
        backend: the configured mozmake binary, and a copy of the build
        env with the jobserver flags stripped (``MAKEFLAGS``/``MFLAGS``
        passed through to a nested make cause a CI incompatibility).
        ``make_env`` still carries the mozconfig ``mk_add_options
        "export X=Y"`` already applied to ``env``."""
        make = config.substs.get("GMAKE") or "mozmake"
        make_env = {k: v for k, v in env.items() if k not in ("MAKEFLAGS", "MFLAGS")}
        return make, make_env

    def _mozconfig_exports(self, config):
        """Env vars set by the mozconfig's `mk_add_options "export X=Y"`.

        The make backend applies these via `$objdir/.mozconfig.mk`
        (`-include`d by `config/config.mk`); the Ninja backend drives the
        compile directly and bypasses that file, so they must be applied
        to the build environment explicitly. Without it, e.g. the
        cross-macOS mozconfig's `PATH`/`LD_LIBRARY_PATH` additions (which
        put cctools and clang's `dsymutil` on PATH for rustc) are dropped
        and linking rust programs fails.

        The values were shell-expanded when the mozconfig was evaluated
        (so an entry like `PATH` already embeds the prior `$PATH`), so
        they are assigned directly, overriding the inherited environment
        the same way `.mozconfig.mk` does for mozmake."""
        exports = {}
        mozconfig = getattr(config, "mozconfig", None)
        for line in (mozconfig or {}).get("make_extra") or []:
            if line.startswith("export "):
                key, sep, value = line[len("export ") :].partition("=")
                if sep == "=":
                    exports[key] = value
        return exports

    def _dump_sccache_stats(self, config, output, env):
        """Mirror the top-level `default::` recipe in `Makefile.in` that
        writes `sccache-stats.json`. The Ninja backend builds everything
        itself and only delegates `automation/build` to mozmake, so it
        never runs that make target -- without this the CI
        `sccache-stats.json` artifact is missing and mozharness'
        `_load_sccache_stats` fatals when `USE_SCCACHE` is set
        (testing/mozharness/mozharness/mozilla/building/buildbase.py).

        Reads the build `env`, not `os.environ`: the gating/output vars
        come from the mozconfig (e.g. `build/mozconfig.cache` exports
        `SCCACHE_VERBOSE_STATS`) and reach make's environment but not the
        bare mach process environment. Gates on `USE_SCCACHE` to match
        mozharness' fatal condition exactly, and writes under `UPLOAD_DIR`
        when `UPLOAD_PATH` is unset (the make recipe uses `UPLOAD_PATH`)."""
        if env.get("USE_SCCACHE") != "1" or env.get("SCCACHE_DISABLE") == "1":
            return
        ccache = config.substs.get("CCACHE")
        upload_dir = env.get("UPLOAD_PATH") or env.get("UPLOAD_DIR")
        if not ccache or not upload_dir:
            return
        ccache_cmd = shlex.split(ccache)
        stats_path = os.path.join(upload_dir, "sccache-stats.json")
        try:
            stats = subprocess.run(
                ccache_cmd + ["--show-adv-stats", "--stats-format=json"],
                capture_output=True,
                env=env,
                check=False,
            )
        except OSError as e:
            output.write_line(f"ninja: could not run sccache for stats: {e}")
            return
        if stats.returncode != 0:
            output.write_line(
                f"ninja: sccache stats exited {stats.returncode}; "
                f"not writing {stats_path}"
            )
            return
        try:
            Path(upload_dir).mkdir(parents=True, exist_ok=True)
            Path(stats_path).write_bytes(stats.stdout)
        except OSError as e:
            output.write_line(f"ninja: failed to write {stats_path}: {e}")
            return
        output.write_line(f"ninja: wrote sccache stats to {stats_path}")

    def _upload_ninja_log(self, config):
        """In automation, copy `.ninja_log` into the upload dir so it is
        kept as a build artifact, the same way `sccache.log` is."""
        upload_path = os.environ.get("UPLOAD_PATH")
        if "MOZ_AUTOMATION" not in os.environ or not upload_path:
            return
        log_path = mozpath.join(config.topobjdir, ".ninja_log")
        if os.path.exists(log_path):
            shutil.copy(log_path, mozpath.join(upload_path, "ninja.log"))

    def _record_ninja_log_markers(self, config, output, ninja_start_wall):
        """Parse `<topobjdir>/.ninja_log` and emit per-edge resource
        markers. ninja records `start_ms end_ms` per edge relative to
        the start of the build; we anchor those against
        `ninja_start_wall` and feed each edge as a marker on the active
        SystemResourceMonitor.

        The monitor lives on `output` (the BuildOutputManager), not on
        the driver — `BuildDriver` doesn't expose it directly."""
        log_path = mozpath.join(config.topobjdir, ".ninja_log")
        if not Path(log_path).exists():
            return
        monitor = getattr(output, "monitor", None)
        if monitor is None:
            return
        resources = getattr(monitor, "resources", None)
        if resources is None or resources.start_time is None:
            return
        self._load_rust_lib_outputs_from_disk()
        # With explicit per-crate rustc edges there is no cargo --timings
        # parent/sub-marker model, so every rust edge buckets under a single
        # `Rust` marker rather than per-crate `Rust:<crate>` rows.
        self._explicit_rust_edges = bool(
            getattr(config, "substs", {}).get(
                "MOZ_NINJA_EXPERIMENTAL_EXPLICIT_RUSTC_EDGES"
            )
        )
        # ninja writes one `.ninja_log` line per output, so a
        # multi-output edge (IPDL/WebIDL/XPIDL codegen) shows up as N
        # lines that share one start/end and one command hash. Group by
        # `(command_hash, start_ms, end_ms)` so an edge's outputs collapse
        # into a single marker; without this the profile fills with
        # thousands of identical-duration entries, one per generated file.
        edges = {}  # (hash, start_ms, end_ms) -> [output_path, ...]
        order = []  # first-seen edge keys, for stable marker ordering
        try:
            with Path(log_path).open(encoding="utf-8") as f:
                for raw in f:
                    line = raw.rstrip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("\t")
                    if len(parts) < 5:
                        continue
                    try:
                        start_ms = int(parts[0])
                        end_ms = int(parts[1])
                        log_mtime = int(parts[2])
                    except ValueError:
                        continue
                    output_path = parts[3]
                    command_hash = parts[4]
                    # Filter out entries from prior ninja sessions.
                    # `.ninja_log` accumulates across builds (Recompact
                    # only dedupes by output, never drops by age), and
                    # each entry's start_ms/end_ms are relative to the
                    # session that produced it — anchoring an old
                    # entry against this session's `ninja_start_wall`
                    # would falsely show the old edge as "ran this
                    # build".
                    #
                    # The log's `mtime` field is ninja's internal
                    # TimeStamp encoding (see ninja/src/disk_interface.cc):
                    #   * POSIX: ns since Unix epoch.
                    #   * Windows: 100-ns ticks since ~2001-01-01
                    #     (FILETIME minus 12622780800s, where 1970→2001
                    #     is 978307200s, so the platform conversion is
                    #     `value / 1e7 + 978307200`).
                    # If ninja bumps its log version and changes the
                    # encoding, this conversion needs an update.
                    if os.name == "nt":
                        mtime_unix = log_mtime / 1e7 + 978307200
                    else:
                        mtime_unix = log_mtime / 1e9
                    if mtime_unix < ninja_start_wall:
                        continue
                    key = (command_hash, start_ms, end_ms)
                    outs = edges.get(key)
                    if outs is None:
                        outs = []
                        edges[key] = outs
                        order.append(key)
                    outs.append(mozpath.normsep(output_path))
        except OSError:
            return
        # Track each cargo-edge's start wall-time so `_record_cargo_timings`
        # can anchor the per-crate sub-markers from `cargo-timing-<label>-*.html`
        # against the parent edge's real start.
        rust_edges = []  # list of (end_wall, start_mono, label)
        for command_hash, start_ms, end_ms in order:
            outs = edges[(command_hash, start_ms, end_ms)]
            kind, label = self._classify_ninja_group(outs)
            start_wall = ninja_start_wall + start_ms / 1000.0
            end_wall = ninja_start_wall + end_ms / 1000.0
            start_mono = resources.convert_to_monotonic_time(start_wall)
            end_mono = resources.convert_to_monotonic_time(end_wall)
            # Multi-output edges keep a clean `label` (cargo timings glob
            # on it) but show the fan-out in the marker text.
            extra = len(outs) - 1
            if extra:
                text = f"{label} (+{extra} output{'s' if extra != 1 else ''})"
            else:
                text = label
            resources.record_marker(
                kind, start_mono, end_mono, {"type": "Text", "text": text}
            )
            # Cargo edges get per-crate sub-markers from their
            # `--timings` HTML; record the parent's start so
            # `_record_cargo_timings` can anchor them inside its span.
            if kind.startswith("Rust:"):
                rust_edges.append((end_wall, start_mono, label))
        if rust_edges:
            self._record_cargo_timings(config, resources, rust_edges)

    def _record_cargo_timings(self, config, resources, rust_edges):
        """Find cargo's `--timings` HTML reports and emit per-crate
        sub-markers under the parent edge's `Rust:<label>` name,
        anchored at the edge's `start_mono` from `.ninja_log` so each
        sub-marker lands inside the parent's time slot.

        Cargo writes one HTML per invocation to
        `<CARGO_TARGET_DIR>/cargo-timings/cargo-timing-<ts>.html`, and
        Firefox sets `CARGO_TARGET_DIR=<topobjdir>` for every rust edge
        so all reports share one directory. `cargo_build.py` renames
        each invocation's HTML to `cargo-timing-<label>-<ts>.html` so
        we can pair HTML to edge deterministically (instead of by
        mtime ordering, which is fragile with concurrent cargo edges).

        UNIT_DATA is the JS array embedded in the HTML; each entry has
        `name`, `version`, optional `target`, `start`, and `duration`,
        with times in seconds relative to the cargo invocation's
        start."""
        import glob
        from itertools import dropwhile, islice, takewhile

        timings_dir = mozpath.join(config.topobjdir, "cargo-timings")
        if not Path(timings_dir).is_dir():
            return
        for _end_wall, start_mono, label in rust_edges:
            candidates = sorted(
                glob.glob(mozpath.join(timings_dir, f"cargo-timing-{label}-*.html")),
                key=os.path.getmtime,
            )
            if not candidates:
                continue
            # Most recent labelled HTML wins (in case prior builds left
            # stale renamed reports behind).
            html_path = candidates[-1]
            try:
                with Path(html_path).open(encoding="utf-8") as fh:
                    unit_data = dropwhile(
                        lambda l: l.rstrip() != "const UNIT_DATA = [", fh
                    )
                    unit_data = islice(unit_data, 1, None)
                    lines = takewhile(lambda l: l.rstrip() != "];", unit_data)
                    entries = json.loads("[" + "".join(lines) + "]")
            except (OSError, ValueError):
                continue
            # Sub-markers share the parent's `Rust:<libname>` name so
            # the Firefox Profiler renders the wide parent span and
            # the individual crate sub-spans on the same row.
            crate_marker = f"Rust:{label}"
            for entry in entries:
                try:
                    name = "{} v{}{}".format(
                        entry["name"],
                        entry["version"],
                        entry.get("target", ""),
                    )
                    start_offset = entry["start"] or 0
                    duration = entry["duration"] or 0
                except (KeyError, TypeError):
                    continue
                resources.record_marker(
                    crate_marker,
                    start_mono + start_offset,
                    start_mono + start_offset + duration,
                    {"type": "Text", "text": name},
                )

    def _classify_ninja_group(self, outputs):
        """Classify a single ninja edge from its set of `.ninja_log`
        outputs. The primary output of a multi-output codegen edge
        carries the descriptive `<output>.runspec.json` sidecar
        (IPDL/WebIDL/XPIDL), while its sibling outputs fall through to
        the generic "Generated" bucket; prefer the first specific
        classification so the edge is named for what it is rather than
        for an arbitrary sibling output."""
        fallback = None
        for output_path in outputs:
            kind, label = self._classify_ninja_edge(output_path)
            if kind != "Generated":
                return kind, label
            if fallback is None:
                fallback = (kind, label)
        return fallback

    def _classify_ninja_edge(self, output_path):
        """Bucket a ninja edge's primary output by what kind of work it
        represents, for marker grouping in the build profile."""
        norm = mozpath.normsep(output_path)
        base = mozpath.basename(norm)
        ext = mozpath.splitext(norm)[1].lower()
        # Cargo edges produce `.lib`/`.a` outputs that look like
        # ordinary static libs. Use `Rust:<libname>` as the marker
        # name so the per-crate sub-build markers (emitted in
        # `_record_cargo_timings`) land on the same marker-chart row
        # as their parent cargo build, and so all rust rows cluster
        # alphabetically in the profiler.
        explicit = getattr(self, "_explicit_rust_edges", False)
        if norm in self._rust_lib_outputs:
            libname = self._rust_lib_outputs[norm]
            # Cargo's one-edge-per-lib build plus --timings sub-markers cluster
            # on a `Rust:<lib>` row; explicit per-crate edges have no
            # sub-markers, so they all bucket under a single `Rust`.
            if explicit:
                return "Rust", libname
            return f"Rust:{libname}", libname
        # Explicit per-crate rustc and build-script edges (flag on) also bucket
        # under `Rust`, identified by the spec sidecar they write -- otherwise
        # their .rlib/.exe/.json outputs fall into Generated/Program/Compile.
        if explicit:
            rustc_crate = self._rustc_edge_crate(norm)
            if rustc_crate:
                return "Rust", rustc_crate
        if ext in (".obj", ".o"):
            return "Compile", base
        if ext == ".wasm":
            return "WasmCompile", base
        if ext in (".lib", ".a"):
            return "StaticLib", base
        if ext in (".dll", ".so", ".dylib"):
            return "SharedLib", base
        if ext == ".exe":
            return "Program", base
        # Stamp / marker outputs have no descriptive basename. Run edges
        # write a sibling `<output>.runspec.json` whose `description` is the
        # label, so classify from that -- any new run edge is handled here
        # automatically, with no per-edge-type table to maintain.
        desc = self._runspec_description(norm)
        if desc:
            kind, _, text = desc.partition(" ")
            return kind, text or base
        return "Generated", base

    def _runspec_description(self, norm):
        """Description from a run edge's sibling `<output>.runspec.json`,
        or None. Lets the profiler label every run edge by its emitted
        description without enumerating edge types here."""
        path = Path(mozpath.join(self._topobjdir, norm) + ".runspec.json")
        if not path.exists():
            return None
        try:
            with path.open(encoding="utf-8") as fh:
                return json.load(fh).get("description")
        except (OSError, ValueError):
            return None

    def _rustc_edge_crate(self, norm):
        """Crate name for an explicit per-crate rustc or build-script edge,
        from the spec sidecar it writes, or None. The crate rustc edges write
        `<output>.rustc-spec.json` carrying `crate_name`; build-script run
        edges write `<output>.run-build-spec.json` carrying the package name in
        `env.CARGO_PKG_NAME`."""
        base = mozpath.join(self._topobjdir, norm)
        spec = base + ".rustc-spec.json"
        if os.path.exists(spec):
            try:
                with open(spec, encoding="utf-8") as fh:
                    return json.load(fh).get("crate_name")
            except (OSError, ValueError):
                return None
        run = base + ".run-build-spec.json"
        if os.path.exists(run):
            try:
                with open(run, encoding="utf-8") as fh:
                    return json.load(fh).get("env", {}).get("CARGO_PKG_NAME")
            except (OSError, ValueError):
                return None
        return None

    def _load_rust_lib_outputs_from_disk(self):
        """Repopulate `_rust_lib_outputs` from the persisted manifest.

        `./mach build` may run on a NinjaBackend instance that never
        went through emit (build-backend already up to date). Read
        the manifest written during the previous emit so cargo edges
        get classified correctly."""
        if self._rust_lib_outputs:
            return
        path = Path(mozpath.join(self._topobjdir, ".ninja-rust-libs.json"))
        if not path.exists():
            return
        try:
            with path.open(encoding="utf-8") as fh:
                self._rust_lib_outputs = json.load(fh)
        except (OSError, ValueError):
            pass

    # ---------------------------------------------------------------------
    # Data helpers
    # ---------------------------------------------------------------------
