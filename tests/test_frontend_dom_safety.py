import re
from pathlib import Path


APP_JS = Path(__file__).resolve().parents[1] / "web" / "static" / "app.js"


def _load_source() -> str:
    return APP_JS.read_text(encoding="utf-8")


def test_app_js_only_uses_innerhtml_to_clear_elements():
    """innerHTML may only be used to *clear* elements (assigning the empty string)."""
    app_source = _load_source()
    unsafe_assignments = []

    for match in re.finditer(r"\binnerHTML\s*=\s*([^;\n]+)", app_source):
        assigned_value = match.group(1).strip()
        if assigned_value not in {"''", '""'}:
            line_number = app_source.count("\n", 0, match.start()) + 1
            unsafe_assignments.append(f"line {line_number}: {match.group(0).strip()}")

    assert unsafe_assignments == []


def test_app_js_does_not_assign_outerhtml():
    """outerHTML assignment is as dangerous as innerHTML and must not be used."""
    app_source = _load_source()
    matches = []
    for match in re.finditer(r"\bouterHTML\s*=", app_source):
        line_number = app_source.count("\n", 0, match.start()) + 1
        matches.append(f"line {line_number}: {match.group(0).strip()}")
    assert matches == []


def test_app_js_does_not_use_insertadjacenthtml():
    """insertAdjacentHTML accepts raw HTML — banned in favor of DOM construction."""
    app_source = _load_source()
    matches = []
    for match in re.finditer(r"\binsertAdjacentHTML\s*\(", app_source):
        line_number = app_source.count("\n", 0, match.start()) + 1
        matches.append(f"line {line_number}: {match.group(0).strip()}")
    assert matches == []


def test_app_js_does_not_set_text_html_transfer():
    """setData('text/html', ...) leaks raw markup into the drag transfer payload."""
    app_source = _load_source()
    pattern = re.compile(r"setData\s*\(\s*['\"]text/html['\"]")
    matches = []
    for match in pattern.finditer(app_source):
        line_number = app_source.count("\n", 0, match.start()) + 1
        matches.append(f"line {line_number}: {match.group(0).strip()}")
    assert matches == []


def test_app_js_does_not_use_runtime_string_evaluation():
    """eval() executes arbitrary code; banned in this codebase."""
    app_source = _load_source()
    # Word-boundary check excludes "evaluate", "medieval", etc.
    forbidden = re.compile(r"(?<![A-Za-z0-9_])eval\s*\(")
    matches = []
    for match in forbidden.finditer(app_source):
        line_number = app_source.count("\n", 0, match.start()) + 1
        matches.append(f"line {line_number}: {match.group(0).strip()}")
    assert matches == []


def test_app_js_does_not_use_document_write():
    """document.write is XSS-prone and should never appear."""
    app_source = _load_source()
    matches = []
    for match in re.finditer(r"\bdocument\s*\.\s*write(?:ln)?\s*\(", app_source):
        line_number = app_source.count("\n", 0, match.start()) + 1
        matches.append(f"line {line_number}: {match.group(0).strip()}")
    assert matches == []
