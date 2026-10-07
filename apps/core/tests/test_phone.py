import doctest

from django.test import SimpleTestCase

from apps.core import phone
from apps.core.phone import (
    ascii_digits,
    format_phone_display,
    normalize_mobile,
    normalize_phone,
    whatsapp_link,
    with_ascii_digits,
)


class DocstringExampleTests(SimpleTestCase):
    """Also run the examples written in phone.py's docstrings.

    (A plain test rather than a `load_tests` DocTestSuite: doctest cases hold module objects,
    which `manage.py test --parallel` cannot send to its worker processes.)
    """

    def test_docstring_examples(self):
        result = doctest.testmod(phone)
        self.assertGreater(result.attempted, 0, "phone.py has no docstring examples to check")
        self.assertEqual(result.failed, 0, "A docstring example in apps/core/phone.py failed (details above)")


class AsciiDigitsTests(SimpleTestCase):
    def test_converts_urdu_arabic_and_hindi_digits(self):
        self.assertEqual(ascii_digits("۰۳۰۰-۱۲۳۴۵۶۷"), "03001234567")  # Urdu (Extended Arabic-Indic)
        self.assertEqual(ascii_digits("٠٣٠٠١٢٣٤٥٦٧"), "03001234567")  # Arabic-Indic
        self.assertEqual(ascii_digits("०९८७६५४३२१०"), "09876543210")  # Devanagari
        self.assertEqual(ascii_digits("+92 (300) 123-4567"), "923001234567")

    def test_nothing_else_survives(self):
        self.assertEqual(ascii_digits("abc²"), "")  # superscript two is not a decimal digit
        self.assertEqual(ascii_digits(None), "")

    def test_with_ascii_digits_keeps_the_formatting(self):
        self.assertEqual(with_ascii_digits("۰۳۰۰-۱۲۳۴۵۶۷"), "0300-1234567")
        self.assertEqual(with_ascii_digits("+९१ ९८७६५ ४३२१०"), "+91 98765 43210")
        self.assertEqual(with_ascii_digits("0300-1234567"), "0300-1234567")
        self.assertEqual(with_ascii_digits(None), "")


class NormalizeMobileTests(SimpleTestCase):
    """Patients' mobile / WhatsApp numbers: wrong numbers become "" so no wa.me link is built."""

    def assert_numbers(self, cases, country="PK"):
        for raw, expected in cases:
            with self.subTest(raw=raw, country=country):
                self.assertEqual(normalize_mobile(raw, country), expected)
                self.assertTrue(normalize_mobile(raw, country).isascii())

    def test_pakistani_numbers_typed_any_way(self):
        self.assert_numbers([
            ("0300-1234567", "923001234567"),
            ("03001234567", "923001234567"),
            ("300 1234567", "923001234567"),
            ("+92 300 1234567", "923001234567"),
            ("0092 300 1234567", "923001234567"),
            ("92 300 1234567", "923001234567"),
            ("0390-1234567", "923901234567"),  # the demo clinic's unallocated prefix
        ])

    def test_trunk_zero_after_the_country_code_is_dropped(self):
        self.assert_numbers([
            ("+92 0300 1234567", "923001234567"),
            ("+92 (0)300 1234567", "923001234567"),
            ("0092 0300 1234567", "923001234567"),
            ("92 0300 1234567", "923001234567"),
            ("092 300 1234567", "923001234567"),
        ])
        self.assert_numbers([("+91 098765 43210", "919876543210")], country="IN")

    def test_urdu_and_hindi_digits(self):
        self.assert_numbers([("۰۳۰۰۱۲۳۴۵۶۷", "923001234567"), ("٠٣٠٠-١٢٣٤٥٦٧", "923001234567")])
        self.assert_numbers([("०९८७६५४३२१०", "919876543210")], country="IN")

    def test_wrong_length_or_not_a_mobile_is_refused(self):
        self.assert_numbers([
            ("0300-123456", ""),  # one digit short
            ("0300-12345678", ""),  # one digit too many
            ("+92 300 123456", ""),
            ("0300 12345", ""),
            ("042-35761234", ""),  # a Lahore landline: no WhatsApp
            ("+92 42 35761234", ""),
            ("1234567890", ""),  # 10 digits, but no Pakistani mobile starts with 1
            ("12345678901", ""),  # no country code and no leading 0: can't tell what it is
            ("abc", ""),
            ("12", ""),
            ("", ""),
            (None, ""),
        ])
        self.assert_numbers([("98765 4321", ""), ("+91 12345 67890", "")], country="IN")

    def test_indian_numbers(self):
        self.assert_numbers([
            ("98765 43210", "919876543210"),
            ("91234 56789", "919123456789"),  # starts with 91, but it is the national number
            ("+91 98765 43210", "919876543210"),
            ("098765 43210", "919876543210"),
            ("70123 45678", "917012345678"),
        ], country="IN")

    def test_numbers_abroad_typed_with_a_plus_or_00(self):
        self.assert_numbers([
            ("+971 50 123 4567", "971501234567"),  # a relative in Dubai
            ("00966 55 123 4567", "966551234567"),
            ("+91 98765 43210", "919876543210"),  # an Indian number at a Pakistani clinic
        ])


class NormalizePhoneTests(SimpleTestCase):
    """Any phone, e.g. the clinic's landline: the same parsing, a looser length check."""

    def test_landlines_still_work(self):
        for raw, expected in [
            ("042-35761234", "924235761234"),
            ("042-3576-1234", "924235761234"),
            ("051-1234567", "92511234567"),  # Islamabad: 9 digits after the country code
            ("+92 (0)42 35761234", "924235761234"),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_phone(raw, "PK"), expected)

    def test_shared_fixes_apply_here_too(self):
        self.assertEqual(normalize_phone("+92 0300 1234567", "PK"), "923001234567")
        self.assertEqual(normalize_phone("۰۳۰۰۱۲۳۴۵۶۷", "PK"), "923001234567")
        self.assertEqual(normalize_phone("०९८७६५४३२१०", "IN"), "919876543210")

    def test_rubbish_is_refused(self):
        for raw in ("abc", "12", "12345", "", None, "1" * 16):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_phone(raw, "PK"), "")

    def test_mobile_flag_is_the_strict_check(self):
        self.assertEqual(normalize_phone("0300-123456", "PK", mobile=True), "")
        self.assertEqual(normalize_phone("0300-1234567", "PK", mobile=True), "923001234567")


class WhatsappLinkTests(SimpleTestCase):
    def test_link_only_ever_holds_ascii_digits(self):
        self.assertEqual(whatsapp_link("923001234567"), "https://wa.me/923001234567")
        self.assertEqual(whatsapp_link("۹۲۳۰۰۱۲۳۴۵۶۷"), "https://wa.me/923001234567")
        self.assertEqual(whatsapp_link("923001234567", "Hi & bye"), "https://wa.me/923001234567?text=Hi%20%26%20bye")

    def test_display(self):
        self.assertEqual(format_phone_display("923001234567"), "+92 300 1234567")
        self.assertEqual(format_phone_display("971501234567"), "+971501234567")
        self.assertEqual(format_phone_display(""), "")
