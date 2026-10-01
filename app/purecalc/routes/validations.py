"""Identifier/checksum validation routes - hand-written standard algorithms,
no dependency. Each docstring names the published standard implemented."""

import re

from pydantic import BaseModel, Field

from app.purecalc.registry import ComputeError, ComputeSpec, register


def _digits_only(s: str) -> str:
    return re.sub(r"[\s-]", "", s)


def luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# ----------------------------------------------------------------- iban
_BIC_RE = re.compile(r"^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$")
_IBAN_RE = re.compile(r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}$")


class IbanInput(BaseModel):
    iban: str
    bic: str | None = None


class IbanOutput(BaseModel):
    valid: bool
    country: str
    bic_format_valid: bool | None = None


def compute_iban(inp: IbanInput) -> IbanOutput:
    iban = _digits_only(inp.iban).upper()
    if not _IBAN_RE.match(iban):
        raise ComputeError("invalid_format", "IBAN must be 2 letters + 2 check digits + up to 30 alphanumerics")
    rearranged = iban[4:] + iban[:4]
    numeric = "".join(str(int(c, 36)) for c in rearranged)
    valid = int(numeric) % 97 == 1
    bic_ok = bool(_BIC_RE.match(inp.bic.upper())) if inp.bic else None
    return IbanOutput(valid=valid, country=iban[:2], bic_format_valid=bic_ok)


register(ComputeSpec(
    slug="validate/iban", price="$0.002", service_name="validate-iban",
    description="ISO 13616 IBAN mod-97 checksum validation, with optional BIC (ISO 9362) format check.",
    tags=["iban", "bic", "bank account", "validation", "checksum", "swift"],
    input_model=IbanInput, output_model=IbanOutput, compute=compute_iban,
    sample_input={"iban": "GB82 WEST 1234 5698 7654 32", "bic": "NWBKGB2L"},
    sample_output={"valid": True, "country": "GB", "bic_format_valid": True},
))


# ------------------------------------------------------------------- vin
_VIN_TRANS = {**{str(d): d for d in range(10)}, **{
    "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8,
    "J": 1, "K": 2, "L": 3, "M": 4, "N": 5, "P": 7, "R": 9,
    "S": 2, "T": 3, "U": 4, "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9,
}}
_VIN_WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]
_WMI_TABLE = {
    "1HG": ("Honda", "USA"), "1FA": ("Ford", "USA"), "1G1": ("Chevrolet", "USA"),
    "5YJ": ("Tesla", "USA"), "WBA": ("BMW", "Germany"), "WVW": ("Volkswagen", "Germany"),
    "WDD": ("Mercedes-Benz", "Germany"), "JHM": ("Honda", "Japan"), "JN1": ("Nissan", "Japan"),
    "JT2": ("Toyota", "Japan"), "KNA": ("Kia", "South Korea"), "KMH": ("Hyundai", "South Korea"),
    "VF1": ("Renault", "France"), "VF3": ("Peugeot", "France"),
}


class VinInput(BaseModel):
    vin: str = Field(pattern=r"^[A-HJ-NPR-Z0-9]{17}$")


class VinOutput(BaseModel):
    valid_check_digit: bool
    manufacturer: str
    country: str


def compute_vin(inp: VinInput) -> VinOutput:
    vin = inp.vin.upper()
    total = sum(_VIN_TRANS[c] * w for c, w in zip(vin, _VIN_WEIGHTS))
    remainder = total % 11
    expected = "X" if remainder == 10 else str(remainder)
    manufacturer, country = _WMI_TABLE.get(vin[:3], ("unknown", "unknown"))
    return VinOutput(valid_check_digit=(expected == vin[8]), manufacturer=manufacturer, country=country)


