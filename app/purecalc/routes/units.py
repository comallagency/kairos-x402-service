"""Units/numbers pure-compute routes - stdlib (fractions) plus hand-written
conversions using legally/scientifically EXACT defined factors (1959
international yard/pound agreement, US legal gallon), never approximations."""

from fractions import Fraction

from pydantic import BaseModel, Field

from app.purecalc.registry import ComputeError, ComputeSpec, register

# ------------------------------------------------------------- unit/convert
_LENGTH = {"m": 1.0, "km": 1000.0, "cm": 0.01, "mm": 0.001,
           "mi": 1609.344, "yd": 0.9144, "ft": 0.3048, "in": 0.0254}
_MASS = {"kg": 1.0, "g": 0.001, "mg": 0.000001, "lb": 0.45359237, "oz": 0.028349523125}
_VOLUME = {"l": 1.0, "ml": 0.001, "gal": 3.785411784, "qt": 0.946352946}
_CATEGORIES = {"length": _LENGTH, "mass": _MASS, "volume": _VOLUME}


def _temp_to_celsius(value: float, unit: str) -> float:
    if unit == "C":
        return value
    if unit == "F":
        return (value - 32) * 5 / 9
    if unit == "K":
        return value - 273.15
    raise ComputeError("unknown_unit", f"{unit!r} is not a temperature unit (C, F, K)")


def _celsius_to(value: float, unit: str) -> float:
    if unit == "C":
        return value
    if unit == "F":
        return value * 9 / 5 + 32
    if unit == "K":
        return value + 273.15
    raise ComputeError("unknown_unit", f"{unit!r} is not a temperature unit (C, F, K)")


class UnitConvertInput(BaseModel):
    value: float
    from_unit: str
    to_unit: str


class UnitConvertOutput(BaseModel):
    value: float
    unit: str


def compute_unit_convert(inp: UnitConvertInput) -> UnitConvertOutput:
    if inp.from_unit in ("C", "F", "K") or inp.to_unit in ("C", "F", "K"):
        celsius = _temp_to_celsius(inp.value, inp.from_unit)
        return UnitConvertOutput(value=round(_celsius_to(celsius, inp.to_unit), 6), unit=inp.to_unit)

    for table in _CATEGORIES.values():
        if inp.from_unit in table and inp.to_unit in table:
            base = inp.value * table[inp.from_unit]
            return UnitConvertOutput(value=round(base / table[inp.to_unit], 10), unit=inp.to_unit)

    raise ComputeError("incompatible_units", f"{inp.from_unit!r} and {inp.to_unit!r} are not in the same unit category")


register(ComputeSpec(
    slug="unit/convert", price="$0.002", service_name="unit-convert",
    description="Convert a value between units of length, mass, volume or temperature (C/F/K).",
    tags=["unit conversion", "length", "mass", "volume", "temperature", "metric", "imperial"],
    input_model=UnitConvertInput, output_model=UnitConvertOutput, compute=compute_unit_convert,
    sample_input={"value": 1, "from_unit": "mi", "to_unit": "km"},
    sample_output={"value": 1.609344, "unit": "km"},
))


