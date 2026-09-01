"""Deterministic chart rendering with matplotlib.

The same bargain as the SQL layer: the AI client decides *what* to draw and
this module decides *how*, with no judgement of its own. Every visual choice —
the palette, the mark widths, the gridlines, the label placement — is fixed
here, and everything a user might reasonably want changed (chart type, axes,
size, colors, legend, labels) arrives as an explicit argument. Nothing is
generated, sampled or inferred by a model at render time, so the same result
and the same arguments always produce the same PNG.

Values arrive as TEXT, like everything else out of SQLite, and are converted
with the same ``TRY_REAL`` rules the SQL layer uses. A value that will not
convert is *dropped*, never coerced to zero — a missing bar is honest, a
zero-height one is a lie. The count comes back in ``skipped_values`` so the
caller can say so out loud.

Palette and mark specs follow a validated categorical scheme: eight hues in a
fixed order (never cycled, never generated), assigned by series rather than by
rank, and checked for colour-vision separation in both the light and dark
variants. Charts that put every series against every other one — scatter —
cap at three, because that is as far as the all-pairs separation holds.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path as FilePath
from typing import Any, Callable, Iterable, Sequence

import matplotlib

matplotlib.use("Agg")  # headless: no display, no GUI toolkit, no global figure manager

from matplotlib.backends.backend_agg import FigureCanvasAgg  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.patches import PathPatch  # noqa: E402
from matplotlib.path import Path as MplPath  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

from .casting import try_real  # noqa: E402

# Written once at import and never mutated, which is what keeps the rest of the
# module safe to call from more than one request at a time. Everything that
# varies per chart (colours, sizes) is set on the Figure instead.
matplotlib.rcParams.update({
    # DejaVu ships with matplotlib. Naming it explicitly means a chart rendered
    # in the container and one rendered on a laptop are the same image, and no
    # font-fallback warning is ever emitted.
    "font.family": "sans-serif",
    "font.sans-serif": ["DejaVu Sans"],
    "axes.titlesize": 11,
    "axes.labelsize": 9,
    "xtick.labelsize": 8,
    "ytick.labelsize": 8,
    "legend.fontsize": 8.5,
    "figure.dpi": 100,
    "savefig.dpi": 100,
    "path.simplify": True,
    "svg.fonttype": "none",
})


class ChartError(Exception):
    """Raised when a chart request cannot be honoured as asked."""


# --------------------------------------------------------------------------
# Theme
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Theme:
    """One complete set of colours: surface, ink, chrome and the series hues."""

    surface: str
    primary_ink: str
    secondary_ink: str
    muted_ink: str
    gridline: str
    axis: str
    deemphasis: str
    series: tuple[str, ...]


# The dark column is the same eight hues re-stepped for a dark surface, not a
# different palette, so a series keeps its identity across themes.
THEMES: dict[str, Theme] = {
    "light": Theme(
        surface="#fcfcfb",
        primary_ink="#0b0b0b",
        secondary_ink="#52514e",
        muted_ink="#898781",
        gridline="#e1e0d9",
        axis="#c3c2b7",
        deemphasis="#c9c8c1",
        series=("#2a78d6", "#eb6834", "#1baf7a", "#eda100",
                "#e87ba4", "#008300", "#4a3aa7", "#e34948"),
    ),
    "dark": Theme(
        surface="#1a1a19",
        primary_ink="#ffffff",
        secondary_ink="#c3c2b7",
        muted_ink="#898781",
        gridline="#2c2c2a",
        axis="#383835",
        deemphasis="#4a4a46",
        series=("#3987e5", "#d95926", "#199e70", "#c98500",
                "#d55181", "#008300", "#9085e9", "#e66767"),
    ),
}

# Past eight, a ninth hue would have to be invented, and an invented hue is
# indistinguishable from an existing one under colour-vision deficiency.
MAX_SERIES = 8

# Forms where every series is measured against every other one, rather than
# only against its neighbours, hold their separation for three.
ALL_PAIRS_MAX_SERIES = 3

CHART_TYPES = ("bar", "barh", "line", "area", "scatter", "pie")

# A pie stops being readable as part-to-whole well before this.
MAX_PIE_SLICES = 6

# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------

DEFAULT_WIDTH_PX = 600
DPI = 100

# Height that suits each form at the default width. barh is absent because its
# height is a function of how many bars there are — see _default_height.
DEFAULT_HEIGHTS_PX = {
    "bar": 380,
    "line": 360,
    "area": 360,
    "scatter": 420,
    "pie": 400,
}

MIN_WIDTH_PX, MAX_WIDTH_PX = 240, 2400
MIN_HEIGHT_PX, MAX_HEIGHT_PX = 180, 2400

# Marks stay thin; the data is the only thing allowed to be loud.
MAX_BAR_THICKNESS_PX = 24
BAR_END_RADIUS_PX = 4
LINE_WIDTH_PT = 2.0
MARKER_SIZE_PT = 5.0          # ~10px diameter at 100 dpi
SCATTER_AREA_PT2 = 34.0       # ~8px diameter
SURFACE_GAP_PT = 1.5          # ~2px of surface colour between touching marks
AREA_FILL_ALPHA = 0.10
GRID_WIDTH_PT = 0.8

# How many rotate-then-thin passes the axis gets before it stops trying.
MAX_TICK_FITTING_PASSES = 8


def _default_height(chart_type: str, width_px: int, categories: int) -> int:
    """The height that suits this form, at this width, for this much data."""
    if chart_type == "barh":
        # Horizontal bars are a list: the height follows the number of rows
        # rather than a fixed aspect, so the labels never crush together.
        return _clamp(110 + 34 * max(categories, 1), 220, 1000)
    base = DEFAULT_HEIGHTS_PX[chart_type]
    # Keep the aspect ratio if the caller widened or narrowed the chart.
    scaled = round(base * width_px / DEFAULT_WIDTH_PX)
    return _clamp(scaled, MIN_HEIGHT_PX, MAX_HEIGHT_PX)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


# --------------------------------------------------------------------------
# Numbers
# --------------------------------------------------------------------------

def _number(value: object) -> float:
    """Convert one cell to a float, or NaN when it does not convert.

    Delegates to :func:`casting.try_real` so a value that SQL would have
    skipped is a value the chart skips too — the picture and the aggregate
    agree about what counts as a number.
    """
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else math.nan
    converted = try_real(value)
    return math.nan if converted is None else converted


def _label(value: object) -> str:
    """Category text for one cell, with missing values named rather than blank.

    A NULL category drawn as an empty string leaves a bar nobody can identify,
    which reads as a rendering bug rather than as what it is — a real group in
    the data whose key is missing.
    """
    if value is None:
        return "(null)"
    text = str(value)
    return text if text.strip() else "(blank)"


def format_number(value: float) -> str:
    """Compact, readable form of a value for a tick or a direct label."""
    if not math.isfinite(value):
        return ""
    magnitude = abs(value)
    for cutoff, divisor, suffix in ((1e12, 1e12, "T"), (1e9, 1e9, "B"), (1e6, 1e6, "M")):
        if magnitude >= cutoff:
            scaled = value / divisor
            text = f"{scaled:,.1f}".rstrip("0").rstrip(".")
            return f"{text}{suffix}"
    if magnitude >= 1000 or value == int(value):
        return f"{value:,.0f}"
    if magnitude >= 1:
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return f"{value:,.4g}"


def format_plain(value: float) -> str:
    """Bare tick label — no comma grouping, no magnitude suffix. The right
    default for an ordinal axis (a year, an id, a zip code) where digits are
    read as a value's identity rather than compared by size.
    """
    if not math.isfinite(value):
        return ""
    if value == int(value):
        return f"{int(value)}"
    return f"{value:.4g}"


def format_comma(value: float) -> str:
    """Comma-grouped tick label at full precision — grouped like format_number
    but never abbreviated to a K/M/B/T suffix, for when the exact digit count
    matters more than a compact width.
    """
    if not math.isfinite(value):
        return ""
    if value == int(value):
        return f"{value:,.0f}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


# Named axis formats a caller can request explicitly with x_format/y_format,
# overriding the role-based default (format_number for a value axis,
# format_plain for an ordinal one). "auto" / None keeps that default.
AXIS_FORMATS: dict[str, Callable[[float], str]] = {
    "plain": format_plain,
    "comma": format_comma,
    "compact": format_number,
}


def _axis_formatter(
    requested: str | None, default: Callable[[float], str]
) -> Callable[[float], str]:
    """Resolve an x_format/y_format argument to a formatter function.

    Resolved eagerly rather than inside the FuncFormatter closure, so a typo
    like x_format="commas" fails the call immediately instead of surfacing
    only when matplotlib draws a tick during savefig().
    """
    if requested is None or requested == "auto":
        return default
    try:
        return AXIS_FORMATS[requested]
    except KeyError:
        raise ChartError(
            f"format {requested!r} is not recognised; use one of "
            f"{', '.join(sorted(AXIS_FORMATS))}, or 'auto'"
        ) from None


# --------------------------------------------------------------------------
# Column selection
# --------------------------------------------------------------------------

def _as_list(value: str | Sequence[str] | None) -> list[str] | None:
    """Accept either one column name or several, as models pass both."""
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    return list(value)


def _index_of(column: str, columns: Sequence[str]) -> int:
    """Resolve a column name to its position, case-insensitively as a fallback."""
    if column in columns:
        return columns.index(column)
    lowered = [c.lower() for c in columns]
    if column.lower() in lowered:
        return lowered.index(column.lower())
    raise ChartError(
        f"column {column!r} is not in this result; it has {list(columns)}"
    )


def _numeric_share(rows: Sequence[Sequence[Any]], index: int) -> float:
    """Fraction of a column's values that convert to a number."""
    if not rows:
        return 0.0
    return sum(1 for row in rows if not math.isnan(_number(row[index]))) / len(rows)


