"""A person as the app shows them - Flask's ``user_management/services/profile.py``."""

from __future__ import annotations


def _normalized(value: str | None) -> str:
    return (value or "").strip()


def display_name(user) -> str:
    """"First Last", or the email when the account carries no name."""
    name = f"{_normalized(user.first_name)} {_normalized(user.last_name)}".strip()
    return name or _normalized(user.email) or _normalized(user.username)


def initials(user) -> str:
    """The avatar's letters - the first of each name, upper-cased; "?" with neither."""
    letters = (_normalized(user.first_name)[:1] + _normalized(user.last_name)[:1]).upper()
    return letters or "?"


def person_payload(user) -> dict:
    return {
        "id": str(user.id),
        "first_name": _normalized(user.first_name),
        "last_name": _normalized(user.last_name),
        "name": display_name(user),
        "initials": initials(user),
        "email": _normalized(user.email),
    }
