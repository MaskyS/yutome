"""Thin questionary wrappers used by `yutome setup`.

questionary is lazy-imported so commands that never hit the wizard (find,
list, show, sync, etc.) don't pay its startup cost. Wrappers translate
Ctrl-C / Ctrl-D into a clean typer.Abort and apply a single brand style
across every prompt.

When stdin is not a TTY (CI, CliRunner, piped input) we fall back to plain
typer.prompt / typer.confirm so test harnesses and scripted setup
(`yutome setup ... < input.txt`) keep working — questionary's
prompt_toolkit backend can't read from a non-TTY stream.
"""
from __future__ import annotations

import sys
import webbrowser
from typing import Callable, Iterable

import typer


_STYLE = None


def _is_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def is_interactive() -> bool:
    return _is_tty()


def _style():
    global _STYLE
    if _STYLE is None:
        import questionary

        # Brand palette derived from the Yutome icon: deep purple + the
        # orange dot. Falls back to terminal defaults if truecolor isn't
        # supported.
        _STYLE = questionary.Style(
            [
                ("qmark", "fg:#E9663D bold"),
                ("question", "bold"),
                ("answer", "fg:#3D1D49 bold"),
                ("pointer", "fg:#E9663D bold"),
                ("highlighted", "fg:#3D1D49 bold"),
                ("selected", "fg:#E9663D"),
                ("instruction", "fg:#888888"),
            ]
        )
    return _STYLE


def _ask(prompt):
    answer = prompt.ask()
    if answer is None:
        # Ctrl-C / Ctrl-D — questionary returns None instead of raising.
        # Surface as a clean abort so setup doesn't continue with junk data.
        raise typer.Abort()
    return answer


def confirm(message: str, *, default: bool = False) -> bool:
    if not _is_tty():
        return typer.confirm(message, default=default)
    import questionary

    return _ask(questionary.confirm(message, default=default, style=_style()))


def text(message: str, *, default: str = "") -> str:
    if not _is_tty():
        return typer.prompt(message, default=default, show_default=bool(default)).strip()
    import questionary

    return _ask(
        questionary.text(message, default=default, style=_style())
    ).strip()


def password(message: str) -> str:
    if not _is_tty():
        return typer.prompt(message, hide_input=True, default="", show_default=False).strip()
    import questionary

    return _ask(questionary.password(message, style=_style())).strip()


def select(message: str, choices: list, *, default=None):
    """Single-pick prompt.

    Each item in ``choices`` may be:

    - a plain ``str`` — legacy. Returned as-is when selected.
    - a ``(label,)`` 1-tuple — a TTY visual separator (e.g. a section header
      like ``"─── On this Mac ───"``). In non-TTY it's printed but not
      numbered.
    - a ``(label, value)`` 2-tuple — a choice that displays ``label`` and
      returns ``value`` when picked.
    - a ``(label, value, disabled_reason)`` 3-tuple — a choice that is shown
      greyed-out in TTY (cannot be picked). In non-TTY it's numbered but
      decorated with the disabled reason, and selecting it returns the value
      anyway so existing late-check fallbacks (e.g. "Node missing") still
      run.

    ``default`` matches a choice's ``value`` (or its label for plain strings).
    """

    def label_of(item) -> str:
        if isinstance(item, tuple):
            return item[0]
        return item

    def value_of(item):
        if isinstance(item, tuple):
            return item[1] if len(item) >= 2 else item[0]
        return item

    def is_separator(item) -> bool:
        return isinstance(item, tuple) and len(item) == 1

    def disabled_of(item) -> str | None:
        if isinstance(item, tuple) and len(item) >= 3:
            return item[2]
        return None

    if not _is_tty():
        typer.echo(message)
        selectable: list = []
        for item in choices:
            if is_separator(item):
                typer.echo(f"  {label_of(item)}")
                continue
            selectable.append(item)
            label = label_of(item)
            disabled_reason = disabled_of(item)
            suffix = f"  ({disabled_reason})" if disabled_reason else ""
            typer.echo(f"  {len(selectable)}. {label}{suffix}")
        labels = [label_of(item) for item in selectable]
        values = [value_of(item) for item in selectable]
        default_index = 1
        for index, item in enumerate(selectable, 1):
            if default is not None and (value_of(item) == default or label_of(item) == default):
                default_index = index
                break
        while True:
            raw = typer.prompt("Choose by number or label", default=str(default_index)).strip()
            if raw.isdigit() and 1 <= int(raw) <= len(selectable):
                return values[int(raw) - 1]
            if raw in labels:
                return values[labels.index(raw)]
            typer.echo("[WARN] Invalid choice; try again.")

    import questionary

    pretty: list = []
    default_value = None
    for item in choices:
        if is_separator(item):
            pretty.append(questionary.Separator(label_of(item)))
            continue
        disabled_reason = disabled_of(item)
        if isinstance(item, tuple):
            pretty.append(
                questionary.Choice(
                    title=label_of(item),
                    value=value_of(item),
                    disabled=disabled_reason,
                )
            )
            if default is not None and (value_of(item) == default or label_of(item) == default):
                default_value = value_of(item)
        else:
            pretty.append(item)
            if default is not None and item == default:
                default_value = item

    if default_value is None and default is not None:
        default_value = default

    return _ask(
        questionary.select(message, choices=pretty, default=default_value, style=_style())
    )


