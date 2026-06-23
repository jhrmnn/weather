#!/usr/bin/env python3
"""Assemble the static GitHub Pages site.

Generates the meteogram PNGs and writes the HTML pages (from
``site/template.html``) plus the images into the output directory.

The site covers **every location in** ``collect.LOCATIONS`` and is rendered as a
**top-level city selector**: a single page per language embeds the figures for
all cities and a ``<select>`` dropdown switches between them client-side (no page
reload). Each figure pair is rendered once per city per language.

The site is also **bilingual** (English and Czech): a matching HTML page is
written for each language. The English page is ``index.html`` (the site
default); Czech lives at ``cs.html``. Every page links to the other via a small
language switcher, and the selected city is remembered across the switch.

Renders from ``--data-dir`` — a checkout of the ``data`` branch — using the
latest archived response. Fetching is a separate, throttled job; this never
calls the API.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import unicodedata

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Allow running as ``python site/build.py`` from the repo root: make the
# repository root (``meteogram.py``) and ``data/`` (``collect.py``) importable.
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "data"))

import collect  # noqa: E402
import meteogram  # noqa: E402

# Languages to render, in display order. The first is the site default and is
# written to ``index.html``; the rest get ``<lang>.html``.
LANGS = ["en", "cs"]

# Display name shown in the language switcher.
LANG_NAME = {"en": "English", "cs": "Čeština"}

# Per-language HTML chrome (everything outside the figures). The figure text
# itself is translated in ``meteogram.STRINGS``.
HTML_STRINGS = {
    "en": {
        "title": "ECMWF ENS 2 m Temperature Meteogram",
        "subtitle_tail": "ECMWF IFS 0.25° ensemble, model-native 3-hourly",
        "alt_meteo": "ECMWF ENS 2 m temperature meteogram",
        "label_city": "City",
        "label_updated": "Last updated:",
        "label_refresh": "Refreshes every 3 hours",
        "alt_evo": "ECMWF ENS median evolution: how the 2 m temperature "
                   "ensemble median for each time shifted across successive "
                   "model runs",
        "caption_evo": "How the ensemble <strong>median</strong> for each "
                       "time has shifted across successive model runs, "
                       "colour-coded by run initialisation time. Builds up as "
                       "runs are archived.",
        "footer": 'Data from the '
                  '<a href="https://open-meteo.com/en/docs/ensemble-api">'
                  'Open-Meteo Ensemble API</a> (CC BY 4.0), based on the '
                  'ECMWF IFS ensemble (ECMWF data, CC BY 4.0). Source on '
                  '<a href="https://github.com/jhrmnn/weather">GitHub</a>.',
    },
    "cs": {
        "title": "ECMWF ENS meteogram teploty ve 2 m",
        "subtitle_tail": "soubor ECMWF IFS 0.25°, nativní 3hodinové "
                         "rozlišení modelu",
        "alt_meteo": "Meteogram teploty ve 2 m ECMWF ENS",
        "label_city": "Město",
        "label_updated": "Naposledy aktualizováno:",
        "label_refresh": "Aktualizuje se každé 3 hodiny",
        "alt_evo": "Vývoj mediánu ECMWF ENS: jak se medián souboru teploty "
                   "ve 2 m pro každý čas posouval během po sobě jdoucích "
                   "běhů modelu",
        "caption_evo": "Jak se <strong>medián</strong> souboru pro každý čas "
                       "posouval během po sobě jdoucích běhů modelu, barevně "
                       "odlišeno podle času inicializace běhu. Doplňuje se s "
                       "přibývajícími běhy.",
        "footer": 'Data z '
                  '<a href="https://open-meteo.com/en/docs/ensemble-api">'
                  'Open-Meteo Ensemble API</a> (CC BY 4.0), založeno na '
                  'souboru ECMWF IFS (data ECMWF, CC BY 4.0). Zdrojový kód na '
                  '<a href="https://github.com/jhrmnn/weather">GitHubu</a>.',
    },
}


def _page_name(lang: str) -> str:
    """HTML file name for ``lang`` (the default language is ``index.html``)."""
    return "index.html" if lang == LANGS[0] else f"{lang}.html"


def _lang_nav(current: str) -> str:
    """Build the language-switcher markup, highlighting the current page."""
    parts = []
    for lang in LANGS:
        name = LANG_NAME[lang]
        if lang == current:
            parts.append(f'<a aria-current="page">{name}</a>')
        else:
            parts.append(f'<a href="{_page_name(lang)}">{name}</a>')
    return '<span class="sep">·</span>'.join(parts)


def _slug(name: str) -> str:
    """ASCII filename/anchor slug for a city name (``Český Krumlov`` → ``cesky-krumlov``)."""
    ascii_name = (
        unicodedata.normalize("NFKD", name)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    out = "".join(c if c.isalnum() else "-" for c in ascii_name.lower())
    return "-".join(part for part in out.split("-") if part)


def _version(path: str) -> str:
    """Cache-busting token derived from a file's contents.

    The ``?v=`` query string changes only when the figure actually changes, so
    browsers fetch a fresh image instead of serving a stale cached one while
    still caching an unchanged one. (GitHub Pages ignores custom cache headers,
    so a content-hashed URL is the portable fix.)
    """
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()[:12]


def _city_options(cities: list, default_slug: str) -> str:
    """Build the ``<option>`` markup for the city dropdown."""
    parts = []
    for loc in cities:
        slug = _slug(loc.name)
        selected = " selected" if slug == default_slug else ""
        parts.append(f'<option value="{slug}"{selected}>{loc.name}</option>')
    return "".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="_site",
                        help="directory to write the site into (default: _site)")
    parser.add_argument("--data-dir", required=True,
                        help="raw-data archive to render from (a checkout of "
                             "the data branch)")
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    # The city selector mirrors the archived locations. Skip any that have no
    # archived data yet (a freshly added location before its first fetch).
    cities = []
    for loc in collect.LOCATIONS:
        try:
            payload = collect.load_latest(args.data_dir, loc)
        except FileNotFoundError:
            print(f"  skipping {loc.name}: no archived data yet")
            continue
        data = meteogram.parse_payload(payload, loc.latitude, loc.longitude)
        runs = [meteogram.parse_payload(p, loc.latitude, loc.longitude)
                for p in collect.load_all(args.data_dir, loc)]
        cities.append((loc, data, runs))

    if not cities:
        raise SystemExit("no archived data for any location; nothing to build")

    default_slug = _slug(cities[0][0].name)

    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "template.html")) as fh:
        template = fh.read()

    updated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    written = []
    for lang in LANGS:
        s = HTML_STRINGS[lang]

        # One figure pair per city per language (text and dates are localised).
        # ``city_data`` drives the client-side dropdown: it maps each city slug
        # to its localised image URLs and subtitle.
        city_data = {}
        for loc, data, runs in cities:
            slug = _slug(loc.name)
            image_name = f"meteogram.{slug}.{lang}.png"
            evolution_name = f"evolution.{slug}.{lang}.png"
            image_path = os.path.join(args.output_dir, image_name)
            evolution_path = os.path.join(args.output_dir, evolution_name)
            meteogram.plot(data, image_path, station_name=loc.name, lang=lang)
            meteogram.plot_median_evolution(runs, evolution_path,
                                            station_name=loc.name, lang=lang)
            coords = meteogram._format_coords(loc.latitude, loc.longitude)
            city_data[slug] = {
                "sub": f"{loc.name} ({coords})",
                "meteo": f"{image_name}?v={_version(image_path)}",
                "evo": f"{evolution_name}?v={_version(evolution_path)}",
            }

        default = city_data[default_slug]
        replacements = {
            "__LANG__": lang,
            "__LANG_NAV__": _lang_nav(lang),
            "__TITLE__": s["title"],
            "__H1__": s["title"],
            "__LABEL_CITY__": s["label_city"],
            "__CITY_OPTIONS__": _city_options([c[0] for c in cities],
                                              default_slug),
            "__CITY_SUB__": default["sub"],
            "__SUBTITLE_TAIL__": s["subtitle_tail"],
            "__ALT_METEO__": s["alt_meteo"],
            "__LABEL_UPDATED__": s["label_updated"],
            "__UPDATED__": updated,
            "__LABEL_REFRESH__": s["label_refresh"],
            "__ALT_EVO__": s["alt_evo"],
            "__CAPTION_EVO__": s["caption_evo"],
            "__FOOTER__": s["footer"],
            "__METEO_SRC__": default["meteo"],
            "__EVO_SRC__": default["evo"],
            "__CITY_DATA__": json.dumps(city_data),
            "__DEFAULT_CITY__": default_slug,
        }
        html = template
        for token, value in replacements.items():
            html = html.replace(token, value)

        page = os.path.join(args.output_dir, _page_name(lang))
        with open(page, "w") as fh:
            fh.write(html)
        written.append(page)

    print(f"Built bilingual {len(cities)}-city site in {args.output_dir} "
          f"(pages: {', '.join(written)})")


if __name__ == "__main__":
    main()