def resolve_columns(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    chart_type: str,
    x: str | None,
    y: Sequence[str] | None,
) -> tuple[int, list[int]]:
    """Work out which column is the x axis and which are the series.

    Explicit ``x`` and ``y`` always win. Left unspecified, the first column
    becomes x and every remaining column that is mostly numeric becomes a
    series, which is the shape a ``GROUP BY`` naturally produces.
    """
    if not columns:
        raise ChartError("the result has no columns to plot")

    x_index = _index_of(x, columns) if x else 0

    if y:
        y_indexes = [_index_of(name, columns) for name in y]
    else:
        y_indexes = [
            i for i in range(len(columns))
            if i != x_index and _numeric_share(rows, i) > 0.5
        ]
        if not y_indexes:
            raise ChartError(
                f"no numeric column found to plot against {columns[x_index]!r}. "
                f"The result has {list(columns)} — name the value column with y=, "
                "or wrap it in TRY_REAL() in the query"
            )

    if x_index in y_indexes:
        raise ChartError(
            f"column {columns[x_index]!r} cannot be both the x axis and a series"
        )

    if chart_type == "pie" and len(y_indexes) != 1:
        raise ChartError(
            f"a pie chart shows one value column; {len(y_indexes)} were selected "
            f"({[columns[i] for i in y_indexes]}). Pass a single y=, or use a "
            "stacked bar to compare several"
        )

    limit = ALL_PAIRS_MAX_SERIES if chart_type == "scatter" else MAX_SERIES
    if len(y_indexes) > limit:
        reason = (
            "a scatter plot puts every series against every other one, and the "
            "palette only separates three that way"
            if chart_type == "scatter"
            else "the palette has eight hues and does not invent a ninth"
        )
        raise ChartError(
            f"{len(y_indexes)} series is too many for a {chart_type} chart — {reason}. "
            "Group the tail into an 'Other' bucket in SQL, or draw fewer series"
        )

    return x_index, y_indexes


