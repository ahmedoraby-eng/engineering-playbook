"""Merge train (ADR-020 E1): after a merge, bring only the next pull request in the wave's order up to date."""

from .config import Config, ConfigError, parse
from .train import Options, Report, run

__all__ = ["Config", "ConfigError", "Options", "Report", "parse", "run"]
