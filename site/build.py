#!/usr/bin/env python3
"""Assemble the static GitHub Pages site.

Generates the meteogram PNGs and writes the HTML pages (from
``site/template.html``) plus the images into the output directory.

The site covers **every location in** ``collect.LOCATIONS`` and **every model in**
``collect.MODELS``, rendered with two **top-level selectors**: a single page per
language embeds the figures for all city/model combinations, and ``<select>``
dropdowns for city and model switch between them client-side (no page reload).
Each figure pair is rendered once per city per model per language; a model that
has no archived data for a given city yet is simply skipped for it.

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
import i18n  # noqa: E402
import meteogram  # noqa: E402

# Languages to render, in display order (the first is the site default, written
# to ``index.html``); display names for the language switcher; and the
# per-language HTML chrome (everything outside the figures). All three come from
# the ``i18n`` catalogues, the same source as the figure text.
LANGS = i18n.LANGS
LANG_NAME = i18n.LANG_NAME
HTML_STRINGS = i18n.HTML_STRINGS


def _page_name(lang: str) -> str:
    """HTML file name for ``lang`` (the default language is ``index.html``)."""
    return "index.html" if lang == LANGS[0] else f"{lang}.html"


def _page_href(lang: str) -> str:
    """Relative URL for ``lang`` (the default language lives at the directory
    root, so it links to ``./`` rather than the bare ``index.html``)."""
    return "./" if lang == LANGS[0] else f"{lang}.html"


def _lang_nav(current: str) -> str:
    """Build the language-switcher markup, highlighting the current page."""
    parts = []
    for lang in LANGS:
        name = LANG_NAME[lang]
        if lang == current:
            parts.append(f'<a aria-current="page">{name}</a>')
        else:
            parts.append(f'<a href="{_page_href(lang)}">{name}</a>')
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


def _city_options(cities: list, default_slug: str, lang: str) -> str:
    """Build the ``<option>`` markup for the city dropdown, with localised labels.

    The ``value`` is the language-independent slug (so the selected city carries
    across the language switch); only the displayed label is localised.
    """
    parts = []
    for loc in cities:
        slug = _slug(loc.name)
        name = i18n.city_name(lang, slug, loc.name)
        selected = " selected" if slug == default_slug else ""
        parts.append(f'<option value="{slug}"{selected}>{name}</option>')
    return "".join(parts)


def _model_options(models: list, default_id: str) -> str:
    """Build the ``<option>`` markup for the model dropdown."""
    parts = []
    for model in models:
        selected = " selected" if model.id == default_id else ""
        parts.append(
            f'<option value="{model.id}"{selected}>{model.label}</option>')
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

    # The selectors mirror the archived data: a city appears if at least one
    # model has data for it, and within a city only the models that have data
    # are offered. Each entry pairs a location with a model_id -> (model, latest,
    # runs) map, in ``collect.MODELS`` order. Skip cities/models not yet fetched.
    cities = []  # list[(Location, dict[str, tuple[Model, EnsembleData, list]])]
    for loc in collect.LOCATIONS:
        per_model = {}
        for model in collect.MODELS:
            try:
                payload = collect.load_latest(args.data_dir, loc, model)
            except FileNotFoundError:
                continue
            data = meteogram.parse_payload(payload, loc.latitude, loc.longitude)
            runs = [meteogram.parse_payload(p, loc.latitude, loc.longitude)
                    for p in collect.load_all(args.data_dir, loc, model)]
            per_model[model.id] = (model, data, runs)
        if per_model:
            cities.append((loc, per_model))
        else:
            print(f"  skipping {loc.name}: no archived data yet")

    if not cities:
        raise SystemExit("no archived data for any location; nothing to build")

    # Models present for at least one city, in display order, for the selector.
    models_present = [m for m in collect.MODELS
                      if any(m.id in pm for _, pm in cities)]

    default_slug = _slug(cities[0][0].name)
    # Default model: the configured default if the default city has it, else that
    # city's first available model (selectors must open on a valid combination).
    default_city_models = cities[0][1]
    default_model_id = (collect.DEFAULT_MODEL.id
                        if collect.DEFAULT_MODEL.id in default_city_models
                        else next(iter(default_city_models)))

    # Stable comparison-plot colour per model (by registry order), so a model
    # keeps the same colour across cities even when some lack it.
    model_color = {m.id: meteogram.COMPARE_COLORS[i % len(meteogram.COMPARE_COLORS)]
                   for i, m in enumerate(collect.MODELS)}

    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "template.html")) as fh:
        template = fh.read()

    updated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    written = []
    for lang in LANGS:
        s = HTML_STRINGS[lang]

        # Per city: one model-comparison ("integration") figure plus one
        # ensemble/history figure pair per model (text/dates localised).
        # ``city_data`` drives the client-side selectors: each city slug maps to
        # its subtitle, the comparison image (city-level), and a per-model map of
        # localised image URLs and the model-specific subtitle tail.
        city_data = {}
        for loc, per_model in cities:
            slug = _slug(loc.name)
            # Localised city name for figures and chrome; the slug stays
            # language-independent so URLs and the remembered selection match.
            name = i18n.city_name(lang, slug, loc.name)
            coords = meteogram._format_coords(loc.latitude, loc.longitude)

            # Model-comparison plot: each model's latest-run median on one axis.
            integ_name = f"integration.{slug}.{lang}.png"
            integ_path = os.path.join(args.output_dir, integ_name)
            series = [(model.label, model_color[model_id], data)
                      for model_id, (model, data, runs) in per_model.items()]
            meteogram.plot_model_comparison(series, integ_path,
                                            station_name=name, lang=lang)

            model_map = {}
            for model_id, (model, data, runs) in per_model.items():
                image_name = f"meteogram.{slug}.{model_id}.{lang}.png"
                evolution_name = f"evolution.{slug}.{model_id}.{lang}.png"
                image_path = os.path.join(args.output_dir, image_name)
                evolution_path = os.path.join(args.output_dir, evolution_name)
                meteogram.plot(data, image_path, station_name=name,
                               lang=lang, model_label=model.label,
                               cadence=model.cadence)
                meteogram.plot_median_evolution(
                    runs, evolution_path, station_name=name, lang=lang,
                    model_label=model.label, cadence=model.cadence)
                tail = s["subtitle_tail"].format(
                    model=model.label, cadence=i18n.cadence(lang, model.cadence))
                model_map[model_id] = {
                    "tail": tail,
                    "meteo": f"{image_name}?v={_version(image_path)}",
                    "evo": f"{evolution_name}?v={_version(evolution_path)}",
                }
            city_data[slug] = {
                "sub": f"{name} ({coords})",
                "integ": f"{integ_name}?v={_version(integ_path)}",
                "models": model_map,
            }

        default_city = city_data[default_slug]
        default = default_city["models"][default_model_id]
        replacements = {
            "__LANG__": lang,
            "__LANG_NAV__": _lang_nav(lang),
            "__TITLE__": s["title"],
            "__H1__": s["title"],
            "__LABEL_CITY__": s["label_city"],
            "__LABEL_MODEL__": s["label_model"],
            "__CITY_OPTIONS__": _city_options([c[0] for c in cities],
                                              default_slug, lang),
            "__MODEL_OPTIONS__": _model_options(models_present,
                                                default_model_id),
            "__CITY_SUB__": default_city["sub"],
            "__SUBTITLE_TAIL__": default["tail"],
            "__ALT_METEO__": s["alt_meteo"],
            "__ALT_INTEG__": s["alt_integ"],
            "__CAPTION_INTEG__": s["caption_integ"],
            "__LABEL_UPDATED__": s["label_updated"],
            "__UPDATED__": updated,
            "__LABEL_REFRESH__": s["label_refresh"],
            "__ALT_EVO__": s["alt_evo"],
            "__CAPTION_EVO__": s["caption_evo"],
            "__FOOTER__": s["footer"],
            "__INTEG_SRC__": default_city["integ"],
            "__METEO_SRC__": default["meteo"],
            "__EVO_SRC__": default["evo"],
            "__CITY_DATA__": json.dumps(city_data),
            "__DEFAULT_CITY__": default_slug,
            "__DEFAULT_MODEL__": default_model_id,
        }
        html = template
        for token, value in replacements.items():
            html = html.replace(token, value)

        page = os.path.join(args.output_dir, _page_name(lang))
        with open(page, "w") as fh:
            fh.write(html)
        written.append(page)

    print(f"Built bilingual {len(cities)}-city × {len(models_present)}-model "
          f"site in {args.output_dir} (pages: {', '.join(written)})")


if __name__ == "__main__":
    main()
