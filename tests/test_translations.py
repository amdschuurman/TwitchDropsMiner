import json

from src.config import LANG_PATH
from src.i18n.translator import GUIInvFilters, GUISettings


def test_all_language_settings_include_gui_settings_schema_keys():
    required_settings_keys = set(GUISettings.__annotations__)
    english_settings = json.loads((LANG_PATH / "English.json").read_text(encoding="utf-8"))["gui"][
        "settings"
    ]
    missing_by_language = {}

    for filepath in LANG_PATH.glob("*.json"):
        translation = json.loads(filepath.read_text(encoding="utf-8"))
        settings = translation["gui"]["settings"]
        missing = sorted(required_settings_keys - set(settings))
        if missing:
            missing_by_language[filepath.name] = missing

    assert sorted(set(english_settings) - required_settings_keys) == []
    assert missing_by_language == {}


def test_all_language_files_include_gui_inventory_filter_schema_keys():
    required_filter_keys = set(GUIInvFilters.__annotations__)
    english_filters = json.loads((LANG_PATH / "English.json").read_text(encoding="utf-8"))["gui"][
        "inventory"
    ]["filters"]
    missing_by_language = {}

    for filepath in LANG_PATH.glob("*.json"):
        translation = json.loads(filepath.read_text(encoding="utf-8"))
        filters = translation["gui"]["inventory"]["filters"]
        missing = sorted(required_filter_keys - set(filters))
        if missing:
            missing_by_language[filepath.name] = missing

    assert sorted(set(english_filters) - required_filter_keys) == []
    assert missing_by_language == {}
