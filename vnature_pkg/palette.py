"""Палитра и цветовая математика: сила kitty, kitty_colors, matugen."""

from __future__ import annotations

import colorsys
import subprocess

from .common import (
    TEMPLATES,
    CONFIG_DIR,
    MATUGEN_DIR,
    VNatureError,
    SINK,
    toml_string,
    expand,
    require,
)


# Режимы kitty - единственный список: подпись и точка на шкале силы 0–100.
# None - off: чистая база (kitty_base или STABLE), палитра не подмешивается.
# В config/preset/state kitty_mode - имя отсюда или целое 0–100.
KITTY_MODES = {
    "accent": (
        "Accent - минимум изменений",
        15,
    ),
    "balanced": (
        "Balanced - адаптивно",
        50,
    ),
    "full": (
        "Full - ебашим",
        100,
    ),
    "off": (
        "Off - чистая база, без палитры",
        None,
    ),
}

# Сила -> (surface, ansi), кусочно-линейно между точками.
# surface: насколько фон и текст уходят от базы к палитре;
# ansi: насколько яркость и насыщенность ANSI уходят к палитре.
# Точки 15/50/100 - прежние accent/balanced/full, их вывод не меняется.
KITTY_CURVE = (
    (0, 0.0, 0.0),
    (15, 0.06, 0.0),
    (50, 0.35, 0.4),
    (100, 1.0, 0.8),
)


def effective_kitty_mode(
    preset,
    cfg,
):
    mode = preset.get(
        "kitty_mode",
        "inherit",
    )

    if mode == "inherit":
        return cfg["kitty_mode"]

    return mode


def kitty_strength(value):
    # Имя режима, целое 0–100 или строка "35"/"35%" -> 0–100 или "off".
    # None - значение непонятное.
    if isinstance(value, bool):
        return None

    if isinstance(value, str):
        if value in KITTY_MODES:
            point = KITTY_MODES[value][1]
            return "off" if point is None else point

        value = value.strip().removesuffix("%")

        if not value.isdigit():
            return None

        value = int(value)

    if isinstance(value, int) and 0 <= value <= 100:
        return value

    return None


def kitty_mode_value(strength):
    # Для хранения: точка шкалы -> её имя, иначе само число; "off" как есть.
    for name, (_, point) in KITTY_MODES.items():
        if point is not None and point == strength:
            return name

    return strength


def normalize_kitty_mode(value, default):
    strength = kitty_strength(value)

    if strength is None:
        return default

    return kitty_mode_value(strength)


def kitty_params(strength):
    # -> (surface, ansi) или None для off.
    if strength == "off":
        return None

    for point, surface, ansi in KITTY_CURVE:
        if strength == point:
            return surface, ansi

    for (low, s1, a1), (high, s2, a2) in zip(KITTY_CURVE, KITTY_CURVE[1:]):
        if low < strength < high:
            t = (strength - low) / (high - low)
            return s1 + (s2 - s1) * t, a1 + (a2 - a1) * t

    raise VNatureError(
        f"сила kitty вне шкалы: {strength}"
    )


def kitty_label(mode):
    # "inherit", "off", "50% (balanced)" или "35%"
    if mode == "inherit":
        return "inherit"

    strength = kitty_strength(mode)

    if strength in (None, "off"):
        return str(mode)

    name = kitty_mode_value(strength)

    return (
        f"{strength}% ({name})"
        if name != strength
        else f"{strength}%"
    )


# ============================================================
# Palette
# ============================================================

def read_palette(path):
    result = {}

    for line in path.read_text(
        encoding="utf-8"
    ).splitlines():

        line = line.strip()

        if (
            not line
            or line.startswith("#")
            or "=" not in line
        ):
            continue

        key, value = (
            line.split("=", 1)
        )

        result[
            key.strip()
        ] = value.strip()

    return result


def hex_rgb(value):
    value = value.lstrip("#")

    return tuple(
        int(
            value[position:position + 2],
            16,
        )
        for position
        in (0, 2, 4)
    )


def rgb_hex(rgb):
    return (
        "#"
        + "".join(
            f"{max(0, min(255, round(v))):02x}"
            for v in rgb
        )
    )