def checkbox(
    message: str,
    choices: list[str],
    *,
    defaults: Iterable[str] = (),
    instruction: str | None = None,
    use_search_filter: bool = False,
    erase_when_done: bool = False,
    validate: Callable[[list[str]], bool | str] | None = None,
) -> list[str]:
    if not _is_tty():
        default_set = set(defaults)
        typer.echo(message)
        for index, choice in enumerate(choices, 1):
            tick = "x" if choice in default_set else " "
            typer.echo(f"  [{tick}] {index}. {choice}")
        raw = typer.prompt(
            "Numbers to toggle (comma-separated), or blank to accept defaults", default=""
        ).strip()
        result = set(default_set)
        if raw:
            for token in raw.split(","):
                token = token.strip()
                if token.isdigit() and 1 <= int(token) <= len(choices):
                    label = choices[int(token) - 1]
                    if label in result:
                        result.discard(label)
                    else:
                        result.add(label)
        return [c for c in choices if c in result]
    import questionary

    default_set = set(defaults)
    choice_objs = [{"name": c, "checked": c in default_set} for c in choices]
    checkbox_kwargs = {
        "message": message,
        "choices": choice_objs,
        "instruction": instruction,
        "use_search_filter": use_search_filter,
        "erase_when_done": erase_when_done,
        "style": _style(),
    }
    if use_search_filter:
        checkbox_kwargs["use_jk_keys"] = False
    if validate is not None:
        checkbox_kwargs["validate"] = validate
    return _ask(
        questionary.checkbox(**checkbox_kwargs)
    )


def press_any_key(message: str = "Press Enter to continue …") -> None:
    """Block until the user presses a key. No-op when stdin isn't a TTY.

    Used to gate multi-step walkthroughs (the Google Cloud Console OAuth
    setup) so a first-timer can complete one step before being shown the
    next. CliRunner tests get the no-op behavior so they don't hang waiting
    for input that isn't there.

    Ctrl-C / Ctrl-D raise typer.Abort just like every other wrapper in this
    module — questionary returns None on interrupt, and we must NOT silently
    advance to the next step (the original bug: Ctrl-C between OAuth
    walkthrough steps was eaten, marching the user forward instead of
    aborting).
    """
    if not _is_tty():
        return
    import questionary

    try:
        prompt = questionary.press_any_key_to_continue(message, style=_style())
    except AttributeError:
        # Defensive: older questionary versions ship the helper under a
        # different name; fall back to a plain confirm.
        prompt = questionary.confirm(message, default=True, style=_style())
    if prompt.ask() is None:
        raise typer.Abort()


def offer_to_open(
    url: str,
    *,
    prompt: str = "Open the signup page in your browser?",
    default: bool = True,
) -> bool:
    """Ask before opening a URL. Returns True if the browser was launched.

    Use after the step's info block has already printed the URL — this is
    the action prompt, not the explanation.

    Skipped silently on non-TTY (CI / scripted setup): pointless to open a
    browser there and we don't want to consume a scripted stdin line.
    """
    if not _is_tty():
        return False
    if confirm(prompt, default=default):
        webbrowser.open(url)
        return True
    return False
