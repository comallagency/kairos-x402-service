"""5 reference-verified cases per route. References are widely published
real-world identifiers (Wikipedia's own IBAN/VIN/ISBN/EAN/ISIN worked
examples, the well-known Visa Luhn test number 4111111111111111, JPMorgan
Chase's published ABA routing number 021000021, Apple Inc.'s real ISIN
US0378331005) or the published checksum algorithm traced by hand to
construct a fresh valid value (IMEI, SIRET, a second routing number/ISIN) -
independently verified with a standalone script before being hardcoded
here, never "run this code, copy its output.\""""

import pytest

from app.purecalc.registry import ComputeError
from app.purecalc.routes.validations import (
    EanInput, IbanInput, ImeiInput, IsbnInput, IsinInput, LuhnInput,
    RoutingInput, SiretInput, VinInput,
    compute_ean, compute_iban, compute_imei, compute_isbn, compute_isin,
    compute_luhn, compute_routing, compute_siret, compute_vin,
)


# ------------------------------------------------------------------ iban
def test_iban_wikipedia_uk_example_valid():
    r = compute_iban(IbanInput(iban="GB82 WEST 1234 5698 7654 32"))
    assert r.valid is True and r.country == "GB"


def test_iban_bundesbank_german_example_valid():
    r = compute_iban(IbanInput(iban="DE89 3704 0044 0532 0130 00"))
    assert r.valid is True and r.country == "DE"


def test_iban_french_example_valid():
    r = compute_iban(IbanInput(iban="FR14 2004 1010 0505 0001 3M02 606"))
    assert r.valid is True and r.country == "FR"


def test_iban_altered_digit_invalid():
    r = compute_iban(IbanInput(iban="GB82 WEST 1234 5698 7654 31"))
    assert r.valid is False


def test_iban_bic_format_check():
    r = compute_iban(IbanInput(iban="GB82 WEST 1234 5698 7654 32", bic="NWBKGB2L"))
    assert r.bic_format_valid is True


# -------------------------------------------------------------------- vin
def test_vin_wikipedia_example_check_digit_valid():
    r = compute_vin(VinInput(vin="1HGCM82633A004352"))
    assert r.valid_check_digit is True
    assert r.manufacturer == "Honda" and r.country == "USA"


def test_vin_altered_check_digit_invalid():
    r = compute_vin(VinInput(vin="1HGCM82633A004353"))
    assert r.valid_check_digit is False


def test_vin_unknown_wmi():
    r = compute_vin(VinInput(vin="9ZZCM82633A004352"))
    assert r.manufacturer == "unknown"


def test_vin_rejects_forbidden_letters():
    with pytest.raises(Exception):
        VinInput(vin="IHGCM82633A004352")  # 'I' is never valid in a VIN


def test_vin_rejects_wrong_length():
    with pytest.raises(Exception):
        VinInput(vin="1HGCM82633A00435")  # 16 chars


# ------------------------------------------------------------------- isbn
def test_isbn10_classic_example_valid():
    r = compute_isbn(IsbnInput(isbn="0-306-40615-2"))
    assert r.valid is True and r.format == "ISBN-10"


def test_isbn13_equivalent_valid():
    r = compute_isbn(IsbnInput(isbn="978-0-306-40615-7"))
    assert r.valid is True and r.format == "ISBN-13"


def test_isbn10_altered_digit_invalid():
    r = compute_isbn(IsbnInput(isbn="0-306-40615-1"))
    assert r.valid is False


def test_isbn_x_check_digit():
    # 0-8044-2957-X is a commonly cited ISBN-10 example using the X check digit.
    r = compute_isbn(IsbnInput(isbn="0-8044-2957-X"))
    assert r.format == "ISBN-10"


def test_isbn_wrong_length_rejected():
    with pytest.raises(ComputeError):
        compute_isbn(IsbnInput(isbn="12345"))


# ------------------------------------------------------------------- luhn
def test_luhn_famous_visa_test_number():
    r = compute_luhn(LuhnInput(number="4111111111111111"))
    assert r.valid is True


def test_luhn_second_valid_test_number():
    r = compute_luhn(LuhnInput(number="4532015112830366"))
    assert r.valid is True


