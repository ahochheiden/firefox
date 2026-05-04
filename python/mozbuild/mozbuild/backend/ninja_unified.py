# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Ninja-only unified-chunk planner.

Two modes:

  * `legacy`: mirror `UnifiedSources.unified_source_mapping` 1:1.
    Behavior-equivalent to the recursive-make backend.

  * `crossdir`: experimental. Legacy chunks whose source count falls in the
    configured `[min_input, max_input]` range are dissolved into individual
    candidates. Candidates are bucketed by `(owner_key, compile_fingerprint,
    canonical_suffix, cwd_compatibility, scope_key)` and repacked into new
    planner-owned unified chunks of up to `max_output` sources. Legacy chunks
    outside the input range, and candidates that fail any eligibility check,
    pass through as legacy.

The planner doesn't know how to compile or link — the caller (NinjaBackend)
supplies per-UnifiedSources attribution (owner_key, compile_fingerprint, …)
and consumes the planned chunks, routing crossdir objects into the right
linkable's link inputs and skipping the dissolved legacy compiles.

`compile_fingerprint` and `owner_key` were added as `None` placeholders in
Phase 1; in this phase they become the keys that drive packing decisions.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, List, Optional, Tuple

import mozpack.path as mozpath


REJECT_REASONS = (
    "chunk-too-small",
    "chunk-too-large",
    "generated",
    "no-owner-key",
    "no-fingerprint",
    "unsupported-suffix",
    "third-party",
    "local-cap",
    "cwd-sensitive",
    "bucket-too-small",
    "per-source-flags",
    "unsupported-kind",
    "no-scope-key",
)

# Path prefixes that we conservatively keep on the legacy path: third-party
# code where unified-context behavior is most fragile (CWD-sensitive plugin
# diagnostics, vendored macros, ARC/no-ARC mixing, etc.).
THIRD_PARTY_PREFIXES = (
    "third_party/",
    "media/lib",
    "gfx/cairo",
    "gfx/skia",
    "third_party/libwebrtc",
)


@dataclass(frozen=True)
class UnifiedSourceRecord:
    """One source file headed for a unified chunk."""

    source: str
    objdir: str
    relobjdir: str
    canonical_suffix: str
    is_generated: bool = False
    declaration_index: int = 0
    # Crossdir-only metadata. Carried on every record so the planner can
    # roundtrip through legacy mode without losing information needed to
    # reconstruct rejection reasons.
    owner_key: Optional[Tuple] = None
    compile_fingerprint: Optional[Tuple] = None
    scope_key: Optional[str] = None
    cwd_compatibility: Optional[str] = None


@dataclass
class UnifiedChunk:
    """A single planned unified translation unit.

    `kind == "legacy"` chunks correspond 1:1 to a row in some
    `UnifiedSources.unified_source_mapping`; `kind == "crossdir"` chunks
    are planner-owned and live under `$topobjdir/ninja-unified/<scope>/`.
    """

    kind: str  # "legacy" | "crossdir"
    unified_file: str
    output_directory: str
    unified_source_path: str
    object_basename: str
    source_filenames: List[str]
    relobjdir: str
    objdir: str
    canonical_suffix: str
    compile_fingerprint: Optional[Tuple] = None
    owner_key: Optional[Tuple] = None
    records: List[UnifiedSourceRecord] = field(default_factory=list)
    # For crossdir chunks: the legacy `(unified_filename, objdir)` rows
    # whose sources were pulled into this chunk. Their compile edges must
    # be skipped, and their object paths must be removed from any
    # link-input list.
    dissolved_legacy: List[Tuple[str, str]] = field(default_factory=list)
    # The legacy chunk this chunk corresponds to (for kind == "legacy"
    # only; helps diagnostics reference the source mapping row).
    legacy_index: Optional[int] = None
    bucket_key_hash: Optional[str] = None


@dataclass(frozen=True)
class UnifiedAttribution:
    """Caller-supplied metadata for crossdir eligibility.

    Set fields to `None` to mark them as unresolved — the planner treats
    that as a hard rejection (e.g. `no-owner-key`, `no-fingerprint`).
    Boolean flags express conservative opt-outs that come from the build
    config (third-party path, local FILES_PER_UNIFIED_FILE override,
    per-source flags present, etc.).
    """

    owner_key: Optional[Tuple] = None
    compile_fingerprint: Optional[Tuple] = None
    scope_key: Optional[str] = None
    cwd_compatibility: Optional[str] = None
    kind: str = "target"  # "target" | "host" | "wasm"
    is_third_party: bool = False
    has_local_cap: bool = False
    has_per_source_flags: bool = False


