#!/usr/bin/env python3
"""Assemble the static GitHub Pages site.

Generates the meteogram PNG and writes ``index.html`` (from
``site/template.html``) plus the image into the output directory.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

# Allow running as ``python site/build.py`` from the repo root: make the
# repository root (where ``meteogram.py`` lives) importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import meteogram  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="_site",
                        help="directory to write the site into (default: _site)")
    parser.add_argument("--latitude", type=float, default=52.55)
    parser.add_argument("--longitude", type=float, default=13.41)
    parser.add_argument("--name", default="Berlin")
    parser.add_argument("--forecast-days", type=int, default=11)
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    data_path = os.path.join(args.output_dir, "data.json")

    data = meteogram.fetch(args.latitude, args.longitude, args.forecast_days)
    snapshot = meteogram.to_dict(data, station_name=args.name)
    with open(data_path, "w") as fh:
        json.dump(snapshot, fh, separators=(",", ":"))

    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "template.html")) as fh:
        template = fh.read()

    updated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html = template.replace("__UPDATED__", updated)

    with open(os.path.join(args.output_dir, "index.html"), "w") as fh:
        fh.write(html)

    print(f"Built site in {args.output_dir} (data: {data_path})")


if __name__ == "__main__":
    main()
