"""Командная строка: argparse, старые формы, main/run."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

from .common import (
    VERSION,
    LEGACY_PRESETS,
    PRESETS,
    VNatureError,
    expand,
    ensure_dirs,
    set_global,
    warn,
)
from .config import (
    load_config,
)
from .presets import (
    preset_exists,
    legacy_presets,
    migrate_presets,
)
from .commands import (
    apply_preset,
    apply_image,
    save_current,
    delete_preset,
    go_back,
    show_current,
    show_presets,
)
from .tui import (
    interactive,
)


# ============================================================
# CLI
# ============================================================

COMMANDS = {
    # команда: (алиасы, справка)
    "apply": (["--eblo PRESET"], "применить пресет"),
    "image": (["--poh PATH"], "палитра из картинки"),
    "save": (["chotko"], "сохранить текущую тему как пресет"),
    "undo": (["govno"], "вернуть предыдущую тему (повтор - обратно)"),
    "list": (["kto"], "список пресетов"),
    "status": (["cho"], "что применено сейчас"),
    "delete": (["nahui"], "удалить пресет"),
    "migrate": ([], "скопировать пресеты из старого места (presets/ в клоне)"),
}

WALLPAPER_FLAGS = (
    "--eblo",
    "--wallpaper",
)


def translate_legacy(argv):
    # Старые формы -> новые: "--poh IMG", "--eblo PRESET", "PRESET", "PATH".
    # --eblo/--wallpaper переносятся в конец, чтобы работать в любой позиции.
    wallpaper = [a for a in argv if a in WALLPAPER_FLAGS]
    args = [a for a in argv if a not in WALLPAPER_FLAGS]

    poh = "--poh" in args
    args = [a for a in args if a != "--poh"]

    index = 0

    while index < len(args):
        if args[index] == "--wallpaper-dir":
            index += 2
        elif args[index].startswith("-"):
            index += 1
        else:
            break

    if index < len(args):
        word = args[index]
        known = set(COMMANDS) | {
            alias
            for aliases, _ in COMMANDS.values()
            for alias in aliases
        }

        if poh and word != "image":
            args.insert(index, "image")

        elif word not in known:
            if preset_exists(word):
                args.insert(index, "apply")

            elif expand(word).is_file():
                args.insert(index, "image")

    elif poh:
        args.append("image")

    if poh:
        args.append("--first-color")

    return args + wallpaper


GLOBAL_FLAGS = (
    "--wallpaper-dir",
    "--dry-run",
    "--version",
    "-h",
    "--help",
)

# Сообщения argparse -> сообщения vnature.
ARGPARSE_ERRORS = (
    (r"unrecognized arguments: (.*)", "лишние аргументы: {0}"),
    (r"the following arguments are required: (.*)", "не хватает аргумента: {0}"),
    (r"argument (\S+): expected one argument", "флагу {0} нужно значение"),
    (r"argument (\S+): ignored explicit argument '(.*)'", "флаг {0} не принимает значение: {1}"),
    (r"argument КОМАНДА: invalid choice: '(.*?)'",
     "не понял: {0} - это не команда, не пресет и не картинка"),
)


class HelpFormatter(argparse.RawDescriptionHelpFormatter):

    def add_usage(self, usage, actions, groups, prefix=None):
        return super().add_usage(
            usage,
            actions,
            groups,
            "использование: " if prefix is None else prefix,
        )


class Parser(argparse.ArgumentParser):

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("formatter_class", HelpFormatter)
        kwargs.setdefault("allow_abbrev", False)
        kwargs["add_help"] = False

        super().__init__(*args, **kwargs)

        self._positionals.title = "аргументы"
        self._optionals.title = "флаги"

        self.add_argument(
            "-h",
            "--help",
            action="help",
            help="показать эту справку",
        )

    def error(self, message):
        for pattern, text in ARGPARSE_ERRORS:
            match = re.match(pattern, message)

            if match:
                message = text.format(*match.groups())
                break

        sys.stderr.write(
            f"vnature: {message}\n"
            "справка: vnature --help\n"
        )

        sys.exit(2)


def add_global_flags(target, default):
    target.add_argument(
        "--wallpaper-dir",
        metavar="DIR",
        default=default,
        help="папка обоев вместо wallpaper_dir из config.toml",
    )

    target.add_argument(
        "--dry-run",
        action="store_true",
        default=default or False,
        help="всё проверить, но ничего не менять",
    )


def build_parser():
    parser = Parser(
        prog="vnature",
        description=(
            "Темы для Hyprland из обоев через matugen. "
            "Без команды открывается TUI."
        ),
        epilog=(
            "Старые формы тоже работают:\n"
            "  vnature PRESET         = apply PRESET\n"
            "  vnature --eblo PRESET  = apply PRESET --eblo\n"
            "  vnature PATH           = image PATH\n"
            "  vnature --poh PATH     = image PATH --first-color"
        ),
    )

    add_global_flags(parser, None)

    parser.add_argument(
        "--version",
        action="version",
        version=f"vnature {VERSION}",
        help="показать версию",
    )

    commands = parser.add_subparsers(
        dest="command",
        metavar="КОМАНДА",
        title="команды",
    )

    def add(name):
        aliases, text = COMMANDS[name]

        # "--eblo PRESET" и "--poh PATH" - не алиасы argparse, только для справки
        command = commands.add_parser(
            name,
            aliases=[a for a in aliases if not a.startswith("-")],
            help=text + "".join(
                f" (или {a})"
                for a in aliases
                if a.startswith("-")
            ),
        )

        # Глобальные флаги принимаются и после команды.
        add_global_flags(command, argparse.SUPPRESS)

        return command

    def wallpaper_flag(command, text):
        command.add_argument(
            *WALLPAPER_FLAGS,
            dest="eblo",
            action="store_true",
            help=text,
        )

    command = add("apply")
    command.add_argument("preset", metavar="PRESET")
    wallpaper_flag(command, "вместе с привязанными к пресету обоями")

    command = add("image")
    command.add_argument("path", metavar="PATH")
    command.add_argument(
        "--first-color",
        action="store_true",
        help="не спрашивать source color, взять первый",
    )

    command = add("save")
    command.add_argument("name", metavar="NAME")
    wallpaper_flag(command, "привязать к пресету текущие обои")

    add("undo")
    add("list")
    add("status")

    command = add("delete")
    command.add_argument("name", metavar="NAME")

    add("migrate")

    return parser


def parse_args(argv):
    parser = build_parser()
    argv = translate_legacy(argv)

    # Иначе "--wall /x kto" даёт непонятное "invalid choice: '/x'".
    for token in argv:
        if not token.startswith("-"):
            break

        if token in WALLPAPER_FLAGS:
            parser.error(
                f"{token} работает только с apply или save"
            )

        if token.split("=", 1)[0] not in GLOBAL_FLAGS:
            parser.error(
                f"неизвестный флаг: {token}"
            )

    args = parser.parse_args(
        argv
    )

    for name, (aliases, _) in COMMANDS.items():
        if args.command in aliases:
            args.command = name

    return args


def main(argv=None):
    args = parse_args(
        sys.argv[1:]
        if argv is None
        else argv
    )

    set_global("DRY_RUN", args.dry_run)

    ensure_dirs()

    cfg = load_config(
        args.wallpaper_dir
    )

    # Явно заданную папку проверяем сразу, папку из config.toml - при использовании.
    if args.wallpaper_dir is not None:
        if not cfg["wallpaper_dir"].is_dir():
            raise VNatureError(
                f"нет папки обоев: {cfg['wallpaper_dir']}"
            )

    # В TUI об этом спросит диалог.
    if args.command not in (None, "migrate") and legacy_presets():
        warn(
            f"пресеты лежат в старом месте: {LEGACY_PRESETS}\n"
            f"  новое место: {PRESETS}\n"
            "  перенести (копией, старые останутся): vnature migrate"
        )

    if args.command is None:
        interactive(cfg)

    elif args.command == "apply":
        apply_preset(
            args.preset,
            cfg,
            with_wallpaper=args.eblo,
        )

    elif args.command == "image":
        apply_image(
            args.path,
            cfg,
            poh=args.first_color,
        )

    elif args.command == "save":
        save_current(
            args.name,
            cfg,
            with_wallpaper=args.eblo,
        )

    elif args.command == "undo":
        go_back(cfg)

    elif args.command == "list":
        show_presets()

    elif args.command == "status":
        show_current()

    elif args.command == "delete":
        delete_preset(
            args.name
        )

    elif args.command == "migrate":
        migrate_presets()


def run():

    try:
        main()

    except VNatureError as error:

        print(
            f"vnature: {error}",
            file=sys.stderr,
        )

        sys.exit(1)

    except subprocess.CalledProcessError as error:

        print(
            "vnature: команда упала "
            f"с кодом "
            f"{error.returncode}",
            file=sys.stderr,
        )

        sys.exit(
            error.returncode
            or 1
        )
