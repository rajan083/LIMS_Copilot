import os
from datetime import datetime
import plotly.graph_objects as go


CHARTS_DIR = "exports"

VALID_CHART_TYPES = {
    "bar", "horizontal_bar", "stacked_bar",
    "line", "area",
    "pie", "donut",
    "scatter",
    "histogram",
}

_UNLABELED_TYPES = {"histogram"}

_SINGLE_SERIES_TYPES = {"pie", "donut"}

MAX_PIE_DIRECT_LABELS = 12

_PALETTE = [
    "#4C78A8", "#F58518", "#54A24B", "#E45756", "#72B7B2",
    "#EECA3B", "#B279A2", "#FF9DA6", "#9D755D", "#BAB0AC",
]


VALID_MODES = {"offline", "online"}
DEFAULT_MODE = "offline"

def _timestamp():
    return datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")


def _ensure_dir():
    os.makedirs(CHARTS_DIR, exist_ok=True)


def _build_filename(label, chart_type, mode=DEFAULT_MODE):
    safe_label = (label or "chart").strip().replace(" ", "_")
    extension = "png" if mode == "offline" else "html"
    return os.path.join(CHARTS_DIR, f"chart_{chart_type}_{safe_label}_{_timestamp()}.{extension}")


_DEFAULT_WIDTH = 1100
_DEFAULT_HEIGHT = 700
_DEFAULT_FONT_SIZE = 13
_DEFAULT_TITLE_FONT_SIZE = 20
_DEFAULT_LEGEND_FONT_SIZE = 12

_BASE_LAYOUT = dict(
    template="plotly_white",
    font=dict(size=_DEFAULT_FONT_SIZE, family="Arial"),
    margin=dict(l=75, r=55, t=90, b=95),
    hovermode="x unified",
    legend=dict(
        orientation="h",
        yanchor="bottom",
        y=1.01,
        xanchor="right",
        x=1,
        font=dict(size=_DEFAULT_LEGEND_FONT_SIZE, family="Arial"),
    ),
    width=_DEFAULT_WIDTH,
    height=_DEFAULT_HEIGHT,
)


def _category_xaxis(labels, rotate=True):
    n = len(labels)
    axis = dict(
        type="category",
        categoryorder="array",
        categoryarray=labels,
        automargin=True,
        tickangle=-45 if (rotate and n > 6) else 0,
    )
    if n > 40:
        axis["tickmode"] = "auto"
        axis["nticks"] = 30
    return axis


def _wrap_title(title, max_chars=70):
    """Keep long titles from colliding with legends or plot content."""
    if not title:
        return None

    words = str(title).split()
    lines = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > max_chars:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)

    return "<br>".join(lines)


def _adaptive_dimensions(chart_type, labels, width=None, height=None):
    n = len(labels or [])
    longest = max((len(str(x)) for x in (labels or [])), default=0)

    if width is None:
        if chart_type in ("pie", "donut"):
            width = max(_DEFAULT_WIDTH, 850 + min(longest, 35) * 7)
        elif chart_type == "horizontal_bar":
            width = max(_DEFAULT_WIDTH, 1000)
        else:
            width = max(_DEFAULT_WIDTH, 850 + min(n, 20) * 12)

    if height is None:
        if chart_type in ("pie", "donut"):
            height = max(_DEFAULT_HEIGHT, 400 + n * 28)
        elif chart_type == "horizontal_bar":
            height = max(_DEFAULT_HEIGHT, 260 + n * 30)
        else:
            height = max(_DEFAULT_HEIGHT, 650)

    return int(width), int(height)


def _adaptive_font_size(font_size, labels):
    if font_size is not None:
        return max(8, min(32, int(font_size)))

    n = len(labels or [])
    longest = max((len(str(x)) for x in (labels or [])), default=0)

    if n > 35 or longest > 45:
        return 11
    if n > 20 or longest > 30:
        return 12
    return _DEFAULT_FONT_SIZE


