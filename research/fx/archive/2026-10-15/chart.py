"""A standalone SVG price chart, using only the standard library."""

from decimal import Decimal
from pathlib import Path
from typing import Sequence
from xml.etree.ElementTree import Element, SubElement, ElementTree

from .bots import PricePoint


def write_price_chart(prices: Sequence[PricePoint], path: str | Path,
                      *, title: str = "FX price") -> Path:
    if not prices:
        raise ValueError("run the simulation before rendering a chart")

    width, height = 960, 420
    left, right, top, bottom = 100, 30, 70, 70
    plot_width = width - left - right
    plot_height = height - top - bottom
    start, end = prices[0].time_seconds, prices[-1].time_seconds
    duration = max(end - start, 1)
    low = min(p.price for p in prices)
    high = max(p.price for p in prices)
    padding = (high - low) * Decimal("0.08") if high != low else low * Decimal("0.01")
    low, high = low - padding, high + padding
    unit, divisor = ("hours", 3600) if duration >= 3600 else (
        ("minutes", 60) if duration >= 120 else ("seconds", 1))

    svg = Element("svg", {
        "xmlns": "http://www.w3.org/2000/svg", "viewBox": f"0 0 {width} {height}",
        "role": "img", "aria-labelledby": "chart-title chart-description",
    })
    SubElement(svg, "title", id="chart-title").text = title
    SubElement(svg, "desc", id="chart-description").text = (
        f"{len(prices)} sampled prices, from {start} to {end} simulated seconds. "
        f"First price {prices[0].price}; last price {prices[-1].price}.")
    SubElement(svg, "rect", width=str(width), height=str(height), fill="#ffffff")
    group = SubElement(svg, "g", {
        "font-family": "sans-serif", "font-size": "14", "fill": "#334155",
    })

    def text(x: float, y: float, value: str, **attrs: str) -> None:
        SubElement(group, "text", x=f"{x:.2f}", y=f"{y:.2f}", **attrs).text = value

    def x_at(seconds: float) -> float:
        return left + (seconds - start) / duration * plot_width

    def y_at(price: Decimal) -> float:
        return top + float((high - price) / (high - low)) * plot_height

    text(left, 32, title, **{"font-size": "22", "font-weight": "500"})
    text(left, 55, "Gold per foreign unit", **{"fill": "#64748b"})
    for i in range(6):
        price = low + (high - low) * Decimal(i) / 5
        y = y_at(price)
        SubElement(group, "line", x1=str(left), x2=str(width - right),
                   y1=f"{y:.2f}", y2=f"{y:.2f}", stroke="#e2e8f0")
        text(left - 12, y + 5, f"{price:.8g}", **{"text-anchor": "end"})
        seconds = start + (end - start) * i / 5
        if end > start or i == 0:
            text(x_at(seconds), height - bottom + 25, f"{seconds / divisor:g}",
                 **{"text-anchor": "middle"})
    text(left + plot_width / 2, height - 18, f"Simulated time ({unit})",
         **{"text-anchor": "middle"})
    points = " ".join(f"{x_at(p.time_seconds):.2f},{y_at(p.price):.2f}" for p in prices)
    SubElement(group, "polyline", points=points, fill="none", stroke="#2563eb",
               **{"stroke-width": "2", "stroke-linejoin": "round"})
    if len(prices) == 1:
        SubElement(group, "circle", cx=f"{x_at(start):.2f}",
                   cy=f"{y_at(prices[0].price):.2f}", r="3", fill="#2563eb")

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ElementTree(svg).write(path, encoding="utf-8", xml_declaration=True)
    return path
