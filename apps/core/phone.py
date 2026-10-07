"""Phone numbers for WhatsApp and phone calls.

Staff type numbers the way patients say them ("0300-1234567", "98765 43210", "+92 0300 1234567"),
sometimes with Urdu or Hindi digits. WhatsApp needs the international form without "+"
("923001234567").

Two checks:

    normalize_mobile(raw, country)  STRICT, for a patient's mobile / WhatsApp number. A Pakistani or
                                    Indian number must have exactly 10 digits after the country code
                                    and start like a mobile (3 in Pakistan; 6, 7, 8 or 9 in India).
                                    Other countries' numbers are accepted when typed with + or 00
                                    (relatives abroad, e.g. "+971 50 123 4567").
    normalize_phone(raw, country)   LENIENT, for any phone (e.g. the clinic's landline "042-35761234")
                                    and for tel: links.

Both return "" when the number can't be understood, so a wa.me link is never built for a bad number.
"""

import unicodedata
from urllib.parse import quote

COUNTRY_CODES = {"PK": "92", "IN": "91"}
NATIONAL_NUMBER_LENGTH = 10  # both Pakistan and India mobiles: 10 digits after the country code
# First digit of a mobile number after the country code. (The demo clinic's 0390- numbers pass:
# they look like real mobiles but no network uses that prefix.)
MOBILE_FIRST_DIGITS = {"92": "3", "91": "6789"}
# Any international number: country code + number, at most 15 digits (the E.164 limit).
MIN_LENGTH, MAX_LENGTH = 10, 15


def ascii_digits(text):
    """Only the digits of `text`, as plain 0-9: '۰۳۰۰-۱۲۳' (Urdu digits) -> '0300123'.

    Urdu, Arabic and Hindi (Devanagari) digits are converted; everything else is dropped.
    """
    return "".join(str(unicodedata.decimal(ch)) for ch in str(text or "") if ch.isdecimal())


def with_ascii_digits(text):
    """The number as typed, with Urdu / Hindi digits turned into 0-9: '۰۳۰۰-۱۲۳۴۵۶۷' -> '0300-1234567'.

    For saving what staff typed, so searches and tel: links work on it.
    """
    return "".join(str(unicodedata.decimal(ch)) if ch.isdecimal() else ch for ch in str(text or ""))


def normalize_phone(raw, country="PK", *, mobile=False):
    """Return the digits-only international number, or "" if it can't be understood.

    mobile=True applies the strict mobile check (see the module docstring); normalize_mobile()
    is the same thing with a clearer name.

    >>> normalize_phone("0300-1234567", "PK")
    '923001234567'
    >>> normalize_phone("+92 300 1234567", "PK")
    '923001234567'
    >>> normalize_phone("+92 0300 1234567", "PK")
    '923001234567'
    >>> normalize_phone("98765 43210", "IN")
    '919876543210'
    >>> normalize_phone("042-35761234", "PK")
    '924235761234'
    """
    text = str(raw or "").strip()
    digits = ascii_digits(text)
    if not digits:
        return ""

    country_code = COUNTRY_CODES.get(country, "92")
    typed_international = text.startswith("+") or digits.startswith("00")
    number = _international_digits(text, digits, country_code)
    number = _without_trunk_zero(number)

    home_code = next((code for code in COUNTRY_CODES.values() if number.startswith(code)), None)
    if mobile:
        if home_code is not None:
            national = number[len(home_code):]
            is_mobile = len(national) == NATIONAL_NUMBER_LENGTH and national[0] in MOBILE_FIRST_DIGITS[home_code]
            return number if is_mobile else ""
        if not typed_international:
            return ""  # not a home number, and not written as +<country code>: can't tell what it is
    return number if MIN_LENGTH <= len(number) <= MAX_LENGTH else ""


def normalize_mobile(raw, country="PK"):
    """A patient's mobile / WhatsApp number in international digits, or "" if it isn't one.

    >>> normalize_mobile("0300-1234567", "PK")
    '923001234567'
    >>> normalize_mobile("0300-123456", "PK")
    ''
    >>> normalize_mobile("042-35761234", "PK")
    ''
    >>> normalize_mobile("+971 50 123 4567", "PK")
    '971501234567'
    """
    return normalize_phone(raw, country, mobile=True)


def _international_digits(text, digits, country_code):
    """The number with its country code in front, worked out from how it was typed."""
    if text.startswith("+"):
        return digits
    if digits.startswith("00"):  # 0092 300 1234567
        return digits[2:]
    if digits.startswith(country_code):
        national = digits[len(country_code):]
        # 92 300 1234567, or 92 0300 1234567 (country code typed without the +)
        if len(national) == NATIONAL_NUMBER_LENGTH or (
            len(national) == NATIONAL_NUMBER_LENGTH + 1 and national.startswith("0")
        ):
            return digits
    if digits.startswith("0"):  # 0300 1234567: drop the 0 a call inside the country starts with
        national = digits[1:]
        if national.startswith(country_code) and len(national) == len(country_code) + NATIONAL_NUMBER_LENGTH:
            national = national[len(country_code):]  # 092 300 1234567
        return country_code + national
    if len(digits) == NATIONAL_NUMBER_LENGTH:  # 300 1234567, or 98765 43210 in India
        return country_code + digits
    return digits


def _without_trunk_zero(number):
    """'9203001234567' (+92 0300...) -> '923001234567'. Numbers never start with 0 after the code."""
    for code in COUNTRY_CODES.values():
        if number.startswith(code + "0"):
            return code + number[len(code) + 1:]
    return number


def whatsapp_link(number, text=""):
    """A wa.me link that opens WhatsApp with the chat and message ready to send."""
    url = f"https://wa.me/{ascii_digits(number)}"
    if text:
        url += "?text=" + quote(text)
    return url


def format_phone_display(number):
    """'923001234567' -> '+92 300 1234567' (for display only)."""
    if not number:
        return ""
    for code in COUNTRY_CODES.values():
        if number.startswith(code) and len(number) == len(code) + NATIONAL_NUMBER_LENGTH:
            rest = number[len(code):]
            return f"+{code} {rest[:3]} {rest[3:]}"
    return f"+{number}"
