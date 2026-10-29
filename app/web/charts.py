"""Chart geometry for server-rendered SVG. Kept in Python so templates only place shapes."""

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Bar:
    x: float
    y: float
    width: float
    height: float
    path: str  # top corners rounded, square at the baseline
    label: str
    value: int
    hit_x: float
    hit_width: float


@dataclass(frozen=True)
class BarChart:
    width: int
    height: int
    baseline: float
    bars: list[Bar]
    gridlines: list[tuple[float, int]]  # (y, value)
    max_value: int


def _bar_path(x: float, y: float, w: float, h: float, r: float) -> str:
    if h <= 0:
        return ""
    r = min(r, w / 2, h)
    base = y + h
    return (
        f"M{x:.1f},{base:.1f}V{y + r:.1f}Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f}"
        f"H{x + w - r:.1f}Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f}V{base:.1f}Z"
    )


def nice_max(value: int) -> int:
    if value <= 4:
        return 4
    for step in (5, 10, 20, 25, 50, 100, 200, 250, 500):
        if value <= step * 4:
            return -(-value // step) * step
    return value


def weekly_bars(weeks: list[tuple[date, int]], *, width: int = 640, height: int = 180) -> BarChart:
    """Vertical bars, 2px gap between neighbours, 4px rounded tops anchored to the baseline."""
    left, right, top, bottom = 28, 4, 8, 22
    plot_w, plot_h = width - left - right, height - top - bottom
    top_value = nice_max(max((v for _, v in weeks), default=0))
    slot = plot_w / max(len(weeks), 1)
    gap = 2
    bar_w = max(min(slot - gap, 36), 2)
    bars = []
    for i, (week, value) in enumerate(weeks):
        h = plot_h * value / top_value if top_value else 0
        x = left + i * slot + (slot - bar_w) / 2
        y = top + plot_h - h
        bars.append(
            Bar(
                x=x,
                y=y,
                width=bar_w,
                height=h,
                path=_bar_path(x, y, bar_w, h, 4),
                label=f"{week:%b} {week.day}",
                value=value,
                hit_x=left + i * slot,
                hit_width=slot,
            )
        )
    gridlines = [(top + plot_h - plot_h * frac, round(top_value * frac)) for frac in (0.5, 1.0)]
    return BarChart(width, height, top + plot_h, bars, gridlines, top_value)