def _build_figure(
    chart_type,
    labels,
    series,
    title=None,
    x_label=None,
    y_label=None,
    width=None,
    height=None,
    font_size=None,
):
    fig = go.Figure()
    colors = _PALETTE

    width, height = _adaptive_dimensions(chart_type, labels, width, height)
    font_size = _adaptive_font_size(font_size, labels)
    title_text = _wrap_title(title)

    layout = dict(_BASE_LAYOUT)
    layout["width"] = width
    layout["height"] = height
    layout["font"] = dict(size=font_size, family="Arial")
    layout["legend"] = dict(
        orientation="h",
        yanchor="bottom",
        y=1.01,
        xanchor="right",
        x=1,
        font=dict(size=max(9, font_size - 1), family="Arial"),
    )

    if chart_type == "histogram":
        for i, (name, values) in enumerate(series.items()):
            fig.add_trace(go.Histogram(
                x=values,
                name=name,
                opacity=0.75,
                marker_color=colors[i % len(colors)],
            ))
        fig.update_layout(barmode="overlay", **layout)
        fig.update_xaxes(title_text=x_label, automargin=True)
        fig.update_yaxes(title_text=y_label, automargin=True)

    elif chart_type in ("pie", "donut"):
        name, raw_values = next(iter(series.items()))
        filtered = [(l, v) for l, v in zip(labels, raw_values) if v]
        if not filtered:
            filtered = list(zip(labels, raw_values))
        plot_labels, values = (list(t) for t in zip(*filtered))
        many_labels = len(plot_labels) > MAX_PIE_DIRECT_LABELS

        fig.add_trace(go.Pie(
            labels=plot_labels,
            values=values,
            hole=0.45 if chart_type == "donut" else 0,
            texttemplate="%{value:,.2f}<br>(%{percent})",
            textposition="outside",
            hovertemplate="%{label}<br>Value: %{value:,.2f}<br>Share: %{percent}<extra></extra>",
            marker=dict(colors=[colors[i % len(colors)] for i in range(len(plot_labels))]),
            sort=False,
            domain=dict(x=[0.02, 0.66], y=[0.05, 0.92]),
        ))

        if len(labels) > 12:
            layout["legend"] = dict(
                orientation="v",
                x=0.70,
                xanchor="left",
                y=0.50,
                yanchor="middle",
                font=dict(size=max(9, font_size - 1), family="Arial"),
                bgcolor="rgba(255,255,255,0.85)",
                borderwidth=0,
            )
            layout["margin"] = dict(l=40, r=40, t=100, b=45)
        else:
            layout["legend"] = dict(
                orientation="h",
                x=0.5,
                xanchor="center",
                y=-0.02,
                yanchor="top",
                font=dict(size=max(9, font_size - 1), family="Arial"),
            )
            layout["margin"] = dict(l=45, r=45, t=100, b=85)

        fig.update_layout(**layout)

    else:
        x = labels
        axis = _category_xaxis(labels)

        if chart_type == "bar":
            for i, (name, values) in enumerate(series.items()):
                fig.add_trace(go.Bar(
                    x=x, y=values, name=name,
                    marker_color=colors[i % len(colors)],
                    text=values, textposition="outside",
                ))
            fig.update_layout(barmode="group", **layout)

        elif chart_type == "horizontal_bar":
            for i, (name, values) in enumerate(series.items()):
                fig.add_trace(go.Bar(
                    y=x, x=values, name=name, orientation="h",
                    marker_color=colors[i % len(colors)],
                    text=values, textposition="outside",
                ))
            fig.update_layout(barmode="group", **layout)
            fig.update_yaxes(title_text=x_label, **axis)
            fig.update_xaxes(title_text=y_label, automargin=True)
            if len(series) <= 1:
                fig.update_layout(showlegend=False)
            if title_text:
                fig.update_layout(
                    title=dict(
                        text=f"<b>{title_text}</b>",
                        x=0.5, xanchor="center", y=0.99, yanchor="top",
                        font=dict(size=max(16, font_size + 6), family="Arial"),
                    )
                )
            return fig

        elif chart_type == "stacked_bar":
            for i, (name, values) in enumerate(series.items()):
                fig.add_trace(go.Bar(
                    x=x, y=values, name=name,
                    marker_color=colors[i % len(colors)],
                    text=values, textposition="inside",
                ))
            fig.update_layout(barmode="stack", **layout)

        elif chart_type == "line":
            for i, (name, values) in enumerate(series.items()):
                fig.add_trace(go.Scatter(
                    x=x, y=values, name=name, mode="lines+markers",
                    line=dict(color=colors[i % len(colors)]),
                    text=values, textposition="top center",
                ))
            fig.update_layout(**layout)

        elif chart_type == "area":
            for i, (name, values) in enumerate(series.items()):
                fig.add_trace(go.Scatter(
                    x=x, y=values, name=name, mode="lines",
                    fill="tozeroy", opacity=0.6,
                    line=dict(color=colors[i % len(colors)])
                ))
            fig.update_layout(**layout)

        elif chart_type == "scatter":
            for i, (name, values) in enumerate(series.items()):
                fig.add_trace(go.Scatter(
                    x=x, y=values, name=name, mode="markers",
                    marker=dict(color=colors[i % len(colors)], size=9),
                    text=values, textposition="top center",
                ))
            fig.update_layout(**layout)

        else:
            raise ValueError(f"Unsupported chart_type: {chart_type}")

        fig.update_xaxes(title_text=x_label, **axis)
        fig.update_yaxes(title_text=y_label, automargin=True)

    if title_text:
        fig.update_layout(
            title=dict(
                text=f"<b>{title_text}</b>",
                x=0.5,
                xanchor="center",
                y=0.99,
                yanchor="top",
                font=dict(size=max(16, font_size + 6), family="Arial"),
            )
        )

    if len(series) <= 1 and chart_type not in ("pie", "donut"):
        fig.update_layout(showlegend=False)

    return fig


