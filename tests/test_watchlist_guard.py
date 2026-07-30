"""Static-analysis guards for the client half of the watch-list wipe fix.

Same idiom as tests/test_frontend_dom_safety.py: regex over the source, no
browser, no JS runtime.

Background — the defect these pin down. ``autoCleanWantedQueue()`` deleted every
watched game whose campaigns were all fully claimed and then persisted the
result via ``saveSettings()``. It ran from the ``initial_state`` socket handler,
so on every page load and every socket reconnect, with no user gesture behind
it. On one account it computed an empty list, POSTed it, and the miner mined
nothing for five days without a word. The same premature save also shipped
``language: ""`` straight out of a ``<select>`` whose options had not loaded yet.

Both app.js copies are asserted. ``web/static/app.js`` is the one actually
served (``src/web/app.py`` resolves ``_web_dir`` to ``<repo>/web``);
``src/web/app.js`` is a dev-time mirror. Checking only the served copy would let
the mirror rot into a fix-shaped file that silently reintroduces the bug the
next time somebody copies it back, so the two are also asserted identical over
the functions this fix touches — those functions only. The files legitimately
differ elsewhere (fork branding, cache-bust query strings), and the mirror still
carries DOM-safety regressions that ``tests/test_frontend_dom_safety.py`` forbids
in the served copy, so parity here is deliberately per-function and the direction
of travel is one-way: served copy to mirror, never back.
"""

import re
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[1]

SERVED_APP_JS = "web/static/app.js"
MIRROR_APP_JS = "src/web/app.js"
APP_JS_COPIES = (SERVED_APP_JS, MIRROR_APP_JS)

SERVED_INDEX_HTML = "web/index.html"
MIRROR_INDEX_HTML = "src/web/index.html"
# Both markup copies are held to the structural assertions below. A whole-file
# compare is NOT possible (the mirror keeps its own fork branding), so only the
# controls this fix depends on are pinned, in both files.
INDEX_HTML_COPIES = (SERVED_INDEX_HTML, MIRROR_INDEX_HTML)


def _read(relative_path: str) -> str:
    return (_REPO_ROOT / relative_path).read_text(encoding="utf-8")


def extract_function(source: str, name: str) -> str:
    """Return the source of a top-level ``function <name>(...) { ... }``.

    Top-level functions in this bundle close with a ``}`` in column 0, so the
    body runs from the declaration to the first such brace. The balance
    assertion turns a mis-extraction into a loud failure instead of a silently
    truncated haystack that would make every other assertion here vacuous.
    """
    declaration = re.search(rf"\bfunction\s+{re.escape(name)}\s*\(", source)
    assert declaration, f"function {name}() not found"
    start = declaration.start()
    closing = re.search(r"\n\}", source[start:])
    assert closing, f"function {name}() has no column-0 closing brace"
    body = source[start : start + closing.end()]
    assert body.count("{") == body.count("}"), f"mis-extracted body for {name}()"
    return body


@pytest.mark.parametrize("path", APP_JS_COPIES)
def test_auto_clean_is_gated_on_the_opt_in_setting(path):
    """The destructive cleaner must not run unless the user opted in."""
    body = extract_function(_read(path), "autoCleanWantedQueue")

    guard = re.search(r"if\s*\([^)]*auto_clean_watchlist[^)]*\)\s*return", body)
    assert guard, (
        f"{path}: autoCleanWantedQueue() must early-return unless the "
        "auto_clean_watchlist setting is enabled"
    )
    persist = body.index("saveSettings(")
    assert guard.start() < persist, (
        f"{path}: the opt-in guard must run before anything is persisted"
    )


@pytest.mark.parametrize("path", APP_JS_COPIES)
def test_auto_clean_never_persists_an_empty_watch_list(path):
    """Even when opted in, the cleaner may not remove the last watched game."""
    body = extract_function(_read(path), "autoCleanWantedQueue")

    guard = re.search(r"if\s*\(\s*cleaned\.length\s*===?\s*0\s*\)", body)
    assert guard, f"{path}: autoCleanWantedQueue() must guard against an empty result"
    assignment = re.search(r"state\.settings\.games_to_watch\s*=\s*cleaned", body)
    assert assignment, f"{path}: expected the cleaned list to be assigned to state"
    assert guard.end() < assignment.start(), (
        f"{path}: the empty-result guard must come before the assignment, "
        "otherwise the empty list is already in state when it fires"
    )
    assert re.search(r"\breturn\b", body[guard.end():assignment.start()]), (
        f"{path}: the empty-result guard must bail out, not just log"
    )


@pytest.mark.parametrize("path", APP_JS_COPIES)
def test_save_settings_takes_an_explicit_intent_option(path):
    """Emptying the list stays possible — but only via a declared intent."""
    source = _read(path)

    signature = re.search(r"function\s+saveSettings\s*\(\s*(\w+)\s*=\s*\{\s*\}\s*\)", source)
    assert signature, f"{path}: saveSettings() must accept an options object"

    body = extract_function(source, "saveSettings")
    assert "allowEmptyGames" in body, (
        f"{path}: saveSettings() must read the allowEmptyGames option"
    )
    assert "allow_empty_games_to_watch" in body, (
        f"{path}: saveSettings() must be able to send allow_empty_games_to_watch, "
        "otherwise the server refuses every deliberate clear-all"
    )