def test_luhn_altered_digit_invalid():
    r = compute_luhn(LuhnInput(number="4111111111111112"))
    assert r.valid is False


def test_luhn_tolerates_spaces_and_dashes():
    r = compute_luhn(LuhnInput(number="4111-1111-1111-1111"))
    assert r.valid is True


def test_luhn_single_valid_digit():
    r = compute_luhn(LuhnInput(number="00"))
    assert r.valid is True


# -------------------------------------------------------------------- ean
def test_ean13_wikipedia_example_valid():
    r = compute_ean(EanInput(code="4006381333931"))
    assert r.valid is True and r.format == "EAN-13"


def test_ean13_altered_digit_invalid():
    r = compute_ean(EanInput(code="4006381333932"))
    assert r.valid is False


def test_ean8_hand_constructed_valid():
    r = compute_ean(EanInput(code="40170008"))
    assert r.valid is True and r.format == "EAN-8"


def test_ean8_altered_digit_invalid():
    r = compute_ean(EanInput(code="40170009"))
    assert r.valid is False


def test_ean_wrong_length_rejected():
    with pytest.raises(Exception):
        EanInput(code="123456")


# ------------------------------------------------------------------- imei
def test_imei_hand_constructed_valid():
    r = compute_imei(ImeiInput(imei="490154203237518"))
    assert r.valid is True


def test_imei_altered_digit_invalid():
    r = compute_imei(ImeiInput(imei="490154203237519"))
    assert r.valid is False


def test_imei_wrong_length_rejected():
    with pytest.raises(Exception):
        ImeiInput(imei="49015420323751")  # 14 digits


def test_imei_non_digit_rejected():
    with pytest.raises(Exception):
        ImeiInput(imei="49015420323751A")


def test_imei_all_zeros_is_luhn_valid():
    r = compute_imei(ImeiInput(imei="000000000000000"))
    assert r.valid is True


# --------------------------------------------------------------- routing
def test_routing_jpmorgan_chase_published_number():
    r = compute_routing(RoutingInput(routing_number="021000021"))
    assert r.valid is True


def test_routing_second_hand_constructed_valid():
    r = compute_routing(RoutingInput(routing_number="110000013"))
    assert r.valid is True


def test_routing_altered_digit_invalid():
    r = compute_routing(RoutingInput(routing_number="021000020"))
    assert r.valid is False


def test_routing_wrong_length_rejected():
    with pytest.raises(Exception):
        RoutingInput(routing_number="02100002")


def test_routing_all_zeros_valid():
    r = compute_routing(RoutingInput(routing_number="000000000"))
    assert r.valid is True


# ------------------------------------------------------------------- isin
def test_isin_apple_real_published_isin():
    r = compute_isin(IsinInput(isin="US0378331005"))
    assert r.valid is True and r.country == "US"


def test_isin_hand_constructed_french_valid():
    r = compute_isin(IsinInput(isin="FR0012345676"))
    assert r.valid is True and r.country == "FR"


def test_isin_altered_digit_invalid():
    r = compute_isin(IsinInput(isin="US0378331006"))
    assert r.valid is False


def test_isin_wrong_length_rejected():
    with pytest.raises(Exception):
        IsinInput(isin="US037833100")


def test_isin_lowercase_tolerated():
    r = compute_isin(IsinInput(isin="us0378331005"))
    assert r.valid is True


# ------------------------------------------------------------------ siret
def test_siret_hand_constructed_valid():
    r = compute_siret(SiretInput(siret="55230123450006"))
    assert r.valid is True and r.siren == "552301234"


def test_siret_second_hand_constructed_valid():
    r = compute_siret(SiretInput(siret="73204567890124"))
    assert r.valid is True and r.siren == "732045678"


def test_siret_altered_digit_invalid():
    r = compute_siret(SiretInput(siret="55230123450007"))
    assert r.valid is False


def test_siret_wrong_length_rejected():
    with pytest.raises(Exception):
        SiretInput(siret="5523012345000")


def test_siret_all_zeros_valid():
    r = compute_siret(SiretInput(siret="00000000000000"))
    assert r.valid is True
