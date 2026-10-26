"""Шаблоны -> stage -> установка конфигов приложений."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from .common import (
    DRY_RUN,
    TEMPLATES,
    CURRENT,
    CACHE,
    POLYBAR_GENERATED,
    ROFI_GENERATED,
    HYPRLAND_THEME,
    SWAYNC_THEME,
    WAYBAR_THEME,
    OBSIDIAN_GENERATED,
    KITTY_THEME,
    VNatureError,
    info,
)
from .palette import (
    read_palette,
    STABLE,
    kitty_base,
    kitty_colors,
)
from .wallpaper import (
    set_wallpaper,
)
from .state import (
    read_state,
    write_state,
    remember_current,
)
from .apps import (
    obsidian_vaults,
    sync_obsidian_theme,
    reload_polybar,
    reload_hyprland,
    reload_swaync,
    reload_kitty,
)


# ============================================================
# Templates
# ============================================================

def render_template(
    template,
    values,
):
    if not template.is_file():
        return None

    text = template.read_text(
        encoding="utf-8"
    )

    for key, value in (
        values.items()
    ):
        text = text.replace(
            "{{"
            + key
            + "}}",
            value,
        )

    if (
        "{{" in text
        or "}}" in text
    ):
        raise VNatureError(
            f"в шаблоне "
            f"{template.name} "
            f"остались переменные"
        )

    return text


def render_apps(
    palette_file,
    kitty_mode,
    stage,
    base=STABLE,
):
    # Только рендер и проверка в stage/rendered, реальные файлы не трогаем.
    palette = read_palette(
        palette_file
    )

    missing = sorted(
        set(
            read_palette(
                TEMPLATES
                / "palette.conf"
            )
        )
        - set(palette)
    )

    if missing:
        raise VNatureError(
            f"в палитре {palette_file} "
            f"нет ключей: "
            + ", ".join(missing)
        )

    hypr = dict(
        palette
    )

    hypr["hypr_primary"] = (
        palette["primary"]
        .lstrip("#")
    )

    hypr["hypr_secondary"] = (
        palette["secondary"]
        .lstrip("#")
    )

    hypr["hypr_outline"] = (
        palette["outline"]
        .lstrip("#")
    )

    kitty = kitty_colors(
        palette,
        kitty_mode,
        base,
    )

    # шаблон, куда, папка приложения (None - пишем в кэш всегда), значения
    jobs = [
        ("polybar.ini", POLYBAR_GENERATED, None, palette),
        ("rofi.rasi", ROFI_GENERATED, None, palette),
        ("hyprland.lua", HYPRLAND_THEME, HYPRLAND_THEME.parent, hypr),
        ("swaync.css", SWAYNC_THEME, SWAYNC_THEME.parent, palette),
        ("waybar.css", WAYBAR_THEME, WAYBAR_THEME.parent, palette),
        ("obsidian.css", OBSIDIAN_GENERATED, None, palette),
        ("kitty.conf", KITTY_THEME, KITTY_THEME.parent, {**palette, **kitty}),
    ]

    rendered_dir = (
        stage
        / "rendered"
    )

    rendered_dir.mkdir(
        exist_ok=True,
    )

    rendered = {}

    for template_name, output, app_dir, values in jobs:

        text = render_template(
            TEMPLATES
            / template_name,

            values,
        )

        if text is None:
            continue

        staged = (
            rendered_dir
            / template_name
        )

        staged.write_text(
            text,
            encoding="utf-8",
        )

        rendered[template_name] = (
            staged,
            output,
            app_dir,
        )

    return rendered


def install_apps(rendered):
    installed = set()

    for template_name, (
        staged,
        output,
        app_dir,
    ) in rendered.items():

        if (
            app_dir is not None
            and not app_dir.is_dir()
        ):
            info(
                f"{app_dir.name}: нет "
                f"{app_dir}, пропускаю"
            )
            continue

        output.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        temporary = (
            output.parent
            / (
                "."
                + output.name
                + ".tmp"
            )
        )

        shutil.copyfile(
            staged,
            temporary,
        )

        temporary.replace(
            output
        )

        installed.add(
            template_name
        )

    if "obsidian.css" in installed:
        sync_obsidian_theme(
            OBSIDIAN_GENERATED
        )

    return (
        "polybar.ini" in installed,
        "kitty.conf" in installed,
    )


# ============================================================
# Applying
# ============================================================

def build_stage(
    palette,
    *,
    name,
    wallpaper,
    kitty_mode,
):
    stage = Path(
        tempfile.mkdtemp(
            prefix="stage-",
            # dry-run не пишет даже во временные папки ~/.cache/vnature
            dir=None if DRY_RUN else CACHE,
        )
    )

    try:
        shutil.copy2(
            palette,
            stage / "palette.conf",
        )

        write_state(
            stage,
            name=name,
            wallpaper=wallpaper,
            kitty_mode=kitty_mode,
        )

    except BaseException:
        shutil.rmtree(
            stage,
            ignore_errors=True,
        )
        raise

    return stage


def dry_run_report(
    rendered,
    wallpaper,
):
    for staged, output, app_dir in (
        rendered.values()
    ):
        skip = (
            app_dir is not None
            and not app_dir.is_dir()
        )

        info(
            "dry-run: "
            + (
                "пропустил бы "
                if skip
                else "записал бы "
            )
            + str(output)
        )

    if "obsidian.css" in rendered:
        info(
            "dry-run: Obsidian vault'ы: "
            + (
                ", ".join(
                    map(str, obsidian_vaults())
                )
                or "нет"
            )
        )

    if wallpaper:
        info(
            f"dry-run: обои {wallpaper}"
        )

    info(
        "dry-run: ничего не записано"
    )


def commit_stage(stage, cfg):
    try:
        palette = (
            stage
            / "palette.conf"
        )

        state = read_state(
            stage
        )

        wallpaper = (
            Path(state["wallpaper"])
            if state.get("wallpaper")
            else None
        )

        if (
            wallpaper
            and not wallpaper.is_file()
        ):
            raise VNatureError(
                f"обои исчезли: "
                f"{wallpaper}"
            )

        rendered = render_apps(
            palette,
            state["kitty_mode"],
            stage,
            kitty_base(cfg),
        )

        if DRY_RUN:
            dry_run_report(
                rendered,
                wallpaper,
            )
            return

        # Обои до записи конфигов: если hyprpaper упадёт, конфиги целы.
        if wallpaper:
            set_wallpaper(
                wallpaper
            )

        (
            polybar_changed,
            kitty_changed,
        ) = install_apps(
            rendered
        )

        shutil.rmtree(
            stage
            / "rendered"
        )

        remember_current()

        if CURRENT.exists():
            shutil.rmtree(
                CURRENT
            )

        shutil.move(
            str(stage),
            str(CURRENT),
        )

    finally:
        if stage.exists():
            shutil.rmtree(
                stage,
                ignore_errors=True,
            )

    if polybar_changed:
        reload_polybar()

    if kitty_changed:
        reload_kitty()

    reload_hyprland()
    reload_swaync()
