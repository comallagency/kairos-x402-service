"""5 reference-verified cases per route. Unit conversions use legally/
scientifically EXACT defined factors (the 1959 international yard/pound
agreement: 1 mile = 1609.344 m and 1 lb = 453.59237 g exactly; water's
definitional freeze/boil points; 0 K = -273.15 C by definition of the
Kelvin scale) - not textbook approximations. Roman numerals and base
conversion use well-known, independently checkable facts (1994 is the
classic "MCMXCIV" example; 255 = 0xFF = 0b11111111 are elementary,
checkable-by-hand binary/hex facts)."""

import pytest

from app.purecalc.registry import ComputeError
from app.purecalc.routes.units import (
    FractionInput, MoneyAllocateInput, MoneyFormatInput, OrdinalInput,
    RadixInput, RomanInput, UnitConvertInput, WordsInput,
    compute_fraction, compute_money_allocate, compute_money_format,
    compute_ordinal, compute_radix, compute_roman, compute_unit_convert,
    compute_words,
)


# ------------------------------------------------------------ unit/convert
def test_convert_mile_to_km_exact_1959_definition():
    r = compute_unit_convert(UnitConvertInput(value=1, from_unit="mi", to_unit="km"))
    assert r.value == pytest.approx(1.609344, abs=1e-9)


def test_convert_pound_to_gram_exact_avoirdupois_definition():
    r = compute_unit_convert(UnitConvertInput(value=1, from_unit="lb", to_unit="g"))
    assert r.value == pytest.approx(453.59237, abs=1e-6)


def test_convert_celsius_to_fahrenheit_freezing_point():
    r = compute_unit_convert(UnitConvertInput(value=0, from_unit="C", to_unit="F"))
    assert r.value == 32.0


def test_convert_celsius_to_fahrenheit_boiling_point():
    r = compute_unit_convert(UnitConvertInput(value=100, from_unit="C", to_unit="F"))
    assert r.value == 212.0


def test_convert_celsius_to_kelvin_absolute_zero_offset():
    r = compute_unit_convert(UnitConvertInput(value=0, from_unit="C", to_unit="K"))
    assert r.value == 273.15


def test_convert_incompatible_categories_rejected():
    with pytest.raises(ComputeError):
        compute_unit_convert(UnitConvertInput(value=1, from_unit="kg", to_unit="m"))


# ------------------------------------------------------------- number/roman
def test_roman_1994_classic_example():
    r = compute_roman(RomanInput(value=1994))
    assert r.roman == "MCMXCIV"


def test_roman_58_wikipedia_example():
    r = compute_roman(RomanInput(value=58))
    assert r.roman == "LVIII"


def test_roman_subtractive_notation_4_and_9():
    assert compute_roman(RomanInput(value=4)).roman == "IV"
    assert compute_roman(RomanInput(value=9)).roman == "IX"


def test_roman_to_int_roundtrip():
    r = compute_roman(RomanInput(roman="MCMXCIV"))
    assert r.value == 1994


def test_roman_invalid_symbol_rejected():
    with pytest.raises(ComputeError):
        compute_roman(RomanInput(roman="MCMXCIVZ"))


# ------------------------------------------------------------- number/words
def test_words_zero():
    assert compute_words(WordsInput(value=0)).words == "zero"


def test_words_123():
    assert compute_words(WordsInput(value=123)).words == "one hundred twenty-three"


def test_words_42():
    assert compute_words(WordsInput(value=42)).words == "forty-two"


def test_words_one_thousand():
    assert compute_words(WordsInput(value=1000)).words == "one thousand"


def test_words_one_million():
    assert compute_words(WordsInput(value=1_000_000)).words == "one million"


# ------------------------------------------------------------- number/radix
def test_radix_255_decimal_to_hex():
    r = compute_radix(RadixInput(value="255", from_base=10, to_base=16))
    assert r.value == "ff"


def test_radix_255_decimal_to_binary():
    r = compute_radix(RadixInput(value="255", from_base=10, to_base=2))
    assert r.value == "11111111"


def test_radix_hex_to_decimal():
    r = compute_radix(RadixInput(value="ff", from_base=16, to_base=10))
    assert r.value == "255"


def test_radix_zero():
    r = compute_radix(RadixInput(value="0", from_base=10, to_base=16))
    assert r.value == "0"


def test_radix_base36_hand_verified():
    # 100 = 2*36 + 28, digit 28 -> 'a'+18 = 's' -> "2s"
    r = compute_radix(RadixInput(value="100", from_base=10, to_base=36))
    assert r.value == "2s"


