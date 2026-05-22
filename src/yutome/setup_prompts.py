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
from typing import Iterable

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


def select(message: str, choices: list[str], *, default: str | None = None) -> str:
    if not _is_tty():
        # Render as a numbered list and accept a number or exact label.
        typer.echo(message)
        for index, choice in enumerate(choices, 1):
            typer.echo(f"  {index}. {choice}")
        default_index = (choices.index(default) + 1) if default in choices else 1
        while True:
            raw = typer.prompt(
                "Choose by number or label", default=str(default_index)
            ).strip()
            if raw.isdigit() and 1 <= int(raw) <= len(choices):
                return choices[int(raw) - 1]
            if raw in choices:
                return raw
            typer.echo("[WARN] Invalid choice; try again.")
    import questionary

    return _ask(
        questionary.select(message, choices=choices, default=default, style=_style())
    )


def checkbox(
    message: str,
    choices: list[str],
    *,
    defaults: Iterable[str] = (),
    instruction: str | None = None,
    use_search_filter: bool = False,
    erase_when_done: bool = False,
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
    return _ask(
        questionary.checkbox(**checkbox_kwargs)
    )


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