@dataclass
class PlannerOptions:
    mode: str = "legacy"  # "legacy" | "crossdir"
    min_input: int = 1
    max_input: int = 8
    max_output: int = 16
    topobjdir: str = ""
    # Eligible canonical suffixes for crossdir mode. Conservative initial
    # set: target C++ only.
    crossdir_suffixes: Tuple[str, ...] = (".cpp",)


@dataclass
class PlanResult:
    chunks: List[UnifiedChunk]
    plan_diagnostics: List[dict]
    rejections: List[dict]


class NinjaUnifiedPlanner:
    """Collects `UnifiedSources` during emit and produces `UnifiedChunk`s.

    Inputs are duck-typed: `obj` must expose `unified_source_mapping`,
    `objdir`, `relobjdir`, `canonical_suffix`, `generated_files`. The
    accompanying `UnifiedAttribution` carries everything else.
    """

    def __init__(self, options: Optional[PlannerOptions] = None):
        self._options = options or PlannerOptions()
        self._inputs: List[Tuple[Any, UnifiedAttribution]] = []

    def add_unified_sources(
        self,
        obj,
        attribution: Optional[UnifiedAttribution] = None,
    ) -> None:
        """Register a `UnifiedSources`-shaped object plus its attribution.

        For legacy mode the attribution is unused; passing the legacy
        default is safe. Callers that don't have full owner/fingerprint
        information until later (the canonical case in `NinjaBackend`,
        which doesn't know all linkables until `consume_finished`) can
        register obj with a placeholder and supply attribution later via
        `attribute()`.
        """
        self._inputs.append((obj, attribution or UnifiedAttribution()))

    def attribute(self, attribute_fn) -> None:
        """Replace each registered obj's attribution via the supplied
        callable. The callable receives the original obj and must return
        a `UnifiedAttribution`. Order of registered objs is preserved.
        """
        self._inputs = [(obj, attribute_fn(obj)) for obj, _ in self._inputs]

    def plan(self) -> PlanResult:
        """Materialize chunks. Behavior depends on `options.mode`."""
        if self._options.mode == "legacy":
            return self._plan_legacy()
        if self._options.mode == "crossdir":
            return self._plan_crossdir()
        raise ValueError(
            f"Unknown ninja unified planner mode: {self._options.mode!r}"
        )

    # ------------------------------------------------------------------
    # Legacy mode

    def _plan_legacy(self) -> PlanResult:
        chunks: List[UnifiedChunk] = []
        diagnostics: List[dict] = []
        for obj, attribution in self._inputs:
            for legacy_idx, (uname, srcs) in enumerate(obj.unified_source_mapping):
                chunk = self._make_legacy_chunk(obj, legacy_idx, uname, srcs, attribution)
                chunks.append(chunk)
                diagnostics.append(_plan_entry(chunk, reason="legacy"))
        return PlanResult(chunks=chunks, plan_diagnostics=diagnostics, rejections=[])

    def _make_legacy_chunk(
        self, obj, legacy_idx, uname, srcs, attribution
    ) -> UnifiedChunk:
        objdir = mozpath.normsep(obj.objdir)
        relobjdir = obj.relobjdir
        canonical_suffix = obj.canonical_suffix
        generated = set(getattr(obj, "generated_files", ()) or ())
        source_list = list(srcs)
        records = [
            UnifiedSourceRecord(
                source=mozpath.normsep(src),
                objdir=objdir,
                relobjdir=relobjdir,
                canonical_suffix=canonical_suffix,
                is_generated=src in generated,
                declaration_index=src_idx,
                owner_key=attribution.owner_key,
                compile_fingerprint=attribution.compile_fingerprint,
                scope_key=attribution.scope_key,
                cwd_compatibility=attribution.cwd_compatibility,
            )
            for src_idx, src in enumerate(source_list)
        ]
        return UnifiedChunk(
            kind="legacy",
            unified_file=uname,
            output_directory=objdir,
            unified_source_path=mozpath.join(objdir, uname),
            object_basename=mozpath.splitext(uname)[0],
            source_filenames=source_list,
            relobjdir=relobjdir,
            objdir=objdir,
            canonical_suffix=canonical_suffix,
            compile_fingerprint=attribution.compile_fingerprint,
            owner_key=attribution.owner_key,
            records=records,
            legacy_index=legacy_idx,
        )

    # ------------------------------------------------------------------
    # Crossdir mode

    def _plan_crossdir(self) -> PlanResult:
        opts = self._options
        all_chunks: List[UnifiedChunk] = []
        diagnostics: List[dict] = []
        rejections: List[dict] = []

        # First pass: classify every legacy row as "kept legacy" or
        # "candidate for crossdir packing". Candidates carry enough info
        # to either be repacked or fall back to legacy with a recorded
        # rejection reason.
        candidates: List[Tuple[Any, int, str, str, UnifiedAttribution]] = []
        # candidate tuples: (obj, legacy_idx, uname, src_path, attribution)
        kept_legacy: List[Tuple[Any, int, str, list, UnifiedAttribution]] = []
        # kept tuples: (obj, legacy_idx, uname, srcs, attribution)

        for obj, attribution in self._inputs:
            for legacy_idx, (uname, srcs) in enumerate(obj.unified_source_mapping):
                size = len(srcs)
                # Size gate: only chunks within [min_input, max_input] are
                # eligible. Outside the range stays legacy with a recorded
                # reason so diagnostics show why.
                if size < opts.min_input:
                    kept_legacy.append((obj, legacy_idx, uname, list(srcs), attribution))
                    rejections.append(
                        _reject_entry(uname, obj.objdir, srcs, "chunk-too-small")
                    )
                    continue
                if size > opts.max_input:
                    kept_legacy.append((obj, legacy_idx, uname, list(srcs), attribution))
                    rejections.append(
                        _reject_entry(uname, obj.objdir, srcs, "chunk-too-large")
                    )
                    continue

                # Per-UnifiedSources eligibility checks. If any apply, the
                # whole legacy chunk stays — we don't dissolve it.
                whole_chunk_reject = self._whole_chunk_rejection(obj, attribution)
                if whole_chunk_reject:
                    kept_legacy.append((obj, legacy_idx, uname, list(srcs), attribution))
                    rejections.append(
                        _reject_entry(uname, obj.objdir, srcs, whole_chunk_reject)
                    )
                    continue

                # Per-source eligibility. Generated sources are conservative
                # rejects in this initial crossdir; if any source in the
                # chunk is generated, keep the whole chunk legacy.
                generated = set(getattr(obj, "generated_files", ()) or ())
                if any(s in generated for s in srcs):
                    kept_legacy.append((obj, legacy_idx, uname, list(srcs), attribution))
                    rejections.append(
                        _reject_entry(uname, obj.objdir, srcs, "generated")
                    )
                    continue

                # Eligible. Each source becomes a candidate.
                for s in srcs:
                    candidates.append((obj, legacy_idx, uname, s, attribution))

        # Emit kept-legacy chunks.
        for obj, legacy_idx, uname, srcs, attribution in kept_legacy:
            chunk = self._make_legacy_chunk(obj, legacy_idx, uname, srcs, attribution)
            all_chunks.append(chunk)
            diagnostics.append(_plan_entry(chunk, reason="legacy"))

        # Bucket candidates. scope_key is intentionally NOT part of the
        # bucket — once owner_key (root binary) and compile_fingerprint
        # (flag tuple) match, sources from any top-level subdir can be
        # packed together. scope_key still rides on attribution for
        # diagnostics and for choosing a deterministic output subdir
        # below; it just no longer narrows bucketing.
        buckets: dict = defaultdict(list)
        for cand in candidates:
            obj, legacy_idx, uname, src, attribution = cand
            bucket_key = (
                attribution.owner_key,
                attribution.compile_fingerprint,
                obj.canonical_suffix,
                attribution.cwd_compatibility,
            )
            buckets[bucket_key].append(cand)

        # Pack each bucket.
        for bucket_key, members in sorted(
            buckets.items(), key=lambda kv: _bucket_sort_key(kv[0])
        ):
            packed_chunks, packing_rejections = self._pack_bucket(bucket_key, members)
            for chunk in packed_chunks:
                all_chunks.append(chunk)
                diagnostics.append(_plan_entry(chunk, reason="crossdir"))
            rejections.extend(packing_rejections)

        return PlanResult(
            chunks=all_chunks,
            plan_diagnostics=diagnostics,
            rejections=rejections,
        )

    def _whole_chunk_rejection(self, obj, attribution: UnifiedAttribution) -> Optional[str]:
        """Return a rejection reason if the whole chunk shouldn't be
        dissolved into candidates, else None."""
        opts = self._options
        if obj.canonical_suffix not in opts.crossdir_suffixes:
            return "unsupported-suffix"
        if attribution.kind != "target":
            return "unsupported-kind"
        if attribution.owner_key is None:
            return "no-owner-key"
        if attribution.compile_fingerprint is None:
            return "no-fingerprint"
        # scope_key is informational now (used to choose an output
        # subdir for legibility). It no longer narrows bucketing, so
        # missing scope_key is no longer a rejection reason.
        if attribution.is_third_party:
            return "third-party"
        if attribution.has_local_cap:
            return "local-cap"
        if attribution.has_per_source_flags:
            return "per-source-flags"
        # CWD compatibility: require an explicit token. Sources flagged as
        # CWD-sensitive (e.g. third-party plugin checks that key on getcwd)
        # are rejected; conservatively require everything to claim a class.
        if attribution.cwd_compatibility is None:
            return "cwd-sensitive"
        # Conservative third-party path check on relobjdir as a backup to
        # the explicit `is_third_party` flag — catches anything the caller
        # hasn't tagged but lives under a known vendor tree.
        relobjdir = obj.relobjdir or ""
        if any(
            relobjdir == p.rstrip("/") or relobjdir.startswith(p)
            for p in THIRD_PARTY_PREFIXES
        ):
            return "third-party"
        return None

    def _pack_bucket(self, bucket_key, members):
        """Pack a single bucket's candidates into planner-owned chunks.

        `members` is a list of `(obj, legacy_idx, uname, src, attribution)`
        tuples sharing the same bucket_key. Sorted internally by
        normalized source path for deterministic output.
        """
        opts = self._options
        owner_key, fingerprint, suffix, cwd_compat = bucket_key

        # Sort by normalized source path. Stable across runs.
        members_sorted = sorted(
            members,
            key=lambda c: (mozpath.normsep(c[3]), c[1], c[2]),
        )
        bucket_hash = _bucket_key_hash(bucket_key)
        # Output subdir: pick the most common scope_key among the
        # bucket's candidates so cross-tree chunks land somewhere
        # legible. Falls back to "all" when nothing is set.
        scope_counts: dict = defaultdict(int)
        for c in members_sorted:
            sk = c[4].scope_key
            if sk:
                scope_counts[sk] += 1
        scope_key = (
            max(scope_counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
            if scope_counts
            else "all"
        )

        # Single-candidate buckets: nothing to gain by packing into a
        # planner-owned single-source TU. Spec defers this; reject as
        # `bucket-too-small` and let it fall back to legacy.
        if len(members_sorted) < 2:
            rejections = [
                _reject_entry(c[2], c[0].objdir, [c[3]], "bucket-too-small")
                for c in members_sorted
            ]
            # Caller still needs to keep the original legacy chunks for
            # these singletons. We reconstruct that by emitting the
            # original legacy rows (deduped by (objdir, uname)).
            kept = self._reconstruct_legacy_for(members_sorted)
            return kept, rejections

        chunks: List[UnifiedChunk] = []
        rejections: List[dict] = []

        # Walk sorted candidates, packing into chunks of up to max_output.
        index = 0
        chunk_idx = 0
        scope_dir = scope_key
        out_dir = mozpath.join(opts.topobjdir, "ninja-unified", scope_dir)
        # Stable file stem: include bucket hash to guarantee uniqueness
        # across distinct buckets within the same scope, and a serial
        # index across chunks within the bucket. Keep the basename short
        # for Windows path-length headroom.
        suffix_letter = suffix.lstrip(".") or "cpp"
        while index < len(members_sorted):
            slice_ = members_sorted[index : index + opts.max_output]
            index += len(slice_)

            stem = f"UnifiedNinja_{suffix_letter}_{scope_dir.replace('/', '_')}_{bucket_hash[:8]}_{chunk_idx}"
            unified_file = stem + suffix
            unified_path = mozpath.join(out_dir, unified_file)
            obj_path = mozpath.join(out_dir, stem + ".obj")  # backend may rewrite
            chunk_idx += 1

            source_paths = [mozpath.normsep(c[3]) for c in slice_]
            records = [
                UnifiedSourceRecord(
                    source=src,
                    objdir=mozpath.normsep(slice_[i][0].objdir),
                    relobjdir=slice_[i][0].relobjdir,
                    canonical_suffix=suffix,
                    is_generated=False,
                    declaration_index=i,
                    owner_key=owner_key,
                    compile_fingerprint=fingerprint,
                    scope_key=scope_key,
                    cwd_compatibility=cwd_compat,
                )
                for i, src in enumerate(source_paths)
            ]

            # Track the legacy chunks dissolved into this crossdir chunk.
            # Multiple candidates may come from the same legacy chunk; we
            # dedupe by (objdir, uname).
            dissolved = []
            seen_legacy = set()
            for c in slice_:
                obj_, _, uname, _, _ = c
                key = (mozpath.normsep(obj_.objdir), uname)
                if key in seen_legacy:
                    continue
                seen_legacy.add(key)
                dissolved.append(key)

            chunk = UnifiedChunk(
                kind="crossdir",
                unified_file=unified_file,
                output_directory=out_dir,
                unified_source_path=unified_path,
                object_basename=stem,
                source_filenames=source_paths,
                relobjdir=mozpath.relpath(out_dir, opts.topobjdir)
                if opts.topobjdir
                else mozpath.dirname(out_dir),
                objdir=out_dir,
                canonical_suffix=suffix,
                compile_fingerprint=fingerprint,
                owner_key=owner_key,
                records=records,
                dissolved_legacy=dissolved,
                bucket_key_hash=bucket_hash,
            )
            chunks.append(chunk)

        return chunks, rejections

    def _reconstruct_legacy_for(self, members):
        """Re-emit legacy chunks for candidates that ended up rejected at
        the bucket stage (bucket-too-small). Deduped by (objdir, uname)."""
        seen = set()
        out = []
        for obj, legacy_idx, uname, src, attribution in members:
            key = (mozpath.normsep(obj.objdir), uname)
            if key in seen:
                continue
            seen.add(key)
            srcs = dict(obj.unified_source_mapping)[uname]
            out.append(self._make_legacy_chunk(obj, legacy_idx, uname, srcs, attribution))
        return out


# ----------------------------------------------------------------------
# Diagnostics helpers


def _plan_entry(chunk: UnifiedChunk, reason: str) -> dict:
    return {
        "reason": reason,
        "kind": chunk.kind,
        "unified_file": chunk.unified_file,
        "output_directory": chunk.output_directory,
        "unified_source_path": chunk.unified_source_path,
        "object_basename": chunk.object_basename,
        "source_filenames": list(chunk.source_filenames),
        "relobjdir": chunk.relobjdir,
        "objdir": chunk.objdir,
        "canonical_suffix": chunk.canonical_suffix,
        "owner_key": _stringify(chunk.owner_key),
        "fingerprint_hash": _hash(chunk.compile_fingerprint),
        "bucket_key_hash": chunk.bucket_key_hash,
        "dissolved_legacy": [
            {"unified_file": uname, "objdir": objdir}
            for objdir, uname in chunk.dissolved_legacy
        ] if chunk.kind == "crossdir" else [],
    }


def _reject_entry(uname: str, objdir: str, sources: Iterable[str], reason: str) -> dict:
    return {
        "reason": reason,
        "original_unified_file": uname,
        "original_objdir": mozpath.normsep(objdir),
        "sources": [mozpath.normsep(s) for s in sources],
    }


def _bucket_key_hash(bucket_key: Tuple) -> str:
    import hashlib

    return hashlib.sha1(repr(bucket_key).encode("utf-8")).hexdigest()


def _hash(value) -> Optional[str]:
    if value is None:
        return None
    import hashlib

    return hashlib.sha1(repr(value).encode("utf-8")).hexdigest()


def _stringify(value) -> Any:
    if value is None:
        return None
    return repr(value)


def _bucket_sort_key(bucket_key: Tuple):
    return tuple("" if v is None else repr(v) for v in bucket_key)
