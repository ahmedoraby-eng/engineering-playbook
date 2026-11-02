"""Main guard: a detective check that flags any update to the main branch by an unexpected actor."""

from .guard import Push, check

__all__ = ["Push", "check"]
