# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Ninja-specific build output parsing and footer rendering.

Plugs into `BuildMonitor` via the `BuildOutputHandler` protocol declared
in `mozbuild.controller.building`. Recognizes ninja's
`[%f/%t %e %E %r]` status format set in `runtime.RuntimeMixin.build`
and renders a ninja-flavored progress footer."""

import re
import sys

from rich.style import Style
from rich.text import Text

RE_NINJA_STATUS = re.compile(
    r"^\[(?P<finished>\d+)/(?P<total>\d+)\s+"
    r"(?P<elapsed>[\d.]+)\s+"
    r"(?P<eta>[\d.?]*)\s+"
    r"(?P<running>\d+)\]\s?(?P<tail>.*)$"
)

EDGE_TYPE_STYLE = {
    name: Style.parse(spec)
    for name, spec in {
        "CXX": "blue",
        "CC": "green",
        "HOST_CXX": "bright_blue",
        "HOST_CC": "bright_green",
        "WASM_CXX": "magenta",
        "WASM_CC": "magenta",
        "AS": "yellow",
        "AR": "color(173)",
        "LINK": "bold blue",
        "HOST_LINK": "bold bright_blue",
        "WASM_LINK": "bold magenta",
        "STAMP": "color(67)",
        "INSTALL": "color(215)",
        "PP": "yellow",
        "GEN": "bright_green",
        "GEN_RC": "color(208)",
        "RC": "color(215)",
        "IPDL": "bright_magenta",
        "WebIDL": "bright_magenta",
        "XPIDL": "bright_magenta",
        "JAR": "bright_yellow",
        "ZIP": "color(178)",
        "CARGO": "color(136)",
        "CARGO_TEST": "color(100)",
        "RUSTC": "color(136)",
        "BUILD-SCRIPT": "color(172)",
        "CHECK": "color(67)",
        "STRIP": "color(245)",
        "DUMP_SYMS": "color(141)",
        "WINCHECKSEC": "color(111)",
        "LANGPACK": "color(177)",
        "REPACKAGE": "color(99)",
        "WINNT": "color(202)",
        "Regenerating": "color(220)",
    }.items()
}

EDGE_BRACKET_STYLE = Style.parse("bright_black")


def _format_seconds(seconds):
    seconds = int(seconds)
    return f"{seconds // 60}m {seconds % 60:02d}s"


def _format_tail(tail):
    parts = tail.split(None, 1)
    if not parts:
        return tail
    edge_type = parts[0]
    style = EDGE_TYPE_STYLE.get(edge_type)
    if style is None:
        return tail
    rest = parts[1] if len(parts) > 1 else ""
    if not sys.stderr.isatty():
        return f"[{edge_type}] {rest}".rstrip()
    label = (
        EDGE_BRACKET_STYLE.render("[")
        + style.render(edge_type)
        + EDGE_BRACKET_STYLE.render("]")
    )
    return f"{label} {rest}".rstrip()


class NinjaOutputHandler:
    """Progress parser + footer renderer for the Ninja backend."""

    defers_live_start = True

    def __init__(self):
        self._state = None

    def parse_progress(self, line):
        m = RE_NINJA_STATUS.match(line)
        if not m:
            return None
        try:
            eta_raw = m.group("eta")
            self._state = {
                "finished": int(m.group("finished")),
                "total": int(m.group("total")),
                "elapsed": float(m.group("elapsed")),
                "eta": float(eta_raw) if eta_raw and eta_raw != "?" else None,
                "running": int(m.group("running")),
            }
        except ValueError:
            return None
        tail = m.group("tail")
        return _format_tail(tail) if tail else ""

    def render_footer(self):
        state = self._state
        if state is None:
            return None
        eta = state["eta"]
        eta_str = _format_seconds(eta) if eta is not None else "--"
        t = Text()
        t.append("BUILD: ", style="bright_black")
        t.append("[", style="bright_black")
        t.append(str(state["finished"]), style="cyan")
        t.append("/", style="bright_black")
        t.append(str(state["total"]), style="cyan")
        t.append("] ", style="bright_black")
        t.append("elapsed: ", style="bright_black")
        t.append(_format_seconds(state["elapsed"]), style="cyan")
        t.append(" eta: ", style="bright_black")
        t.append(eta_str, style="cyan")
        t.append(" running tasks: ", style="bright_black")
        t.append(str(state["running"]), style="cyan")
        return t