@pytest.mark.parametrize("path", APP_JS_COPIES)
def test_save_settings_does_not_always_send_the_watch_list(path):
    """The bare always-send pattern is what POSTed the empty list."""
    body = extract_function(_read(path), "saveSettings")

    bare = re.search(r"games_to_watch:\s*state\.settings\.games_to_watch\s*\|\|\s*\[\]", body)
    assert bare is None, (
        f"{path}: saveSettings() must not unconditionally POST "
        "games_to_watch: state.settings.games_to_watch || []"
    )
    # Any games_to_watch key that survives has to be conditional. ``(?<!\w)``
    # keeps the allow_empty_games_to_watch flag out of the match.
    for match in re.finditer(r"(?<!\w)games_to_watch\s*:", body):
        line_start = body.rfind("\n", 0, match.start()) + 1
        line_end = body.find("\n", match.start())
        line = body[line_start:line_end if line_end != -1 else len(body)]
        assert "..." in line or "allowEmptyGames" in line, (
            f"{path}: unconditional games_to_watch key in saveSettings(): {line.strip()}"
        )


@pytest.mark.parametrize("path", APP_JS_COPIES)
def test_save_settings_does_not_post_a_raw_dom_language(path):
    """A <select> whose options have not loaded yields '' — never POST that."""
    body = extract_function(_read(path), "saveSettings")

    raw = re.search(
        r"language:\s*document\.getElementById\(\s*['\"]language['\"]\s*\)\s*\.value", body
    )
    assert raw is None, (
        f"{path}: saveSettings() must not send the #language <select> value directly; "
        "an unpopulated select reads '' and poisons the stored language"
    )
    assert "state.settings.language" in body, (
        f"{path}: the language must fall back to the loaded setting"
    )
    assert re.search(r"\.\.\.\([^)]*language[^)]*\?", body), (
        f"{path}: the language key must be omitted when no real value is available"
    )


@pytest.mark.parametrize(
    "function_name",
    ["toggleGameWatch", "removeGameFromWatch", "deselectAllGames"],
)
@pytest.mark.parametrize("path", APP_JS_COPIES)
def test_user_gestures_still_declare_intent_to_empty(path, function_name):
    """The user must keep being able to clear the list on purpose."""
    body = extract_function(_read(path), function_name)

    assert re.search(r"saveSettings\(\s*\{[^}]*allowEmptyGames\s*:\s*true[^}]*\}\s*\)", body), (
        f"{path}: {function_name}() can legitimately empty the watch list, so it must "
        "pass { allowEmptyGames: true } or the server will refuse the write"
    )


@pytest.mark.parametrize("function_name", ["autoCleanWantedQueue", "saveSettings"])
def test_both_app_js_copies_stay_in_lockstep(function_name):
    """A fix that lands in one copy only is a fix waiting to be undone."""
    served = extract_function(_read(SERVED_APP_JS), function_name)
    mirror = extract_function(_read(MIRROR_APP_JS), function_name)

    assert served == mirror, (
        f"{function_name}() drifted between {SERVED_APP_JS} and {MIRROR_APP_JS}"
    )


@pytest.mark.parametrize("path", APP_JS_COPIES)
def test_settings_ui_hydrates_the_opt_in_toggle(path):
    """Without hydration the toggle silently resets itself on every reload."""
    body = extract_function(_read(path), "updateSettingsUI")

    assert "auto_clean_watchlist" in body, (
        f"{path}: updateSettingsUI() must restore the auto_clean_watchlist toggle"
    )


@pytest.mark.parametrize("path", INDEX_HTML_COPIES)
def test_page_exposes_the_opt_in_toggle(path):
    """An opt-in setting with no control is an opt-in nobody can exercise.

    Asserted in the mirror too: its own app.js queries, hydrates and binds
    ``auto-clean-watchlist-toggle``, so a mirror without the element is a copy
    that cannot work if anyone ever serves it.
    """
    html = _read(path)

    assert re.search(
        r"<input[^>]*type=\"checkbox\"[^>]*id=\"auto-clean-watchlist-toggle\"", html
    ), f"{path}: missing the auto-clean-watchlist-toggle checkbox"


@pytest.mark.parametrize("path", INDEX_HTML_COPIES)
def test_language_select_placeholder_carries_no_real_value(path):
    """The static placeholder <option> must not look like a chosen language.

    ``saveSettings()`` only omits the language key when the ``<select>`` has no
    real value. The markup shipped ``<option value="en">``, so before
    ``loadLanguages()`` replaced the option set the select read ``"en"`` — a
    value that passes the falsy check — and every early save POSTed a language
    the user never picked, silently overwriting the stored one. An empty value
    is what makes the omit-when-blank path in ``saveSettings()`` reachable at
    all, so it is the markup half of that fix and belongs under test with it.
    """
    block = re.search(
        r"<select[^>]*\bid=\"language\"[^>]*>(.*?)</select>", _read(path), re.S
    )
    assert block, f"{path}: no <select id=\"language\"> found"

    values = re.findall(r"<option[^>]*\bvalue=\"([^\"]*)\"", block.group(1))
    assert values == [""], (
        f"{path}: the #language <select> must ship exactly one placeholder option "
        f"with an empty value, found {values!r}"
    )
