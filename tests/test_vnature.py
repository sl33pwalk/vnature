import contextlib
import importlib.machinery
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "vnature"


class Isolated(unittest.TestCase):
    """Каждый тест грузит vnature заново с временным HOME и presets/."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)

        loader = importlib.machinery.SourceFileLoader("vnature", str(SCRIPT))
        spec = importlib.util.spec_from_loader("vnature", loader)
        self.v = importlib.util.module_from_spec(spec)
        loader.exec_module(self.v)

        self.v.PRESETS = self.home / "presets"
        self.v.PRESETS.mkdir()
        self.v.ensure_dirs()

        # Никаких сигналов настоящему kitty и вызовов hyprctl/swaync из тестов.
        self.calls = []
        for name in ("reload_kitty", "reload_hyprland", "reload_swaync",
                     "reload_polybar", "set_wallpaper"):
            setattr(self.v, name, lambda *a, _name=name: self.calls.append(_name))

    def tearDown(self):
        os.environ["HOME"] = self.old_home
        self.tmp.cleanup()

    def full_palette(self):
        keys = self.v.read_palette(self.v.TEMPLATES / "palette.conf")
        path = self.home / "palette.conf"
        path.write_text("".join(f"{key}=#336699\n" for key in keys))
        return path


class PresetNames(Isolated):

    def test_bad_names_rejected(self):
        for name in ("", "../x", "..", "a/b", "a\\b", ".hidden", "a..b"):
            with self.subTest(name=name):
                self.assertFalse(self.v.valid_preset_name(name))
                self.assertFalse(self.v.preset_exists(name))
                with self.assertRaises(self.v.VNatureError):
                    self.v.preset_path(name)

    def test_good_names_accepted(self):
        for name in ("autumn", "paint_girl", "a.b", "тема"):
            with self.subTest(name=name):
                self.assertEqual(self.v.preset_path(name), self.v.PRESETS / name)

    def test_remove_refuses_symlink_outside(self):
        outside = self.home / "outside"
        outside.mkdir()
        (outside / "palette.conf").write_text("x=1\n")
        (self.v.PRESETS / "evil").symlink_to(outside)

        with self.assertRaises(self.v.VNatureError):
            self.v.remove_preset("evil")

        self.assertTrue((outside / "palette.conf").is_file())

    def test_remove_inside_presets(self):
        (self.v.PRESETS / "ok").mkdir()
        self.v.remove_preset("ok")
        self.assertFalse((self.v.PRESETS / "ok").exists())


class Rendering(Isolated):

    def test_missing_key_fails_before_writing(self):
        palette = self.home / "bad.conf"
        palette.write_text("primary=#ffffff\n")
        stage = self.home / "stage"
        stage.mkdir()

        with self.assertRaises(self.v.VNatureError):
            self.v.render_apps(palette, "accent", stage)

        self.assertEqual(list(self.v.GENERATED.iterdir()), [])
        self.assertFalse((self.home / ".config/kitty").exists())

    def test_render_does_not_touch_real_paths(self):
        stage = self.home / "stage"
        stage.mkdir()
        for mode in ("accent", "balanced", "full"):
            rendered = self.v.render_apps(self.full_palette(), mode, stage)
            for staged, _, _ in rendered.values():
                self.assertNotIn("{{", staged.read_text())

        self.assertEqual(list(self.v.GENERATED.iterdir()), [])

    def test_install_skips_missing_app_dirs(self):
        (self.home / ".config/kitty").mkdir(parents=True)
        stage = self.home / "stage"
        stage.mkdir()
        rendered = self.v.render_apps(self.full_palette(), "accent", stage)

        polybar, kitty = self.v.install_apps(rendered)

        self.assertTrue(kitty)
        self.assertTrue(self.v.KITTY_THEME.is_file())
        self.assertTrue(polybar)
        self.assertTrue(self.v.POLYBAR_GENERATED.is_file())
        for app in ("hypr", "waybar", "swaync"):
            self.assertFalse((self.home / ".config" / app).exists(), app)

    def test_failed_commit_removes_stage(self):
        palette = self.home / "bad.conf"
        palette.write_text("primary=#ffffff\n")
        stage = self.v.build_stage(palette, name="x", wallpaper=None, kitty_mode="accent")

        with self.assertRaises(self.v.VNatureError):
            self.v.commit_stage(stage, self.v.load_config())

        self.assertFalse(stage.exists())
        self.assertFalse(self.v.CURRENT.exists())


class ArgParsing(Isolated):

    def setUp(self):
        super().setUp()
        (self.v.PRESETS / "autumn").mkdir()
        (self.v.PRESETS / "autumn" / "palette.conf").write_text("x=1\n")
        self.image = self.home / "w.jpg"
        self.image.write_bytes(b"")

    def parse(self, line):
        return self.v.parse_args(line.split())

    def assert_exit_2(self, line):
        with self.assertRaises(SystemExit) as caught, \
                contextlib.redirect_stderr(io.StringIO()):
            self.parse(line)
        self.assertEqual(caught.exception.code, 2, line)

    def test_eblo_any_position_and_legacy_forms(self):
        for line in (
            "apply autumn --eblo",
            "apply --eblo autumn",
            "--eblo apply autumn",
            "apply autumn --wallpaper",
            "--eblo autumn",
            "autumn --eblo",
        ):
            with self.subTest(line=line):
                args = self.parse(line)
                self.assertEqual((args.command, args.preset, args.eblo),
                                 ("apply", "autumn", True))

        args = self.parse("autumn")
        self.assertEqual((args.command, args.preset, args.eblo), ("apply", "autumn", False))

    def test_aliases(self):
        self.assertEqual(self.parse("kto").command, "list")
        self.assertEqual(self.parse("cho").command, "status")
        self.assertEqual(self.parse("govno").command, "undo")
        args = self.parse("nahui autumn")
        self.assertEqual((args.command, args.name), ("delete", "autumn"))
        args = self.parse("chotko new --eblo")
        self.assertEqual((args.command, args.name, args.eblo), ("save", "new", True))
        args = self.parse("save new")
        self.assertEqual((args.command, args.name, args.eblo), ("save", "new", False))
        self.assertIsNone(self.parse("").command)

    def test_image_forms(self):
        for line, first in (
            (f"--poh {self.image}", True),
            (f"{self.image} --poh", True),
            (f"image {self.image} --first-color", True),
            (f"{self.image}", False),
            ("$HOME/w.jpg", False),
            ("~/w.jpg", False),
        ):
            with self.subTest(line=line):
                args = self.parse(line)
                self.assertEqual((args.command, args.first_color), ("image", first))

    def test_global_flags_any_position(self):
        for line in ("--dry-run apply autumn", "apply autumn --dry-run", "autumn --dry-run"):
            with self.subTest(line=line):
                self.assertTrue(self.parse(line).dry_run)
        self.assertFalse(self.parse("apply autumn").dry_run)

        for line in ("--wallpaper-dir /x kto", "kto --wallpaper-dir /x"):
            with self.subTest(line=line):
                args = self.parse(line)
                self.assertEqual((args.command, args.wallpaper_dir), ("list", "/x"))

    def test_errors_exit_2(self):
        for line in (
            "apply autumn --bogus",
            "list extra",
            "kto --eblo",
            "undo --first-color",
            "apply",
            "nonexistent-thing",
            "--wall /x kto",
            "--eblo",
        ):
            with self.subTest(line=line):
                self.assert_exit_2(line)


class WallpaperDir(Isolated):

    def test_priority(self):
        self.v.CONFIG_FILE.unlink(missing_ok=True)
        self.v.CONFIG_FILE.write_text('mode = "dark"\n')
        self.assertEqual(self.v.load_config()["wallpaper_dir"], self.home / "Pictures/wp")

        self.v.CONFIG_FILE.write_text('wallpaper_dir = "$HOME/cfg"\n')
        self.assertEqual(self.v.load_config()["wallpaper_dir"], self.home / "cfg")

        self.assertEqual(self.v.load_config("~/flag")["wallpaper_dir"], self.home / "flag")

    def test_default_when_config_missing(self):
        self.v.CONFIG_FILE.unlink(missing_ok=True)
        self.assertEqual(self.v.load_config()["wallpaper_dir"], self.home / "Pictures/wp")

    def test_missing_explicit_dir_is_error(self):
        with self.assertRaises(self.v.VNatureError) as caught:
            self.v.main(["--wallpaper-dir", str(self.home / "nope"), "kto"])
        self.assertIn("нет папки обоев", str(caught.exception))

    def test_images_errors_and_filter(self):
        folder = self.home / "wp"
        with self.assertRaises(self.v.VNatureError):
            self.v.wallpaper_images(folder)

        folder.mkdir()
        (folder / "notes.txt").write_text("")
        with self.assertRaises(self.v.VNatureError):
            self.v.wallpaper_images(folder)

        (folder / "b.PNG").write_bytes(b"")
        (folder / "a.jpg").write_bytes(b"")
        self.assertEqual([p.name for p in self.v.wallpaper_images(folder)], ["a.jpg", "b.PNG"])


class ConfigKeys(Isolated):

    def test_set_key_keeps_comments_and_foreign_keys(self):
        self.v.CONFIG_FILE.write_text(
            "# мой конфиг\n"
            'wallpaper_dir = "~/Pictures/wp"  # старый\n'
            "custom = 42\n"
            'kitty_mode = "accent"\n'
            "\n"
            "[extra]\n"
            'kitty_mode = "чужой"\n'
        )

        self.v.set_config_key("kitty_mode", "full")
        self.v.set_config_key("wallpaper_dir", "~/new")
        self.v.set_config_key("mode", "light")

        text = self.v.CONFIG_FILE.read_text()
        self.assertIn("# мой конфиг\n", text)
        self.assertIn("custom = 42\n", text)
        self.assertIn('[extra]\nkitty_mode = "чужой"\n', text)

        data = self.v.tomllib.loads(text)
        self.assertEqual(data["kitty_mode"], "full")
        self.assertEqual(data["wallpaper_dir"], "~/new")
        self.assertEqual(data["mode"], "light")
        self.assertEqual(data["extra"], {"kitty_mode": "чужой"})

    def test_dry_run_does_not_write(self):
        self.v.ensure_config()
        before = self.v.CONFIG_FILE.read_text()
        self.v.DRY_RUN = True
        with contextlib.redirect_stdout(io.StringIO()):
            self.v.set_config_key("kitty_mode", "full")
        self.assertEqual(self.v.CONFIG_FILE.read_text(), before)


REAL_PRESETS = SCRIPT.parent / "presets"

# Сектора оттенка (градусы) для базовых ANSI красного, зелёного и синего.
SECTORS = {
    "color1": (330, 20),
    "color9": (330, 20),
    "color2": (75, 165),
    "color4": (185, 250),
    "color12": (185, 250),
}


def in_sector(hue, sector):
    low, high = sector
    return low <= hue <= high if low < high else hue >= low or hue <= high


class KittyColors(Isolated):

    def palettes(self):
        """Палитры пресетов (если есть локально) + синтетические крайние случаи."""
        keys = self.v.read_palette(self.v.TEMPLATES / "palette.conf")

        def synthetic(default, **roles):
            return {key: roles.get(key, default) for key in keys}

        result = {
            "flat-336699": synthetic("#336699"),
            "grey": synthetic("#777777", background="#202020", foreground="#dddddd"),
            "light": synthetic("#6d5e0f", background="#fff8f2", foreground="#1f1b16",
                               surface_container_high="#f0e6dd", surface_container_low="#fbf2ea",
                               primary_container="#f8e287", foreground_muted="#4d4639"),
            "neon": synthetic("#ff00ff", background="#000000", foreground="#00ff00"),
        }

        if REAL_PRESETS.is_dir():
            for directory in sorted(REAL_PRESETS.iterdir()):
                palette = directory / "palette.conf"
                if palette.is_file():
                    result[directory.name] = self.v.read_palette(palette)

        return result

    def hue(self, value):
        return self.v.to_hls(value)[0] * 360

    def each(self):
        for name, palette in self.palettes().items():
            for mode, (_, params) in self.v.KITTY_MODES.items():
                if params is not None:
                    yield name, mode, palette, self.v.kitty_colors(palette, mode)

    def test_contrast_and_color0(self):
        count = 0
        for name, mode, _, colors in self.each():
            with self.subTest(palette=name, mode=mode):
                bg = colors["kitty_background"]
                self.assertGreaterEqual(self.v.contrast(colors["kitty_foreground"], bg), 4.5)
                self.assertNotEqual(colors["kitty_color0"], bg)
                self.assertGreaterEqual(self.v.contrast(colors["kitty_color0"], bg), 1.25)
                self.assertGreaterEqual(
                    self.v.contrast(colors["kitty_selection_fg"], colors["kitty_selection_bg"]), 4.5)
                count += 1
        self.assertGreaterEqual(count, 4 * 3)

    def test_ansi_hue_kept(self):
        stable = self.v.STABLE
        for name, mode, _, colors in self.each():
            for key in self.v.ANSI_ROLES:
                with self.subTest(palette=name, mode=mode, slot=key):
                    delta = abs(self.hue(colors["kitty_" + key]) - self.hue(stable[key])) % 360
                    self.assertLessEqual(min(delta, 360 - delta), 10)
            for key, sector in SECTORS.items():
                with self.subTest(palette=name, mode=mode, slot=key):
                    self.assertTrue(in_sector(self.hue(colors["kitty_" + key]), sector),
                                    f"{key}={colors['kitty_' + key]}")

    def test_accent_is_minimal_and_full_is_strong(self):
        stable = self.v.STABLE
        for name, palette in self.palettes().items():
            with self.subTest(palette=name):
                accent = self.v.kitty_colors(palette, "accent")
                for key in (*self.v.ANSI_ROLES, "color7", "color8", "color15"):
                    self.assertEqual(accent["kitty_" + key], stable[key], key)
                shift = max(abs(a - b) for a, b in zip(
                    self.v.hex_rgb(accent["kitty_background"]), self.v.hex_rgb(stable["background"])))
                self.assertLessEqual(shift, 16)

                full = self.v.kitty_colors(palette, "full")
                self.assertEqual(full["kitty_background"], palette["background"])

    def test_off_is_pure_base(self):
        stable = self.v.STABLE
        for name, palette in self.palettes().items():
            with self.subTest(palette=name):
                off = self.v.kitty_colors(palette, "off")
                self.assertEqual(off["kitty_background"], stable["background"])
                self.assertEqual(off["kitty_foreground"], stable["foreground"])
                for n in range(16):
                    self.assertEqual(off[f"kitty_color{n}"], stable[f"color{n}"])
                palette_colors = {v.lower() for v in palette.values()}
                self.assertFalse(set(off.values()) & palette_colors - set(stable.values()))

    def test_unknown_mode_is_error(self):
        with self.assertRaises(self.v.VNatureError):
            self.v.kitty_colors(self.palettes()["grey"], "bogus")

    def test_render_all_presets_all_modes(self):
        stage = self.home / "stage"
        stage.mkdir()
        palettes = self.palettes()
        for name in palettes:
            path = self.home / f"{name}.conf"
            path.write_text("".join(f"{k}={v}\n" for k, v in palettes[name].items()))
            for mode in self.v.KITTY_MODES:
                with self.subTest(palette=name, mode=mode):
                    rendered = self.v.render_apps(path, mode, stage)
                    self.assertEqual(len(rendered), 7)
                    for staged, _, _ in rendered.values():
                        self.assertNotIn("{{", staged.read_text())


class KittyRefresh(Isolated):

    def setUp(self):
        super().setUp()
        (self.home / ".config/kitty").mkdir(parents=True)
        self.cfg = self.v.load_config()  # kitty_mode = accent
        for name, mode in (("own", "full"), ("inh", "inherit"), ("other", "inherit")):
            (self.v.PRESETS / name).mkdir()
            (self.v.PRESETS / name / "palette.conf").write_text(self.full_palette().read_text())
            self.v.save_preset_meta(name, kitty_mode=mode)

        # previous — другая тема; refresh не должен её трогать
        self.v.PREVIOUS.mkdir(parents=True)
        self.v.write_state(self.v.PREVIOUS, name="other", wallpaper=None, kitty_mode="accent")
        self.previous = (self.v.PREVIOUS / "state.toml").read_text()

    def current(self, name, kitty_mode, wallpaper=None):
        self.v.CURRENT.mkdir(parents=True, exist_ok=True)
        (self.v.CURRENT / "palette.conf").write_text(self.full_palette().read_text())
        self.v.write_state(self.v.CURRENT, name=name, wallpaper=wallpaper, kitty_mode=kitty_mode)

    def state_mode(self):
        return self.v.read_state(self.v.CURRENT)["kitty_mode"]

    def assert_applied(self, message, mode, title):
        self.assertEqual(message, f"kitty {mode} применён к теме {title}")
        self.assertEqual(self.state_mode(), mode)
        self.assertTrue(self.v.KITTY_THEME.is_file())
        self.assertEqual(self.calls, ["reload_kitty"])
        # только kitty: ни generated, ни других приложений, previous цел
        self.assertEqual(list(self.v.GENERATED.iterdir()), [])
        self.assertFalse((self.home / ".config/hypr").exists())
        self.assertEqual((self.v.PREVIOUS / "state.toml").read_text(), self.previous)

    def assert_untouched(self, message, mode):
        self.assertEqual(message, "сохранено, применится при следующей теме")
        self.assertEqual(self.state_mode(), mode)
        self.assertFalse(self.v.KITTY_THEME.exists())
        self.assertEqual(self.calls, [])

    def test_global_mode_with_inherit_preset(self):
        self.current("inh", "accent")
        self.cfg["kitty_mode"] = "balanced"
        self.assert_applied(self.v.refresh_kitty(self.cfg), "balanced", "inh")

    def test_global_mode_with_image_theme(self):
        wallpaper = self.home / "w.jpg"
        wallpaper.write_bytes(b"")
        self.current("__temporary__", "accent", wallpaper)
        self.cfg["kitty_mode"] = "full"
        self.assert_applied(self.v.refresh_kitty(self.cfg), "full", "из картинки w.jpg")

    def test_global_mode_with_own_mode_preset_untouched(self):
        self.current("own", "full")
        self.cfg["kitty_mode"] = "balanced"
        self.assert_untouched(self.v.refresh_kitty(self.cfg), "full")

    def test_preset_mode_applied_preset(self):
        self.current("own", "full")
        self.v.save_preset_meta("own", kitty_mode="balanced")
        self.assert_applied(self.v.refresh_kitty(self.cfg, {"own": "balanced"}), "balanced", "own")

    def test_preset_mode_to_inherit_uses_global(self):
        self.current("own", "full")
        self.assert_applied(self.v.refresh_kitty(self.cfg, {"own": "inherit"}), "accent", "own")

    def test_preset_mode_not_applied_preset(self):
        self.current("inh", "accent")
        self.v.save_preset_meta("own", kitty_mode="balanced")
        self.assert_untouched(self.v.refresh_kitty(self.cfg, {"own": "balanced"}), "accent")

    def test_off_writes_pure_base(self):
        self.current("inh", "accent")
        self.cfg["kitty_mode"] = "off"
        self.assert_applied(self.v.refresh_kitty(self.cfg), "off", "inh")
        text = self.v.KITTY_THEME.read_text()
        self.assertIn(f"background            {self.v.STABLE['background']}\n", text)
        self.assertIn(f"color1                {self.v.STABLE['color1']}\n", text)
        self.assertNotIn("#336699", text)

    def test_dry_run_only_prints(self):
        self.current("inh", "accent")
        self.cfg["kitty_mode"] = "full"
        self.v.DRY_RUN = True
        with contextlib.redirect_stdout(io.StringIO()) as out:
            message = self.v.refresh_kitty(self.cfg)
        self.assertEqual(message, "dry-run: применил бы kitty full к теме inh")
        self.assertIn("записал бы", out.getvalue())
        self.assertEqual(self.state_mode(), "accent")
        self.assertFalse(self.v.KITTY_THEME.exists())
        self.assertEqual(self.calls, [])

    def test_no_theme_or_unknown_preset(self):
        self.assertFalse(self.v.CURRENT.exists())
        self.assertEqual(self.v.refresh_kitty(self.cfg), "сохранено, применится при следующей теме")
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.v.current_kitty_target(
            {"name": "deleted", "kitty_mode": "full", "wallpaper": None}, self.cfg))

    def test_kitty_missing_updates_state_without_reload(self):
        (self.home / ".config/kitty").rmdir()
        self.current("inh", "accent")
        self.cfg["kitty_mode"] = "full"
        with contextlib.redirect_stdout(io.StringIO()):
            message = self.v.refresh_kitty(self.cfg)
        self.assertIn("kitty не найден", message)
        self.assertEqual(self.state_mode(), "full")
        self.assertEqual(self.calls, [])


class KittyBase(Isolated):

    def test_default_is_stable(self):
        self.assertIs(self.v.kitty_base({"kitty_base": None}), self.v.STABLE)

    def test_override_and_fallback(self):
        theme = self.v.CONFIG_DIR / "my.conf"
        theme.write_text("# тема\nbackground #000000\ncolor1 #FF0000\nfont_size 10\n")
        base = self.v.kitty_base({"kitty_base": "my.conf"})
        self.assertEqual(base["background"], "#000000")
        self.assertEqual(base["color1"], "#ff0000")
        self.assertEqual(base["color2"], self.v.STABLE["color2"])

    def test_bad_color_and_missing_file(self):
        theme = self.home / "bad.conf"
        theme.write_text("color1 red\n")
        with self.assertRaises(self.v.VNatureError):
            self.v.kitty_base({"kitty_base": str(theme)})
        with self.assertRaises(self.v.VNatureError):
            self.v.kitty_base({"kitty_base": "~/nope.conf"})


class RussianArgparse(Isolated):

    def run_cli(self, line):
        out, err = io.StringIO(), io.StringIO()
        with self.assertRaises(SystemExit) as caught, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.v.parse_args(line.split())
        return caught.exception.code, out.getvalue() + err.getvalue()

    def test_errors(self):
        for line, expected in (
            ("--wall /x kto", "неизвестный флаг: --wall"),
            ("nonexistent-thing", "не понял: nonexistent-thing"),
            ("apply", "не хватает аргумента: PRESET"),
            ("kto extra", "лишние аргументы: extra"),
            ("--eblo", "--eblo работает только с apply или save"),
            ("--wallpaper-dir", "флагу --wallpaper-dir нужно значение"),
        ):
            with self.subTest(line=line):
                code, text = self.run_cli(line)
                self.assertEqual(code, 2)
                self.assertIn(expected, text)
                self.assertIn("справка: vnature --help", text)

    def test_help_is_russian(self):
        for line in ("--help", "apply -h"):
            with self.subTest(line=line):
                code, text = self.run_cli(line)
                self.assertEqual(code, 0)
                self.assertIn("использование:", text)
                self.assertIn("показать эту справку", text)
                self.assertNotIn("show this help", text)
                self.assertNotIn("usage:", text)
        self.assertIn("показать версию", self.run_cli("--help")[1])
        self.assertIn("undo (govno)", self.run_cli("--help")[1])


if __name__ == "__main__":
    unittest.main()
