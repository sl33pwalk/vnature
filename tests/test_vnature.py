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
            self.v.commit_stage(stage)

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


if __name__ == "__main__":
    unittest.main()
