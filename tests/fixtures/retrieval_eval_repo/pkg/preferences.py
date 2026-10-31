"""Synthetic user preference operations."""


def lookup(name):
    """Look up a display preference by name."""
    return {"preference": name}


def save_theme_choice(choice):
    """Save a selected display theme."""
    return {"theme": choice}
