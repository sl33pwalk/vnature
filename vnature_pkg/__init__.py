"""vnature: обои -> matugen -> палитра -> конфиги kitty, waybar, hyprland, rofi, polybar, swaync, obsidian.

Пакет отдаёт все имена модулей одним пространством, как было в монолитном скрипте.
"""

import sys
import types

from .common import *
from .palette import *
from .config import *
from .presets import *
from .wallpaper import *
from .state import *
from .apps import *
from .render import *
from .commands import *
from .tui import *
from .cli import *


class _Package(types.ModuleType):
    # vnature_pkg.NAME = x (так делают тесты) доходит до всех модулей, где NAME есть.
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        set_global(name, value)


sys.modules[__name__].__class__ = _Package
