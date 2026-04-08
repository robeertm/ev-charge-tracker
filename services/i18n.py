"""Lightweight i18n system using JSON translation files."""
import json
import os

_TRANSLATIONS = {}
_LANG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'translations')
_DEFAULT_LANG = 'de'
_current_lang = _DEFAULT_LANG

SUPPORTED_LANGUAGES = [
    ('de', 'Deutsch'),
    ('en', 'English'),
    ('fr', 'Francais'),
    ('es', 'Espanol'),
    ('it', 'Italiano'),
    ('nl', 'Nederlands'),
]


def _load_lang(lang):
    if lang not in _TRANSLATIONS:
        path = os.path.join(_LANG_DIR, f'{lang}.json')
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                _TRANSLATIONS[lang] = json.load(f)
        else:
            _TRANSLATIONS[lang] = {}
    return _TRANSLATIONS[lang]


def set_language(lang):
    global _current_lang
    if any(code == lang for code, _ in SUPPORTED_LANGUAGES):
        _current_lang = lang
    _load_lang(_current_lang)


def get_language():
    return _current_lang


def t(key, **kwargs):
    """Translate a key. Falls back to German, then returns the key itself."""
    text = _load_lang(_current_lang).get(key)
    if text is None and _current_lang != _DEFAULT_LANG:
        text = _load_lang(_DEFAULT_LANG).get(key)
    if text is None:
        return key
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, IndexError):
            pass
    return text


def init_app(app):
    """Register t() as Jinja2 global and set language from AppConfig."""
    from models.database import AppConfig

    @app.before_request
    def _set_lang():
        lang = AppConfig.get('app_language', _DEFAULT_LANG)
        set_language(lang)

    app.jinja_env.globals['t'] = t
    app.jinja_env.globals['current_lang'] = get_language
    app.jinja_env.globals['supported_languages'] = SUPPORTED_LANGUAGES
