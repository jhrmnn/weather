#!/usr/bin/env python3
"""Draw an ECMWF-style 2 m temperature meteogram from Open-Meteo data.

This fetches an ensemble model from the Open-Meteo Ensemble API (the ECMWF IFS
0.25° ensemble, ``ecmwf_ifs025``, by default; ``--model`` selects another of
``collect.MODELS``) and draws an ECMWF-style box-and-whisker meteogram for 2 m
temperature.

An ensemble has one control forecast (the base ``temperature_2m`` series) plus a
number of perturbed members (``temperature_2m_member01`` ...), varying by model
(e.g. 51 total for ECMWF IFS, 31 for NOAA GEFS); the parser collects whichever
members the API returns. Data is requested at the model-*native* temporal
resolution — 3-hourly or hourly depending on the model — rather than
Open-Meteo's default 1-hourly interpolation.

Box-and-whisker convention (matching ECMWF's own meteograms):

    * wide box      -> 25th-75th percentile (interquartile range)
    * narrow box    -> 10th-25th and 75th-90th percentiles
    * whisker line  -> minimum and maximum across the ensemble
    * horizontal bar-> median (50th percentile)
    * thick red line-> median track (50th percentile connected across time)
    * blue line     -> control forecast

The box-and-whisker glyphs sit on the model-native 3-hourly steps, but the
median and control *tracks* are first resampled onto a finer grid with a
minimum-curvature natural cubic spline so the connecting curves read smoothly,
without the kinks of a shape-preserving interpolant.

Reference: https://confluence.ecmwf.int/display/FUG/Section+8.1.4+Meteograms
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from dataclasses import dataclass

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker as mticker  # noqa: E402
import numpy as np  # noqa: E402
from scipy.interpolate import CubicSpline  # noqa: E402

N_PERTURBED = 50  # perturbed members; the control is the base series (member 0)

# Smooth-line interpolation: the box-and-whisker glyphs stay on the model-native
# 3-hourly steps, but the control and median *tracks* are drawn on a finer grid
# so the connecting curves read smoothly. ``FINE_STEPS_PER_INTERVAL`` sub-samples
# each 3-hourly interval this many times.
FINE_STEPS_PER_INTERVAL = 12

# ECMWF-style colours.
BOX_GREY = "#D9D9D9"  # ensemble box fill (light neutral grey)
CONTROL_BLUE = "#0000FF"  # control forecast line
MEDIAN_RED = "#FF0000"   # median tracking line

# --- Internationalisation -------------------------------------------------
# Both figures can be rendered in English ("en") or Czech ("cs"). All
# user-visible strings, plus locale-dependent date names, live in the ``i18n``
# package (one ``<lang>.toml`` catalogue per language) so the two language
# versions stay in lock-step and translations are editable without touching
# code. The names below mirror the catalogue's data shapes; date-formatting and
# plural logic stay here, sourcing their vocabulary from the catalogues.
from i18n import (  # noqa: E402
    DAY_ABBR as _DAY_ABBR,
    DAY_FULL as _DAY_FULL,
    DEFAULT_LANG,
    LANGS,
    MONTH_ABBR as _MONTH_ABBR,
    MONTH_FULL as _MONTH_FULL,
    cadence as _cadence,
    runs_phrase as _runs_phrase,
    tr as _tr,
)

# Default figure model label/cadence used when a caller doesn't specify one (the
# meteogram CLI and ad-hoc plotting); mirrors ``collect.DEFAULT_MODEL``.
DEFAULT_MODEL_LABEL = "ECMWF IFS 0.25°"
DEFAULT_CADENCE = "3h"


def _fmt_day_tick(d: dt.datetime, lang: str) -> str:
    """X-axis day tick, e.g. ``Mon23`` / ``Po23``."""
    return f"{_DAY_ABBR[lang][d.weekday()]}{d.day:02d}"


def _fmt_init(d: dt.datetime, lang: str) -> str:
    """Long run-initialisation stamp for figure subtitles."""
    if lang == "cs":
        return (f"{_DAY_FULL['cs'][d.weekday()]} {d.day}. "
                f"{_MONTH_FULL['cs'][d.month - 1]} {d.year} "
                f"{d.hour:02d} UTC")
    return (f"{_DAY_FULL['en'][d.weekday()]} {d.day:02d} "
            f"{_MONTH_FULL['en'][d.month - 1]} {d.year} {d.hour:02d} UTC")


def _fmt_span(d: dt.datetime, lang: str) -> str:
    """Short run stamp used at each end of the evolution time span."""
    if lang == "cs":
        return (f"{_DAY_ABBR['cs'][d.weekday()]} {d.day}. "
                f"{_MONTH_ABBR['cs'][d.month - 1]} {d.hour:02d} UTC")
    return (f"{_DAY_ABBR['en'][d.weekday()]} {d.day:02d} "
            f"{_MONTH_ABBR['en'][d.month - 1]} {d.hour:02d} UTC")


@dataclass
class EnsembleData:
    """Container for a fetched ensemble time series at one point."""

    times: np.ndarray   # 1-D array of datetime64[m], shape (T,)
    members: np.ndarray  # 2-D array, shape (M, T); row 0 is the control
    latitude: float      # grid point latitude returned by the API
    longitude: float     # grid point longitude returned by the API
    requested_lat: float
    requested_lon: float
    elevation: float
    variable: str
    units: str
    run_time: dt.datetime | None = None  # model run, if known (see run_or_start)

    @property
    def control(self) -> np.ndarray:
        """Control forecast (the unperturbed member 0)."""
        return self.members[0]

    @property
    def window_start(self) -> dt.datetime:
        """Start of the returned forecast window (the first step).

        For the Open-Meteo forecast/ensemble API this is always 00:00 UTC of
        the current day, regardless of which model run produced the data, so it
        is *not* the run time — see ``run_or_start``.
        """
        return self.times[0].astype("datetime64[s]").astype(dt.datetime)

    @property
    def run_or_start(self) -> dt.datetime:
        """The model run's initialisation time, falling back to the window start.

        ``run_time`` is populated from the model metadata at fetch time; older
        archived payloads (and ad-hoc parses) without it fall back to the
        window start.
        """
        return self.run_time if self.run_time is not None else self.window_start

    @property
    def n_members(self) -> int:
        return self.members.shape[0]


def parse_payload(
    payload: dict,
    requested_lat: float,
    requested_lon: float,
    variable: str = "temperature_2m",
) -> EnsembleData:
    """Parse a raw Open-Meteo response (live or archived) into ``EnsembleData``.

    ``requested_lat``/``requested_lon`` are the coordinates that were asked for
    (the payload only carries the snapped grid point), used for the title.

    The ``model_run_time`` stamp that ``collect.fetch_raw`` attaches (the run's
    UTC initialisation time) is read into ``run_time`` when present; payloads
    without it fall back to the window start for labelling.
    """
    member_vars = [variable] + [
        f"{variable}_member{n:02d}" for n in range(1, N_PERTURBED + 1)
    ]

    hourly = payload["hourly"]
    times = np.array(hourly["time"], dtype="datetime64[m]")

    run_stamp = payload.get("model_run_time")
    run_time = dt.datetime.fromisoformat(run_stamp) if run_stamp else None

    # Collect every member that the API actually returned, control first.
    rows = []
    for name in member_vars:
        if name in hourly:
            rows.append([np.nan if v is None else v for v in hourly[name]])
    members = np.array(rows, dtype=float)

    units = payload.get("hourly_units", {}).get(variable, "")
    return EnsembleData(
        times=times,
        members=members,
        latitude=payload["latitude"],
        longitude=payload["longitude"],
        requested_lat=requested_lat,
        requested_lon=requested_lon,
        elevation=payload.get("elevation", float("nan")),
        variable=variable,
        units=units,
        run_time=run_time,
    )


def _format_coords(lat: float, lon: float) -> str:
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"{abs(lat):.2f}°{ns} {abs(lon):.2f}°{ew}"


def _draw_legend_glyph(ax: plt.Axes, lang: str = DEFAULT_LANG) -> None:
    """Draw a small schematic box-and-whisker key inside ``ax``."""
    # Schematic percentile levels (arbitrary, evenly readable spacing). The
    # percentile keys double as their own labels; the named levels are
    # translated below.
    levels = {
        "max": 8.0,
        "90%": 6.6,
        "75%": 5.2,
        "median": 4.0,
        "25%": 2.8,
        "10%": 1.4,
        "min": 0.0,
    }
    label_for = {
        "max": _tr(lang, "glyph_max"),
        "median": _tr(lang, "glyph_median"),
        "min": _tr(lang, "glyph_min"),
    }
    xc = 0.0
    ax.vlines(xc, levels["min"], levels["max"], color="black", linewidth=0.9)
    ax.bar(xc, levels["90%"] - levels["10%"], bottom=levels["10%"], width=1.0,
           color=BOX_GREY, edgecolor="black", linewidth=0.7)
    ax.bar(xc, levels["75%"] - levels["25%"], bottom=levels["25%"], width=2.0,
           color=BOX_GREY, edgecolor="black", linewidth=0.7)
    ax.hlines(levels["median"], xc - 1.0, xc + 1.0, color="black", linewidth=1.1)
    # Median tracking line key (thick red, drawn through the median level).
    ax.plot([xc - 1.0, xc + 1.0], [levels["median"], levels["median"]],
            color=MEDIAN_RED, linewidth=2.6, solid_capstyle="round")
    for key, y in levels.items():
        ax.text(1.7, y, label_for.get(key, key), va="center", ha="left",
                fontsize=6.5)

    # Control forecast key.
    ax.plot([-1.2, 0.6], [-1.8, -1.8], color=CONTROL_BLUE, linewidth=1.6)
    ax.text(1.7, -1.8, _tr(lang, "glyph_control"), va="center", ha="left",
            fontsize=6.5, color=CONTROL_BLUE)

    ax.set_xlim(-2.2, 5.0)
    ax.set_ylim(-3.0, 9.2)
    ax.set_facecolor("white")
    ax.patch.set_alpha(0.92)
    for spine in ax.spines.values():
        spine.set_edgecolor("0.6")
        spine.set_linewidth(0.6)
    ax.set_xticks([])
    ax.set_yticks([])


def plot(data: EnsembleData, output: str, station_name: str | None = None,
         station_height: float | None = None,
         lang: str = DEFAULT_LANG,
         model_label: str = DEFAULT_MODEL_LABEL,
         cadence: str = DEFAULT_CADENCE) -> None:
    """Render the ECMWF-style box-and-whisker meteogram to ``output``.

    ``lang`` selects the language of all labels and date names ("en" or "cs").
    ``model_label`` is the ensemble model's display name (e.g. "ECMWF IFS 0.25°")
    and ``cadence`` its cadence id ("1h"/"3h"/"6h"); both feed the localised
    titles and footer.
    """
    # Percentiles across all members at every time step.
    qs = np.nanpercentile(data.members, [0, 10, 25, 50, 75, 90, 100], axis=0)
    p_min, p10, p25, p50, p75, p90, p_max = qs

    x = mdates.date2num(data.times.astype("datetime64[s]").astype(dt.datetime))
    spacing = float(np.median(np.diff(x)))  # ~0.125 days (3 h)
    wide = spacing * 0.62
    narrow = spacing * 0.30

    # Finer grid for the smooth control/median tracks (boxes stay 3-hourly).
    # A natural cubic spline (zero second derivative at the ends) uniquely
    # minimises total curvature, giving the smoothest C2 tracks through the
    # 3-hourly values.
    x_fine = np.linspace(x[0], x[-1],
                         (len(x) - 1) * FINE_STEPS_PER_INTERVAL + 1)
    p50_fine = CubicSpline(x, p50, bc_type="natural")(x_fine)
    control_fine = CubicSpline(x, data.control, bc_type="natural")(x_fine)

    fig, ax = plt.subplots(figsize=(16, 5.6), dpi=140)

    # Whisker (min->max); the boxes drawn on top hide its central section.
    ax.vlines(x, p_min, p_max, color="black", linewidth=0.7, zorder=2)
    # Narrow boxes: 10th-90th percentile.
    ax.bar(x, p90 - p10, bottom=p10, width=narrow, color=BOX_GREY,
           edgecolor="black", linewidth=0.5, zorder=3, align="center")
    # Wide box: 25th-75th percentile.
    ax.bar(x, p75 - p25, bottom=p25, width=wide, color=BOX_GREY,
           edgecolor="black", linewidth=0.5, zorder=4, align="center")
    # Median.
    ax.hlines(p50, x - wide / 2, x + wide / 2, color="black", linewidth=0.9,
              zorder=5)
    # Median tracking line (thick red), cubic-Hermite-smoothed across time.
    ax.plot(x_fine, p50_fine, color=MEDIAN_RED, linewidth=2.6, zorder=6,
            label=_tr(lang, "legend_median"), solid_capstyle="round")
    # Control forecast, cubic-Hermite-smoothed.
    ax.plot(x_fine, control_fine, color=CONTROL_BLUE, linewidth=1.4, zorder=7,
            label=_tr(lang, "legend_control"))

    # --- axes cosmetics -------------------------------------------------
    ax.set_xlim(x[0] - spacing, x[-1] + spacing)
    ax.xaxis_date()
    ax.xaxis.set_major_locator(mdates.DayLocator())
    ax.xaxis.set_major_formatter(
        mticker.FuncFormatter(lambda v, _pos: _fmt_day_tick(
            mdates.num2date(v), lang)))
    ax.tick_params(axis="x", length=0)
    plt.setp(ax.get_xticklabels(), ha="center")

    ax.yaxis.set_major_locator(plt.MultipleLocator(6))
    ax.yaxis.set_minor_locator(plt.MultipleLocator(2))
    lo = np.floor((np.nanmin(p_min) - 1) / 2) * 2
    hi = np.ceil((np.nanmax(p_max) + 1) / 2) * 2
    ax.set_ylim(lo, hi)

    ax.grid(True, which="major", axis="both", linestyle=(0, (3, 3)),
            color="0.65", linewidth=0.6)
    ax.set_axisbelow(True)
    ax.set_ylabel(_tr(lang, "ylabel").format(units=data.units))

    # Month labels beneath the day ticks.
    months = data.times.astype("datetime64[M]")
    for m in np.unique(months):
        sel = x[months == m]
        label = _MONTH_ABBR[lang][m.astype(dt.date).month - 1]
        ax.text(float(np.mean(sel)), -0.075, label,
                transform=ax.get_xaxis_transform(),
                ha="center", va="top", fontsize=9, color="0.25")
    ax.text(1.0, -0.075, str(data.window_start.year), transform=ax.transAxes,
            ha="right", va="top", fontsize=9, color="0.25")

    # Titles.
    coords = _format_coords(data.requested_lat, data.requested_lon)
    where = f"{station_name + ' ' if station_name else ''}{coords}"
    if station_height is not None:
        where += f" ({station_height:g} m)"
    init = _fmt_init(data.run_or_start, lang)
    fig.suptitle(_tr(lang, "meteogram_suptitle"), x=0.5, y=0.98,
                 fontsize=14, ha="center")
    line1 = _tr(lang, "meteogram_line1").format(
        where=where, n=data.n_members, model=model_label,
        cadence=_cadence(lang, cadence))
    line2 = _tr(lang, "meteogram_line2").format(init=init)
    ax.set_title(f"{line1}\n{line2}", fontsize=9.5)

    # Schematic box-and-whisker key (upper-left, usually empty early in run).
    key = ax.inset_axes([0.012, 0.58, 0.085, 0.40])
    _draw_legend_glyph(key, lang)

    fig.text(0.005, 0.005, _tr(lang, "footer").format(model=model_label),
             fontsize=7, color="0.5", ha="left", va="bottom")

    fig.tight_layout(rect=(0, 0.02, 1, 0.96))
    fig.savefig(output, dpi=140)
    plt.close(fig)


def _smooth_track(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Resample ``y(x)`` onto a fine grid with a natural cubic spline.

    Mirrors the median/control smoothing in :func:`plot`; needs at least three
    points, otherwise the original samples are returned unchanged.
    """
    if len(x) < 3:
        return x, y
    x_fine = np.linspace(x[0], x[-1], (len(x) - 1) * FINE_STEPS_PER_INTERVAL + 1)
    return x_fine, CubicSpline(x, y, bc_type="natural")(x_fine)


