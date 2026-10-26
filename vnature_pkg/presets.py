"""Хранилище пресетов: имена, пути, palette.conf и preset.toml."""

from __future__ import annotations

import shutil
import tomllib

from .common import (
    DRY_RUN,
    LEGACY_PRESETS,
    PRESETS,
    VNatureError,
    info,
    toml_string,
    toml_value,
)
from .palette import (
    kitty_strength,
    normalize_kitty_mode,
)


# ============================================================
# Presets
# ============================================================

def valid_preset_name(name):
    return bool(
        name
        and not name.startswith(".")
        and "/" not in name
        and "\\" not in name
        and ".." not in name
    )


def preset_path(name):
    if not valid_preset_name(name):
        raise VNatureError(
            f"плохое имя пресета: {name!r}"
        )

    return PRESETS / name


def preset_exists(name):
    return (
        valid_preset_name(name)
        and
        (
            preset_path(name)
            / "palette.conf"
        ).is_file()
    )


def get_presets(root=None):
    root = root or PRESETS

    if not root.exists():
        return []

    return sorted(
        directory.name
        for directory in root.iterdir()
        if (
            directory.is_dir()
            and valid_preset_name(directory.name)
            and
            (
                directory
                / "palette.conf"
            ).is_file()
        )
    )


def remove_preset(name):
    path = preset_path(name).resolve()

    # Симлинк или хитрое имя не должны увести rmtree за пределы presets/.
    if path.parent != PRESETS.resolve():
        raise VNatureError(
            f"пресет вне {PRESETS}: {path}"
        )

    if DRY_RUN:
        info(
            f"dry-run: удалил бы {path}"
        )
        return

    shutil.rmtree(path)


def load_preset(name):
    directory = preset_path(name)

    if not (
        directory
        / "palette.conf"
    ).is_file():
        raise VNatureError(
            f"такого пресета нет: {name}"
        )

    metadata = {
        "wallpaper": None,
        "kitty_mode": "inherit",
    }

    meta_file = (
        directory
        / "preset.toml"
    )

    if meta_file.is_file():
        try:
            with meta_file.open("rb") as f:
                raw = tomllib.load(f)
        except Exception as error:
            raise VNatureError(
                f"битый preset.toml "
                f"у {name}: {error}"
            )

        if raw.get("wallpaper"):
            metadata["wallpaper"] = str(
                raw["wallpaper"]
            )

        mode = raw.get(
            "kitty_mode",
            "inherit",
        )

        if mode != "inherit":
            metadata["kitty_mode"] = normalize_kitty_mode(
                mode,
                "inherit",
            )

    return metadata


def save_preset_meta(
    name,
    wallpaper=None,
    kitty_mode="inherit",
):
    if kitty_mode != "inherit":
        if kitty_strength(kitty_mode) is None:
            raise VNatureError(
                f"неизвестный kitty_mode: "
                f"{kitty_mode}"
            )

        kitty_mode = normalize_kitty_mode(
            kitty_mode,
            None,
        )

    directory = preset_path(name)

    lines = []

    if wallpaper:
        lines.append(
            "wallpaper = "
            + toml_string(wallpaper)
        )

    lines.append(
        "kitty_mode = "
        + toml_value(kitty_mode)
    )

    if DRY_RUN:
        info(
            f"dry-run: записал бы {directory / 'preset.toml'}: "
            + "; ".join(lines)
        )
        return

    (
        directory
        / "preset.toml"
    ).write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def legacy_presets():
    # Пресеты в старом месте, если в новом пусто: тогда предлагаем перенос.
    if LEGACY_PRESETS.resolve() == PRESETS.resolve() or get_presets():
        return []

    return get_presets(LEGACY_PRESETS)


def migrate_presets():
    # Копия из старого места; старые не трогаем, существующие не перезаписываем.
    names = get_presets(LEGACY_PRESETS)

    if not names:
        raise VNatureError(
            f"в старом месте пресетов нет: {LEGACY_PRESETS}"
        )

    for name in names:
        target = PRESETS / name

        if target.exists() or target.is_symlink():
            info(f"уже есть, пропускаю: {name}")
            continue

        if DRY_RUN:
            info(f"dry-run: скопировал бы {LEGACY_PRESETS / name} -> {target}")
            continue

        # Сначала во временную папку (точка - get_presets её не видит), потом rename:
        # оборванное копирование не оставит полупресет.
        partial = PRESETS / f".{name}.part"
        shutil.rmtree(partial, ignore_errors=True)

        try:
            shutil.copytree(LEGACY_PRESETS / name, partial, symlinks=True)
            partial.rename(target)
        except BaseException:
            shutil.rmtree(partial, ignore_errors=True)
            raise

        info(f"перенёс: {name}")

    if not DRY_RUN:
        info(f"старые пресеты остались в {LEGACY_PRESETS} - удалишь сам, когда убедишься")