# --------------------------------------------------------------------------
# Shapes
# --------------------------------------------------------------------------

def _rounded_rect_path(
    x0: float, y0: float, x1: float, y1: float,
    rx: float, ry: float, corners: Iterable[str],
) -> MplPath:
    """A rectangle with a rounded radius on the named corners only.

    ``rx`` and ``ry`` are separate because the x and y axes have different
    data-per-pixel scales; passing the same number of *pixels* through each
    makes the corner circular on screen even though it is elliptical in data
    space.
    """
    corners = set(corners)
    rx = max(0.0, min(rx, abs(x1 - x0) / 2))
    ry = max(0.0, min(ry, abs(y1 - y0) / 2))

    verts: list[tuple[float, float]] = []
    codes: list[int] = []

    def start(point: tuple[float, float]) -> None:
        verts.append(point)
        codes.append(MplPath.MOVETO)

    def line(point: tuple[float, float]) -> None:
        verts.append(point)
        codes.append(MplPath.LINETO)

    def arc(control: tuple[float, float], point: tuple[float, float]) -> None:
        verts.extend((control, point))
        codes.extend((MplPath.CURVE3, MplPath.CURVE3))

    bl, tl = "bl" in corners, "tl" in corners
    tr, br = "tr" in corners, "br" in corners

    start((x0, y0 + ry if bl else y0))
    line((x0, y1 - ry if tl else y1))
    if tl:
        arc((x0, y1), (x0 + rx, y1))
    line((x1 - rx if tr else x1, y1))
    if tr:
        arc((x1, y1), (x1, y1 - ry))
    line((x1, y0 + ry if br else y0))
    if br:
        arc((x1, y0), (x1 - rx, y0))
    line((x0 + rx if bl else x0, y0))
    if bl:
        arc((x0, y0), (x0, y0 + ry))

    verts.append(verts[0])
    codes.append(MplPath.CLOSEPOLY)
    return MplPath(verts, codes)


def _round_data_ends(fig: Figure, ax: Any, bars: Sequence[Any], vertical: bool) -> None:
    """Round the data end of each bar, leaving the baseline end square.

    The radius is specified in pixels, so the layout has to be settled before
    it can be converted into data units — hence the draw pass first.
    """
    fig.draw_without_rendering()
    origin = ax.transData.inverted().transform((0.0, 0.0))
    offset = ax.transData.inverted().transform((BAR_END_RADIUS_PX, BAR_END_RADIUS_PX))
    rx, ry = abs(offset[0] - origin[0]), abs(offset[1] - origin[1])

    for rect in bars:
        width, height = rect.get_width(), rect.get_height()
        if not (math.isfinite(width) and math.isfinite(height)) or width == 0 or height == 0:
            continue
        x0, y0 = rect.get_x(), rect.get_y()
        x1, y1 = x0 + width, y0 + height

        if vertical:
            corners = ("tl", "tr") if height > 0 else ("bl", "br")
        else:
            corners = ("tr", "br") if width > 0 else ("tl", "bl")

        patch = PathPatch(
            _rounded_rect_path(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1),
                               rx, ry, corners),
            facecolor=rect.get_facecolor(),
            edgecolor=rect.get_edgecolor(),
            linewidth=rect.get_linewidth(),
            joinstyle="round",
            zorder=rect.get_zorder(),
        )
        rect.remove()
        ax.add_patch(patch)


