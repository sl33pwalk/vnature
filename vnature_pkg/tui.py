"""Curses TUI: вкладки, списки, ползунок kitty, превью."""

from __future__ import annotations

import curses
import fcntl
import os
import shutil
import subprocess
import struct
import sys
import tempfile
import termios
import textwrap
import time
from pathlib import Path

from .common import (
    DRY_RUN,
    CONFIG_FILE,
    CURRENT,
    LEGACY_PRESETS,
    PRESETS,
    VNatureError,
    info,
    warn,
    expand,
    set_global,
)
from .palette import (
    KITTY_MODES,
    kitty_strength,
    kitty_mode_value,
    kitty_label,
    read_palette,
    hex_rgb,
    contrast,
    kitty_base,
    kitty_colors,
)
from .config import (
    set_config_key,
)
from .presets import (
    preset_path,
    get_presets,
    remove_preset,
    load_preset,
    save_preset_meta,
    legacy_presets,
    migrate_presets,
)
from .wallpaper import (
    wallpaper_images,
    resolve_wallpaper,
    portable_wallpaper,
)
from .state import (
    read_state,
    current_wallpaper,
)
from .commands import (
    apply_preset,
    current_kitty_target,
    refresh_kitty,
    apply_image,
    save_current,
    go_back,
)


# ============================================================
# TUI
# ============================================================

MIN_HEIGHT = 10
MIN_WIDTH = 40

# Горячие клавиши: контекст -> ((клавиши, что делают), ...).
# Отсюда строятся справка (?) и подсказки внизу; тест проверяет,
# что клавиши разделов не пересекаются с общими.
KEYMAP = {
    "общие": (
        (("1", "2", "3", "4", "Tab", "S-Tab"), "вкладки"),
        (("↑", "↓", "j", "k", "PgUp", "PgDn"), "выбор"),
        (("s",), "Чётко - сохранить текущую тему"),
        (("u",), "Говно - назад"),
        (("?",), "справка"),
        (("q",), "выход"),
    ),
    "Обои": (
        (("Enter",), "выбрать source color"),
        (("f",), "Мне пох - бери первый"),
        (("o",), "папка обоев"),
    ),
    "Пресеты": (
        (("Enter",), "только тема"),
        (("w",), "тема + обои (--eblo)"),
        (("m",), "режим kitty пресета"),
        (("b",), "привязать текущие обои"),
        (("x",), "убрать привязанные обои"),
        (("d",), "удалить нахуй"),
    ),
    "Kitty": (
        (("←", "→", "h", "l"), "сила ±5%"),
        (("S-←", "S-→", "H", "L"), "сила ±25%"),
        (("a", "b", "f"), "точки: Accent / Balanced / Full - ебашим"),
        (("o",), "Off - чистая база, без палитры"),
        (("Enter",), "сохранить в config"),
        (("Esc",), "вернуть как было"),
    ),
    "Kitty пресета": (
        (("←", "→", "h", "l"), "сила ±5%"),
        (("S-←", "S-→", "H", "L"), "сила ±25%"),
        (("a", "b", "f"), "точки: Accent / Balanced / Full - ебашим"),
        (("o",), "Off - чистая база, без палитры"),
        (("i",), "наследовать глобальный"),
        (("Enter",), "сохранить в пресет"),
        (("Esc",), "вернуть как было и закрыть"),
    ),
    "Настройки": (
        (("Enter",), "изменить"),
    ),
}


TABS = ("Обои", "Пресеты", "Kitty", "Настройки")


def tab_key(name, tab):
    # Клавиша -> номер вкладки (1–4, Tab, Shift+Tab) или None.
    if name in ("1", "2", "3", "4"):
        return int(name) - 1

    if name == "Tab":
        return (tab + 1) % len(TABS)

    if name == "S-Tab":
        return (tab - 1) % len(TABS)

    return None


# Пауза после последнего нажатия ползунка, потом kitty перерисовывается.
SLIDER_DELAY = 0.2


