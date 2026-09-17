"""Run with PYTHONPATH=src python -m unittest discover -s tests."""

import copy
import itertools
import unittest

from PIL import Image, ImageChops

from hongtai_screen_app.themes import dashboard_theme as dashboard
from hongtai_screen_app.themes import widget_styles as gothic


class WidgetStyleTests(unittest.TestCase):
    def test_inheritance_preserves_saved_data_and_explicit_overrides(self):
        el = {"id": "test", "type": "text", "color": None, "font": None}
        original = copy.deepcopy(el)
        resolved = gothic.resolve_element(el, {"widget_style": "gothic"})
        self.assertEqual(el, original)
        self.assertEqual(resolved["font"], "cinzel")
        self.assertEqual(resolved["color"], gothic.STYLES["gothic"]["defaults"]["text_color"])
        override = {**el, "font": "default", "color": [1, 2, 3]}
        resolved = gothic.resolve_element(override, {"widget_style": "gothic"})
        self.assertEqual(resolved["font"], "default")
        self.assertEqual(resolved["color"], [1, 2, 3])
        opted_out = {**el, "widget_style": "default"}
        self.assertIs(gothic.resolve_element(opted_out, {"widget_style": "gothic"}), opted_out)
        self.assertIs(gothic.resolve_element(el, {}), el)

    def test_default_style_keeps_legacy_render_identical(self):
        preset = dashboard.BUILTIN_DASHBOARD_PRESETS["Neon Horizon"]
        fonts = dashboard.Fonts()
        original, layout = dashboard.build_static_background(960, 480, fonts, preset["elements"], preset["background"])
        explicit, other = dashboard.build_static_background(960, 480, fonts, preset["elements"],
            {**preset["background"], "widget_style": "default"})
        self.assertIsNone(ImageChops.difference(original, explicit).getbbox())
        self.assertEqual(layout["elements"], other["elements"])

    def test_meter_bounds_clamping_and_missing_value(self):
        el = gothic.resolve_element({"type": "bar", "widget_style": "gothic"})
        empty = gothic.meter_tile(200, 9, None, el)
        self.assertEqual(empty.tobytes(), gothic.meter_tile(200, 9, 0, el).tobytes())
        full = gothic.meter_tile(200, 9, 1, el)
        self.assertEqual(full.tobytes(), gothic.meter_tile(200, 9, 2, el).tobytes())
        self.assertNotEqual(empty.tobytes(), full.tobytes())
        box = {"cx": 150, "cy": 150, "x0": 144, "y0": 50, "w": 12, "h": 200}
        el.update(orientation="vertical", show_value=False, show_title=False)
        img = Image.new("RGB", (300, 300))
        gothic.draw_bar(img, el, box, 70, dashboard.STAT_DEFS["ram"], dashboard._cached_scaled_font)
        bounds = img.getbbox()
        self.assertTrue(144 <= bounds[0] < bounds[2] <= 156)
        self.assertTrue(50 <= bounds[1] < bounds[3] <= 250)

    def test_missing_gauge_has_no_live_needle(self):
        el = gothic.resolve_element({"type": "gauge", "stat": "cpu_load", "widget_style": "gothic"})
        g = dashboard.gauge_layout(150, 150, 65)
        images = []
        for value in (None, 0, 50, 100):
            img = Image.new("RGB", (300, 300))
            gothic.draw_gauge_dynamic(img, el, g, value, dashboard.STAT_DEFS["cpu_load"], dashboard._cached_scaled_font)
            images.append(img.tobytes())
        self.assertEqual(len(set(images)), 4)
        el["opacity"] = 0
        transparent = Image.new("RGB", (300, 300))
        gothic.draw_gauge_dynamic(transparent, el, g, 50, dashboard.STAT_DEFS["cpu_load"], dashboard._cached_scaled_font)
        self.assertIsNone(transparent.getbbox())

    def test_media_content_stays_inside_its_box(self):
        base = {"type": "media", "widget_style": "gothic"}
        long_track = {"title": "A very long song title "*12, "artist": "A very long artist "*12,
                      "duration": 246, "position": 83, "art": Image.new("RGB", (150, 200), (80, 25, 40))}
        for style, (width, height) in itertools.product(gothic.STYLES, ((302, 283), (80, 60))):
            box = {"x0": 20, "y0": 20, "w": width, "h": height}
            for flags in itertools.product((True, False), repeat=3):
                el = gothic.resolve_element({**base, "widget_style": style,
                    **dict(zip(("show_art", "show_name", "show_time"), flags))})
                for media in (None, long_track, {"title": "No duration", "duration": None}):
                    with self.subTest(style=style, size=(width, height), flags=flags, media=bool(media)):
                        img = Image.new("RGB", (400, 400))
                        gothic.draw_media(img, el, box, media, dashboard._cached_scaled_font, None, "Awaiting Spotify")
                        bounds = img.getbbox()
                        if bounds:
                            self.assertTrue(20 <= bounds[0] < bounds[2] <= 20+width)
                            self.assertTrue(20 <= bounds[1] < bounds[3] <= 20+height)

    def test_presets_use_bundled_fonts_and_render_without_data(self):
        for name in ("Nocturne Cathedral", "Neon Ronin", "Arcane Observatory"):
            with self.subTest(name=name):
                p = dashboard.BUILTIN_DASHBOARD_PRESETS[name]
                original = copy.deepcopy(p)
                fonts = dashboard.Fonts()
                bg, layout = dashboard.build_static_background(960, 480, fonts, p["elements"], p["background"])
                frame = dashboard.render_frame(bg, layout, 960, 480, fonts, {}, None)
                self.assertEqual(frame.size, (960, 480))
                self.assertEqual(p, original)
                self.assertEqual(len({el["id"] for el in p["elements"]}), len(p["elements"]))
        self.assertEqual(dashboard.load_font(24, family="cinzel").getname()[0], "Cinzel")
        self.assertEqual(dashboard.load_font(24, family="unifraktur").getname()[0], "UnifrakturCook")


if __name__ == "__main__":
    unittest.main()
