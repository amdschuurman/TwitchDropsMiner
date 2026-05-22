from typing import get_type_hints

from src.config.settings import InventoryFilters, default_settings


def test_default_inventory_filters_include_linked_and_not_linked():
    inv = default_settings["inventory_filters"]
    assert "show_linked" in inv
    assert "show_not_linked" in inv
    # Both default to True so users see all link states by default.
    assert inv["show_linked"] is True
    assert inv["show_not_linked"] is True


def test_inventory_filters_typeddict_has_show_linked():
    hints = get_type_hints(InventoryFilters)
    assert "show_linked" in hints
    assert "show_not_linked" in hints
    assert hints["show_linked"] is bool