# ----------------------------------------------------------- number/ordinal
def test_ordinal_1st_2nd_3rd():
    assert compute_ordinal(OrdinalInput(value=1)).ordinal == "1st"
    assert compute_ordinal(OrdinalInput(value=2)).ordinal == "2nd"
    assert compute_ordinal(OrdinalInput(value=3)).ordinal == "3rd"


def test_ordinal_4th_and_general_th():
    assert compute_ordinal(OrdinalInput(value=4)).ordinal == "4th"
    assert compute_ordinal(OrdinalInput(value=100)).ordinal == "100th"


def test_ordinal_teens_exception_11_12_13():
    assert compute_ordinal(OrdinalInput(value=11)).ordinal == "11th"
    assert compute_ordinal(OrdinalInput(value=12)).ordinal == "12th"
    assert compute_ordinal(OrdinalInput(value=13)).ordinal == "13th"


def test_ordinal_21st_22nd_23rd():
    assert compute_ordinal(OrdinalInput(value=21)).ordinal == "21st"
    assert compute_ordinal(OrdinalInput(value=22)).ordinal == "22nd"
    assert compute_ordinal(OrdinalInput(value=23)).ordinal == "23rd"


def test_ordinal_111_112_113_also_th():
    # 111/112/113 end in 11/12/13 -> th, not st/nd/rd, same teens exception.
    assert compute_ordinal(OrdinalInput(value=111)).ordinal == "111th"
    assert compute_ordinal(OrdinalInput(value=112)).ordinal == "112th"
    assert compute_ordinal(OrdinalInput(value=113)).ordinal == "113th"


# ------------------------------------------------------- fraction/simplify
def test_fraction_four_eighths():
    r = compute_fraction(FractionInput(numerator=4, denominator=8))
    assert (r.numerator, r.denominator) == (1, 2)


def test_fraction_six_ninths():
    r = compute_fraction(FractionInput(numerator=6, denominator=9))
    assert (r.numerator, r.denominator) == (2, 3)


def test_fraction_already_simplified():
    r = compute_fraction(FractionInput(numerator=3, denominator=7))
    assert (r.numerator, r.denominator) == (3, 7)


def test_fraction_negative():
    r = compute_fraction(FractionInput(numerator=-4, denominator=8))
    assert (r.numerator, r.denominator) == (-1, 2)


def test_fraction_zero_denominator_rejected():
    with pytest.raises(ComputeError):
        compute_fraction(FractionInput(numerator=1, denominator=0))


# ----------------------------------------------------------- money/format
def test_money_format_basic():
    r = compute_money_format(MoneyFormatInput(amount=1234567.5))
    assert r.formatted == "$1,234,567.50"


def test_money_format_negative():
    r = compute_money_format(MoneyFormatInput(amount=-500))
    assert r.formatted == "-$500.00"


def test_money_format_zero():
    r = compute_money_format(MoneyFormatInput(amount=0))
    assert r.formatted == "$0.00"


def test_money_format_custom_symbol():
    r = compute_money_format(MoneyFormatInput(amount=10, currency_symbol="€"))
    assert r.formatted == "€10.00"


def test_money_format_zero_decimals():
    r = compute_money_format(MoneyFormatInput(amount=1234.56, decimals=0))
    assert r.formatted == "$1,235"


# --------------------------------------------------------- money/allocate
def test_allocate_fowler_classic_5_cents_3_ways():
    r = compute_money_allocate(MoneyAllocateInput(amount_cents=5, ratios=[1, 1, 1]))
    assert r.allocations == [2, 2, 1]
    assert sum(r.allocations) == 5


def test_allocate_sums_exactly_for_awkward_split():
    r = compute_money_allocate(MoneyAllocateInput(amount_cents=100, ratios=[1, 1, 1]))
    assert sum(r.allocations) == 100
    assert sorted(r.allocations) == [33, 33, 34]


def test_allocate_clean_ratio_split():
    r = compute_money_allocate(MoneyAllocateInput(amount_cents=100, ratios=[1, 2, 1]))
    assert r.allocations == [25, 50, 25]


def test_allocate_sum_always_equals_amount_property():
    # General invariant, not just the hand-picked cases above.
    for amount, ratios in [(7, [1, 1]), (1000, [3, 5, 7]), (1, [1, 1, 1, 1, 1])]:
        r = compute_money_allocate(MoneyAllocateInput(amount_cents=amount, ratios=ratios))
        assert sum(r.allocations) == amount


def test_allocate_all_zero_ratios_rejected():
    with pytest.raises(ComputeError):
        compute_money_allocate(MoneyAllocateInput(amount_cents=100, ratios=[0, 0]))
