import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import patch_official_zh as patcher  # noqa: E402


def make_app(tmp: Path) -> Path:
    app = tmp / "Claude.app"
    assets = app / "Contents/Resources/ion-dist/assets/v1"
    assets.mkdir(parents=True)
    return app


class MergeTests(unittest.TestCase):
    def test_official_strings_replace_english(self):
        english = {"a": "Hello", "b": "World"}
        official = {"a": "你好", "extra": "多余"}
        merged, translated = patcher.merge_with_fallback(english, official)
        self.assertEqual(merged, {"a": "你好", "b": "World"})
        self.assertEqual(translated, 1)

    def test_missing_official_key_falls_back_to_english(self):
        merged, translated = patcher.merge_with_fallback({"a": "Hello"}, {})
        self.assertEqual(merged, {"a": "Hello"})
        self.assertEqual(translated, 0)

    def test_empty_official_value_falls_back(self):
        merged, translated = patcher.merge_with_fallback({"a": "Hello"}, {"a": ""})
        self.assertEqual(merged, {"a": "Hello"})
        self.assertEqual(translated, 0)


class WhitelistTests(unittest.TestCase):
    def test_registers_official_locale_in_every_chunk(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = make_app(Path(tmp))
            assets = app / "Contents/Resources/ion-dist/assets/v1"
            for name in ("shared-123.js", "shared-62.js"):
                (assets / name).write_text(
                    f"const a={patcher.BASE_LANGUAGE_LIST}];", encoding="utf-8"
                )

            patcher.patch_language_whitelist(app, "zh-Hans")

            for name in ("shared-123.js", "shared-62.js"):
                text = (assets / name).read_text(encoding="utf-8")
                self.assertIn('"zh-Hans"', text, name)

    def test_replaces_previous_chinese_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = make_app(Path(tmp))
            assets = app / "Contents/Resources/ion-dist/assets/v1"
            path = assets / "shared-123.js"
            path.write_text(
                f'const a={patcher.BASE_LANGUAGE_LIST},"zh-CN"];', encoding="utf-8"
            )

            patcher.patch_language_whitelist(app, "zh-Hans")

            text = path.read_text(encoding="utf-8")
            self.assertIn('"zh-Hans"', text)
            self.assertNotIn('"zh-CN"', text)

    def test_missing_whitelist_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = make_app(Path(tmp))
            (app / "Contents/Resources/ion-dist/assets/v1/index-abc.js").write_text(
                "// no whitelist here", encoding="utf-8"
            )
            with self.assertRaises(SystemExit):
                patcher.patch_language_whitelist(app, "zh-Hans")


class InstallTests(unittest.TestCase):
    def test_installs_merged_catalog_and_dynamic(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = make_app(Path(tmp))
            i18n = app / "Contents/Resources/ion-dist/i18n"
            (i18n / "dynamic").mkdir(parents=True)
            (i18n / "en-US.json").write_text(
                json.dumps({"a": "Hello", "b": "World"}), encoding="utf-8"
            )
            (i18n / "dynamic/en-US.json").write_text(
                json.dumps({"d": "Soon"}), encoding="utf-8"
            )

            catalogs = {
                "zh-Hans.json": {"a": "你好"},
                "dynamic/zh-Hans.json": {"d": "即将"},
            }

            def fake_fetch(locale, kind="", *, required=True):
                return catalogs[f"{kind + '/' if kind else ''}{locale}.json"]

            with mock.patch.object(patcher, "fetch_catalog", side_effect=fake_fetch):
                patcher.install_frontend_locale(app, "zh-Hans")

            merged = json.loads((i18n / "zh-Hans.json").read_text(encoding="utf-8"))
            self.assertEqual(merged, {"a": "你好", "b": "World"})
            dynamic = json.loads(
                (i18n / "dynamic/zh-Hans.json").read_text(encoding="utf-8")
            )
            self.assertEqual(dynamic, {"d": "即将"})

    def test_missing_dynamic_catalog_falls_back_to_english(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = make_app(Path(tmp))
            i18n = app / "Contents/Resources/ion-dist/i18n"
            (i18n / "dynamic").mkdir(parents=True)
            (i18n / "en-US.json").write_text(json.dumps({"a": "Hello"}), encoding="utf-8")
            (i18n / "dynamic/en-US.json").write_text(
                json.dumps({"d": "Soon"}), encoding="utf-8"
            )

            def fake_fetch(locale, kind="", *, required=True):
                return {"a": "你好"} if not kind else None

            with mock.patch.object(patcher, "fetch_catalog", side_effect=fake_fetch):
                patcher.install_frontend_locale(app, "zh-Hans")

            dynamic = json.loads(
                (i18n / "dynamic/zh-Hans.json").read_text(encoding="utf-8")
            )
            self.assertEqual(dynamic, {"d": "Soon"})


if __name__ == "__main__":
    unittest.main()