def slide(value, key, fallback, inherit=False):
    # Шаг ползунка силы kitty. value: 0–100, "off" или "inherit";
    # fallback - от какой силы шагать с off/inherit.
    # -> новое значение или None, если клавиша не про ползунок.
    points = {"a": "accent", "b": "balanced", "f": "full"}

    if key in points:
        return KITTY_MODES[points[key]][1]

    if key == "o":
        return "off"

    if key == "i":
        return "inherit" if inherit else None

    start = value if isinstance(value, int) else fallback

    if key in ("←", "h"):
        return max(0, (start - 1) // 5 * 5)

    if key in ("→", "l"):
        return min(100, (start // 5 + 1) * 5)

    if key in ("S-←", "H"):
        return max(0, start - 25)

    if key in ("S-→", "L"):
        return min(100, start + 25)

    return None


# С этой ширины - две панели (список | превью), уже - одна колонка.
TWO_PANELS_WIDTH = 80


def panels(width):
    # -> (x списка, ширина списка, x превью или None, ширина превью)
    if width < TWO_PANELS_WIDTH:
        return 2, width - 4, None, 0

    left = max(30, min(48, width * 38 // 100))
    return 2, left, 2 + left + 2, width - left - 7


# Пауза после выбора, прежде чем звать icat: при пролистывании не запускаем зря.
IMAGE_DELAY = 0.25

# Удалить все картинки kitty на экране (q=2 - терминал ничего не отвечает).
KITTY_DELETE_IMAGES = b"\x1b_Ga=d,q=2\x1b\\"


def window_pixels():
    # (колонки, строки, ширина и высота окна в пикселях) или None, если терминал не сказал.
    try:
        rows, cols, width, height = struct.unpack(
            "HHHH",
            fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8),
        )
    except OSError:
        return None

    return (cols, rows, width, height) if width and height else None


def icat_command(path, stamp, box, pixels):
    # stream: картинка целиком в выводе (кешируется); --use-window-size: без запросов к терминалу.
    x, y, width, height = box
    return [
        "kitten",
        "icat",
        "--stdin=no",
        "--transfer-mode=stream",
        "--use-window-size=" + ",".join(map(str, pixels)),
        f"--place={width}x{height}@{x}x{y}",
        "--align=left",
        path,
    ]


def tilde(path):
    # Для показа в TUI: домашний каталог как ~.
    text = str(path)
    home = str(Path.home())
    return "~" + text[len(home):] if text == home or text.startswith(home + "/") else text


def image_size(path):
    # (ширина, высота) из заголовка PNG/JPEG без сторонних библиотек; иначе None.
    try:
        with open(path, "rb") as f:
            head = f.read(26)

            if head[:8] == b"\x89PNG\r\n\x1a\n":
                return struct.unpack(">II", head[16:24])

            if head[:2] != b"\xff\xd8":
                return None

            f.seek(2)

            while True:
                marker = f.read(4)

                if len(marker) < 4 or marker[0] != 0xFF:
                    return None

                kind, length = marker[1], struct.unpack(">H", marker[2:])[0]

                if kind in (0xC0, 0xC1, 0xC2):
                    height, width = struct.unpack(">xHH", f.read(5))
                    return width, height

                f.seek(length - 2, 1)

    except (OSError, struct.error):
        return None


def nearest256(value):
    # Ближайший цвет из 256 цветов xterm (куб 6×6×6 или серая шкала).
    rgb = hex_rgb(value)
    levels = (0, 95, 135, 175, 215, 255)

    cube = [min(range(6), key=lambda i: abs(levels[i] - c)) for c in rgb]
    cube_rgb = [levels[i] for i in cube]

    gray = min(23, max(0, round((sum(rgb) / 3 - 8) / 10)))
    gray_rgb = [8 + 10 * gray] * 3

    def distance(other):
        return sum((a - b) ** 2 for a, b in zip(rgb, other))

    if distance(gray_rgb) < distance(cube_rgb):
        return 232 + gray

    return 16 + 36 * cube[0] + 6 * cube[1] + cube[2]

# Имена клавиш, в которых TUI думает о вводе (get_wch -> имя).
KEY_NAMES = {
    "\n": "Enter",
    "\r": "Enter",
    curses.KEY_ENTER: "Enter",
    "\x1b": "Esc",
    "\t": "Tab",
    curses.KEY_BTAB: "S-Tab",
    curses.KEY_UP: "↑",
    curses.KEY_DOWN: "↓",
    curses.KEY_LEFT: "←",
    curses.KEY_RIGHT: "→",
    curses.KEY_SLEFT: "S-←",
    curses.KEY_SRIGHT: "S-→",
    curses.KEY_PPAGE: "PgUp",
    curses.KEY_NPAGE: "PgDn",
    curses.KEY_HOME: "Home",
    curses.KEY_END: "End",
    curses.KEY_RESIZE: "resize",
}


def key_name(key):
    return KEY_NAMES.get(key, key if isinstance(key, str) else None)


def edit_line(text, cursor, key):
    # Один шаг однострочного редактора; key - из get_wch (str или int).
    # -> (text, cursor, итог): итог None - продолжаем, "ok" или "cancel".
    name = key_name(key)

    if name == "Enter":
        return text, cursor, "ok"

    if name == "Esc":
        return text, cursor, "cancel"

    if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
        if cursor == 0:
            return text, cursor, None

        return text[:cursor - 1] + text[cursor:], cursor - 1, None

    if key == curses.KEY_DC:
        return text[:cursor] + text[cursor + 1:], cursor, None

    if name == "←":
        return text, max(0, cursor - 1), None

    if name == "→":
        return text, min(len(text), cursor + 1), None

    if name == "Home" or key == "\x01":
        return text, 0, None

    if name == "End" or key == "\x05":
        return text, len(text), None

    if key == "\x15":  # Ctrl+U
        return "", 0, None

    if isinstance(key, str) and key.isprintable():
        return text[:cursor] + key + text[cursor:], cursor + len(key), None

    return text, cursor, None


def scroll_top(index, top, rows):
    # Первая видимая строка списка, чтобы index был на экране.
    if index < top:
        return index

    if index >= top + rows:
        return index - rows + 1

    return top


class TUI:

    def __init__(self, scr, cfg):
        self.scr = scr
        self.cfg = cfg
        self.log = []
        self.status = ""
        self.tab = 0
        self.positions = {}

        scr.keypad(True)
        self.setup_colors()
        self.setup_graphics()

        try:
            curses.curs_set(0)
            curses.set_escdelay(25)
        except curses.error:
            pass

    # ---------- сообщения и действия ----------

    def message(self, text, mark=False):
        # mark: True - предупреждение из warn(), "✗" - ошибка действия.
        prefix = {True: "⚠ ", "✗": "✗ "}.get(mark, "")
        self.log.append(prefix + text)
        self.status = self.log[-1]

    def act(self, function, *args, **kwargs):
        # Сообщения действия собираются в лог; ошибка или больше одной
        # строки показываются окном, одна строка - в статусе внизу.
        self.log = []
        failed = False

        try:
            function(*args, **kwargs)

        except VNatureError as error:
            self.message(str(error), "✗")
            failed = True

        except subprocess.CalledProcessError as error:
            self.message(f"команда упала с кодом {error.returncode}", "✗")
            failed = True

        if failed or len(self.log) > 1:
            self.popup(self.log)

        return not failed

    def suspend(self, function, *args, **kwargs):
        # Для matugen с вопросами: отдаём терминал, потом возвращаемся.
        self.hide_image()
        curses.def_prog_mode()
        curses.endwin()
        set_global("SINK", None)

        try:
            function(*args, **kwargs)

        except VNatureError as error:
            warn(error)

        except subprocess.CalledProcessError as error:
            warn(f"команда упала с кодом {error.returncode}")

        finally:
            set_global("SINK", self.message)
            input("\nEnter - назад в vnature...")
            curses.reset_prog_mode()
            self.scr.refresh()

    # ---------- рисование ----------

    def put(self, y, x, text, attribute=0, width=None):
        height, screen_width = self.scr.getmaxyx()
        width = screen_width - x - 1 if width is None else min(width, screen_width - x - 1)

        if 0 <= y < height and 0 <= x < screen_width and width > 0:
            try:
                self.scr.addnstr(y, x, text, width, attribute)
            except curses.error:
                pass

    def too_small(self):
        height, width = self.scr.getmaxyx()

        if height >= MIN_HEIGHT and width >= MIN_WIDTH:
            return False

        self.hide_image()
        self.scr.erase()

        try:
            self.scr.addnstr(
                0,
                0,
                f"Окно маловато ({height}×{width}), "
                f"нужно хотя бы {MIN_HEIGHT}×{MIN_WIDTH}. q - выход",
                max(1, width - 1),
            )
        except curses.error:
            pass

        self.scr.refresh()

        return True

    def key(self, timeout=None):
        self.scr.timeout(-1 if timeout is None else timeout)

        try:
            return self.scr.get_wch()
        except curses.error:
            return None

    # ---------- диалоги ----------

    def menu(self, title, items, index=0):
        # Список с выбором: индекс или None (q/Esc).
        top = 0

        while True:
            if self.too_small():
                if key_name(self.key()) in ("q", "Esc"):
                    return None
                continue

            height, width = self.scr.getmaxyx()
            rows = max(1, height - 5)
            top = scroll_top(index, top, rows)

            self.scr.erase()
            self.put(1, 2, title, curses.A_BOLD)

            if not items:
                self.put(3, 2, "(пусто)", curses.A_DIM)

            for position in range(top, min(len(items), top + rows)):
                selected = position == index
                self.put(
                    3 + position - top,
                    2,
                    ("❯ " if selected else "  ") + items[position],
                    curses.A_REVERSE if selected else curses.A_NORMAL,
                )

            hint = "↑/↓ j/k · Enter · q/Esc"

            if len(items) > rows:
                hint += f" · {index + 1}/{len(items)}"

            self.put(4 + min(max(1, len(items)), rows), 2, hint, curses.A_DIM)
            self.put(height - 1, 2, self.status, curses.A_DIM)
            self.scr.refresh()
            self.hide_image()

            name = key_name(self.key())

            if name in ("q", "Esc"):
                return None

            if not items:
                if name == "Enter":
                    return None

            elif name in ("↑", "k"):
                index = (index - 1) % len(items)

            elif name in ("↓", "j"):
                index = (index + 1) % len(items)

            elif name == "Enter":
                return index

    def popup(self, lines, title="vnature"):
        while True:
            if self.too_small():
                self.key()
                return

            height, width = self.scr.getmaxyx()
            self.scr.erase()
            self.put(1, 2, title, curses.A_BOLD)
            rows = height - 5

            for position, line in enumerate(lines[:rows]):
                self.put(3 + position, 2, line)

            if len(lines) > rows:
                self.put(3 + rows - 1, 2, f"… ещё {len(lines) - rows + 1}", curses.A_DIM)

            self.put(height - 1, 2, "любая клавиша - дальше", curses.A_DIM)
            self.scr.refresh()
            self.hide_image()

            if key_name(self.key()) != "resize":
                return

    def ask(self, prompt, text=""):
        # Строка ввода внутри curses: str или None (Esc). Кириллица - через get_wch.
        cursor = len(text)

        try:
            curses.curs_set(1)
        except curses.error:
            pass

        try:
            while True:
                height, width = self.scr.getmaxyx()
                self.scr.erase()
                lines = prompt.split("\n")

                for position, line in enumerate(lines):
                    self.put(1 + position, 2, line, curses.A_BOLD)

                # Хвост строки, если она длиннее окна.
                row = 2 + len(lines)
                room = max(1, width - 6)
                start = max(0, cursor - room + 1)
                self.put(row, 2, "> " + text[start:start + room])
                self.put(row + 2, 2, "Enter - готово · Esc - отмена · Ctrl+U - очистить", curses.A_DIM)

                try:
                    self.scr.move(row, min(width - 1, 4 + cursor - start))
                except curses.error:
                    pass

                self.scr.refresh()
                self.hide_image()
                key = self.key()

                if key is None or key_name(key) == "resize":
                    continue

                text, cursor, result = edit_line(text, cursor, key)

                if result == "ok":
                    return text.strip()

                if result == "cancel":
                    return None

        finally:
            try:
                curses.curs_set(0)
            except curses.error:
                pass

    def pick_kitty_mode(self, inherit=True):
        modes = [("Наследовать глобальный", "inherit")] if inherit else []
        modes += [(label, mode) for mode, (label, _) in KITTY_MODES.items()]
        index = self.menu("Режим Kitty", [label for label, _ in modes])

        return None if index is None else modes[index][1]

    # ---------- действия ----------

    def apply_wallpaper(self, image, first_color):
        if first_color:
            self.act(apply_image, str(image), self.cfg, poh=True)
        else:
            self.suspend(apply_image, str(image), self.cfg, poh=False)

    def apply_preset(self, name, with_wallpaper):
        self.act(apply_preset, name, self.cfg, with_wallpaper=with_wallpaper)

    def save(self):
        name = self.ask("Имя пресета:")

        if not name:
            return

        eblo = self.menu("Привязать текущие обои?", ["Нет", "Да"])

        if eblo is None:
            return

        kitty_mode = self.pick_kitty_mode(inherit=True)

        if kitty_mode is None:
            return

        self.act(save_current, name, self.cfg, with_wallpaper=eblo == 1, kitty_mode=kitty_mode)

    def undo(self):
        self.act(go_back, self.cfg)

    def preset_kitty(self, name):
        return self.kitty_slider(name)

    def fallback_strength(self):
        # От чего шагать с off/inherit: глобальная сила (off -> 0).
        strength = kitty_strength(self.cfg["kitty_mode"])
        return strength if isinstance(strength, int) else 0

    def stored(self, value):
        return value if value == "inherit" else kitty_mode_value(value)

    def live(self, value, preset):
        # Живое применение: только kitty, через refresh_kitty; итог - в статус.
        mode = self.stored(value)
        self.log = []

        try:
            if preset is None:
                info(refresh_kitty(dict(self.cfg, kitty_mode=mode)))
            else:
                info(refresh_kitty(self.cfg, {preset: mode}))

        except VNatureError as error:
            self.message(str(error), "✗")

    def persist(self, value, preset):
        mode = self.stored(value)

        def change():
            if preset is None:
                set_config_key("kitty_mode", mode)
                self.cfg["kitty_mode"] = mode
            else:
                current = load_preset(preset)
                save_preset_meta(preset, wallpaper=current["wallpaper"], kitty_mode=mode)

            info(f"сохранено: kitty {kitty_label(mode)}" + (f" для {preset}" if preset else ""))

        self.act(change)

    def value_label(self, value):
        if value == "inherit":
            return f"inherit → {kitty_label(self.cfg['kitty_mode'])}"

        if value == "off":
            return "off - чистая база"

        return kitty_label(value)

    def draw_slider(self, first, x, width, value, preset, pending):
        title = "Kitty: сила палитры - " + (f"пресет {preset}" if preset else "глобально")
        self.put(first, x, title, self.role("accent"))

        bar = max(10, min(50, width - 26))
        strength = value if isinstance(value, int) else (
            self.fallback_strength() if value == "inherit" else 0
        )
        position = round(strength / 100 * (bar - 1))
        line = "━" * position + "●" + "─" * (bar - 1 - position)
        style = self.role("accent") if isinstance(value, int) else self.role("muted")

        self.put(first + 2, x, "◀ " + line + " ▶  " + self.value_label(value), style)

        marks = [" "] * bar
        for letter, mode in (("a", "accent"), ("b", "balanced"), ("f", "full")):
            marks[round(KITTY_MODES[mode][1] / 100 * (bar - 1))] = letter
        self.put(first + 3, x + 2, "".join(marks), self.role("muted"))

        hints = "←/→ ±5% · Shift ±25% · a b f - точки · o - off" + (" · i - inherit" if preset else "")
        self.put(first + 5, x, hints, self.role("muted"), width)
        self.put(
            first + 6,
            x,
            "Enter - сохранить в " + ("пресет" if preset else "config") + " · Esc - вернуть как было",
            self.role("muted"),
            width,
        )

        if pending:
            self.put(first + 7, x, "kitty перерисуется через 200 мс после последнего нажатия", self.role("ok"), width)

    def draw_kitty_preview(self, first, last, x, width, value, preset):
        if self.palette is None:
            self.put(first, x, "темы пока нет - превью не из чего строить", self.role("muted"), width)
            return

        mode = self.stored(value)
        effective = self.cfg["kitty_mode"] if mode == "inherit" else mode

        try:
            colors = kitty_colors(self.palette, effective, kitty_base(self.cfg))
        except VNatureError as error:
            self.put(first, x, str(error), self.role("error"), width)
            return

        bg, fg = colors["kitty_background"], colors["kitty_foreground"]
        cell = max(2, min(6, (width - 8) // 8))
        rows = [
            ("живое превью (эта же kitty)", None),
            ("фон и текст", "sample"),
            ("normal", range(0, 8)),
            ("bright", range(8, 16)),
        ]
        y = first

        for title, content in rows:
            if y > last:
                return

            if content is None:
                self.put(y, x, title, self.role("accent"), width)

            elif content == "sample":
                self.put(y, x, "фон ", 0)
                self.put(y, x + 4, "▇" * cell, self.color(bg))
                self.put(y, x + 5 + cell, " $ git status · текст Aa ", self.color(fg, bg), width - cell - 6)

            else:
                self.put(y, x, title, 0)

                for offset, number in enumerate(content):
                    self.put(y, x + 7 + offset * cell, "▇" * cell, self.color(colors[f"kitty_color{number}"]))

            y += 1

        state = read_state(CURRENT)

        if preset is None:
            target = current_kitty_target(state, dict(self.cfg, kitty_mode=effective))
        else:
            target = current_kitty_target(state, self.cfg, {preset: mode})

        if state is None:
            note = "темы нет - применится при следующей теме"
        elif preset is not None and state["name"] != preset:
            note = f"пресет {preset} не применён - применится при следующей теме"
        elif target is None:
            note = "текущая тема не из пресета - не затронет"
        elif target != kitty_mode_value(kitty_strength(effective)):
            note = f"{state['name']} использует свой режим - глобальный ползунок на неё не влияет"
        else:
            note = f"применится к теме: {'из картинки' if state['name'] == '__temporary__' else state['name']}"

        lines = [
            f"контраст текст/фон {contrast(fg, bg):.1f} · color0/фон {contrast(colors['kitty_color0'], bg):.2f}",
            note,
        ]

        for offset, line in enumerate(lines):
            if y + 1 + offset <= last:
                self.put(y + 1 + offset, x, line, self.role("muted"), width)

    def kitty_slider(self, preset=None):
        # Ползунок силы kitty. -> "quit", если нажали q.
        context = "Kitty" if preset is None else "Kitty пресета"

        try:
            original = self.cfg["kitty_mode"] if preset is None else load_preset(preset)["kitty_mode"]
        except VNatureError as error:
            self.message(str(error), "✗")
            return None

        original = original if original == "inherit" else kitty_strength(original)
        value = applied = original
        due = None
        self.status = ""

        while True:
            if self.too_small():
                key = self.key()
                name = key_name(key)
            else:
                first, last = self.draw_chrome(context)
                height, width = self.scr.getmaxyx()
                x, left_width, right, right_width = panels(width)
                self.draw_slider(first, x, left_width, value, preset, due is not None)

                if right is None:
                    self.draw_kitty_preview(first + 9, last, x, left_width, value, preset)
                else:
                    self.separator(first, last, right - 2)
                    self.draw_kitty_preview(first, last, right, right_width, value, preset)

                self.scr.refresh()
                self.sync_image()

                timeout = None if due is None else max(0, int((due - time.monotonic()) * 1000))
                key = self.key(timeout)
                name = key_name(key)

            if key is None:
                if due is not None:
                    self.live(value, preset)
                    applied, due = value, None
                continue

            new = slide(value, name, self.fallback_strength(), inherit=preset is not None)

            if new is not None:
                if new != value:
                    value, due = new, time.monotonic() + SLIDER_DELAY
                continue

            if name == "Enter":
                if applied != value:
                    self.live(value, preset)
                    applied, due = value, None

                if value != original:
                    self.persist(value, preset)
                    original = value
                else:
                    self.status = "без изменений"

                if preset is not None:
                    return None

            elif name in ("Esc", "q") or (preset is None and (tab_key(name, self.tab) is not None or name in ("s", "u"))):
                # Уходим или отменяем: несохранённое возвращаем как было.
                if applied != original:
                    self.live(original, preset)
                    applied = original

                if value != original:
                    self.status = f"не сохранено - вернул {self.value_label(original)}"
                    value, due = original, None

                if name == "q":
                    return "quit"

                if preset is not None:
                    return None

                if tab_key(name, self.tab) is not None:
                    return "tab", tab_key(name, self.tab)

                if name == "s":
                    self.save()

                elif name == "u":
                    self.undo()
                    original = value = applied = kitty_strength(self.cfg["kitty_mode"])

            elif name == "?":
                self.help(context)

    def bind_wallpaper(self, name):
        def change():
            wallpaper = current_wallpaper()

            if not wallpaper:
                raise VNatureError("не знаю текущие обои")

            preset = load_preset(name)
            save_preset_meta(
                name,
                wallpaper=portable_wallpaper(wallpaper, self.cfg),
                kitty_mode=preset["kitty_mode"],
            )
            info(f"{name}: привязал {wallpaper.name}")

        self.act(change)

    def unbind_wallpaper(self, name):
        def change():
            preset = load_preset(name)
            save_preset_meta(name, wallpaper=None, kitty_mode=preset["kitty_mode"])
            info(f"{name}: обои отвязаны")

        self.act(change)

    def delete(self, name):
        if self.menu("Точно?", ["Нет", "Да, нахуй"]) != 1:
            return

        def change():
            remove_preset(name)
            info(f"нахуй: {name}")

        self.act(change)

    def offer_migration(self):
        names = legacy_presets()

        if not names:
            return

        choice = self.menu(
            f"Пресеты в старом месте: {tilde(LEGACY_PRESETS)} ({len(names)})",
            [f"Перенести в {tilde(PRESETS)} (копией, старые останутся)", "Не сейчас"],
        )

        if choice == 0:
            self.act(migrate_presets)

    def wallpaper_dir(self):
        raw = self.ask(
            f"Папка обоев: {self.cfg['wallpaper_dir']}\n"
            "Новая папка (пусто - оставить):"
        )

        if not raw:
            return

        def change():
            images = wallpaper_images(expand(raw))
            set_config_key("wallpaper_dir", raw)
            self.cfg["wallpaper_dir"] = expand(raw)
            info(f"папка обоев: {raw} (картинок: {len(images)})")

        self.act(change)

    # ---------- цвета ----------

    # роль -> (ключ палитры, атрибут без цветов)
    ROLES = {
        "accent": ("primary", curses.A_BOLD),
        "muted": ("foreground_muted", curses.A_DIM),
        "error": ("error", curses.A_BOLD),
        "ok": ("tertiary", 0),
    }

    def setup_colors(self):
        self.colors = curses.has_colors()
        self.pairs = {}
        self.palette = None
        self.palette_stamp = None

        if self.colors:
            curses.start_color()

            try:
                curses.use_default_colors()
            except curses.error:
                pass

    def load_palette(self):
        # Цвета TUI - из палитры текущей темы; перечитываем, когда она сменилась.
        path = CURRENT / "palette.conf"

        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            stamp = None

        if stamp != self.palette_stamp:
            self.palette_stamp = stamp

            try:
                self.palette = read_palette(path) if stamp else None
            except (OSError, ValueError):
                self.palette = None

    def truecolor(self):
        return self.colors and curses.COLORS >= 256 and self.palette is not None

    def pair(self, fg, bg=-1):
        if not self.colors:
            return 0

        if (fg, bg) not in self.pairs:
            number = len(self.pairs) + 1

            if number >= curses.COLOR_PAIRS:
                return 0

            try:
                curses.init_pair(number, fg, bg)
            except curses.error:
                return 0

            self.pairs[fg, bg] = number

        return curses.color_pair(self.pairs[fg, bg])

    def color(self, value, bg=None):
        # Цвет палитры -> ближайший из 256 (палитру терминала не трогаем).
        return self.pair(
            nearest256(value),
            -1 if bg is None else nearest256(bg),
        )

    def role(self, name):
        key, fallback = self.ROLES[name]

        if self.truecolor() and key in self.palette:
            return self.color(self.palette[key]) | (fallback & curses.A_BOLD)

        return fallback

    def selected(self):
        if self.truecolor():
            return self.color(self.palette["foreground"], self.palette["primary_container"]) | curses.A_BOLD

        return curses.A_REVERSE

    # ---------- каркас экрана ----------

    def draw_chrome(self, context):
        # Шапка, полоска 16 цветов, подвал. -> (первая, последняя) строка тела.
        height, width = self.scr.getmaxyx()
        self.load_palette()
        self.scr.erase()
        self.image_want = None

        state = read_state(CURRENT)

        if state:
            theme = "из картинки" if state["name"] == "__temporary__" else state["name"]
            wallpaper = Path(state["wallpaper"]).name if state.get("wallpaper") else "-"
            header = f" vnature · тема {theme} · обои {wallpaper} · kitty {kitty_label(state['kitty_mode'])}"
        else:
            header = " vnature · темы пока нет"

        self.put(0, 0, header, self.role("accent"))

        if DRY_RUN:
            self.put(0, width - 11, "[dry-run]", self.role("error"))

        # ANSI 0–15 самого терминала: в kitty с vnature.conf это ровно текущая тема.
        count = 16 if not self.colors or curses.COLORS >= 16 else 8
        cell = max(1, (width - 2) // count)

        for number in range(count):
            self.put(1, 1 + number * cell, "▇" * cell, self.pair(number))

        x = 1
        compact = width < 60

        for number, name in enumerate(TABS):
            label = f"{number + 1} {name}" if compact else f"[{number + 1} {name}]"
            chosen = number == self.tab and context in TABS
            self.put(2, x, label, self.selected() if chosen else self.role("muted"))
            x += len(label) + (1 if compact else 2)

        if not compact:
            self.put(2, width - 22, "u назад · ? справка", self.role("muted"))

        self.put(3, 0, "─" * width, self.role("muted"))
        self.put(height - 2, 0, "─" * width, self.role("muted"))

        hints = " · ".join(
            f"{'/'.join(keys)} {text}"
            for keys, text in KEYMAP.get(context, ())
        ) + " · 1-4 вкладки · s чётко · u говно · ? · q"
        status = self.status[-(width // 2):] if self.status else ""
        self.put(height - 1, 1, hints, self.role("muted"), max(0, width - len(status) - 4))

        if status:
            self.put(
                height - 1,
                width - len(status) - 2,
                status,
                self.role("error") if status[:1] in "✗⚠" else self.role("ok"),
            )

        return 4, height - 3

    def draw_list(self, first, last, x, width, items, index, top):
        rows = last - first + 1

        if not items:
            self.put(first, x, "(пусто)", self.role("muted"))

        for position in range(top, min(len(items), top + rows)):
            chosen = position == index
            self.put(
                first + position - top,
                x,
                (("❯ " if chosen else "  ") + items[position]).ljust(width),
                self.selected() if chosen else 0,
                width,
            )

        if len(items) > rows:
            counter = f" {index + 1}/{len(items)} "
            self.put(last, x + width - len(counter), counter, self.role("muted"))

    def help(self, context):
        lines = []

        for name in ("общие", context):
            lines.append(name.capitalize() + ":")
            lines += [f"  {' '.join(keys):<22} {text}" for keys, text in KEYMAP.get(name, ())]
            lines.append("")

        self.popup(lines, "Справка")

    # ---------- правая панель ----------

    def separator(self, first, last, x):
        for y in range(first, last + 1):
            self.put(y, x, "│", self.role("muted"))

    def lines(self, first, last, x, width, lines):
        # lines: [(текст, роль или None)]
        for offset, (text, role) in enumerate(lines):
            if first + offset > last:
                return first + offset

            self.put(first + offset, x, text, self.role(role) if role else 0, width)

        return first + len(lines)

    def swatches(self, y, x, width, colors):
        # colors: [(подпись, #hex)] -> "▇▇▇ подпись" подряд
        for label, value in colors:
            if x + 4 + len(label) > x + width:
                return

            self.put(y, x, "▇▇▇", self.color(value))
            self.put(y, x + 4, label)
            x += 6 + len(label)

    def preview(self, context, value, first, last, x, width):
        if value is None:
            return

        if context == "Обои":
            self.preview_wallpaper(value, first, last, x, width)

        elif context == "Пресеты":
            self.preview_preset(value, first, last, x, width)

        elif context == "Настройки":
            self.preview_setting(value, first, last, x, width)

    def preview_wallpaper(self, image, first, last, x, width):
        size = image_size(image)
        mb = image.stat().st_size / 1024 / 1024
        bound = [
            name for name in get_presets()
            if self.preset_wallpaper(name) == image.resolve()
        ]

        y = self.lines(first, last, x, width, [
            (image.name, "accent"),
            (f"{size[0]}×{size[1]} · {mb:.1f} МБ" if size else f"{mb:.1f} МБ", None),
            (tilde(image.parent), "muted"),
            ("в пресетах: " + (", ".join(bound) if bound else "-"), "muted"),
        ])
        self.want_image(image, y + 1, last, x, width)

    def preset_wallpaper(self, name):
        try:
            reference = load_preset(name)["wallpaper"]
        except VNatureError:
            return None

        return resolve_wallpaper(reference, self.cfg) if reference else None

    def preview_preset(self, name, first, last, x, width):
        try:
            preset = load_preset(name)
            palette = read_palette(preset_path(name) / "palette.conf")
        except (VNatureError, OSError) as error:
            self.put(first, x, str(error), self.role("error"), width)
            return

        state = read_state(CURRENT)
        wallpaper = self.preset_wallpaper(name)
        mode = preset["kitty_mode"]

        y = self.lines(first, last, x, width, [
            (name + ("  ● применён сейчас" if state and state["name"] == name else ""), "accent"),
            ("", None),
        ])

        if y <= last:
            self.swatches(y, x, width, [(key, palette[key]) for key in ("primary", "secondary", "tertiary", "error") if key in palette])
        if y + 1 <= last:
            self.swatches(y + 1, x, width, [(key, palette[key]) for key in ("background", "surface_container", "outline") if key in palette])
        if y + 2 <= last and "foreground" in palette and "background" in palette:
            self.put(y + 2, x, " $ git status · текст на фоне палитры ", self.color(palette["foreground"], palette["background"]), width)

        self.lines(y + 4, last, x, width, [
            ("обои: " + (f"{wallpaper.name}" + ("" if wallpaper.is_file() else " (файла нет)") if wallpaper else "не привязаны"), None),
            ("kitty: " + (f"inherit → {kitty_label(self.cfg['kitty_mode'])}" if mode == "inherit" else kitty_label(mode)), None),
        ])

    def preview_setting(self, key, first, last, x, width):
        if key == "wallpaper_dir":
            folder = self.cfg["wallpaper_dir"]

            try:
                count = f"картинок: {len(wallpaper_images(folder))}"
            except VNatureError as error:
                count = str(error)

            lines = [("Папка обоев", "accent"), (tilde(folder), None), (count, "muted"),
                     ("", None), ("Enter - сменить; пишется в config.toml", "muted")]
        else:
            raw = self.cfg.get("kitty_base")

            try:
                kitty_base(self.cfg)
                check = "читается"
            except VNatureError as error:
                check = str(error)

            lines = [("База kitty", "accent"), (raw or "STABLE (встроенная)", None), (check, "muted"),
                     ("", None), ("из неё берутся фон, текст и color0–15;", "muted"),
                     ("палитра подмешивается по силе kitty", "muted")]

        lines.append(("", None))
        lines.append((f"config: {tilde(CONFIG_FILE)}", "muted"))
        self.lines(first, last, x, width, lines)

    # ---------- картинка справа (kitty graphics через kitten icat) ----------

    def setup_graphics(self):
        # Только в настоящем kitty: не в tmux/screen, есть kitten, известен размер ячейки в пикселях.
        self.image_want = None
        self.image_requested = None
        self.image_shown = None
        self.image_since = 0.0
        self.image_cache = {}
        self.image_job = None
        self.graphics = (
            (os.environ.get("TERM") == "xterm-kitty" or "KITTY_WINDOW_ID" in os.environ)
            and not os.environ.get("TMUX")
            and not os.environ.get("STY")
            and shutil.which("kitten") is not None
            and window_pixels() is not None
        )

    def want_image(self, path, first, last, x, width):
        if not self.graphics or last - first < 4 or width < 10:
            return

        pixels = window_pixels()

        if pixels is None:
            return

        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            return

        key = (str(path), stamp, (x, first, width, last - first + 1), pixels)

        # image_want сбрасывается каждый кадр; отсчёт паузы - от смены запрошенной картинки.
        if key != self.image_requested:
            self.image_requested = key
            self.image_since = time.monotonic()

        self.image_want = key

        if key not in self.image_cache:
            elapsed = time.monotonic() - self.image_since
            self.put(first, x, f"превью готовится… {elapsed:.0f} с", self.role("muted"), width)

    def tty_write(self, data):
        try:
            os.write(sys.stdout.fileno(), data)
        except OSError:
            self.graphics = False

    def hide_image(self):
        self.image_want = None
        self.sync_image()

    def sync_image(self):
        # После refresh: убрать старую картинку, показать нужную из кеша
        # или дождаться фонового icat (декодирование больших JPG - секунды).
        if not self.graphics:
            return

        want = self.image_want

        if self.image_shown is not None and self.image_shown != want:
            self.tty_write(KITTY_DELETE_IMAGES)
            self.image_shown = None

        job = self.image_job

        # Окна поверх (справка, диалоги) только прячут картинку; фоновый icat
        # отменяем, лишь когда попросили другую картинку.
        if want is None or want == self.image_shown:
            return

        if job is not None and job[0] != want:
            job[1].kill()
            job[1].wait()
            job[2].close()
            self.image_job = job = None

        if want in self.image_cache:
            # DECSC/DECRC: icat ставит курсор в угол картинки, curses об этом не знает.
            self.tty_write(b"\x1b7" + self.image_cache[want] + b"\x1b8")
            self.image_shown = want
            return

        if job is None:
            if time.monotonic() - self.image_since >= IMAGE_DELAY:
                output = tempfile.TemporaryFile()
                process = subprocess.Popen(
                    icat_command(*want),
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                )
                self.image_job = (want, process, output)
            return

        if job[1].poll() is None:
            return

        self.image_job = None
        job[2].seek(0)
        data = job[2].read()
        job[2].close()

        if job[1].returncode == 0 and data:
            self.image_cache[want] = data
            self.sync_image()

    def image_timeout(self):
        # Пока ждём icat или паузу перед ним - будим цикл каждые 100 мс.
        if self.graphics and self.image_want is not None and self.image_want != self.image_shown:
            return 100

        return None

    # ---------- разделы ----------

    def items(self, context):
        # -> ([(подпись, значение)], ошибка или None)
        if context == "Обои":
            try:
                images = wallpaper_images(self.cfg["wallpaper_dir"])
            except VNatureError as error:
                return [], str(error)

            return [(image.name, image) for image in images], None

        if context == "Пресеты":
            state = read_state(CURRENT)
            current = state["name"] if state else None
            result = []

            for name in get_presets():
                try:
                    preset = load_preset(name)
                except VNatureError as error:
                    result.append((f"{name:<16} ✗ {error}", name))
                    continue

                label = f"{name:<16}{'▣ обои' if preset['wallpaper'] else '':<8}"

                if preset["kitty_mode"] != "inherit":
                    label += f"kitty {kitty_label(preset['kitty_mode'])}"

                if name == current:
                    label += "  ● сейчас"

                result.append((label, name))

            return result, None


        base = self.cfg.get("kitty_base") or "STABLE (встроенная)"
        return [
            (f"Папка обоев: {tilde(self.cfg['wallpaper_dir'])}", "wallpaper_dir"),
            (f"База kitty: {base}", "kitty_base"),
        ], None

    def actions(self, context):
        return {
            "Обои": {
                "Enter": self.choose_source,
                "f": lambda image: image and self.apply_wallpaper(image, True),
                "o": lambda _: self.wallpaper_dir(),
            },
            "Пресеты": {
                "Enter": lambda name: name and self.apply_preset(name, False),
                "w": lambda name: name and self.apply_preset(name, True),
                "m": lambda name: name and self.preset_kitty(name) == "quit" and "quit",
                "b": lambda name: name and self.bind_wallpaper(name),
                "x": lambda name: name and self.unbind_wallpaper(name),
                "d": lambda name: name and self.delete(name),
            },
            "Настройки": {
                "Enter": self.setting,
            },
        }.get(context, {})

    def choose_source(self, image):
        if not image:
            return

        mode = self.menu(image.name, ["Выбрать source color", "Мне пох - бери первый"])

        if mode is not None:
            self.apply_wallpaper(image, mode == 1)

    def setting(self, key):
        if key == "wallpaper_dir":
            self.wallpaper_dir()
            return

        raw = self.ask(
            "База kitty: путь к теме kitty (background, foreground, color0–15)\n"
            "Пусто - встроенная STABLE:",
            self.cfg.get("kitty_base") or "",
        )

        if raw is None:
            return

        def change():
            kitty_base({"kitty_base": raw})  # проверка до записи
            set_config_key("kitty_base", raw)
            self.cfg["kitty_base"] = raw or None
            info(f"база kitty: {raw or 'STABLE'}")
            info(refresh_kitty(self.cfg, force=True))

        self.act(change)

    def browse(self, context):
        # Вкладка со списком. -> "quit" или ("tab", номер).
        index, top = self.positions.get(context, (0, 0))

        while True:
            items, error = self.items(context)
            index = min(index, max(0, len(items) - 1))

            if self.too_small():
                if key_name(self.key()) == "q":
                    return "quit"
                continue

            first, last = self.draw_chrome(context)
            height, width = self.scr.getmaxyx()
            x, list_width, right, right_width = panels(width)

            rows = max(1, last - first + 1)
            top = scroll_top(index, top, rows)
            self.positions[context] = (index, top)

            # Ошибка вместо списка (он тогда пуст), переносом по ширине: путь целиком.
            if error:
                self.lines(first, last, x, list_width,
                           [(line, "error") for line in textwrap.wrap(error, list_width, break_on_hyphens=False)])
            else:
                self.draw_list(first, last, x, list_width, [label for label, _ in items], index, top)

            if right is not None:
                self.separator(first, last, right - 2)
                self.preview(context, items[index][1] if items else None, first, last, right, right_width)

            self.scr.refresh()
            self.sync_image()

            key = self.key(self.image_timeout())

            if key is None:
                continue

            name = key_name(key)
            value = items[index][1] if items else None

            if name == "q":
                return "quit"

            if tab_key(name, self.tab) is not None:
                return "tab", tab_key(name, self.tab)

            if name in ("↑", "k") and items:
                index = (index - 1) % len(items)

            elif name in ("↓", "j") and items:
                index = (index + 1) % len(items)

            elif name == "PgUp":
                index = max(0, index - rows)

            elif name == "PgDn" and items:
                index = min(len(items) - 1, index + rows)

            elif name == "?":
                self.help(context)

            elif name == "s":
                self.save()

            elif name == "u":
                self.undo()

            elif name in self.actions(context):
                if self.actions(context)[name](value) == "quit":
                    return "quit"

    def run(self):
        set_global("SINK", self.message)

        try:
            self.offer_migration()

            while True:
                context = TABS[self.tab]
                result = self.kitty_slider() if context == "Kitty" else self.browse(context)

                if result == "quit":
                    return

                self.tab = result[1]
        finally:
            self.hide_image()
            set_global("SINK", None)


def interactive(cfg):
    curses.wrapper(
        lambda scr: TUI(scr, cfg).run()
    )
