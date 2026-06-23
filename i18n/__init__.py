"""Message catalogues for the meteogram figures and the static site.

All user-visible text and locale date names live in the per-language ``*.toml``
files beside this module, one catalogue per language. This module loads them at
import time and exposes the same data shapes the rest of the code consumes, so
adding a language is just dropping in another ``<lang>.toml`` — no code change.

The figures can be rendered in English (``en``) or Czech (``cs``); the site is
built in every available language. Dates are formatted from the ``[dates]``
tables rather than via ``strftime``/locale, which keeps output identical
regardless of the host's installed locales.
"""
from __future__ import annotations

import os
import tomllib

DEFAULT_LANG = "en"

_DIR = os.path.dirname(os.path.abspath(__file__))


def _load() -> dict[str, dict]:
    catalogues: dict[str, dict] = {}
    for fname in os.listdir(_DIR):
        if fname.endswith(".toml"):
            with open(os.path.join(_DIR, fname), "rb") as fh:
                catalogues[fname[:-len(".toml")]] = tomllib.load(fh)
    return catalogues


_CATALOGUES = _load()

# Available languages in display order: the default first, then the rest
# alphabetically. The default is written to ``index.html`` by the site builder.
LANGS = [DEFAULT_LANG] + sorted(c for c in _CATALOGUES if c != DEFAULT_LANG)

# Data shapes matching what ``meteogram`` and ``site/build`` consume.
LANG_NAME = {lang: cat["name"] for lang, cat in _CATALOGUES.items()}
STRINGS = {lang: cat["strings"] for lang, cat in _CATALOGUES.items()}
HTML_STRINGS = {lang: cat["html"] for lang, cat in _CATALOGUES.items()}
DAY_ABBR = {lang: cat["dates"]["day_abbr"] for lang, cat in _CATALOGUES.items()}
DAY_FULL = {lang: cat["dates"]["day_full"] for lang, cat in _CATALOGUES.items()}
MONTH_ABBR = {lang: cat["dates"]["month_abbr"]
              for lang, cat in _CATALOGUES.items()}
MONTH_FULL = {lang: cat["dates"]["month_full"]
              for lang, cat in _CATALOGUES.items()}


def tr(lang: str, key: str) -> str:
    """Look up a translated figure string, falling back to the default language."""
    return STRINGS.get(lang, STRINGS[DEFAULT_LANG]).get(
        key, STRINGS[DEFAULT_LANG][key])


# Localised "model-native …" cadence phrases, keyed by the cadence id carried on
# each ``collect.Model`` ("1h"/"3h"/"6h"). The wording is translated, so it lives
# here rather than being passed as raw text from the data layer.
CADENCE = {lang: cat["cadence"] for lang, cat in _CATALOGUES.items()}


def cadence(lang: str, key: str) -> str:
    """Localised cadence phrase for a model's cadence id, with default fallback."""
    return CADENCE.get(lang, CADENCE[DEFAULT_LANG]).get(
        key, CADENCE[DEFAULT_LANG][key])


# Localised city display names, keyed by the language-independent city slug. A
# catalogue may omit the ``[cities]`` table (or any individual city) entirely;
# missing entries fall back to the canonical name from ``collect.LOCATIONS``.
CITIES = {lang: cat.get("cities", {}) for lang, cat in _CATALOGUES.items()}


def city_name(lang: str, slug: str, default: str) -> str:
    """Localised city name for a slug, falling back to ``default`` (its canonical
    name) when the language has no translation for it."""
    return CITIES.get(lang, {}).get(slug, default)


def _plural_category(n: int, lang: str) -> str:
    """CLDR-style plural category for ``n``. Czech distinguishes a ``few`` form."""
    if lang == "cs":
        if n == 1:
            return "one"
        if 2 <= n <= 4:
            return "few"
        return "other"
    return "one" if n == 1 else "other"


def runs_phrase(n: int, lang: str) -> str:
    """Localise a ``<count> run(s)`` phrase, including Czech plural forms."""
    plurals = _CATALOGUES.get(lang, _CATALOGUES[DEFAULT_LANG])["plurals"]
    return plurals[f"runs_{_plural_category(n, lang)}"].format(n=n)
