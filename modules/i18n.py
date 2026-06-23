"""Internationalization support.

Provides translation loading, a T() lookup function for Jinja2 templates,
and locale data export for JavaScript via JSON embedding.
"""

import json
import os

LOCALES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "web", "locales"
)

_translations: dict[str, str] = {}
_locale_json_cache: dict[str, str] = {}


def set_language(lang: str) -> None:
    """Load translations for the given language code (e.g. 'zh', 'en')."""
    global _translations
    path = os.path.join(LOCALES_DIR, f"{lang}.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            _translations = json.load(f)
    else:
        _translations = {}


def T(key: str, **kwargs) -> str:
    """Translate *key* into the current language.

    If the key is not found in the loaded locale the key itself is returned
    as a fallback (effectively English).

    Positional-style placeholders ``{0}``, ``{1}``… are supported for
    JavaScript interop.  Named placeholders via ``.format(**kwargs)`` are
    also supported for Jinja2 usage.
    """
    text = _translations.get(key, key)
    if kwargs:
        text = text.format(**kwargs)
    return text


def get_locale_data(lang: str) -> dict:
    """Return the full translation dictionary for *lang*."""
    path = os.path.join(LOCALES_DIR, f"{lang}.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def get_locale_json(lang: str) -> str:
    """Return a JSON string of the locale data for *lang* (cached)."""
    if lang not in _locale_json_cache:
        data = get_locale_data(lang)
        _locale_json_cache[lang] = json.dumps(data, ensure_ascii=False)
    return _locale_json_cache[lang]