register(ComputeSpec(
    slug="validate/vin", price="$0.002", service_name="validate-vin",
    description="North American VIN check-digit validation (NHTSA standard, position 9) plus WMI manufacturer lookup for a small set of known codes.",
    tags=["vin", "vehicle identification number", "wmi", "check digit", "automotive"],
    input_model=VinInput, output_model=VinOutput, compute=compute_vin,
    sample_input={"vin": "1HGCM82633A004352"},
    sample_output={"valid_check_digit": True, "manufacturer": "Honda", "country": "USA"},
))


# ------------------------------------------------------------------ isbn
class IsbnInput(BaseModel):
    isbn: str


class IsbnOutput(BaseModel):
    valid: bool
    format: str


def compute_isbn(inp: IsbnInput) -> IsbnOutput:
    code = _digits_only(inp.isbn).upper()
    if len(code) == 10:
        if not re.match(r"^[0-9]{9}[0-9X]$", code):
            raise ComputeError("invalid_format", "ISBN-10 must be 9 digits plus a check digit (0-9 or X)")
        total = sum((i + 1) * (10 if ch == "X" else int(ch)) for i, ch in enumerate(code))
        return IsbnOutput(valid=total % 11 == 0, format="ISBN-10")
    if len(code) == 13:
        if not code.isdigit():
            raise ComputeError("invalid_format", "ISBN-13 must be 13 digits")
        digits = [int(c) for c in code]
        total = sum(d * (1 if i % 2 == 0 else 3) for i, d in enumerate(digits[:12]))
        check = (10 - total % 10) % 10
        return IsbnOutput(valid=check == digits[12], format="ISBN-13")
    raise ComputeError("invalid_length", "ISBN must be 10 or 13 characters (ignoring hyphens/spaces)")


register(ComputeSpec(
    slug="validate/isbn", price="$0.002", service_name="validate-isbn",
    description="ISBN-10 or ISBN-13 checksum validation (auto-detected by length).",
    tags=["isbn", "book", "validation", "checksum", "publishing"],
    input_model=IsbnInput, output_model=IsbnOutput, compute=compute_isbn,
    sample_input={"isbn": "0-306-40615-2"},
    sample_output={"valid": True, "format": "ISBN-10"},
))


# ------------------------------------------------------------------ luhn
class LuhnInput(BaseModel):
    number: str = Field(pattern=r"^[0-9][0-9\s-]*[0-9]$")


class LuhnOutput(BaseModel):
    valid: bool


def compute_luhn(inp: LuhnInput) -> LuhnOutput:
    digits = _digits_only(inp.number)
    return LuhnOutput(valid=luhn_valid(digits))


register(ComputeSpec(
    slug="validate/luhn", price="$0.002", service_name="validate-luhn",
    description="Luhn (mod 10) checksum validation, as used by most payment card numbers.",
    tags=["luhn", "credit card", "checksum", "mod10", "validation"],
    input_model=LuhnInput, output_model=LuhnOutput, compute=compute_luhn,
    sample_input={"number": "4111111111111111"},
    sample_output={"valid": True},
))


# ------------------------------------------------------------------- ean
class EanInput(BaseModel):
    code: str = Field(pattern=r"^[0-9]{8}$|^[0-9]{13}$")


class EanOutput(BaseModel):
    valid: bool
    format: str


def compute_ean(inp: EanInput) -> EanOutput:
    digits = [int(c) for c in inp.code]
    n = len(digits)
    fmt = "EAN-13" if n == 13 else "EAN-8"
    # EAN-8 and EAN-13 both weight the digit just before the check digit by
    # 3, alternating backward - equivalent to: from the right, excluding the
    # check digit, alternate 3,1,3,1...
    body = digits[:-1]
    total = sum(d * (3 if i % 2 == (len(body) - 1) % 2 else 1) for i, d in enumerate(body))
    check = (10 - total % 10) % 10
    return EanOutput(valid=check == digits[-1], format=fmt)