def mix(a, b, amount):
    left = hex_rgb(a)
    right = hex_rgb(b)

    return rgb_hex(
        tuple(
            x * (1 - amount)
            + y * amount
            for x, y
            in zip(
                left,
                right,
            )
        )
    )


# ============================================================
# Kitty modes
# ============================================================

# База для kitty, если kitty_base в config.toml не задан.
STABLE = {
    "background": "#1d1d1d",
    "foreground": "#f7f6ec",

    "inactive_tab_background": "#171717",
    "inactive_tab_foreground": "#b1b5ae",

    "color0": "#343835",
    "color8": "#585a58",

    "color1": "#ce3e60",
    "color9": "#d18ea6",

    "color2": "#7bb75b",
    "color10": "#767e2b",

    "color3": "#e8b32a",
    "color11": "#77592e",

    "color4": "#4c99d3",
    "color12": "#135879",

    "color5": "#a57fc4",
    "color13": "#5f4190",

    "color6": "#389aac",
    "color14": "#76bbca",

    "color7": "#f9faf6",
    "color15": "#b1b5ae",
}

# Цветные ANSI: из роли палитры берём только яркость и насыщенность.
ANSI_ROLES = {
    "color1": "error",
    "color9": "error",
    "color2": "tertiary",
    "color10": "tertiary",
    "color3": "tertiary",
    "color11": "tertiary",
    "color4": "secondary",
    "color12": "secondary",
    "color5": "primary",
    "color13": "primary",
    "color6": "secondary",
    "color14": "secondary",
}

# Нейтральные ANSI смешиваются с палитрой целиком.
NEUTRAL_ROLES = {
    "color0": "surface_container_high",
    "color8": "outline",
    "color7": "foreground",
    "color15": "foreground_muted",
}

TEXT_CONTRAST = 4.5
COLOR0_CONTRAST = 1.25


def luminance(value):
    def channel(c):
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = map(channel, hex_rgb(value))

    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    # WCAG 2.x contrast ratio
    light, dark = sorted(
        (luminance(a), luminance(b)),
        reverse=True,
    )

    return (light + 0.05) / (dark + 0.05)


def to_hls(value):
    return colorsys.rgb_to_hls(
        *(c / 255 for c in hex_rgb(value))
    )


def from_hls(h, l, s):
    return rgb_hex(
        c * 255
        for c in colorsys.hls_to_rgb(h, l, s)
    )


def ensure_contrast(color, against, ratio):
    # Двигаем только яркость, оттенок сохраняется.
    if contrast(color, against) >= ratio:
        return color

    h, l, s = to_hls(color)

    for step in range(1, 101):
        for sign in (1, -1):
            candidate = from_hls(
                h,
                min(1.0, max(0.0, l + sign * step / 100)),
                s,
            )

            if contrast(candidate, against) >= ratio:
                return candidate

    return max(
        ("#ffffff", "#000000"),
        key=lambda c: contrast(c, against),
    )


def shift_ansi(base, target, amount):
    # Оттенок от базы; яркость и насыщенность - к роли палитры.
    # Цель зажата, чтобы цвет не выцвел в серый и не ушёл в чёрный/белый.
    h, l, s = to_hls(base)
    _, target_l, target_s = to_hls(target)

    target_l = min(0.80, max(0.35, target_l))
    target_s = max(0.35, target_s)

    return from_hls(
        h,
        l + (target_l - l) * amount,
        s + (target_s - s) * amount,
    )


def kitty_base(cfg):
    raw = cfg.get("kitty_base")

    if not raw:
        return STABLE

    path = expand(raw)

    if not path.is_absolute():
        path = CONFIG_DIR / path

    if not path.is_file():
        raise VNatureError(
            f"нет kitty_base: {path}"
        )

    base = dict(STABLE)

    for line in path.read_text(
        encoding="utf-8"
    ).splitlines():

        parts = line.split()

        if len(parts) < 2 or parts[0] not in STABLE:
            continue

        value = parts[1].lower()

        if not (
            len(value) == 7
            and value.startswith("#")
            and all(c in "0123456789abcdef" for c in value[1:])
        ):
            raise VNatureError(
                f"kitty_base {path}: {parts[0]} {parts[1]} "
                "- нужен цвет вида #rrggbb"
            )

        base[parts[0]] = value

    return base