# ------------------------------------------------------------- number/roman
_ROMAN_TABLE = [
    (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
    (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
]
_ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


class RomanInput(BaseModel):
    value: int | None = Field(default=None, ge=1, le=3999)
    roman: str | None = None


class RomanOutput(BaseModel):
    value: int | None = None
    roman: str | None = None


def compute_roman(inp: RomanInput) -> RomanOutput:
    if (inp.value is None) == (inp.roman is None):
        raise ComputeError("ambiguous_input", "provide exactly one of value or roman")

    if inp.value is not None:
        n = inp.value
        out = []
        for v, sym in _ROMAN_TABLE:
            count, n = divmod(n, v)
            out.append(sym * count)
        return RomanOutput(roman="".join(out))

    roman = inp.roman.upper()
    total = 0
    prev = 0
    for ch in reversed(roman):
        if ch not in _ROMAN_VALUES:
            raise ComputeError("invalid_roman", f"{ch!r} is not a valid roman numeral symbol")
        v = _ROMAN_VALUES[ch]
        total += v if v >= prev else -v
        prev = v
    return RomanOutput(value=total)


register(ComputeSpec(
    slug="number/roman", price="$0.002", service_name="number-roman",
    description="Convert an integer (1-3999) to a Roman numeral, or a Roman numeral back to an integer.",
    tags=["roman numerals", "number conversion", "numeral system"],
    input_model=RomanInput, output_model=RomanOutput, compute=compute_roman,
    sample_input={"value": 1994},
    sample_output={"roman": "MCMXCIV"},
))


# ------------------------------------------------------------- number/words
_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
         "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
         "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_SCALES = [(1_000_000_000, "billion"), (1_000_000, "million"), (1_000, "thousand")]


def _three_digits_to_words(n: int) -> str:
    parts = []
    if n >= 100:
        parts.append(f"{_ONES[n // 100]} hundred")
        n %= 100
    if n >= 20:
        tens_word = _TENS[n // 10]
        if n % 10:
            tens_word += f"-{_ONES[n % 10]}"
        parts.append(tens_word)
    elif n > 0:
        parts.append(_ONES[n])
    return " ".join(parts)


class WordsInput(BaseModel):
    value: int = Field(ge=0, le=999_999_999_999)


class WordsOutput(BaseModel):
    words: str


def compute_words(inp: WordsInput) -> WordsOutput:
    n = inp.value
    if n == 0:
        return WordsOutput(words="zero")

    parts = []
    for scale_value, scale_name in _SCALES:
        if n >= scale_value:
            count, n = divmod(n, scale_value)
            parts.append(f"{_three_digits_to_words(count)} {scale_name}")
    if n > 0:
        parts.append(_three_digits_to_words(n))
    return WordsOutput(words=" ".join(parts))


register(ComputeSpec(
    slug="number/words", price="$0.002", service_name="number-words",
    description="Convert an integer into English words.",
    tags=["number to words", "spell out numbers", "english", "numerals"],
    input_model=WordsInput, output_model=WordsOutput, compute=compute_words,
    sample_input={"value": 123},
    sample_output={"words": "one hundred twenty-three"},
))


# ------------------------------------------------------------- number/radix
_RADIX_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


class RadixInput(BaseModel):
    value: str
    from_base: int = Field(ge=2, le=36)
    to_base: int = Field(ge=2, le=36)


class RadixOutput(BaseModel):
    value: str


def compute_radix(inp: RadixInput) -> RadixOutput:
    try:
        n = int(inp.value, inp.from_base)
    except ValueError as exc:
        raise ComputeError("invalid_digits", f"{inp.value!r} is not valid in base {inp.from_base}") from exc

    if n == 0:
        return RadixOutput(value="0")
    negative = n < 0
    n = abs(n)
    digits = []
    while n:
        n, rem = divmod(n, inp.to_base)
        digits.append(_RADIX_DIGITS[rem])
    result = "".join(reversed(digits))
    return RadixOutput(value=("-" + result) if negative else result)


register(ComputeSpec(
    slug="number/radix", price="$0.002", service_name="number-radix",
    description="Convert a number's string representation from one base (2-36) to another.",
    tags=["base conversion", "radix", "hexadecimal", "binary", "number systems"],
    input_model=RadixInput, output_model=RadixOutput, compute=compute_radix,
    sample_input={"value": "255", "from_base": 10, "to_base": 16},
    sample_output={"value": "ff"},
))


# ----------------------------------------------------------- number/ordinal
class OrdinalInput(BaseModel):
    value: int


class OrdinalOutput(BaseModel):
    ordinal: str


def compute_ordinal(inp: OrdinalInput) -> OrdinalOutput:
    n = inp.value
    if 11 <= abs(n) % 100 <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(abs(n) % 10, "th")
    return OrdinalOutput(ordinal=f"{n}{suffix}")


register(ComputeSpec(
    slug="number/ordinal", price="$0.002", service_name="number-ordinal",
    description="Convert an integer to its English ordinal form (1st, 2nd, 3rd, 4th, 11th, 21st...).",
    tags=["ordinal numbers", "english", "number formatting"],
    input_model=OrdinalInput, output_model=OrdinalOutput, compute=compute_ordinal,
    sample_input={"value": 23},
    sample_output={"ordinal": "23rd"},
))


# -------------------------------------------------------- fraction/simplify
class FractionInput(BaseModel):
    numerator: int
    denominator: int


class FractionOutput(BaseModel):
    numerator: int
    denominator: int
    decimal: float


def compute_fraction(inp: FractionInput) -> FractionOutput:
    if inp.denominator == 0:
        raise ComputeError("zero_denominator", "denominator must not be zero")
    f = Fraction(inp.numerator, inp.denominator)
    return FractionOutput(numerator=f.numerator, denominator=f.denominator, decimal=round(float(f), 10))


register(ComputeSpec(
    slug="fraction/simplify", price="$0.002", service_name="fraction-simplify",
    description="Simplify a fraction to lowest terms and return its decimal value.",
    tags=["fraction", "simplify", "lowest terms", "gcd", "math"],
    input_model=FractionInput, output_model=FractionOutput, compute=compute_fraction,
    sample_input={"numerator": 4, "denominator": 8},
    sample_output={"numerator": 1, "denominator": 2, "decimal": 0.5},
))


# ----------------------------------------------------------- money/format
class MoneyFormatInput(BaseModel):
    amount: float
    currency_symbol: str = "$"
    decimals: int = Field(default=2, ge=0, le=6)


class MoneyFormatOutput(BaseModel):
    formatted: str


def compute_money_format(inp: MoneyFormatInput) -> MoneyFormatOutput:
    negative = inp.amount < 0
    magnitude = abs(inp.amount)
    formatted_number = f"{magnitude:,.{inp.decimals}f}"
    sign = "-" if negative else ""
    return MoneyFormatOutput(formatted=f"{sign}{inp.currency_symbol}{formatted_number}")


register(ComputeSpec(
    slug="money/format", price="$0.002", service_name="money-format",
    description="Format a numeric amount with thousands separators, a chosen decimal precision and a currency symbol.",
    tags=["money formatting", "currency", "thousands separator", "number formatting"],
    input_model=MoneyFormatInput, output_model=MoneyFormatOutput, compute=compute_money_format,
    sample_input={"amount": 1234567.5, "currency_symbol": "$", "decimals": 2},
    sample_output={"formatted": "$1,234,567.50"},
))


# ---------------------------------------------------------- money/allocate
class MoneyAllocateInput(BaseModel):
    amount_cents: int = Field(ge=0)
    ratios: list[int] = Field(min_length=1)


class MoneyAllocateOutput(BaseModel):
    allocations: list[int]


def compute_money_allocate(inp: MoneyAllocateInput) -> MoneyAllocateOutput:
    if any(r < 0 for r in inp.ratios):
        raise ComputeError("invalid_ratios", "ratios must be non-negative")
    total_ratio = sum(inp.ratios)
    if total_ratio == 0:
        raise ComputeError("invalid_ratios", "ratios must not all be zero")

    # Fowler's allocation algorithm: give each part its floor share, then
    # distribute the remaining cents one at a time to the parts with the
    # largest fractional remainder, so the total always equals amount_cents
    # exactly and the split tracks the requested ratios as closely as
    # integer cents allow.
    shares = [(inp.amount_cents * r) / total_ratio for r in inp.ratios]
    allocations = [int(s) for s in shares]
    remainder = inp.amount_cents - sum(allocations)
    remainders = sorted(range(len(shares)), key=lambda i: shares[i] - allocations[i], reverse=True)
    for i in remainders[:remainder]:
        allocations[i] += 1
    return MoneyAllocateOutput(allocations=allocations)


register(ComputeSpec(
    slug="money/allocate", price="$0.002", service_name="money-allocate",
    description="Split an integer amount of cents among N ratios with zero rounding loss (Fowler's allocation algorithm) - the parts always sum back to the original amount.",
    tags=["money allocation", "split bill", "rounding", "fowler algorithm", "finance"],
    input_model=MoneyAllocateInput, output_model=MoneyAllocateOutput, compute=compute_money_allocate,
    sample_input={"amount_cents": 5, "ratios": [1, 1, 1]},
    sample_output={"allocations": [2, 2, 1]},
))
