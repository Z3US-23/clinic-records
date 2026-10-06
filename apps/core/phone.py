"""Phone numbers for WhatsApp.

Staff type numbers the way patients say them ("0300-1234567", "98765 43210").
WhatsApp needs the international form without "+" ("923001234567").
"""

import re
from urllib.parse import quote

COUNTRY_CODES = {"PK": "92", "IN": "91"}
NATIONAL_NUMBER_LENGTH = 10  # both Pakistan and India mobiles: 10 digits after the country code


def normalize_phone(raw, country="PK"):
    """Return the digits-only international number, or "" if it can't be understood.

    >>> normalize_phone("0300-1234567", "PK")
    '923001234567'
    >>> normalize_phone("+92 300 1234567", "PK")
    '923001234567'
    >>> normalize_phone("98765 43210", "IN")
    '919876543210'
    """
    if not raw:
        return ""
    text = str(raw).strip()
    digits = re.sub(r"\D", "", text)
    if not digits:
        return ""

    country_code = COUNTRY_CODES.get(country, "92")
    if text.startswith("+"):
        number = digits
    elif digits.startswith("00"):
        number = digits[2:]
    elif digits.startswith(country_code) and len(digits) == len(country_code) + NATIONAL_NUMBER_LENGTH:
        number = digits
    elif digits.startswith("0"):
        number = country_code + digits.lstrip("0")
    elif len(digits) == NATIONAL_NUMBER_LENGTH:
        number = country_code + digits
    else:
        number = digits

    if not 10 <= len(number) <= 15:
        return ""
    return number


def whatsapp_link(number, text=""):
    """A wa.me link that opens WhatsApp with the chat and message ready to send."""
    url = f"https://wa.me/{number}"
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
