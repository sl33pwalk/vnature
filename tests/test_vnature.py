import contextlib
import hashlib
import importlib
import io
import os
import struct
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "vnature"
sys.path.insert(0, str(SCRIPT.parent))


class Isolated(unittest.TestCase):
    """Каждый тест грузит vnature заново с временным HOME и presets/."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)

        for name in [name for name in sys.modules if name.split(".")[0] == "vnature_pkg"]:
            del sys.modules[name]
        self.v = importlib.import_module("vnature_pkg")

        self.v.PRESETS = self.home / "presets"
        self.v.PRESETS.mkdir()
        self.v.ensure_dirs()

        # Никаких сигналов настоящему kitty и вызовов hyprctl/swaync из тестов.
        self.calls = []
        self.real = {}
        for name in ("reload_kitty", "reload_hyprland", "reload_swaync",
                     "reload_polybar", "set_wallpaper"):
            self.real[name] = getattr(self.v, name)
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


# sha256[:16] kitty.conf, снятые со старой реализации режимов (до шкалы силы)
# на синтетических палитрах KittyColors.palettes(): старые режимы не должны меняться.
GOLDEN_KITTY = {
    "flat-336699/accent": "21c5e6b2a54aa6c8",
    "flat-336699/balanced": "7af6d17d8353151f",
    "flat-336699/full": "7b15a126b53202ed",
    "flat-336699/off": "558b0d24238a802c",
    "grey/accent": "0508fcb2e9e749de",
    "grey/balanced": "8a5dcb5768102412",
    "grey/full": "6bc40e3c702f9f57",
    "grey/off": "558b0d24238a802c",
    "light/accent": "df6c2195348adb6d",
    "light/balanced": "6c5eb6a0b80a7acb",
    "light/full": "d1f5156abb289f3f",
    "light/off": "558b0d24238a802c",
    "neon/accent": "7ca134d4fd32e74d",
    "neon/balanced": "f6be17ecf3a03525",
    "neon/full": "40302cb12dff3886",
    "neon/off": "558b0d24238a802c",
}


class KittyStrength(Isolated):

    palettes = KittyColors.palettes

    def test_parse(self):
        ok = {"accent": 15, "balanced": 50, "full": 100, "off": "off",
              0: 0, 35: 35, 100: 100, "35": 35, "35%": 35, " 70 ": 70}
        for value, expected in ok.items():
            with self.subTest(value=value):
                self.assertEqual(self.v.kitty_strength(value), expected)
        for value in (101, -5, "101", "abc", "", True, 3.5, None, "inherit"):
            with self.subTest(value=value):
                self.assertIsNone(self.v.kitty_strength(value))

    def test_storage_value_and_label(self):
        self.assertEqual(self.v.kitty_mode_value(15), "accent")
        self.assertEqual(self.v.kitty_mode_value(35), 35)
        self.assertEqual(self.v.kitty_mode_value("off"), "off")
        self.assertEqual(self.v.normalize_kitty_mode("50", "accent"), "balanced")
        self.assertEqual(self.v.normalize_kitty_mode("мусор", "accent"), "accent")
        self.assertEqual(self.v.kitty_label("balanced"), "50% (balanced)")
        self.assertEqual(self.v.kitty_label(35), "35%")
        self.assertEqual(self.v.kitty_label("off"), "off")
        self.assertEqual(self.v.kitty_label("inherit"), "inherit")

    def test_curve(self):
        self.assertEqual(self.v.kitty_params(15), (0.06, 0.0))
        self.assertEqual(self.v.kitty_params(50), (0.35, 0.4))
        self.assertEqual(self.v.kitty_params(100), (1.0, 0.8))
        self.assertEqual(self.v.kitty_params(0), (0.0, 0.0))
        self.assertIsNone(self.v.kitty_params("off"))
        values = [self.v.kitty_params(s) for s in range(0, 101, 5)]
        for (s1, a1), (s2, a2) in zip(values, values[1:]):
            self.assertLessEqual(s1, s2)
            self.assertLessEqual(a1, a2)

    def render(self, palette, mode):
        text = self.v.render_template(self.v.TEMPLATES / "kitty.conf",
                                      {**palette, **self.v.kitty_colors(palette, mode)})
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def test_old_modes_byte_identical(self):
        palettes = self.palettes()
        for key, expected in GOLDEN_KITTY.items():
            name, mode = key.split("/")
            with self.subTest(key=key):
                self.assertEqual(self.render(palettes[name], mode), expected)
                if mode != "off":
                    point = self.v.KITTY_MODES[mode][1]
                    self.assertEqual(self.render(palettes[name], point), expected)

    def test_numbers_in_config_preset_state(self):
        self.v.CONFIG_FILE.write_text("kitty_mode = 35\n")
        self.assertEqual(self.v.load_config()["kitty_mode"], 35)
        self.v.CONFIG_FILE.write_text('kitty_mode = "50"\n')
        self.assertEqual(self.v.load_config()["kitty_mode"], "balanced")
        self.v.CONFIG_FILE.write_text("kitty_mode = 500\n")
        self.assertEqual(self.v.load_config()["kitty_mode"], "accent")

        self.v.set_config_key("kitty_mode", 70)
        self.assertIn("kitty_mode = 70\n", self.v.CONFIG_FILE.read_text())

        (self.v.PRESETS / "p").mkdir()
        (self.v.PRESETS / "p" / "palette.conf").write_text("x=1\n")
        self.v.save_preset_meta("p", kitty_mode=35)
        self.assertIn("kitty_mode = 35\n", (self.v.PRESETS / "p" / "preset.toml").read_text())
        self.assertEqual(self.v.load_preset("p")["kitty_mode"], 35)
        self.v.save_preset_meta("p", kitty_mode=100)
        self.assertEqual(self.v.load_preset("p")["kitty_mode"], "full")
        with self.assertRaises(self.v.VNatureError):
            self.v.save_preset_meta("p", kitty_mode=101)

        self.v.write_state(self.home, name="x", wallpaper=None, kitty_mode=35)
        self.assertEqual(self.v.read_state(self.home)["kitty_mode"], 35)


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


class TuiPieces(Isolated):

    def type_keys(self, keys, text="", cursor=None):
        cursor = len(text) if cursor is None else cursor
        result = None
        for key in keys:
            text, cursor, result = self.v.edit_line(text, cursor, key)
        return text, cursor, result

    def test_edit_line_cyrillic_and_editing(self):
        curses = self.v.curses
        self.assertEqual(self.type_keys(list("привет") + ["\n"]), ("привет", 6, "ok"))
        self.assertEqual(self.type_keys(["\x7f", "\x7f", "ё", "\n"], "тема"), ("теё", 3, "ok"))
        self.assertEqual(self.type_keys([curses.KEY_LEFT, curses.KEY_LEFT, "ж"], "абв"), ("ажбв", 2, None))
        self.assertEqual(self.type_keys([curses.KEY_HOME, curses.KEY_DC], "абв"), ("бв", 0, None))
        self.assertEqual(self.type_keys(["\x15", "x"], "мусор"), ("x", 1, None))
        self.assertEqual(self.type_keys(["\x1b"], "a")[2], "cancel")
        self.assertEqual(self.type_keys([curses.KEY_BACKSPACE], "")[0:2], ("", 0))
        self.assertEqual(self.type_keys(["\x07"], "a")[0], "a")  # непечатное игнорируем

    def test_scroll_top(self):
        self.assertEqual(self.v.scroll_top(0, 0, 5), 0)
        self.assertEqual(self.v.scroll_top(7, 0, 5), 3)
        self.assertEqual(self.v.scroll_top(2, 3, 5), 2)
        self.assertEqual(self.v.scroll_top(4, 3, 5), 3)

    def test_key_names(self):
        curses = self.v.curses
        self.assertEqual(self.v.key_name("\n"), "Enter")
        self.assertEqual(self.v.key_name(curses.KEY_ENTER), "Enter")
        self.assertEqual(self.v.key_name("\x1b"), "Esc")
        self.assertEqual(self.v.key_name(curses.KEY_SRIGHT), "S-→")
        self.assertEqual(self.v.key_name("ж"), "ж")
        self.assertIsNone(self.v.key_name(curses.KEY_F1))

    def test_nearest256(self):
        n = self.v.nearest256
        self.assertEqual(n("#000000"), 16)
        self.assertEqual(n("#ffffff"), 231)
        self.assertEqual(n("#ff0000"), 196)
        self.assertEqual(n("#5f87af"), 67)
        self.assertEqual(n("#808080"), 244)
        self.assertEqual(n("#7f7f7f"), 244)

    def test_keymap_has_no_conflicts_and_covers_actions(self):
        keymap = self.v.KEYMAP
        common = {k for keys, _ in keymap["общие"] for k in keys}
        tui = self.v.TUI.__new__(self.v.TUI)
        for context, entries in keymap.items():
            if context == "общие":
                continue
            keys = [k for group, _ in entries for k in group]
            with self.subTest(context=context):
                self.assertEqual(len(keys), len(set(keys)), "дубли внутри раздела")
                self.assertFalse(set(keys) & common, "пересечение с общими клавишами")
                self.assertLessEqual(set(tui.actions(context)), set(keys), "действие без строки в справке")

    def test_slide(self):
        slide = self.v.slide
        cases = [
            ((50, "→", 0), 55), ((37, "←", 0), 35), ((37, "l", 0), 40), ((0, "h", 0), 0),
            ((100, "→", 0), 100), (("off", "→", 15), 20), (("inherit", "←", 50), 45),
            ((90, "S-→", 0), 100), ((10, "H", 0), 0), ((40, "L", 0), 65),
            ((70, "a", 0), 15), ((70, "b", 0), 50), ((70, "f", 0), 100), ((70, "o", 0), "off"),
            ((70, "x", 0), None), ((70, "j", 0), None),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                self.assertEqual(slide(*args), expected)
        self.assertIsNone(slide(70, "i", 0))
        self.assertEqual(slide(70, "i", 0, inherit=True), "inherit")

    def test_panels(self):
        self.assertEqual(self.v.panels(79)[2], None)
        x, left, right, right_width = self.v.panels(100)
        self.assertIsNotNone(right)
        self.assertGreaterEqual(left, 30)
        self.assertLessEqual(right + right_width, 100)
        self.assertEqual(self.v.panels(200)[1], 48)

    def test_image_size(self):
        png = self.home / "a.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", 6094, 2832) + b"\x08\x02")
        self.assertEqual(self.v.image_size(png), (6094, 2832))

        jpg = self.home / "a.jpg"
        app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
        sof = b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", 3648, 5472) + b"\x03" + b"\x00" * 9
        jpg.write_bytes(b"\xff\xd8" + app0 + sof + b"\xff\xd9")
        self.assertEqual(self.v.image_size(jpg), (5472, 3648))

        junk = self.home / "a.webp"
        junk.write_bytes(b"RIFF....WEBP")
        self.assertIsNone(self.v.image_size(junk))
        self.assertIsNone(self.v.image_size(self.home / "нет.png"))

    def test_tab_key(self):
        self.assertEqual(self.v.tab_key("1", 3), 0)
        self.assertEqual(self.v.tab_key("4", 0), 3)
        self.assertEqual(self.v.tab_key("Tab", 3), 0)
        self.assertEqual(self.v.tab_key("S-Tab", 0), 3)
        self.assertIsNone(self.v.tab_key("5", 0))
        self.assertIsNone(self.v.tab_key("q", 0))
        self.assertEqual(self.v.TABS, ("Обои", "Пресеты", "Kitty", "Настройки"))

    def test_messages_go_to_sink(self):
        got = []
        self.v.SINK = lambda text, error: got.append((text, error))
        try:
            self.v.info("раз")
            self.v.warn("два")
        finally:
            self.v.SINK = None
        self.assertEqual(got, [("раз", False), ("два", True)])


class KittyReload(Isolated):

    def test_skips_kitty_with_own_watcher(self):
        proc = self.home / "proc"
        def process(pid, comm, *argv):
            (proc / pid).mkdir(parents=True)
            (proc / pid / "comm").write_text(comm + "\n")
            (proc / pid / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        process("100", "kitty", "kitty")
        process("101", "kitty", "kitty")
        process("200", "kitten", "/usr/bin/kitten", "__watch_conf__", "100", "100", "/x/kitty.conf")
        process("300", "bash", "bash")
        (proc / "self").mkdir()

        sent = []
        real_kill = self.v.os.kill
        self.v.os.kill = lambda pid, sig: sent.append((pid, sig))
        try:
            self.real["reload_kitty"](proc)
        finally:
            self.v.os.kill = real_kill
        self.assertEqual(sent, [(101, self.v.signal.SIGUSR1)])


class TuiImage(Isolated):

    def tui(self):
        tui = self.v.TUI.__new__(self.v.TUI)
        tui.graphics = True
        tui.image_want = tui.image_shown = tui.image_job = tui.image_requested = None
        tui.image_since = 0.0
        tui.image_cache = {}
        tui.written = []
        tui.tty_write = tui.written.append
        return tui

    def fake_popen(self):
        test = self
        test.spawned, test.killed = [], []

        class FakePopen:
            returncode = 0
            def __init__(self, command, stdin, stdout, stderr):
                test.spawned.append(command)
                stdout.write(b"IMG")
                self.command = command
            def poll(self):
                return 0
            def kill(self):
                test.killed.append(self.command)
            def wait(self):
                return 0

        self.v.subprocess.Popen, real = FakePopen, self.v.subprocess.Popen
        self.addCleanup(setattr, self.v.subprocess, "Popen", real)

    def test_icat_command(self):
        command = self.v.icat_command("/w/a.jpg", 1, (50, 4, 40, 20), (100, 30, 1000, 600))
        self.assertEqual(command[:2], ["kitten", "icat"])
        self.assertIn("--transfer-mode=stream", command)
        self.assertIn("--use-window-size=100,30,1000,600", command)
        self.assertIn("--place=40x20@50x4", command)
        self.assertEqual(command[-1], "/w/a.jpg")

    def test_background_job_cache_and_cleanup(self):
        self.fake_popen()
        tui = self.tui()
        delete = self.v.KITTY_DELETE_IMAGES
        a = ("/w/a.jpg", 1, (50, 4, 40, 20), (100, 30, 1000, 600))
        b = ("/w/b.jpg", 1, (50, 4, 40, 20), (100, 30, 1000, 600))

        tui.image_want = a
        tui.sync_image()                      # пауза прошла — icat в фоне, в терминал пока ничего
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(tui.written, [])
        tui.sync_image()                      # icat закончил — показываем, кладём в кеш
        self.assertEqual(tui.written, [b"\x1b7IMG\x1b8"])
        self.assertEqual(tui.image_shown, a)

        tui.image_want, tui.image_since = b, self.v.time.monotonic()
        tui.sync_image()                      # другой выбор: старую картинку убрать, icat ещё не звать
        self.assertEqual(tui.written[-1], delete)
        self.assertEqual(len(self.spawned), 1)

        tui.image_want = a
        tui.sync_image()                      # снова a — из кеша, без icat
        self.assertEqual(tui.written[-1], b"\x1b7IMG\x1b8")
        self.assertEqual(len(self.spawned), 1)

        tui.image_want = None
        tui.sync_image()
        self.assertEqual(tui.written[-1], delete)
        self.assertIsNone(tui.image_shown)

    def test_debounce_survives_frames(self):
        # Регрессия: draw_chrome обнуляет image_want каждый кадр — пауза не должна перезапускаться.
        self.fake_popen()
        tui = self.tui()
        tui.put = tui.role = lambda *a, **k: 0
        image = self.home / "a.png"
        image.write_bytes(b"x")
        self.v.window_pixels = lambda: (100, 30, 1000, 600)
        tui.want_image(image, 5, 25, 50, 40)
        since = tui.image_since
        for _ in range(3):
            tui.image_want = None             # как draw_chrome на новом кадре
            tui.want_image(image, 5, 25, 50, 40)
        self.assertEqual(tui.image_since, since)
        tui.image_since -= 1
        tui.sync_image()
        self.assertEqual(len(self.spawned), 1)

    def test_overlay_hides_but_keeps_job(self):
        self.fake_popen()
        tui = self.tui()
        a = ("/w/a.jpg", 1, (0, 0, 20, 10), (1, 1, 1, 1))
        tui.image_want = a
        tui.sync_image()                      # icat запущен
        tui.image_want = None
        tui.sync_image()                      # открыли справку
        self.assertEqual(self.killed, [])
        self.assertIsNotNone(tui.image_job)
        tui.image_want = a
        tui.sync_image()                      # закрыли — результат готов, без второго icat
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(tui.image_shown, a)

    def test_stale_job_is_killed(self):
        self.fake_popen()
        tui = self.tui()
        tui.image_want = ("/w/c.jpg", 1, (0, 0, 20, 10), (1, 1, 1, 1))
        tui.sync_image()
        tui.image_want = ("/w/d.jpg", 1, (0, 0, 20, 10), (1, 1, 1, 1))
        tui.sync_image()
        self.assertEqual(len(self.killed), 1)
        self.assertEqual(self.killed[0][-1], "/w/c.jpg")

    def test_graphics_only_in_real_kitty(self):
        tui = self.v.TUI.__new__(self.v.TUI)
        self.v.window_pixels = lambda: (100, 30, 1000, 600)
        self.v.shutil.which, real_which = (lambda name: "/usr/bin/kitten"), self.v.shutil.which
        self.addCleanup(setattr, self.v.shutil, "which", real_which)
        cases = (
            ({"TERM": "xterm-kitty"}, True),
            ({"TERM": "xterm-256color", "KITTY_WINDOW_ID": "1"}, True),
            ({"TERM": "xterm-kitty", "TMUX": "/tmp/x"}, False),
            ({"TERM": "tmux-256color"}, False),
        )
        for env, expected in cases:
            with self.subTest(env=env), unittest.mock.patch.dict(os.environ, env, clear=True):
                tui.setup_graphics()
                self.assertEqual(tui.graphics, expected)
        self.v.window_pixels = lambda: None
        with unittest.mock.patch.dict(os.environ, {"TERM": "xterm-kitty"}, clear=True):
            tui.setup_graphics()
            self.assertFalse(tui.graphics)


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
