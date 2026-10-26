"""config.toml: чтение, значения по умолчанию, запись ключей."""

from __future__ import annotations

import tomllib

from .common import (
    DRY_RUN,
    CONFIG_FILE,
    VNatureError,
    info,
    toml_value,
    expand,
)
from .palette import (
    normalize_kitty_mode,
)


# ============================================================
# Config
# ============================================================

def ensure_config():
    if CONFIG_FILE.exists():
        return

    CONFIG_FILE.write_text(
        'wallpaper_dir = "~/Pictures/wp"\n'
        'mode = "dark"\n'
        'kitty_mode = "accent"\n',
        encoding="utf-8",
    )


def load_config(
    wallpaper_dir=None,
):
    # Папка обоев: --wallpaper-dir > wallpaper_dir из config.toml > ~/Pictures/wp
    ensure_config()

    try:
        with CONFIG_FILE.open("rb") as f:
            raw = tomllib.load(f)
    except Exception as error:
        raise VNatureError(
            f"не могу прочитать config.toml: {error}"
        )

    wallpaper_dir = expand(
        wallpaper_dir
        if wallpaper_dir is not None
        else raw.get(
            "wallpaper_dir",
            "~/Pictures/wp",
        )
    )

    kitty_mode = normalize_kitty_mode(
        raw.get(
            "kitty_mode",
            "accent",
        ),
        "accent",
    )

    return {
        "wallpaper_dir": wallpaper_dir,
        "mode": str(raw.get("mode", "dark")),
        "kitty_mode": kitty_mode,
        "kitty_base": raw.get("kitty_base"),
    }


def set_config_key(key, value):
    # Меняем одну строку верхнего уровня; комментарии и чужие ключи не трогаем.
    if DRY_RUN:
        info(
            f"dry-run: записал бы {key} = "
            f"{toml_value(value)} в {CONFIG_FILE}"
        )
        return

    ensure_config()

    lines = CONFIG_FILE.read_text(
        encoding="utf-8"
    ).splitlines()

    line = f"{key} = {toml_value(value)}"

    # Ключи верхнего уровня кончаются на первой [таблице].
    end = next(
        (
            position
            for position, text in enumerate(lines)
            if text.lstrip().startswith("[")
        ),
        len(lines),
    )

    for position in range(end):
        name = lines[position].split("=", 1)[0].strip()

        if (
            "=" in lines[position]
            and name == key
        ):
            lines[position] = line
            break

    else:
        lines.insert(
            end,
            line,
        )

    text = "\n".join(lines) + "\n"

    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise VNatureError(
            f"не могу обновить config.toml: {error}"
        )

    temporary = (
        CONFIG_FILE.parent
        / ".config.toml.tmp"
    )

    temporary.write_text(
        text,
        encoding="utf-8",
    )

    temporary.replace(
        CONFIG_FILE
    )