def kitty_colors(
    palette,
    mode,
    base=STABLE,
):
    strength = kitty_strength(mode)

    if strength is None:
        raise VNatureError(
            f"неизвестный kitty_mode: {mode}"
        )

    params = kitty_params(strength)

    if params is None:
        # Ни одного цвета из палитры: только база.
        return {
            "kitty_background": base["background"],
            "kitty_foreground": base["foreground"],

            "kitty_cursor": base["foreground"],
            "kitty_selection_bg": base["color8"],
            "kitty_selection_fg": base["foreground"],

            "kitty_active_tab_fg": base["foreground"],
            "kitty_active_tab_bg": base["color8"],
            "kitty_inactive_tab_fg": base["inactive_tab_foreground"],
            "kitty_inactive_tab_bg": base["inactive_tab_background"],

            "kitty_url": base["color4"],
            "kitty_active_border": base["color8"],

            **{
                f"kitty_color{n}": base[f"color{n}"]
                for n in range(16)
            },
        }

    surface, ansi = params

    bg = mix(
        base["background"],
        palette["background"],
        surface,
    )

    fg = ensure_contrast(
        mix(
            base["foreground"],
            palette["foreground"],
            surface,
        ),
        bg,
        TEXT_CONTRAST,
    )

    accent_fg = ensure_contrast(
        fg,
        palette["primary_container"],
        TEXT_CONTRAST,
    )

    result = {
        "kitty_background": bg,
        "kitty_foreground": fg,

        "kitty_cursor": palette["primary"],
        "kitty_selection_bg": palette["primary_container"],
        "kitty_selection_fg": accent_fg,

        "kitty_active_tab_fg": accent_fg,
        "kitty_active_tab_bg": palette["primary_container"],

        "kitty_inactive_tab_fg": mix(
            base["inactive_tab_foreground"],
            palette["foreground_muted"],
            surface,
        ),

        "kitty_inactive_tab_bg": mix(
            base["inactive_tab_background"],
            palette["surface_container_low"],
            surface,
        ),

        "kitty_url": palette["primary"],
        "kitty_active_border": palette["primary"],
    }

    for key, role in ANSI_ROLES.items():
        result["kitty_" + key] = shift_ansi(
            base[key],
            palette[role],
            ansi,
        )

    for key, role in NEUTRAL_ROLES.items():
        result["kitty_" + key] = mix(
            base[key],
            palette[role],
            ansi,
        )

    # color0 часто служит фоном панелей - не должен слиться с фоном.
    result["kitty_color0"] = ensure_contrast(
        result["kitty_color0"],
        bg,
        COLOR0_CONTRAST,
    )

    return result


# ============================================================
# Matugen
# ============================================================

def prepare_matugen(directory):
    require("matugen")

    palette_template = (
        TEMPLATES
        / "palette.conf"
    )

    (directory / "config.toml").write_text(
        "[config.wallpaper]\n"
        'command = "feh"\n'
        'arguments = ["--bg-fill"]\n'
        "set = false\n\n"
        "[templates.palette]\n"
        "input_path = "
        + toml_string(
            palette_template
        )
        + "\n"
        "output_path = "
        + toml_string(
            directory / "palette.conf"
        )
        + "\n",
        encoding="utf-8",
    )


def generate_palette(
    image,
    cfg,
    poh=False,
    directory=MATUGEN_DIR,
):
    prepare_matugen(directory)

    palette = directory / "palette.conf"

    palette.unlink(
        missing_ok=True
    )

    command = [
        "matugen",
        "-c",
        str(directory / "config.toml"),

        "image",
        str(image),

        "-m",
        cfg["mode"],
    ]

    if poh:
        command += [
            "--source-color-index",
            "0",
        ]

    # В TUI без вопросов matugen молчит: его вывод испортил бы экран curses.
    quiet = poh and SINK is not None

    result = subprocess.run(
        command,
        capture_output=quiet,
        text=True,
    )

    if result.returncode != 0:
        if quiet:
            raise VNatureError(
                f"matugen упал с кодом {result.returncode}: "
                + (result.stderr or result.stdout).strip()[-300:]
            )

        raise subprocess.CalledProcessError(
            result.returncode,
            command,
        )

    if not palette.is_file():
        raise VNatureError(
            "Matugen не создал palette"
        )

    return palette