register(ComputeSpec(
    slug="validate/ean", price="$0.002", service_name="validate-ean",
    description="EAN-8 or EAN-13 barcode checksum validation (auto-detected by length).",
    tags=["ean", "barcode", "upc", "gtin", "checksum", "validation"],
    input_model=EanInput, output_model=EanOutput, compute=compute_ean,
    sample_input={"code": "4006381333931"},
    sample_output={"valid": True, "format": "EAN-13"},
))


# ------------------------------------------------------------------ imei
class ImeiInput(BaseModel):
    imei: str = Field(pattern=r"^[0-9]{15}$")


class ImeiOutput(BaseModel):
    valid: bool


def compute_imei(inp: ImeiInput) -> ImeiOutput:
    return ImeiOutput(valid=luhn_valid(inp.imei))


register(ComputeSpec(
    slug="validate/imei", price="$0.002", service_name="validate-imei",
    description="IMEI (15-digit mobile device identifier) Luhn checksum validation.",
    tags=["imei", "mobile device", "checksum", "luhn", "validation"],
    input_model=ImeiInput, output_model=ImeiOutput, compute=compute_imei,
    sample_input={"imei": "490154203237518"},
    sample_output={"valid": True},
))


# --------------------------------------------------------------- routing
class RoutingInput(BaseModel):
    routing_number: str = Field(pattern=r"^[0-9]{9}$")


class RoutingOutput(BaseModel):
    valid: bool


def compute_routing(inp: RoutingInput) -> RoutingOutput:
    digits = [int(c) for c in inp.routing_number]
    weights = [3, 7, 1, 3, 7, 1, 3, 7, 1]
    total = sum(d * w for d, w in zip(digits, weights))
    return RoutingOutput(valid=total % 10 == 0)


register(ComputeSpec(
    slug="validate/routing", price="$0.002", service_name="validate-routing",
    description="US ABA bank routing number (9-digit) checksum validation.",
    tags=["routing number", "aba", "bank", "checksum", "validation", "us banking"],
    input_model=RoutingInput, output_model=RoutingOutput, compute=compute_routing,
    sample_input={"routing_number": "021000021"},
    sample_output={"valid": True},
))


# ------------------------------------------------------------------ isin
class IsinInput(BaseModel):
    isin: str = Field(pattern=r"^[A-Za-z]{2}[A-Za-z0-9]{9}[0-9]$")


class IsinOutput(BaseModel):
    valid: bool
    country: str


def compute_isin(inp: IsinInput) -> IsinOutput:
    isin = inp.isin.upper()
    body, check = isin[:-1], isin[-1]
    numeric = "".join(str(int(c, 36)) for c in body)
    return IsinOutput(valid=luhn_valid(numeric + check), country=isin[:2])


register(ComputeSpec(
    slug="validate/isin", price="$0.002", service_name="validate-isin",
    description="ISIN (ISO 6166 security identifier) Luhn checksum validation.",
    tags=["isin", "securities", "finance", "checksum", "validation", "iso 6166"],
    input_model=IsinInput, output_model=IsinOutput, compute=compute_isin,
    sample_input={"isin": "US0378331005"},
    sample_output={"valid": True, "country": "US"},
))


# ----------------------------------------------------------------- siret
class SiretInput(BaseModel):
    siret: str = Field(pattern=r"^[0-9]{14}$")


class SiretOutput(BaseModel):
    valid: bool
    siren: str


def compute_siret(inp: SiretInput) -> SiretOutput:
    return SiretOutput(valid=luhn_valid(inp.siret), siren=inp.siret[:9])


register(ComputeSpec(
    slug="validate/siret", price="$0.002", service_name="validate-siret",
    description="French SIRET (14-digit business establishment number) Luhn checksum validation. Note: the rare La Poste SIREN exception to the standard rule is not applied.",
    tags=["siret", "siren", "french business id", "checksum", "validation", "france"],
    input_model=SiretInput, output_model=SiretOutput, compute=compute_siret,
    sample_input={"siret": "55230123450006"},
    sample_output={"valid": True, "siren": "552301234"},
))