# --------------------------------------------------------------------------
# Chrome
# --------------------------------------------------------------------------

def _style_axes(
    ax: Any, theme: Theme, *, value_axis: str, grid: bool,
    x_format: str | None = None, y_format: str | None = None,
) -> None:
    """Apply the recessive chrome: hairline grid, one baseline, muted ticks."""
    ax.set_facecolor(theme.surface)
    ax.set_axisbelow(True)

    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    # Only the axis the categories sit on keeps a rule; the value axis is
    # carried by the gridlines instead of a second line of ink.
    keep = "bottom" if value_axis == "y" else "left"
    drop = "left" if value_axis == "y" else "bottom"
    ax.spines[keep].set_color(theme.axis)
    ax.spines[keep].set_linewidth(0.8)
    ax.spines[drop].set_visible(False)

    if grid:
        ax.grid(
            axis=value_axis, color=theme.gridline, linewidth=GRID_WIDTH_PT,
            linestyle="-",
        )
    ax.tick_params(colors=theme.muted_ink, length=0, pad=4)
    for text in (*ax.get_xticklabels(), *ax.get_yticklabels()):
        text.set_color(theme.muted_ink)

    override = y_format if value_axis == "y" else x_format
    formatter_fn = _axis_formatter(override, format_number)
    formatter = FuncFormatter(lambda value, _pos: formatter_fn(value))
    (ax.yaxis if value_axis == "y" else ax.xaxis).set_major_formatter(formatter)


def _apply_titles(
    ax: Any, theme: Theme, title: str | None, x_label: str | None, y_label: str | None
) -> None:
    if title:
        ax.set_title(title, color=theme.primary_ink, loc="left", pad=10, fontweight="bold")
    if x_label:
        ax.set_xlabel(x_label, color=theme.secondary_ink, labelpad=6)
    if y_label:
        ax.set_ylabel(y_label, color=theme.secondary_ink, labelpad=6)


def _labels_overlap(fig: Figure, ax: Any, pad_px: float = 3.0) -> bool:
    """True when any two drawn x tick labels are closer than ``pad_px``."""
    fig.draw_without_rendering()
    renderer = fig.canvas.get_renderer()
    boxes = sorted(
        (text.get_window_extent(renderer)
         for text in ax.get_xticklabels() if text.get_text()),
        key=lambda box: box.x0,
    )
    return any(
        boxes[i].x1 + pad_px > boxes[i + 1].x0 for i in range(len(boxes) - 1)
    )


def _fit_category_ticks(
    fig: Figure, ax: Any, positions: Sequence[float], labels: Sequence[str],
    theme: Theme, axis: str,
) -> None:
    """Place category labels so they never overlap.

    The labels are *measured* rather than estimated from their length: the
    figure is laid out, the rendered boxes are compared, and the labels are
    rotated and then thinned until they clear each other. Guessing from
    character counts is what puts "2025-012025-02" on an axis.
    """
    if axis == "y":
        ax.set_yticks(list(positions))
        ax.set_yticklabels(labels, color=theme.muted_ink)
        return

    ax.set_xticks(list(positions))
    step, rotation = 1, 0
    for _ in range(MAX_TICK_FITTING_PASSES):
        shown = [text if i % step == 0 else "" for i, text in enumerate(labels)]
        ax.set_xticklabels(
            shown, color=theme.muted_ink, rotation=rotation,
            ha="right" if rotation else "center",
            rotation_mode="anchor" if rotation else None,
        )
        if not _labels_overlap(fig, ax):
            return
        # Rotating buys the most room for the least legibility; only once that
        # is spent does the axis start dropping every second label.
        if rotation == 0:
            rotation = 40
        else:
            step += 1


def _add_legend(fig: Figure, ax: Any, theme: Theme, series_count: int) -> None:
    """A legend below the plot, frameless, for two or more series.

    One series needs none — there is only one colour and the title already
    says what it is.
    """
    handles, labels = ax.get_legend_handles_labels()
    if not handles:
        return
    legend = fig.legend(
        handles, labels,
        loc="outside lower center",
        ncols=min(series_count, 4),
        frameon=False,
        handlelength=1.2,
        handleheight=1.0,
        columnspacing=1.4,
        borderpad=0.2,
    )
    for text in legend.get_texts():
        text.set_color(theme.secondary_ink)


