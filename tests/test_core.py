import csv
import io
import unittest

import app


class CoreHelpersTest(unittest.TestCase):
    def test_parse_float_handles_polish_prices(self):
        self.assertEqual(app.parse_float("12 345,67 zł"), 12345.67)
        self.assertEqual(app.parse_float("1.234,50 EUR"), 1234.50)
        self.assertEqual(app.parse_float("1,234.50"), 1234.50)

    def test_stable_product_code_uses_sku_first(self):
        code, source = app.stable_product_code({"sku": "AB 12/34", "url": "https://example.test/p"}, "Produkt")
        self.assertEqual(code, "AB-12-34")
        self.assertEqual(source, "sku")

    def test_stable_product_code_is_repeatable_without_sku(self):
        item = {"source_domain": "example.test", "url": "https://example.test/p/123", "price": 1999}
        first, first_source = app.stable_product_code(item, "Testowy produkt")
        second, second_source = app.stable_product_code(item, "Testowy produkt")
        self.assertEqual(first, second)
        self.assertEqual(first_source, second_source)
        self.assertIn("EXAMPLE.TEST", first)

    def test_export_csv_can_emit_utf8_bom(self):
        result = {
            "product_code": "SKU-1",
            "name": "Ładowarka testowa",
            "category": "Części zamienne",
            "price": 100,
            "images": ["https://example.test/img.jpg"],
        }
        data = app.export_csv_bytes([result], encoding="UTF-8 BOM")
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig")), delimiter=";"))
        self.assertEqual(rows[0]["product_code"], "SKU-1")

    def test_shoper_validator_reports_missing_core_fields(self):
        errors, warnings = app.validate_shoper_results([{"name": "", "product_code": ""}])
        self.assertTrue(any("product_code" in error for error in errors))
        self.assertTrue(any("brak ceny" in warning for warning in warnings))

    def test_navigation_compares_full_url_not_only_domain(self):
        current = "https://shop.olekmotocykle.com/produkty/detki-tubliss-mousse,2,1530"
        requested = "https://shop.olekmotocykle.com/produkty/kaski-i-gogle/integralne,2,1428"
        self.assertTrue(app.should_navigate_to_requested_url(current, requested))
        self.assertFalse(app.should_navigate_to_requested_url(requested, requested))


if __name__ == "__main__":
    unittest.main()