def _save_chart(chart_type, labels, series, filepath, title=None, x_label=None, y_label=None, mode=DEFAULT_MODE, width=None, height=None, font_size=None,):
    fig = _build_figure(chart_type, labels, series, title=title, x_label=x_label, y_label=y_label, width=width, height=height, font_size=font_size,    )
    _ensure_dir()
    if mode == "offline":
        try:
            # kaleido>=1 rasterizes via a local headless Chrome — both the
            # package and Chrome itself must be present on this machine.
            fig.write_image(filepath, format="png", width=fig.layout.width, height=fig.layout.height)
        except Exception as e:
            raise RuntimeError(
                "PNG export failed — offline mode needs the 'kaleido' package AND a "
                "local Chrome install (run 'plotly_get_chrome' once on this machine). "
                f"Use mode='online' for an interactive HTML chart instead. Original error: {e}"
            ) from e
    else:
        fig.write_html(
            filepath,
            include_plotlyjs="cdn",
            full_html=True,
            config={
                "displaylogo": False,
                "scrollZoom": True,
                "modeBarButtonsToAdd": ["v1hovermode", "toggleSpikelines"],
            },
        )


def generate_chart(labels, series, chart_type="bar", title=None, x_label=None, y_label=None, label=None, mode=DEFAULT_MODE, width=None, height=None, font_size=None,):
    chart_type = str(chart_type).strip().lower()
    mode = str(mode).strip().lower()

    if chart_type not in VALID_CHART_TYPES:
        raise ValueError(
            f"Unsupported chart_type: '{chart_type}'. Must be one of: "
            f"{', '.join(sorted(VALID_CHART_TYPES))}"
        )
    if mode not in VALID_MODES:
        raise ValueError(
            f"Unsupported mode: '{mode}'. Must be one of: {', '.join(sorted(VALID_MODES))}"
        )
    if not series:
        raise ValueError("generate_chart requires a non-empty series")

    if chart_type in _UNLABELED_TYPES:
        for name, values in series.items():
            if not values:
                raise ValueError(f"Series '{name}' has no values to plot")
    else:
        if not labels:
            raise ValueError("generate_chart requires non-empty labels for this chart_type")
        for name, values in series.items():
            if len(values) != len(labels):
                raise ValueError(
                    f"Series '{name}' has {len(values)} values but there are {len(labels)} labels"
                )

    if chart_type in _SINGLE_SERIES_TYPES and len(series) > 1:
        raise ValueError(
            f"'{chart_type}' charts show proportions of a single field only, but "
            f"got {len(series)} series ({', '.join(series.keys())}). Call "
            f"generate_chart once per field, or use chart_type='bar' to compare "
            f"multiple fields side by side."
        )

    filepath = _build_filename(label, chart_type, mode)
    _save_chart(chart_type, labels,series, filepath, title=title, x_label=x_label, y_label=y_label, mode=mode, width=width, height=height, font_size=font_size,)
    return {"filepath": filepath, "mode": mode, "format": "png" if mode == "offline" else "html",}