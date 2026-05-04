# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Ninja-only unified-chunk planner.

Phase 1 mirrors `UnifiedSources.unified_source_mapping` 1:1, materializing
each existing chunk as a `UnifiedChunk`. The data shape is intentionally
richer than what the legacy mapping needs so a later phase can replace
the 1:1 plan with chunking driven by per-source compile fingerprints
(the flag/define hash that determines which sources can actually share a
translation unit) or owner_key (e.g. linkable / library) without
disturbing the surrounding ninja-backend code.

Wired into `NinjaBackend` via `add_unified_sources(obj)` during
`consume_object`; `plan()` is invoked at write time and yields
`UnifiedChunk`s in declaration order. The existing `_unified_by_dir`
bookkeeping stays in place; later phases retire it once the planner
becomes the source of truth.
"""

from dataclasses import dataclass, field
from typing import List, Optional

import mozpack.path as mozpath


@dataclass(frozen=True)
class UnifiedSourceRecord:
    """One source file headed for a unified chunk.

    Holds enough metadata to reroute the source into a different chunk
    in a future phase without consulting the original mozbuild object.
    `is_generated` distinguishes `UnifiedSources.generated_files` from
    `static_files`; the canonical_suffix and declaring objdir are kept
    so per-chunk output paths can be reconstructed independently.
    """

    source: str
    objdir: str
    relobjdir: str
    canonical_suffix: str
    is_generated: bool = False
    declaration_index: int = 0


@dataclass
class UnifiedChunk:
    """A single planned unified translation unit.

    `unified_file` is the basename (e.g. `Unified_cpp_dom_base0.cpp`);
    the full path on disk is `mozpath.join(output_directory,
    unified_file)`. `compile_fingerprint` and `owner_key` are
    placeholders for the next phase (cross-directory grouping by
    per-source compile flags, ownership routing). In Phase 1 both are
    `None` and chunks group purely by their declaring `UnifiedSources`.
    """

    unified_file: str
    output_directory: str
    unified_source_path: str
    object_basename: str
    source_filenames: List[str]
    relobjdir: str
    objdir: str
    canonical_suffix: str
    compile_fingerprint: Optional[str] = None
    owner_key: Optional[str] = None
    records: List[UnifiedSourceRecord] = field(default_factory=list)


class NinjaUnifiedPlanner:
    """Collects `UnifiedSources` during emit and produces `UnifiedChunk`s.

    Phase 1 reproduces today's behavior: each input `UnifiedSources`
    contributes its `unified_source_mapping` entries verbatim, in the
    order they were added. Future phases compute compile fingerprints
    per record and regroup across declaring objects; the public surface
    (`add_unified_sources` / `plan`) is intended to stay stable.
    """

    def __init__(self):
        self._inputs = []

    def add_unified_sources(self, obj) -> None:
        """Register a `UnifiedSources`-shaped object.

        The object must expose:
          * `unified_source_mapping`: iterable of
            `(unified_filename, [source_filenames])` tuples;
          * `objdir`, `relobjdir`, `canonical_suffix`;
          * `generated_files` (used to mark records as generated).

        Standard `UnifiedSources` from the emitter satisfies all of
        these. Duck-typing is intentional so tests can inject minimal
        fixtures without instantiating a full `Context`.
        """
        self._inputs.append(obj)

    def plan(self) -> List[UnifiedChunk]:
        """Materialize chunks. Phase 1: 1:1 with `unified_source_mapping`."""
        chunks: List[UnifiedChunk] = []
        for obj in self._inputs:
            objdir = mozpath.normsep(obj.objdir)
            relobjdir = obj.relobjdir
            canonical_suffix = obj.canonical_suffix
            generated = set(getattr(obj, "generated_files", ()) or ())
            for unified_filename, source_filenames in obj.unified_source_mapping:
                stem = mozpath.splitext(unified_filename)[0]
                source_list = list(source_filenames)
                records = [
                    UnifiedSourceRecord(
                        source=mozpath.normsep(src),
                        objdir=objdir,
                        relobjdir=relobjdir,
                        canonical_suffix=canonical_suffix,
                        is_generated=src in generated,
                        declaration_index=src_idx,
                    )
                    for src_idx, src in enumerate(source_list)
                ]
                chunks.append(
                    UnifiedChunk(
                        unified_file=unified_filename,
                        output_directory=objdir,
                        unified_source_path=mozpath.join(objdir, unified_filename),
                        object_basename=stem,
                        source_filenames=source_list,
                        relobjdir=relobjdir,
                        objdir=objdir,
                        canonical_suffix=canonical_suffix,
                        compile_fingerprint=None,
                        owner_key=None,
                        records=records,
                    )
                )
        return chunks
