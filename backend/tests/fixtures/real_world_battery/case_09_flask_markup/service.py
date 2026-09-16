"""flask.Markup was a re-export of markupsafe.Markup, removed from the
flask namespace entirely in Flask 2.3 -- import from markupsafe directly."""

from flask import Markup


def bold(text: str) -> Markup:
    return Markup(f"<b>{text}</b>")
