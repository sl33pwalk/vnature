"""Обои: список картинок, пути, hyprpaper/feh."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from .common import (
    IMAGE_EXTENSIONS,
    VNatureError,
    require,
)


def wallpaper_images(directory):
    if not directory.is_dir():
        raise VNatureError(
            f"нет папки обоев: {directory} "
            "(wallpaper_dir в config.toml или --wallpaper-dir)"
        )

    images = sorted(
        path
        for path in directory.iterdir()
        if (
            path.is_file()
            and
            path.suffix.lower()
            in IMAGE_EXTENSIONS
        )
    )

    if not images:
        raise VNatureError(
            f"в папке обоев нет картинок: {directory} "
            "(" + ", ".join(sorted(IMAGE_EXTENSIONS)) + ")"
        )

    return images


# ============================================================
# Wallpaper
# ============================================================

def resolve_wallpaper(
    reference,
    cfg,
):
    path = Path(
        os.path.expandvars(
            os.path.expanduser(
                reference
            )
        )
    )

    if not path.is_absolute():
        path = (
            cfg["wallpaper_dir"]
            / path
        )

    return path.resolve()


def portable_wallpaper(
    path,
    cfg,
):
    path = path.resolve()
    library = (
        cfg["wallpaper_dir"]
        .resolve()
    )

    try:
        return str(
            path.relative_to(
                library
            )
        )
    except ValueError:
        return str(path)


def in_hyprland():
    return bool(
        os.environ.get(
            "HYPRLAND_INSTANCE_SIGNATURE"
        )
    )


def ensure_hyprpaper():
    require("hyprctl")
    require("hyprpaper")

    probe = subprocess.run(
        [
            "hyprctl",
            "hyprpaper",
            "listactive",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    if probe.returncode == 0:
        return

    subprocess.Popen(
        ["hyprpaper"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    for _ in range(20):
        time.sleep(0.1)

        probe = subprocess.run(
            [
                "hyprctl",
                "hyprpaper",
                "listactive",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        if probe.returncode == 0:
            return

    raise VNatureError(
        "hyprpaper не поднялся"
    )


def set_hyprland_wallpaper(path):
    ensure_hyprpaper()

    try:
        raw = subprocess.check_output(
            [
                "hyprctl",
                "-j",
                "monitors",
            ],
            text=True,
        )

        monitors = json.loads(raw)

    except (
        subprocess.CalledProcessError,
        json.JSONDecodeError,
    ) as error:
        raise VNatureError(
            f"не могу получить мониторы Hyprland: {error}"
        )

    if not monitors:
        raise VNatureError(
            "Hyprland не вернул мониторы"
        )

    # Явно ставим один wallpaper на каждый подключённый
    # монитор. Так надёжнее fallback wallpaper.
    for monitor in monitors:
        name = monitor.get("name")

        if not name:
            continue

        result = subprocess.run(
            [
                "hyprctl",
                "hyprpaper",
                "wallpaper",
                f"{name}, {path}, cover",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

        if result.returncode != 0:
            raise VNatureError(
                "hyprpaper не поставил обои "
                f"на {name}: "
                f"{result.stderr.strip()}"
            )


def set_wallpaper(path):
    path = Path(path).resolve()

    if in_hyprland():
        set_hyprland_wallpaper(
            path
        )

        persist_helper = (
            Path.home()
            / ".local/bin/hyprpaper-save-current"
        )

        if persist_helper.is_file():
            result = subprocess.run(
                [
                    str(persist_helper),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )

            if result.returncode != 0:
                raise VNatureError(
                    "обои применены, но не удалось "
                    "сохранить их для следующего запуска: "
                    f"{result.stderr.strip()}"
                )

        return

    # Старый добрый backend для i3/X11.
    require("feh")

    subprocess.run(
        [
            "feh",
            "--bg-fill",
            str(path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
