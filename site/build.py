#!/usr/bin/env python3
"""Assemble the static GitHub Pages site.

Generates the meteogram PNG and writes ``index.html`` (from
``site/template.html``) plus the image into the output directory.

Renders from ``--data-dir`` — a checkout of the ``data`` branch — using the
latest archived response. Fetching is a separate, throttled job; this never
calls the API.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Allow running as ``python site/build.py`` from the repo root: make the
# repository root (``meteogram.py``) and ``data/`` (``collect.py``) importable.
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "data"))

import collect  # noqa: E402
import meteogram  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="_site",
                        help="directory to write the site into (default: _site)")
    parser.add_argument("--data-dir", required=True,
                        help="raw-data archive to render from (a checkout of "
                             "the data branch)")
    parser.add_argument("--latitude", type=float, default=52.55)
    parser.add_argument("--longitude", type=float, default=13.41)
    parser.add_argument("--name", default="Berlin")
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    image_path = os.path.join(args.output_dir, "meteogram.png")
    evolution_path = os.path.join(args.output_dir, "evolution.png")

    loc = collect.Location(args.latitude, args.longitude, args.name)
    payload = collect.load_latest(args.data_dir, loc)
    data = meteogram.parse_payload(payload, args.latitude, args.longitude)
    meteogram.plot(data, image_path, station_name=args.name)

    # Median-evolution plot: every archived run's median, colour-coded by run.
    runs = [meteogram.parse_payload(p, args.latitude, args.longitude)
            for p in collect.load_all(args.data_dir, loc)]
    meteogram.plot_median_evolution(runs, evolution_path, station_name=args.name)

    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "template.html")) as fh:
        template = fh.read()

    # Cache-busting token derived from the image contents: the ``?v=`` query
    # string changes only when the figure actually changes, so browsers fetch a
    # fresh image instead of serving a stale cached image while still caching an
    # unchanged one. (GitHub Pages ignores custom cache headers, so a
    # content-hashed URL is the portable fix.)
    def _version(path: str) -> str:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()[:12]

    updated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html = template.replace("__UPDATED__", updated)
    html = html.replace("__IMG_VERSION__", _version(image_path))
    html = html.replace("__EVO_VERSION__", _version(evolution_path))

    with open(os.path.join(args.output_dir, "index.html"), "w") as fh:
        fh.write(html)

    print(f"Built site in {args.output_dir} "
          f"(images: {image_path}, {evolution_path})")


if __name__ == "__main__":
    main()
