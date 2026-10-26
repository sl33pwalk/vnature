"""state current/previous."""

from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

from .common import (
    CURRENT,
    PREVIOUS,
    toml_string,
    toml_value,
)
from .palette import (
    normalize_kitty_mode,
)


# ============================================================
# State
# ============================================================

def read_state(directory):
    meta = (
        directory
        / "state.toml"
    )

    if meta.is_file():
        with meta.open("rb") as f:
            raw = tomllib.load(f)

        return {
            "name": str(
                raw.get(
                    "name",
                    "unknown",
                )
            ),
            "wallpaper": (
                str(raw["wallpaper"])
                if raw.get("wallpaper")
                else None
            ),
            "kitty_mode": normalize_kitty_mode(
                raw.get(
                    "kitty_mode",
                    "accent",
                ),
                "accent",
            ),
        }

    # Совместимость с v0.1.
    if not directory.exists():
        return None

    old_name = directory / "name"
    old_wallpaper = (
        directory / "wallpaper"
    )

    return {
        "name": (
            old_name.read_text().strip()
            if old_name.is_file()
            else "unknown"
        ),
        "wallpaper": (
            old_wallpaper
            .read_text()
            .strip()
            if old_wallpaper.is_file()
            else None
        ),
        "kitty_mode": "accent",
    }


def write_state(
    directory,
    *,
    name,
    wallpaper,
    kitty_mode,
):
    lines = [
        "name = "
        + toml_string(name),

        "kitty_mode = "
        + toml_value(
            kitty_mode
        ),
    ]

    if wallpaper:
        lines.append(
            "wallpaper = "
            + toml_string(
                wallpaper.resolve()
            )
        )

    (
        directory
        / "state.toml"
    ).write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def current_wallpaper():
    state = read_state(CURRENT)

    if not state:
        return None

    wallpaper = state.get(
        "wallpaper"
    )

    if not wallpaper:
        return None

    return Path(wallpaper)


def remember_current():
    if PREVIOUS.exists():
        shutil.rmtree(
            PREVIOUS
        )

    if CURRENT.exists():
        shutil.copytree(
            CURRENT,
            PREVIOUS,
        )
