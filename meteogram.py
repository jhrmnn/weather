#!/usr/bin/env python3
"""Reproduce the ECMWF ENS 2 m temperature meteogram from Open-Meteo data.

This fetches the ECMWF IFS 0.25 deg ensemble (``ecmwf_ifs025``) from the
Open-Meteo Ensemble API and draws an ECMWF-style box-and-whisker meteogram for
2 m temperature.

The ensemble has 51 members: one control forecast (the base ``temperature_2m``
series) plus 50 perturbed members (``temperature_2m_member01`` ...
``temperature_2m_member50``).  Data is requested at the model-*native* temporal
resolution which, for the ECMWF IFS ensemble, is 3-hourly across the whole
forecast horizon (rather than Open-Meteo's default 1-hourly interpolation).

Box-and-whisker convention (matching ECMWF's own meteograms):

    * wide box      -> 25th-75th percentile (interquartile range)
    * narrow box    -> 10th-25th and 75th-90th percentiles
    * whisker line  -> minimum and maximum across the ensemble
    * horizontal bar-> median (50th percentile)
    * thick red line-> median track (50th percentile connected across time)
    * blue line     -> control forecast

Reference: https://confluence.ecmwf.int/display/FUG/Section+8.1.4+Meteograms
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from dataclasses import dataclass

import numpy as np
import requests

# matplotlib is imported lazily inside ``plot()`` so that data fetching and the
# JSON export (used by the web site build) do not require it.

PERCENTILE_LEVELS = [0, 10, 25, 50, 75, 90, 100]

ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
MODEL = "ecmwf_ifs025"
N_PERTURBED = 50  # perturbed members; the control is the base series (member 0)

# ECMWF-style colours.
CYAN = "#00FFFF"      # ensemble box fill
CONTROL_BLUE = "#0000FF"  # control forecast line
MEDIAN_RED = "#FF0000"   # median tracking line


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

    @property
    def control(self) -> np.ndarray:
        """Control forecast (the unperturbed member 0)."""
        return self.members[0]

    @property
    def init_time(self) -> dt.datetime:
        """Forecast initialisation time (first step)."""
        return self.times[0].astype("datetime64[s]").astype(dt.datetime)

    @property
    def n_members(self) -> int:
        return self.members.shape[0]


def fetch(
    latitude: float,
    longitude: float,
    forecast_days: int = 11,
    variable: str = "temperature_2m",
    raw_out: str | None = None,
) -> EnsembleData:
    """Fetch the ECMWF ensemble for ``variable`` at one point from Open-Meteo."""
    member_vars = [variable] + [
        f"{variable}_member{n:02d}" for n in range(1, N_PERTURBED + 1)
    ]
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": variable,
        "models": MODEL,
        "temporal_resolution": "native",  # 3-hourly for ECMWF IFS ENS
        "forecast_days": forecast_days,
        "timezone": "GMT",
    }
    resp = requests.get(ENSEMBLE_URL, params=params, timeout=60)
    resp.raise_for_status()
    payload = resp.json()

    if raw_out:
        with open(raw_out, "w") as fh:
            json.dump(payload, fh, indent=2)

    hourly = payload["hourly"]
    times = np.array(hourly["time"], dtype="datetime64[m]")

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
        requested_lat=latitude,
        requested_lon=longitude,
        elevation=payload.get("elevation", float("nan")),
        variable=variable,
        units=units,
    )


def percentiles(data: EnsembleData) -> dict[str, np.ndarray]:
    """Ensemble percentiles at every time step.

    Returns a dict with ``p_min, p10, p25, p50, p75, p90, p_max`` arrays, each
    of shape ``(T,)``.  Used by both the matplotlib renderer and the JSON
    export so the two stay numerically identical.
    """
    qs = np.nanpercentile(data.members, PERCENTILE_LEVELS, axis=0)
    keys = ["p_min", "p10", "p25", "p50", "p75", "p90", "p_max"]
    return dict(zip(keys, qs))


def to_dict(
    data: EnsembleData,
    station_name: str | None = None,
    station_height: float | None = None,
) -> dict:
    """Build a JSON-serialisable snapshot of the meteogram for web rendering."""

    def clean(arr: np.ndarray) -> list:
        # JSON has no NaN; emit ``null`` for missing values instead.
        return [None if v is None or np.isnan(v) else float(v) for v in arr]

    pct = percentiles(data)
    times = data.times.astype("datetime64[s]").astype(dt.datetime)
    return {
        "times": [t.isoformat() for t in times],
        "control": clean(data.control),
        **{k: clean(v) for k, v in pct.items()},
        "variable": data.variable,
        "units": data.units,
        "requested_lat": data.requested_lat,
        "requested_lon": data.requested_lon,
        "latitude": data.latitude,
        "longitude": data.longitude,
        "elevation": None if np.isnan(data.elevation) else float(data.elevation),
        "init_time": data.init_time.isoformat(),
        "n_members": data.n_members,
        "station_name": station_name,
        "station_height": station_height,
    }


def _format_coords(lat: float, lon: float) -> str:
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"{abs(lat):.2f}°{ns} {abs(lon):.2f}°{ew}"


def _draw_legend_glyph(ax: plt.Axes) -> None:
    """Draw a small schematic box-and-whisker key inside ``ax``."""
    # Schematic percentile levels (arbitrary, evenly readable spacing).
    levels = {
        "max": 8.0,
        "90%": 6.6,
        "75%": 5.2,
        "median": 4.0,
        "25%": 2.8,
        "10%": 1.4,
        "min": 0.0,
    }
    xc = 0.0
    ax.vlines(xc, levels["min"], levels["max"], color="black", linewidth=0.9)
    ax.bar(xc, levels["90%"] - levels["10%"], bottom=levels["10%"], width=1.0,
           color=CYAN, edgecolor="black", linewidth=0.7)
    ax.bar(xc, levels["75%"] - levels["25%"], bottom=levels["25%"], width=2.0,
           color=CYAN, edgecolor="black", linewidth=0.7)
    ax.hlines(levels["median"], xc - 1.0, xc + 1.0, color="black", linewidth=1.1)
    # Median tracking line key (thick red, drawn through the median level).
    ax.plot([xc - 1.0, xc + 1.0], [levels["median"], levels["median"]],
            color=MEDIAN_RED, linewidth=2.6, solid_capstyle="round")
    for label, y in levels.items():
        ax.text(1.7, y, label, va="center", ha="left", fontsize=6.5)

    # Control forecast key.
    ax.plot([-1.2, 0.6], [-1.8, -1.8], color=CONTROL_BLUE, linewidth=1.6)
    ax.text(1.7, -1.8, "control", va="center", ha="left", fontsize=6.5,
            color=CONTROL_BLUE)
    # Median tracking line key entry.
    ax.plot([-1.2, 0.6], [-3.0, -3.0], color=MEDIAN_RED, linewidth=2.6,
            solid_capstyle="round")
    ax.text(1.7, -3.0, "median", va="center", ha="left", fontsize=6.5,
            color=MEDIAN_RED)

    ax.set_xlim(-2.2, 5.0)
    ax.set_ylim(-4.0, 9.2)
    ax.set_facecolor("white")
    ax.patch.set_alpha(0.92)
    for spine in ax.spines.values():
        spine.set_edgecolor("0.6")
        spine.set_linewidth(0.6)
    ax.set_xticks([])
    ax.set_yticks([])


def plot(data: EnsembleData, output: str, station_name: str | None = None,
         station_height: float | None = None) -> None:
    """Render the ECMWF-style box-and-whisker meteogram to ``output``."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    # Percentiles across all members at every time step.
    pct = percentiles(data)
    p_min, p10, p25, p50, p75, p90, p_max = (
        pct["p_min"], pct["p10"], pct["p25"], pct["p50"],
        pct["p75"], pct["p90"], pct["p_max"],
    )

    x = mdates.date2num(data.times.astype("datetime64[s]").astype(dt.datetime))
    spacing = float(np.median(np.diff(x)))  # ~0.125 days (3 h)
    wide = spacing * 0.62
    narrow = spacing * 0.30

    fig, ax = plt.subplots(figsize=(16, 5.6), dpi=140)

    # Whisker (min->max); the boxes drawn on top hide its central section.
    ax.vlines(x, p_min, p_max, color="black", linewidth=0.7, zorder=2)
    # Narrow boxes: 10th-90th percentile.
    ax.bar(x, p90 - p10, bottom=p10, width=narrow, color=CYAN,
           edgecolor="black", linewidth=0.5, zorder=3, align="center")
    # Wide box: 25th-75th percentile.
    ax.bar(x, p75 - p25, bottom=p25, width=wide, color=CYAN,
           edgecolor="black", linewidth=0.5, zorder=4, align="center")
    # Median.
    ax.hlines(p50, x - wide / 2, x + wide / 2, color="black", linewidth=0.9,
              zorder=5)
    # Median tracking line (thick red) connecting the medians across time.
    ax.plot(x, p50, color=MEDIAN_RED, linewidth=2.6, zorder=6,
            label="Median", solid_capstyle="round")
    # Control forecast.
    ax.plot(x, data.control, color=CONTROL_BLUE, linewidth=1.4, zorder=7,
            label="Control forecast")

    # --- axes cosmetics -------------------------------------------------
    ax.set_xlim(x[0] - spacing, x[-1] + spacing)
    ax.xaxis_date()
    ax.xaxis.set_major_locator(mdates.DayLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%a%d"))
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
    ax.set_ylabel(f"2 m Temperature ({data.units})")

    # Month labels beneath the day ticks.
    months = data.times.astype("datetime64[M]")
    for m in np.unique(months):
        sel = x[months == m]
        label = m.astype(dt.date).strftime("%b")
        ax.text(float(np.mean(sel)), -0.075, label,
                transform=ax.get_xaxis_transform(),
                ha="center", va="top", fontsize=9, color="0.25")
    ax.text(1.0, -0.075, str(data.init_time.year), transform=ax.transAxes,
            ha="right", va="top", fontsize=9, color="0.25")

    # Titles.
    coords = _format_coords(data.requested_lat, data.requested_lon)
    where = f"{station_name + ' ' if station_name else ''}{coords}"
    if station_height is not None:
        where += f" ({station_height:g} m)"
    init = data.init_time.strftime("%A %d %B %Y %H UTC")
    fig.suptitle("ECMWF ENS Meteogram – 2 m Temperature", x=0.5, y=0.98,
                 fontsize=14, ha="center")
    ax.set_title(
        f"{where}  ·  ECMWF IFS 0.25° ensemble "
        f"({data.n_members} members), model-native 3-hourly\n"
        f"Control Forecast and ENS Distribution – {init}",
        fontsize=9.5,
    )

    ax.legend(loc="upper right", fontsize=8, framealpha=0.92)

    # Schematic box-and-whisker key (upper-left, usually empty early in run).
    key = ax.inset_axes([0.012, 0.58, 0.085, 0.40])
    _draw_legend_glyph(key)

    fig.text(0.005, 0.005,
             "Data: Open-Meteo Ensemble API (CC BY 4.0) – ECMWF IFS ensemble",
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
    parser.add_argument("--save-json", default=None,
                        help="also write the raw API response to this path")
    args = parser.parse_args()

    print(f"Fetching ECMWF IFS ensemble for {args.latitude}, {args.longitude} "
          f"({args.forecast_days} days, native 3-hourly) ...")
    data = fetch(args.latitude, args.longitude, args.forecast_days,
                 raw_out=args.save_json)
    print(f"  grid point: {data.latitude:.3f}, {data.longitude:.3f}  "
          f"elevation {data.elevation:g} m")
    print(f"  members: {data.n_members}  "
          f"steps: {len(data.times)}  "
          f"({data.times[0]} -> {data.times[-1]})")

    plot(data, args.output, station_name=args.name,
         station_height=args.station_height)
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