# --------------------------------------------------------------------------
# The forms
# --------------------------------------------------------------------------

@dataclass
class _Series:
    label: str
    values: list[float]
    color: str


def _series_colors(
    theme: Theme, count: int, colors: Sequence[str] | None
) -> list[str]:
    """Colours in fixed slot order, or the caller's own, one per series.

    Slot order is deliberate: it is what keeps neighbouring series separable
    under colour-vision deficiency, so the hues are never cycled or reordered
    by rank.
    """
    if colors:
        if len(colors) < count:
            raise ChartError(
                f"{len(colors)} colour(s) given for {count} series; pass one per "
                "series or omit colors= to use the default palette"
            )
        return list(colors[:count])
    return list(theme.series[:count])


def _bar_thickness(count: int, series: int, span_px: float) -> float:
    """Group width in category units, capped so no bar gets fat.

    A bar wider than ~24px reads as a block rather than a mark, so the cap is
    on the *rendered* thickness and the data-unit width is derived from it.
    """
    if span_px <= 0 or count <= 0:
        return 0.8
    per_category_px = span_px / count
    max_group = MAX_BAR_THICKNESS_PX * series / per_category_px
    return max(0.05, min(0.8, max_group))


def _draw_bars(
    fig: Figure, ax: Any, theme: Theme, series: Sequence[_Series],
    labels: Sequence[str], *, horizontal: bool, stacked: bool,
    width_px: int, height_px: int, value_labels: bool,
    bar_colors: Sequence[str] | None = None,
) -> None:
    """Draw grouped or stacked bars.

    ``bar_colors`` recolours the bars of a single series one by one, which is
    how emphasis works: every bar keeps its own slot and its own width, and
    only the fill changes. Splitting the series in two would have moved the
    highlighted bars off their tick marks.
    """
    count = len(labels)
    positions = list(range(count))
    span_px = (height_px if horizontal else width_px) * 0.78
    lanes = 1 if stacked else len(series)
    group = _bar_thickness(count, lanes, span_px)
    thickness = group / lanes

    # A surface-coloured edge is what separates touching marks — the gap is
    # made of surface, never of a contrasting outline drawn around the bar.
    edge = dict(edgecolor=theme.surface, linewidth=SURFACE_GAP_PT) if (
        stacked or lanes > 1
    ) else {}

    positive = [0.0] * count
    negative = [0.0] * count
    drawn: list[Any] = []
    outermost: dict[int, Any] = {}

    for lane, item in enumerate(series):
        if stacked:
            offsets = positions
            bottoms = [
                positive[i] if (math.isfinite(v) and v >= 0) else negative[i]
                for i, v in enumerate(item.values)
            ]
        else:
            shift = (lane - (lanes - 1) / 2) * thickness
            offsets = [p + shift for p in positions]
            bottoms = [0.0] * count

        heights = [0.0 if math.isnan(v) else v for v in item.values]
        kwargs = dict(color=item.color, label=item.label, zorder=2, **edge)
        if horizontal:
            bars = ax.barh(offsets, heights, height=thickness, left=bottoms, **kwargs)
        else:
            bars = ax.bar(offsets, heights, width=thickness, bottom=bottoms, **kwargs)

        for i, (rect, value) in enumerate(zip(bars, item.values)):
            if math.isnan(value):
                rect.remove()  # a missing value gets no bar, and never a zero one
                continue
            if bar_colors is not None:
                rect.set_facecolor(bar_colors[i])
            drawn.append(rect)
            outermost[i] = rect
            if stacked:
                if value >= 0:
                    positive[i] += value
                else:
                    negative[i] += value

    if horizontal:
        ax.set_ylim(-0.6, count - 0.4)
        ax.invert_yaxis()  # first row at the top, the way a list reads
        _fit_category_ticks(fig, ax, positions, labels, theme, axis="y")
    else:
        ax.set_xlim(-0.6, count - 0.4)
        _fit_category_ticks(fig, ax, positions, labels, theme, axis="x")

    # Rounding comes last: the radius is a pixel measurement, so it can only be
    # converted into data units once the limits and the tick layout are final.
    # Only the segment at the far end of a stack carries the rounded end; the
    # interior ones butt against their neighbours.
    _round_data_ends(fig, ax, list(outermost.values()) if stacked else drawn, not horizontal)

    if value_labels:
        _label_bars(ax, theme, series, positions, thickness, lanes,
                    horizontal=horizontal, stacked=stacked)


def stack_totals(series: Sequence["_Series"], count: int) -> list[float | None]:
    """Total of each stack, or None where every segment of it is missing.

    Summing across the series is what makes a stack whose *top* segment is
    missing still report its total; walking lane by lane and labelling on the
    last one silently skipped those stacks.
    """
    totals: list[float | None] = []
    for i in range(count):
        present = [
            item.values[i] for item in series if not math.isnan(item.values[i])
        ]
        totals.append(sum(present) if present else None)
    return totals


