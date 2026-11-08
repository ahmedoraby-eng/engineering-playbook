"""The wave's merge order: `.github/merge-train.json`, written once per wave (ADR-020 E1).

    {"wave": "0.2.2-w1", "order": [226, 227, 230]}

`order` lists pull request numbers, dependencies first. The file is read from the default branch, so a
pull request cannot change the order it is itself judged by: changing it is a reviewed change.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


class ConfigError(ValueError):
    """The order file is not usable. The train stops and goes red rather than guess."""


@dataclass(frozen=True)
class Config:
    wave: str
    order: tuple[int, ...]


def parse(text: str) -> Config:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"merge-train.json is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("merge-train.json must be a JSON object with an 'order' list")
    order = data.get("order")
    if not isinstance(order, list):
        raise ConfigError("merge-train.json needs an 'order' list of pull request numbers")
    numbers: list[int] = []
    for item in order:
        # bool is an int subclass; true is not a pull request number.
        if not isinstance(item, int) or isinstance(item, bool) or item <= 0:
            raise ConfigError(f"'order' holds {item!r}; every entry must be a positive pull request number")
        numbers.append(item)
    duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
    if duplicates:
        raise ConfigError(f"'order' lists {', '.join('#' + str(n) for n in duplicates)} more than once")
    wave = data.get("wave", "")
    if not isinstance(wave, str):
        raise ConfigError("'wave' must be a string")
    return Config(wave=wave.strip(), order=tuple(numbers))
