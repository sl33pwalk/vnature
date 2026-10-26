"""Команды: apply, image, save, delete, undo, status, list."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from .common import (
    DRY_RUN,
    PRESETS,
    STATE_DIR,
    CURRENT,
    PREVIOUS,
    CACHE,
    MATUGEN_DIR,
    VNatureError,
    SINK,
    info,
    warn,
    expand,
)
from .palette import (
    effective_kitty_mode,
    normalize_kitty_mode,
    kitty_label,
    kitty_base,
    generate_palette,
)
from .presets import (
    preset_path,
    preset_exists,
    get_presets,
    remove_preset,
    load_preset,
    save_preset_meta,
)
from .wallpaper import (
    resolve_wallpaper,
    portable_wallpaper,
    set_wallpaper,
)
from .state import (
    read_state,
    write_state,
    current_wallpaper,
)
from .apps import (
    reload_polybar,
    reload_hyprland,
    reload_swaync,
    reload_kitty,
)
from .render import (
    render_apps,
    install_apps,
    build_stage,
    dry_run_report,
    commit_stage,
)


def apply_preset(
    name,
    cfg,
    *,
    with_wallpaper=False,
):
    preset = load_preset(
        name
    )

    palette = (
        preset_path(name)
        / "palette.conf"
    )

    wallpaper = (
        current_wallpaper()
    )

    if with_wallpaper:

        reference = preset.get(
            "wallpaper"
        )

        if not reference:
            info(
                f"у {name} нет "
                f"привязанных обоев"
            )

        else:
            candidate = (
                resolve_wallpaper(
                    reference,
                    cfg,
                )
            )

            if candidate.is_file():
                wallpaper = candidate

            else:
                warn(
                    "привязанные обои "
                    "не найдены: "
                    + str(candidate)
                )

    kitty_mode = (
        effective_kitty_mode(
            preset,
            cfg,
        )
    )

    stage = build_stage(
        palette,

        name=name,

        wallpaper=wallpaper,

        kitty_mode=kitty_mode,
    )

    commit_stage(
        stage,
        cfg,
    )

    if DRY_RUN:
        return

    info(
        f"применён пресет: "
        f"{name}"
    )


def current_kitty_target(
    state,
    cfg,
    preset_modes=None,
):
    # Какой kitty_mode должен быть у текущей темы при нынешних настройках.
    # None - темы нет или её пресет не найден: трогать нечего.
    # preset_modes: {имя: режим} поверх preset.toml (нужно для --dry-run).
    if state is None:
        return None

    name = state["name"]

    if name == "__temporary__":
        return cfg["kitty_mode"]

    if preset_modes and name in preset_modes:
        mode = preset_modes[name]

        if mode != "inherit":
            mode = normalize_kitty_mode(mode, None)

    elif preset_exists(name):
        mode = load_preset(name)["kitty_mode"]

    else:
        return None

    return effective_kitty_mode(
        {"kitty_mode": mode},
        cfg,
    )


def refresh_kitty(
    cfg,
    preset_modes=None,
    force=False,
):
    # После смены kitty_mode: перерендерить только kitty у текущей темы.
    # previous не трогаем, чтобы undo возвращал тему, а не режим kitty.
    state = (
        read_state(CURRENT)
        if (CURRENT / "palette.conf").is_file()
        else None
    )

    target = current_kitty_target(
        state,
        cfg,
        preset_modes,
    )

    if (
        target is None
        or (target == state["kitty_mode"] and not force)
    ):
        return "сохранено, применится при следующей теме"

    title = (
        "из картинки "
        + Path(state["wallpaper"] or "?").name
        if state["name"] == "__temporary__"
        else state["name"]
    )

    stage = Path(
        tempfile.mkdtemp(
            prefix="stage-",
            dir=None if DRY_RUN else CACHE,
        )
    )

    try:
        rendered = render_apps(
            CURRENT / "palette.conf",
            target,
            stage,
            kitty_base(cfg),
        )

        kitty_only = {
            "kitty.conf": rendered["kitty.conf"],
        }

        if DRY_RUN:
            dry_run_report(
                kitty_only,
                None,
            )
            return f"dry-run: применил бы kitty {target} к теме {title}"

        _, installed = install_apps(
            kitty_only
        )

    finally:
        shutil.rmtree(
            stage,
            ignore_errors=True,
        )

    write_state(
        CURRENT,
        name=state["name"],
        wallpaper=(
            Path(state["wallpaper"])
            if state.get("wallpaper")
            else None
        ),
        kitty_mode=target,
    )

    if not installed:
        return f"kitty {target} для темы {title}: kitty не найден, файл не записан"

    reload_kitty()

    return f"kitty {target} применён к теме {title}"


def apply_image(
    image_argument,
    cfg,
    *,
    poh=False,
):
    image = expand(
        image_argument
    ).resolve()

    if not image.is_file():
        raise VNatureError(
            f"нет картинки: {image}"
        )

    matugen_dir = MATUGEN_DIR

    if DRY_RUN:
        matugen_dir = Path(
            tempfile.mkdtemp(
                prefix="vnature-matugen-",
            )
        )

        info(
            "dry-run: matugen всё равно запускается, "
            f"его конфиг и палитра - во временной {matugen_dir}"
        )

    try:
        palette = generate_palette(
            image,
            cfg,
            poh,
            matugen_dir,
        )

        stage = build_stage(
            palette,

            name="__temporary__",

            wallpaper=image,

            kitty_mode=cfg[
                "kitty_mode"
            ],
        )

        commit_stage(
            stage,
            cfg,
        )

    finally:
        if DRY_RUN:
            shutil.rmtree(
                matugen_dir,
                ignore_errors=True,
            )

    if DRY_RUN:
        return

    info(
        f"поставил: {image.name}"
    )

    if SINK is not None:
        return

    print()
    print(
        "Чётко:"
    )
    print(
        "  vnature chotko NAME"
    )
    print(
        "  vnature chotko NAME --eblo"
    )

    print(
        "Говно:"
    )
    print(
        "  vnature govno"
    )


# ============================================================
# Save/delete
# ============================================================

def save_current(
    name,
    cfg,
    *,
    with_wallpaper=False,
    kitty_mode=None,
):
    if not name:
        raise VNatureError(
            "нужно имя"
        )

    palette = (
        CURRENT
        / "palette.conf"
    )

    if not palette.is_file():
        raise VNatureError(
            "текущей темы нет"
        )

    directory = (
        preset_path(name)
    )

    if DRY_RUN:
        info(
            f"dry-run: сохранил бы {directory}"
            + (
                " с текущими обоями"
                if with_wallpaper
                else ""
            )
        )
        return

    directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    shutil.copy2(
        palette,
        directory
        / "palette.conf",
    )

    try:
        old = load_preset(
            name
        )
    except VNatureError:
        old = {
            "wallpaper": None,
            "kitty_mode": "inherit",
        }

    wallpaper_ref = old.get(
        "wallpaper"
    )

    if with_wallpaper:

        wallpaper = (
            current_wallpaper()
        )

        if not wallpaper:
            raise VNatureError(
                "не знаю текущие обои"
            )

        wallpaper_ref = (
            portable_wallpaper(
                wallpaper,
                cfg,
            )
        )

    selected_mode = (
        kitty_mode
        if kitty_mode is not None
        else old.get(
            "kitty_mode",
            "inherit",
        )
    )

    save_preset_meta(
        name,

        wallpaper=wallpaper_ref,

        kitty_mode=selected_mode,
    )

    info(
        f"чётко. сохранено: "
        f"{name}"
    )


def delete_preset(name):
    preset_path(name)

    if not preset_exists(name):
        raise VNatureError(
            f"такого пресета нет: "
            f"{name}"
        )

    if DRY_RUN:
        info(
            f"dry-run: удалил бы {preset_path(name)}"
        )
        return

    answer = input(
        f'Удалить "{name}"? [y/N]: '
    ).strip().lower()

    if answer not in (
        "y",
        "yes",
        "д",
        "да",
    ):
        info("не трогаю")
        return

    remove_preset(
        name
    )

    info(
        f"нахуй: {name}"
    )


# ============================================================
# Undo
# ============================================================

def go_back(cfg):
    if not PREVIOUS.exists():
        raise VNatureError(
            "предыдущего говна нет"
        )

    state = read_state(
        PREVIOUS
    )

    palette = (
        PREVIOUS
        / "palette.conf"
    )

    wallpaper = (
        Path(state["wallpaper"])
        if state.get("wallpaper")
        else None
    )

    stage = Path(
        tempfile.mkdtemp(
            prefix="stage-",
            # dry-run не пишет даже во временные папки ~/.cache/vnature
            dir=None if DRY_RUN else CACHE,
        )
    )

    try:
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

        if (
            wallpaper
            and wallpaper.is_file()
        ):
            set_wallpaper(
                wallpaper
            )

        (
            polybar_changed,
            kitty_changed,
        ) = install_apps(
            rendered
        )

    finally:
        shutil.rmtree(
            stage,
            ignore_errors=True,
        )

    swap = (
        STATE_DIR
        / ".swap"
    )

    if swap.exists():
        shutil.rmtree(
            swap
        )

    if CURRENT.exists():
        CURRENT.rename(
            swap
        )

    PREVIOUS.rename(
        CURRENT
    )

    if swap.exists():
        swap.rename(
            PREVIOUS
        )

    if polybar_changed:
        reload_polybar()

    if kitty_changed:
        reload_kitty()

    reload_hyprland()
    reload_swaync()

    info(
        "вернул предыдущее говно"
    )


# ============================================================
# Info
# ============================================================

def status_lines():
    state = read_state(
        CURRENT
    )

    if not state:
        return [
            "Ничего не отслеживается."
        ]

    lines = [
        f"Тема: {state['name']}",
        f"Kitty: {kitty_label(state['kitty_mode'])}",
    ]

    if state.get(
        "wallpaper"
    ):
        lines.append(
            f"Обои: {state['wallpaper']}"
        )

    return lines


def show_current():
    for line in status_lines():
        print(line)


def show_presets():
    if not get_presets():
        info(
            f"пресетов нет: {PRESETS}"
        )

    for name in get_presets():

        preset = load_preset(
            name
        )

        eblo = (
            "eblo"
            if preset.get(
                "wallpaper"
            )
            else "-"
        )

        print(
            f"{name:20} "
            f"[{eblo:4}] "
            f"kitty="
            f"{kitty_label(preset['kitty_mode'])}"
        )