def _label_bars(
    ax: Any, theme: Theme, series: Sequence[_Series], positions: Sequence[int],
    thickness: float, lanes: int, *, horizontal: bool, stacked: bool,
) -> None:
    """Values at the tip of each bar, outside the mark so nothing is clipped."""

    def annotate(text: str, tip: float, offset: float) -> None:
        outward = 4 if tip >= 0 else -4
        if horizontal:
            ax.annotate(
                text, (tip, offset), textcoords="offset points",
                xytext=(outward, 0), ha="left" if tip >= 0 else "right",
                va="center", color=theme.secondary_ink, fontsize=7.5, zorder=3,
            )
        else:
            ax.annotate(
                text, (offset, tip), textcoords="offset points",
                xytext=(0, outward), ha="center",
                va="bottom" if tip >= 0 else "top",
                color=theme.secondary_ink, fontsize=7.5, zorder=3,
            )

    if stacked:
        for i, total in enumerate(stack_totals(series, len(positions))):
            if total is not None:
                annotate(format_number(total), total, positions[i])
        return

    for lane, item in enumerate(series):
        for i, value in enumerate(item.values):
            if math.isnan(value):
                continue
            annotate(
                format_number(value), value,
                positions[i] + (lane - (lanes - 1) / 2) * thickness,
            )


def _draw_lines(
    fig: Figure, ax: Any, theme: Theme, series: Sequence[_Series],
    labels: Sequence[str], x_values: list[float] | None, *, area: bool,
    stacked: bool, value_labels: bool,
) -> None:
    positions = x_values if x_values is not None else list(range(len(labels)))
    baseline = [0.0] * len(positions)

    for item in series:
        values = item.values
        if area and stacked:
            top = [b + (0.0 if math.isnan(v) else v) for b, v in zip(baseline, values)]
            ax.fill_between(positions, baseline, top, color=item.color,
                            alpha=AREA_FILL_ALPHA, linewidth=0, zorder=1)
            ax.plot(positions, top, color=item.color, linewidth=LINE_WIDTH_PT,
                    solid_capstyle="round", solid_joinstyle="round",
                    label=item.label, zorder=2)
            baseline = top
            continue

        ax.plot(
            positions, values, color=item.color, linewidth=LINE_WIDTH_PT,
            solid_capstyle="round", solid_joinstyle="round", label=item.label,
            marker="o" if len(positions) <= 15 else None,
            markersize=MARKER_SIZE_PT, markerfacecolor=item.color,
            markeredgecolor=theme.surface, markeredgewidth=SURFACE_GAP_PT,
            zorder=2,
        )
        if area:
            ax.fill_between(positions, 0, [0.0 if math.isnan(v) else v for v in values],
                            color=item.color, alpha=AREA_FILL_ALPHA, linewidth=0,
                            zorder=1)

    if x_values is None:
        _fit_category_ticks(fig, ax, positions, labels, theme, axis="x")

    if value_labels:
        # Lines get their value at the end of the line, not on every point.
        for item in series:
            for position, value in zip(reversed(positions), reversed(item.values)):
                if not math.isnan(value):
                    ax.annotate(
                        format_number(value), (position, value),
                        textcoords="offset points", xytext=(6, 0),
                        ha="left", va="center", color=theme.secondary_ink,
                        fontsize=7.5, zorder=3,
                    )
                    break


def _draw_scatter(
    ax: Any, theme: Theme, series: Sequence[_Series], x_values: list[float],
) -> None:
    for item in series:
        ax.scatter(
            x_values, item.values, s=SCATTER_AREA_PT2, color=item.color,
            label=item.label, edgecolors=theme.surface,
            linewidths=SURFACE_GAP_PT, zorder=2,
        )


def _draw_pie(
    ax: Any, theme: Theme, values: list[float], labels: Sequence[str],
    colors: Sequence[str], value_labels: bool,
) -> None:
    if any(value < 0 for value in values):
        raise ChartError(
            "a pie chart cannot show negative values — the slices would not add "
            "up to a whole. Use a bar chart instead"
        )
    total = sum(values)
    if total <= 0:
        raise ChartError("every value in this result is zero, so there is no pie to draw")

    wedges, texts, *rest = ax.pie(
        values,
        labels=list(labels),
        colors=list(colors),
        startangle=90,
        counterclock=False,
        # A slice this thin has no room for text; the legend-free labels
        # outside the pie still name it, and the value stays in the result.
        autopct=(lambda pct: f"{pct:.0f}%" if pct >= 4 else "") if value_labels else None,
        pctdistance=0.72,
        textprops={"color": theme.secondary_ink, "fontsize": 8},
        wedgeprops={"edgecolor": theme.surface, "linewidth": SURFACE_GAP_PT},
    )
    for text in texts:
        text.set_color(theme.secondary_ink)
    # A percentage sitting inside a wedge is the one place text does not wear a
    # text token: it takes white or ink from the fill it lands on, so it always
    # clears contrast against the slice underneath it.
    for text, color in zip(rest[0] if rest else [], colors):
        text.set_color("#ffffff" if _relative_luminance(color) < 0.5 else "#0b0b0b")
        text.set_fontsize(7.5)
    for wedge in wedges:
        wedge.set_zorder(2)
    ax.set_aspect("equal")


