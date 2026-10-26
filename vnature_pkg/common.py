"""Пути, общие ошибки и сообщения, мелкие помощники."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path


# ============================================================
# Paths
# ============================================================

VERSION = "0.4.0"

# --dry-run: всё рендерится и проверяется, но конфиги, обои,
# state и пресеты не меняются, перезагрузок нет.
DRY_RUN = False

# Клон проекта (рядом с исполняемым vnature); шаблоны - внутри пакета.
ROOT = Path(__file__).resolve().parent.parent

# Пресеты пользователя: $XDG_DATA_HOME/vnature/presets, по умолчанию ~/.local/share.
# Относительный XDG_DATA_HOME по спецификации игнорируется.
DATA_DIR = (
    Path(os.environ["XDG_DATA_HOME"])
    if os.path.isabs(os.environ.get("XDG_DATA_HOME", ""))
    else Path.home() / ".local/share"
) / "vnature"

PRESETS = DATA_DIR / "presets"

# Старое место: presets/ в клоне. Отсюда только предлагаем перенос (vnature migrate).
LEGACY_PRESETS = ROOT / "presets"
TEMPLATES = Path(__file__).resolve().parent / "templates"

CONFIG_DIR = Path.home() / ".config/vnature"
CONFIG_FILE = CONFIG_DIR / "config.toml"

STATE_DIR = Path.home() / ".local/state/vnature"
CURRENT = STATE_DIR / "current"
PREVIOUS = STATE_DIR / "previous"

CACHE = Path.home() / ".cache/vnature"
MATUGEN_DIR = CACHE / "matugen"
GENERATED = CACHE / "generated"

POLYBAR_GENERATED = GENERATED / "polybar.ini"
ROFI_GENERATED = GENERATED / "rofi.rasi"

HYPRLAND_THEME = Path.home() / ".config/hypr/vnature.lua"
SWAYNC_THEME = Path.home() / ".config/swaync/vnature.css"
WAYBAR_THEME = Path.home() / ".config/waybar/vnature.css"
OBSIDIAN_GENERATED = GENERATED / "obsidian.css"

KITTY_THEME = Path.home() / ".config/kitty/vnature.conf"

POLYBAR_LAUNCH = Path.home() / ".config/polybar/launch.sh"


IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
    ".jxl",
}


# ============================================================
# Common
# ============================================================

class VNatureError(Exception):
    pass


# Пока открыт TUI, сообщения уходят в него, а не в терминал под curses.
SINK = None


def info(text):
    if SINK is not None:
        SINK(str(text), False)
    else:
        print(f"vnature: {text}")


def warn(text):
    if SINK is not None:
        SINK(str(text), True)
    else:
        print(f"vnature: {text}", file=sys.stderr)


def toml_string(value):
    return json.dumps(str(value), ensure_ascii=False)


def toml_value(value):
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)

    return toml_string(value)


def expand(value):
    return Path(
        os.path.expandvars(
            os.path.expanduser(
                str(value)
            )
        )
    )


def ensure_dirs():
    for path in (
        PRESETS,
        CONFIG_DIR,
        STATE_DIR,
        CACHE,
        MATUGEN_DIR,
        GENERATED,
    ):
        path.mkdir(
            parents=True,
            exist_ok=True,
        )


def require(command):
    if shutil.which(command) is None:
        raise VNatureError(
            f"не найдена команда: {command}"
        )


def set_global(name, value):
    # Модули берут общие имена через from-импорт, поэтому меняем во всех, где оно есть.
    for module in list(sys.modules.values()):
        if getattr(module, "__name__", "").split(".")[0] == __package__ and name in vars(module):
            vars(module)[name] = value
