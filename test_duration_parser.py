import unittest

from duration_parser import parse_duration


class ParseDurationTests(unittest.TestCase):
    def test_converts_supported_units(self):
        cases = {
            "0ms": 0,
            "001ms": 1,
            "1.5s": 1500,
            "0.25m": 15_000,
            "2h": 7_200_000,
            "  3s\n": 3000,
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(parse_duration(value), expected)

    def test_rejects_invalid_syntax_and_inexact_milliseconds(self):
        invalid = [
            "",
            "1",
            "-1s",
            "+1s",
            "1S",
            "1 s",
            "1.ms",
            ".5s",
            "1.2.3s",
            "1.2345s",
            "0.5ms",
        ]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_duration(value)

    def test_rejects_non_strings(self):
        for value in (None, 1, 1.5, True):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    parse_duration(value)


if __name__ == "__main__":
    unittest.main()
