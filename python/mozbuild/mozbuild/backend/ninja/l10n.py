# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import os

import mozpack.path as mozpath


class L10nMixin:
    def _emit_l10n_statements(self, writer):
        """Emit per-locale phony targets for l10n repacks.

        For each non-en-US locale in the app's locale list:
          merge-X         -> l10n_merge
          l10n-X          -> l10n_stage (mode=langpack), needs merge-X
          chrome-X        -> l10n_stage (mode=chrome),   needs merge-X
          package-langpack-X -> package_langpack, needs l10n-X
          repackage-zip-X    -> l10n_repackage,   needs l10n-X
          installers-X    -> phony cascade; on WINNT also runs
                             make package-win32-installer via the
                             winnt_l10n_installer rule.

        en-US is skipped: it is the default build and `mach
        repackage_single_locales` filters it out before dispatching.
        """
        substs = self.environment.substs
        app = substs.get("MOZ_BUILD_APP")
        cfg = self._L10N_APPS.get(app)
        if not cfg:
            return

        locale_list_path = mozpath.join(self._topsrcdir, cfg["locale_list"])
        if not os.path.isfile(locale_list_path):
            return

        with open(locale_list_path, encoding="utf-8") as f:
            locales = [
                line.strip() for line in f if line.strip() and not line.startswith("#")
            ]
        locales = [loc for loc in locales if loc != "en-US"]
        if not locales:
            return

        # Re-emit build.ninja when shipped-locales changes (adds/removes a
        # locale -> different phony set).
        self.backend_input_files.add(mozpath.normsep(locale_list_path))

        # Stamps and dirs.
        stamp_dir = mozpath.join(self._topobjdir, "_l10n")
        merge_root = mozpath.join(stamp_dir, "merge-dir")
        l10n_toml = mozpath.join(self._topsrcdir, cfg["l10n_toml"])
        l10n_manifest = mozpath.join(self._topobjdir, "l10n-manifest.json")
        l10n_basedir = substs.get("L10NBASEDIR", "")
        xpi_stage_root = mozpath.join(self._topobjdir, "dist/xpi-stage")
        dist_bin = mozpath.join(self._topobjdir, "dist/bin")
        l10n_stage_root = mozpath.join(self._topobjdir, "dist/l10n-stage")
        moz_pkg_dir = substs.get("MOZ_APP_NAME", "")
        moz_pkg_appname = substs.get("MOZ_PKG_APPNAME", moz_pkg_dir)
        moz_pkg_version = substs.get("MOZ_PKG_VERSION", "")
        moz_pkg_platform = substs.get("MOZ_PKG_PLATFORM", "")
        moz_simple_pkg = substs.get("MOZ_SIMPLE_PACKAGE_NAME", "")
        pkg_suffix = substs.get("PKG_SUFFIX", "")
        pkg_langpack_path = substs.get("PKG_LANGPACK_PATH", "")
        moz_app_version = substs.get("MOZ_APP_VERSION", "")
        moz_app_maxver = substs.get("MOZ_APP_MAXVERSION", "")
        moz_app_displayname = substs.get("MOZ_APP_DISPLAYNAME", "")
        moz_langpack_eid_host = substs.get("MOZ_LANGPACK_EID_HOST", "")
        moz_widget_toolkit = substs.get("MOZ_WIDGET_TOOLKIT", "")
        moz_pkg_format = substs.get("MOZ_PKG_FORMAT", "")
        moz_pkg_respath = substs.get("MOZ_PKG_RESPATH", "")
        os_arch = substs.get("OS_ARCH", "")
        moz_packager_minify = substs.get("MOZ_PACKAGER_MINIFY", "")
        moz_package_extra_args = substs.get("MOZ_PACKAGE_EXTRA_ARGS", "")
        make = substs.get("GMAKE") or "mozmake"
        is_winnt = os_arch == "WINNT"
        is_cocoa = moz_widget_toolkit == "cocoa"

        # mach.cmd on Windows is the only invocable; mach is a shell script.
        mach_path = mozpath.join(
            self._topsrcdir, "mach.cmd" if os.name == "nt" else "mach"
        )

        writer.newline()
        writer.comment("------ per-locale l10n ------")
        writer.newline()

        for ab_cd in locales:
            merge_target = mozpath.join(merge_root, ab_cd)
            merge_stamp = mozpath.join(stamp_dir, f"merge-{ab_cd}.stamp")
            l10n_stamp = mozpath.join(stamp_dir, f"l10n-{ab_cd}.stamp")
            chrome_stamp = mozpath.join(stamp_dir, f"chrome-{ab_cd}.stamp")
            xpi_stage = mozpath.join(xpi_stage_root, f"locale-{ab_cd}")

            # merge-X: writes the merge tree; the executor touches the stamp.
            self._emit_run_edge(
                writer,
                merge_stamp,
                [
                    {
                        "module": "mozbuild.action.l10n_merge",
                        "args": [
                            f"--locale={ab_cd}",
                            f"--config={mozpath.normsep(l10n_toml)}",
                            f"--l10n-base={l10n_basedir}",
                            f"--target={mozpath.normsep(merge_root)}",
                        ],
                    }
                ],
                f"L10N MERGE {ab_cd}",
                stamp=True,
            )
            writer.build(
                f"merge-{ab_cd}",
                "phony",
                inputs=self._rel_n_path(merge_stamp),
            )

            # l10n-X: stage merged content into dist/xpi-stage/locale-X.
            # `l10n-manifest.json` is written by `CommonBackend.consume_finished`
            # (see backend/common.py); declared implicit so ninja re-stages
            # when a moz.build edit changes manifest contents.
            stage_implicit = [
                self._rel_n_path(merge_stamp),
                self._rel_n_path(l10n_manifest),
            ]
            self._emit_run_edge(
                writer,
                l10n_stamp,
                [
                    {
                        "module": "mozbuild.action.l10n_stage",
                        "args": [
                            f"--locale={ab_cd}",
                            f"--manifest={mozpath.normsep(l10n_manifest)}",
                            f"--merge-tree={mozpath.normsep(merge_target)}",
                            f"--dest={mozpath.normsep(xpi_stage)}",
                            f"--topsrcdir={mozpath.normsep(self._topsrcdir)}",
                            f"--topobjdir={mozpath.normsep(self._topobjdir)}",
                            "--mode=langpack",
                        ],
                    }
                ],
                f"L10N STAGE {ab_cd} (langpack)",
                implicit=stage_implicit,
                stamp=True,
            )
            writer.build(
                f"l10n-{ab_cd}",
                "phony",
                inputs=self._rel_n_path(l10n_stamp),
            )

            # chrome-X: stage merged content into dist/bin.
            self._emit_run_edge(
                writer,
                chrome_stamp,
                [
                    {
                        "module": "mozbuild.action.l10n_stage",
                        "args": [
                            f"--locale={ab_cd}",
                            f"--manifest={mozpath.normsep(l10n_manifest)}",
                            f"--merge-tree={mozpath.normsep(merge_target)}",
                            f"--dest={mozpath.normsep(dist_bin)}",
                            f"--topsrcdir={mozpath.normsep(self._topsrcdir)}",
                            f"--topobjdir={mozpath.normsep(self._topobjdir)}",
                            "--mode=chrome",
                        ],
                    }
                ],
                f"L10N STAGE {ab_cd} (chrome)",
                implicit=stage_implicit,
                stamp=True,
            )
            writer.build(
                f"chrome-{ab_cd}",
                "phony",
                inputs=self._rel_n_path(chrome_stamp),
            )

            # package-langpack-X: produce the langpack .xpi.
            # Diverges from package-name.mk's PKG_LANGPACK_BASENAME: we
            # always include ``ab_cd`` because ninja parses all locales'
            # rules in one graph, so output paths must be unique per
            # locale. The makefile shares a single path and relies on
            # one-locale-per-invocation to avoid collision. Per-locale CI
            # tasks that need the canonical ``target.langpack.xpi``
            # artifact name copy/rename at upload time.
            if moz_simple_pkg:
                langpack_basename = f"{moz_simple_pkg}.{ab_cd}.langpack"
            else:
                langpack_basename = (
                    f"{moz_pkg_appname}-{moz_pkg_version}.{ab_cd}.langpack"
                )
            langpack_file = mozpath.join(
                self._topobjdir,
                "dist",
                pkg_langpack_path + f"{langpack_basename}.xpi",
            )
            # LOCALE_SRCDIR for non-en-US locales is REAL_LOCALE_MERGEDIR
            # minus the trailing "/locales" of the LOCALE_RELATIVEDIR (see
            # config/config.mk's EXPAND_LOCALE_SRCDIR).
            relativedir = cfg["relativedir"].removesuffix("/locales")
            metadata = mozpath.join(merge_target, relativedir, "langpack-metadata.ftl")
            include_args = ["--include=chrome", "--include=localization"]
            if cfg["dist_subdir"]:
                include_args.append(f"--include={cfg['dist_subdir']}")
            include_args.append("--include=manifest.json")
            writer.build(
                self._rel_n_path(langpack_file),
                "package_langpack",
                inputs=None,
                implicit=self._rel_n_path(l10n_stamp),
                variables={
                    "locale": ab_cd,
                    "xpi_stage": self._rel_n_path(xpi_stage),
                    "metadata": self._rel_n_path(metadata),
                    "eid": f"langpack-{ab_cd}@{moz_langpack_eid_host}",
                    "app_version": moz_app_version,
                    "max_app_ver": moz_app_maxver,
                    "app_name": moz_app_displayname,
                    "l10n_basedir": l10n_basedir,
                    "include_args": " ".join(include_args),
                },
            )
            writer.build(
                f"package-langpack-{ab_cd}",
                "phony",
                inputs=self._rel_n_path(langpack_file),
            )

            # repackage-zip-X: re-pack the en-US dist with localized content.
            # Diverges from package-name.mk's PKG_BASENAME: we always
            # include ``ab_cd`` for the same reason as the langpack rule
            # above (ninja-graph uniqueness; canonical names get
            # restored at upload time).
            if moz_simple_pkg:
                pkg_basename = f"{moz_simple_pkg}.{ab_cd}"
            else:
                pkg_basename = (
                    f"{moz_pkg_appname}-{moz_pkg_version}.{ab_cd}.{moz_pkg_platform}"
                )
            package_filename = f"{pkg_basename}{pkg_suffix}"
            zip_out = mozpath.join(self._topobjdir, "dist", package_filename)
            unpack_distdir = mozpath.join(l10n_stage_root, moz_pkg_dir)
            if is_cocoa:
                stagedist = mozpath.join(unpack_distdir, moz_pkg_respath)
            else:
                stagedist = unpack_distdir
            winnt_args = ""
            if is_winnt:
                installer_dir = mozpath.join(
                    self._topobjdir, "browser/installer/windows"
                )
                winnt_args = (
                    f"--installer-dir={self._rel_n_path(installer_dir)} "
                    f"--real-locale-mergedir={self._rel_n_path(merge_target)}"
                )
            non_resource_args = ""  # NON_OMNIJAR_FILES: unset in-tree.
            minify_arg = "--minify" if moz_packager_minify else ""
            writer.build(
                self._rel_n_path(zip_out),
                "l10n_repackage",
                inputs=None,
                implicit=self._rel_n_path(l10n_stamp),
                variables={
                    "locale": ab_cd,
                    "mach": self._rel_n_path(mach_path),
                    "l10n_stage": self._rel_n_path(l10n_stage_root),
                    "unpack_distdir": self._rel_n_path(unpack_distdir),
                    "stagedist": self._rel_n_path(stagedist),
                    "xpi_stage": self._rel_n_path(xpi_stage),
                    "pkg_dir": moz_pkg_dir,
                    "pkg_format": moz_pkg_format,
                    "pkg_filename": package_filename,
                    "moz_widget_toolkit": moz_widget_toolkit,
                    "os_arch": os_arch,
                    "winnt_args": winnt_args,
                    "non_resource_args": non_resource_args,
                    "minify_arg": minify_arg,
                    "package_extra_args": moz_package_extra_args,
                },
            )
            writer.build(
                f"repackage-zip-{ab_cd}",
                "phony",
                inputs=self._rel_n_path(zip_out),
            )

            # installers-X: cascade (Part 14: clobber + l10n + langpack +
            # repackage; WINNT also runs `package-win32-installer` via
            # make). We omit clobber-X here: package_langpack /
            # l10n_repackage rewrite their dest dirs themselves and
            # ninja's edge ordering already gates downstream on
            # l10n-X completing.
            installer_deps = [
                f"l10n-{ab_cd}",
                f"package-langpack-{ab_cd}",
                f"repackage-zip-{ab_cd}",
            ]
            if is_winnt:
                winnt_stamp = mozpath.join(stamp_dir, f"winnt-installer-{ab_cd}.stamp")
                locales_dir = mozpath.join(self._topobjdir, app, "locales")
                self._emit_run_edge(
                    writer,
                    winnt_stamp,
                    [
                        {
                            "module": "mozbuild.action.winnt_l10n_installer",
                            "args": [
                                f"--locale={ab_cd}",
                                f"--make={make}",
                                f"--locales-dir={mozpath.normsep(locales_dir)}",
                            ],
                        }
                    ],
                    f"WINNT INSTALLER {ab_cd}",
                    implicit=[
                        f"l10n-{ab_cd}",
                        f"package-langpack-{ab_cd}",
                        f"repackage-zip-{ab_cd}",
                    ],
                    stamp=True,
                )
                installer_deps.append(self._rel_n_path(winnt_stamp))
            writer.build(
                f"installers-{ab_cd}",
                "phony",
                inputs=installer_deps,
            )