def _relative_luminance(color: str) -> float:
    """Perceived lightness of a hex colour, 0 (black) to 1 (white)."""
    red, green, blue = (int(color.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))
    channels = [
        c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
        for c in (red, green, blue)
    ]
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def render(
    *,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    target: FilePath,
    chart_type: str = "bar",
    x: str | Sequence[str] | None = None,
    y: str | Sequence[str] | None = None,
    title: str | None = None,
    x_label: str | None = None,
    y_label: str | None = None,
    x_format: str | None = None,
    y_format: str | None = None,
    series_labels: Sequence[str] | None = None,
    width_px: int = DEFAULT_WIDTH_PX,
    height_px: int | None = None,
    theme: str = "light",
    colors: Sequence[str] | None = None,
    highlight: Sequence[str] | None = None,
    legend: bool | None = None,
    grid: bool = True,
    stacked: bool = False,
    value_labels: bool = False,
) -> dict[str, Any]:
    """Render one chart to ``target`` and describe what was drawn.

    Every argument is a matplotlib setting, not a hint: the same inputs always
    produce the same file. Returns the metadata the caller needs to tell the
    user what they are looking at — including how many values had to be
    dropped because they were not numbers.
    """
    chart_type = (chart_type or "bar").strip().lower()
    if chart_type not in CHART_TYPES:
        raise ChartError(
            f"unknown chart_type {chart_type!r}; use one of {', '.join(CHART_TYPES)}"
        )

    theme_name = (theme or "light").strip().lower()
    if theme_name not in THEMES:
        raise ChartError(f"unknown theme {theme_name!r}; use 'light' or 'dark'")
    palette = THEMES[theme_name]

    if not rows:
        raise ChartError("the result has no rows, so there is nothing to draw")

    # Models pass a lone column name as often as a list, so normalise every
    # list-shaped argument before anything indexes or iterates it — a bare
    # string would otherwise be read one character at a time.
    x_name = (_as_list(x) or [None])[0]
    y_names = _as_list(y)
    series_labels = _as_list(series_labels)
    colors = _as_list(colors)
    highlight = _as_list(highlight)

    x_index, y_indexes = resolve_columns(columns, rows, chart_type, x_name, y_names)

    warnings: list[str] = []
    labels = [_label(row[x_index]) for row in rows]

    # Line, area and scatter honour a genuinely numeric x axis; anything else
    # (a month string, a category) is plotted at even spacing in row order.
    # A column that is numeric apart from a few gaps stays a numeric axis and
    # loses those rows, the same way a missing value loses its bar — falling
    # back to even spacing would silently relabel the axis instead.
    x_values: list[float] | None = None
    if chart_type in ("line", "area", "scatter"):
        candidate = [_number(row[x_index]) for row in rows]
        missing = sum(1 for value in candidate if math.isnan(value))
        mostly_numeric = missing < len(candidate) / 2

        if chart_type == "scatter" and not mostly_numeric:
            raise ChartError(
                f"a scatter plot needs a numeric x axis, and {columns[x_index]!r} "
                "does not convert to numbers. Wrap it in TRY_REAL() in the query, "
                "or use a bar or line chart"
            )
        if mostly_numeric:
            x_values = candidate
            if missing:
                warnings.append(
                    f"{missing} row(s) have no numeric {columns[x_index]!r} and are "
                    "not on the chart"
                )

    series_names = list(series_labels) if series_labels else [columns[i] for i in y_indexes]
    if len(series_names) != len(y_indexes):
        raise ChartError(
            f"{len(series_names)} series label(s) given for {len(y_indexes)} series"
        )

    palette_colors = _series_colors(palette, len(y_indexes), colors)
    series = [
        _Series(label=name, values=[_number(row[index]) for row in rows], color=color)
        for name, index, color in zip(series_names, y_indexes, palette_colors)
    ]

    skipped = sum(1 for item in series for value in item.values if math.isnan(value))
    if skipped:
        warnings.append(
            f"{skipped} value(s) did not convert to a number and were left out of "
            "the chart rather than drawn as zero"
        )
    if all(math.isnan(value) for item in series for value in item.values):
        raise ChartError(
            "none of the selected values convert to numbers, so there is nothing to "
            "plot. Wrap the column in TRY_REAL() in the query, or check profile_column()"
        )

    # Emphasis: one or more categories in the accent hue, the rest receding.
    highlighted = set(highlight or ())
    if highlighted:
        unknown = highlighted - set(labels)
        if unknown:
            warnings.append(
                f"highlight value(s) {sorted(unknown)} are not in "
                f"{columns[x_index]!r} and were ignored"
            )
        if len(series) > 1:
            warnings.append(
                "highlight applies to categories and was ignored: this chart has "
                "more than one series, where colour already carries identity"
            )
            highlighted = set()

    if chart_type == "pie" and len(labels) > MAX_PIE_SLICES:
        warnings.append(
            f"{len(labels)} slices is past the {MAX_PIE_SLICES} a pie stays readable "
            "at; a bar chart compares these more clearly"
        )
    if stacked and chart_type not in ("bar", "barh", "area"):
        warnings.append(f"stacked has no effect on a {chart_type} chart")
    if stacked and any(
        value < 0 for item in series for value in item.values if not math.isnan(value)
    ):
        warnings.append(
            "this result mixes positive and negative values, which a stacked chart "
            "cannot add up honestly — a grouped chart reads correctly"
        )

    height = height_px or _default_height(chart_type, width_px, len(labels))
    width = _clamp(int(width_px), MIN_WIDTH_PX, MAX_WIDTH_PX)
    height = _clamp(int(height), MIN_HEIGHT_PX, MAX_HEIGHT_PX)

    fig = Figure(figsize=(width / DPI, height / DPI), dpi=DPI, layout="constrained")
    FigureCanvasAgg(fig)  # a real renderer, so tick labels can be measured
    fig.patch.set_facecolor(palette.surface)
    ax = fig.add_subplot(111)

    show_legend = len(series) >= 2 if legend is None else bool(legend)

    if chart_type == "pie":
        _draw_pie(ax, palette, [
            0.0 if math.isnan(value) else value for value in series[0].values
        ], labels, _pie_colors(palette, len(labels), colors, highlighted, labels),
            value_labels)
        ax.set_facecolor(palette.surface)
        show_legend = False
    elif chart_type in ("bar", "barh"):
        _draw_bars(
            fig, ax, palette, series, labels,
            horizontal=chart_type == "barh", stacked=stacked,
            width_px=width, height_px=height, value_labels=value_labels,
            bar_colors=[
                series[0].color if label in highlighted else palette.deemphasis
                for label in labels
            ] if highlighted else None,
        )
        _style_axes(
            ax, palette, value_axis="x" if chart_type == "barh" else "y", grid=grid,
            x_format=x_format, y_format=y_format,
        )
    elif chart_type == "scatter":
        assert x_values is not None
        _draw_scatter(ax, palette, series, x_values)
        _style_axes(ax, palette, value_axis="y", grid=grid, x_format=x_format, y_format=y_format)
        ax.grid(axis="x", color=palette.gridline, linewidth=GRID_WIDTH_PT, linestyle="-")
        scatter_x_fn = _axis_formatter(x_format, format_number)
        ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _p: scatter_x_fn(v)))
    else:
        _draw_lines(
            fig, ax, palette, series, labels, x_values,
            area=chart_type == "area", stacked=stacked, value_labels=value_labels,
        )
        _style_axes(ax, palette, value_axis="y", grid=grid, x_format=x_format, y_format=y_format)
        if x_values is not None:
            line_x_fn = _axis_formatter(x_format, format_plain)
            ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _p: line_x_fn(v)))

    _apply_titles(ax, palette, title, x_label, y_label)
    if show_legend and chart_type != "pie":
        _add_legend(fig, ax, palette, len(series))

    target.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(target, format="png", facecolor=palette.surface, dpi=DPI)

    return {
        "chart_type": chart_type,
        "theme": theme_name,
        "width_px": width,
        "height_px": height,
        "x_column": columns[x_index],
        "y_columns": [columns[i] for i in y_indexes],
        "series_labels": series_names,
        "series_colors": [item.color for item in series],
        "plotted_rows": len(rows),
        "skipped_values": skipped,
        "legend": show_legend,
        "stacked": bool(stacked) and chart_type in ("bar", "barh", "area"),
        "warnings": warnings,
    }


def _pie_colors(
    theme: Theme, count: int, colors: Sequence[str] | None,
    highlighted: set[str], labels: Sequence[str],
) -> list[str]:
    """One colour per slice — a pie's categories *are* its series."""
    if colors:
        if len(colors) < count:
            raise ChartError(
                f"{len(colors)} colour(s) given for {count} slices; pass one per slice"
            )
        return list(colors[:count])
    if highlighted:
        return [
            theme.series[0] if label in highlighted else theme.deemphasis
            for label in labels
        ]
    if count > MAX_SERIES:
        raise ChartError(
            f"{count} slices needs {count} distinct colours and the palette has "
            f"{MAX_SERIES}. Group the tail into an 'Other' bucket in SQL"
        )
    return list(theme.series[:count])
