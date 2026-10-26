"""Побочные эффекты приложений: Obsidian vault'ы, перезагрузки kitty/hyprland/swaync/polybar."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
from pathlib import Path

from .common import (
    POLYBAR_LAUNCH,
    info,
    warn,
)


# ============================================================
# Render applications
# ============================================================

def obsidian_vaults():
    configs = [
        Path.home()
        / ".config"
        / "obsidian"
        / "obsidian.json",

        Path.home()
        / ".var"
        / "app"
        / "md.obsidian.Obsidian"
        / "config"
        / "obsidian"
        / "obsidian.json",
    ]

    result = []
    seen = set()

    for config in configs:

        if not config.is_file():
            continue

        try:
            raw = json.loads(
                config.read_text(
                    encoding="utf-8"
                )
            )
        except Exception as error:
            warn(
                "не могу прочитать "
                f"{config}: {error}"
            )
            continue

        vaults = raw.get(
            "vaults",
            {}
        )

        if not isinstance(
            vaults,
            dict,
        ):
            continue

        for entry in vaults.values():

            if not isinstance(
                entry,
                dict,
            ):
                continue

            raw_path = entry.get(
                "path"
            )

            if not raw_path:
                continue

            vault = Path(
                os.path.expandvars(
                    os.path.expanduser(
                        str(raw_path)
                    )
                )
            ).resolve()

            if not (
                vault.is_dir()
                and
                (vault / ".obsidian").is_dir()
            ):
                continue

            key = str(vault)

            if key in seen:
                continue

            seen.add(key)
            result.append(
                vault
            )

    return result


def sync_obsidian_theme(
    source,
):
    vaults = obsidian_vaults()

    if not vaults:
        warn(
            "Obsidian vault не найден"
        )
        return

    for vault in vaults:

        snippets = (
            vault
            / ".obsidian"
            / "snippets"
        )

        snippets.mkdir(
            parents=True,
            exist_ok=True,
        )

        target = (
            snippets
            / "vnature.css"
        )

        backup = (
            snippets
            / "vnature.css.before-vnature"
        )

        if (
            target.exists()
            and not backup.exists()
        ):
            shutil.copy2(
                target,
                backup,
            )

        temporary = (
            snippets
            / ".vnature.css.tmp"
        )

        shutil.copy2(
            source,
            temporary,
        )

        temporary.replace(
            target
        )

    info(
        "Obsidian: обновлено vault'ов: "
        + str(len(vaults))
    )


def reload_polybar():
    if (
        os.environ.get(
            "XDG_SESSION_TYPE"
        ) != "x11"
    ):
        return

    if not POLYBAR_LAUNCH.is_file():
        return

    subprocess.run(
        [
            "bash",
            str(POLYBAR_LAUNCH),
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def reload_hyprland():
    if not os.environ.get(
        "HYPRLAND_INSTANCE_SIGNATURE"
    ):
        return

    if shutil.which("hyprctl") is None:
        return

    subprocess.run(
        [
            "hyprctl",
            "reload",
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def reload_swaync():
    if shutil.which(
        "swaync-client"
    ) is None:
        return

    subprocess.run(
        [
            "swaync-client",
            "--reload-css",
            "--skip-wait",
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def reload_kitty(proc=Path("/proc")):
    # kitty >= 0.49 перечитывает kitty.conf вместе с include по SIGUSR1
    # (man kitty.conf). Сигналим только своим процессам с comm == kitty.
    # Если у kitty есть свой наблюдатель (auto_reload_config, по умолчанию
    # включён), он сам замечает замену vnature.conf из include, - второй
    # сигнал дал бы лишнюю перерисовку.
    kitties = []
    watched = set()

    for process in proc.iterdir():

        if not process.name.isdigit():
            continue

        try:
            if process.stat().st_uid != os.getuid():
                continue

            argv = (process / "cmdline").read_bytes().split(b"\0")

            if len(argv) > 2 and argv[1] == b"__watch_conf__":
                watched.add(argv[2].decode())

            if (process / "comm").read_text().strip() == "kitty":
                kitties.append(process.name)

        except OSError:
            continue

    for pid in kitties:
        if pid in watched:
            continue

        try:
            os.kill(int(pid), signal.SIGUSR1)
        except OSError:
            continue