def plot_median_evolution(runs: list[EnsembleData], output: str,
                          station_name: str | None = None,
                          station_height: float | None = None,
                          lang: str = DEFAULT_LANG,
                          model_label: str = DEFAULT_MODEL_LABEL,
                          cadence: str = DEFAULT_CADENCE) -> None:
    """Plot how the ensemble median for each time evolved across model runs.

    ``runs`` is a list of archived ensembles, oldest first. Each run's median
    track (50th percentile across members) is drawn against valid time and
    colour-coded by the run's initialisation time, so the forecast's evolution
    from run to run — and whether successive runs are converging — is visible
    at a glance. The most recent run is drawn boldest, on top.
    """
    runs = sorted(runs, key=lambda d: d.run_or_start)
    inits = [d.run_or_start for d in runs]
    init_nums = mdates.date2num(inits)

    cmap = matplotlib.colormaps["viridis"]
    multi = len(set(inits)) > 1
    if multi:
        norm = plt.Normalize(vmin=init_nums[0], vmax=init_nums[-1])
    else:
        norm = plt.Normalize(vmin=init_nums[0] - 1, vmax=init_nums[0] + 1)

    fig, ax = plt.subplots(figsize=(16, 5.6), dpi=140)

    units = runs[-1].units
    all_x: list[np.ndarray] = []
    all_med: list[float] = []
    n = len(runs)
    for i, d in enumerate(runs):
        p50 = np.nanpercentile(d.members, 50, axis=0)
        x = mdates.date2num(d.times.astype("datetime64[s]").astype(dt.datetime))
        all_x.append(x)
        all_med.extend([np.nanmin(p50), np.nanmax(p50)])
        x_fine, p50_fine = _smooth_track(x, p50)
        recency = i / max(n - 1, 1)  # 0 oldest .. 1 newest
        is_newest = i == n - 1
        ax.plot(x_fine, p50_fine, color=cmap(norm(init_nums[i])),
                linewidth=1.2 + 1.6 * recency,
                alpha=0.65 + 0.35 * recency,
                zorder=3 + i,
                solid_capstyle="round",
                label=_tr(lang, "legend_latest") if is_newest else None)

    # --- axes cosmetics -------------------------------------------------
    # Start the x-axis at the latest run's start, so older runs are shown
    # only over the window the newest run covers.
    x_lo = float(all_x[-1][0])
    x_hi = max(float(x[-1]) for x in all_x)
    spacing = float(np.median(np.diff(all_x[-1])))  # ~0.125 days (3 h)
    ax.set_xlim(x_lo - spacing, x_hi + spacing)
    ax.xaxis_date()
    ax.xaxis.set_major_locator(mdates.DayLocator())
    ax.xaxis.set_major_formatter(
        mticker.FuncFormatter(lambda v, _pos: _fmt_day_tick(
            mdates.num2date(v), lang)))
    ax.tick_params(axis="x", length=0)
    plt.setp(ax.get_xticklabels(), ha="center")

    ax.yaxis.set_major_locator(plt.MultipleLocator(2))
    ax.yaxis.set_minor_locator(plt.MultipleLocator(1))
    lo = np.floor((min(all_med) - 1) / 2) * 2
    hi = np.ceil((max(all_med) + 1) / 2) * 2
    ax.set_ylim(lo, hi)

    ax.grid(True, which="major", axis="both", linestyle=(0, (3, 3)),
            color="0.65", linewidth=0.6)
    ax.set_axisbelow(True)
    ax.set_ylabel(_tr(lang, "ylabel").format(units=units))

    # Month labels + year beneath the day ticks.
    months = np.arange(np.datetime64(mdates.num2date(x_lo).date(), "M"),
                       np.datetime64(mdates.num2date(x_hi).date(), "M") + 1)
    for m in months:
        m0 = mdates.date2num(m.astype("datetime64[D]").astype(dt.date))
        m1 = mdates.date2num((m + 1).astype("datetime64[D]").astype(dt.date))
        centre = min(max((m0 + m1) / 2, x_lo), x_hi)
        ax.text(centre, -0.075, _MONTH_ABBR[lang][m.astype(dt.date).month - 1],
                transform=ax.get_xaxis_transform(),
                ha="center", va="top", fontsize=9, color="0.25")
    ax.text(1.0, -0.075, str(mdates.num2date(x_hi).year),
            transform=ax.transAxes, ha="right", va="top", fontsize=9,
            color="0.25")

    # Colour key: a colorbar mapping line colour -> run initialisation time.
    if multi:
        sm = plt.cm.ScalarMappable(norm=norm, cmap=cmap)
        cbar = fig.colorbar(sm, ax=ax, pad=0.01, fraction=0.025)
        loc = mdates.AutoDateLocator()
        cbar.ax.yaxis.set_major_locator(loc)
        cbar.ax.yaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))
        cbar.set_label(_tr(lang, "colorbar"), fontsize=8)
        cbar.ax.tick_params(labelsize=7)

    ax.legend(loc="upper right", fontsize=8, framealpha=0.92)

    # Titles.
    coords = _format_coords(runs[-1].requested_lat, runs[-1].requested_lon)
    where = f"{station_name + ' ' if station_name else ''}{coords}"
    if station_height is not None:
        where += f" ({station_height:g} m)"
    span = (f"{_fmt_span(inits[0], lang)} – "
            f"{_fmt_span(inits[-1], lang)}")
    fig.suptitle(_tr(lang, "evolution_suptitle"), x=0.5, y=0.98,
                 fontsize=14, ha="center")
    line1 = _tr(lang, "evolution_line1").format(
        where=where, model=model_label, cadence=_cadence(lang, cadence))
    line2 = _tr(lang, "evolution_line2").format(
        runs=_runs_phrase(n, lang), span=span)
    ax.set_title(f"{line1}\n{line2}", fontsize=9.5)

    fig.text(0.005, 0.005, _tr(lang, "footer").format(model=model_label),
             fontsize=7, color="0.5", ha="left", va="bottom")

    fig.tight_layout(rect=(0, 0.02, 1, 0.96))
    fig.savefig(output, dpi=140)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--latitude", type=float, default=52.55,
                        help="latitude in degrees (default: 52.55, Berlin)")
    parser.add_argument("--longitude", type=float, default=13.41,
                        help="longitude in degrees (default: 13.41, Berlin)")
    parser.add_argument("--name", default=None,
                        help="optional place name shown in the title")
    parser.add_argument("--station-height", type=float, default=None,
                        help="optional station height in metres shown in the title")
    parser.add_argument("--forecast-days", type=int, default=11,
                        help="forecast length in days (default: 11)")
    parser.add_argument("--output", default="ecmwf_ens_temperature_meteogram.png",
                        help="output image path")
    parser.add_argument("--lang", default=DEFAULT_LANG, choices=LANGS,
                        help="language for labels and dates (default: en)")
    parser.add_argument("--save-json", default=None,
                        help="also write the raw API response to this path")

    # Fetching lives in the data layer (data/collect.py); meteogram only parses
    # and plots. Import it lazily so the plotting API has no fetch dependency.
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "data"))
    import collect  # noqa: E402

    models = {m.id: m for m in collect.MODELS}
    parser.add_argument("--model", default=collect.DEFAULT_MODEL.id,
                        choices=list(models),
                        help="ensemble model id (default: "
                             f"{collect.DEFAULT_MODEL.id})")
    args = parser.parse_args()
    model = models[args.model]

    print(f"Fetching {model.label} ensemble for {args.latitude}, "
          f"{args.longitude} ...")
    payload = collect.fetch_raw(args.latitude, args.longitude, model,
                                args.forecast_days)
    if args.save_json:
        with open(args.save_json, "w") as fh:
            json.dump(payload, fh, indent=2)
    data = parse_payload(payload, args.latitude, args.longitude)
    print(f"  grid point: {data.latitude:.3f}, {data.longitude:.3f}  "
          f"elevation {data.elevation:g} m")
    print(f"  members: {data.n_members}  "
          f"steps: {len(data.times)}  "
          f"({data.times[0]} -> {data.times[-1]})")

    plot(data, args.output, station_name=args.name,
         station_height=args.station_height, lang=args.lang,
         model_label=model.label, cadence=model.cadence)
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
