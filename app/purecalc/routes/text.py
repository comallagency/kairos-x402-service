"""Text/encoding pure-compute routes - stdlib only (difflib, hashlib, hmac,
unicodedata, base64, urllib.parse)."""

import base64
import binascii
import difflib
import hashlib
import hmac as hmac_mod
import re
import unicodedata
import urllib.parse

from pydantic import BaseModel, Field

from app.purecalc.registry import ComputeError, ComputeSpec, register


# ---------------------------------------------------------------- text/case
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_DELIMS = re.compile(r"[\s_\-]+")


def _tokenize(text: str) -> list[str]:
    text = _CAMEL_BOUNDARY.sub(" ", text)
    text = _DELIMS.sub(" ", text)
    return [w for w in text.split(" ") if w]


class CaseInput(BaseModel):
    text: str
    target_case: str = Field(pattern="^(snake|camel|pascal|kebab|title|upper_snake|constant)$")


class CaseOutput(BaseModel):
    result: str


def compute_case(inp: CaseInput) -> CaseOutput:
    words = [w.lower() for w in _tokenize(inp.text)]
    if not words:
        raise ComputeError("empty_text", "no word boundaries found in input text")

    if inp.target_case == "snake":
        result = "_".join(words)
    elif inp.target_case in ("upper_snake", "constant"):
        result = "_".join(w.upper() for w in words)
    elif inp.target_case == "kebab":
        result = "-".join(words)
    elif inp.target_case == "camel":
        result = words[0] + "".join(w.capitalize() for w in words[1:])
    elif inp.target_case == "pascal":
        result = "".join(w.capitalize() for w in words)
    elif inp.target_case == "title":
        result = " ".join(w.capitalize() for w in words)
    else:
        raise ComputeError("unknown_case", f"{inp.target_case!r} is not a supported case style")
    return CaseOutput(result=result)


register(ComputeSpec(
    slug="text/case", price="$0.001", service_name="text-case",
    description="Convert text between case styles: snake_case, camelCase, PascalCase, kebab-case, Title Case, CONSTANT_CASE.",
    tags=["case conversion", "snake case", "camel case", "kebab case", "text formatting"],
    input_model=CaseInput, output_model=CaseOutput, compute=compute_case,
    sample_input={"text": "Hello World Example", "target_case": "snake"},
    sample_output={"result": "hello_world_example"},
))


# ---------------------------------------------------------------- text/diff
class DiffInput(BaseModel):
    a: str
    b: str


class DiffOutput(BaseModel):
    diff: list[str]
    similarity: float


def compute_diff(inp: DiffInput) -> DiffOutput:
    a_lines = inp.a.splitlines()
    b_lines = inp.b.splitlines()
    diff = list(difflib.unified_diff(a_lines, b_lines, lineterm=""))
    similarity = round(difflib.SequenceMatcher(None, inp.a, inp.b).ratio(), 4)
    return DiffOutput(diff=diff, similarity=similarity)


register(ComputeSpec(
    slug="text/diff", price="$0.001", service_name="text-diff",
    description="Unified line diff between two texts, plus an overall similarity ratio (0-1).",
    tags=["text diff", "unified diff", "compare text", "similarity"],
    input_model=DiffInput, output_model=DiffOutput, compute=compute_diff,
    sample_input={"a": "line one\nline two", "b": "line one\nline three"},
    sample_output={"diff": ["--- ", "+++ ", "@@ -1,2 +1,2 @@", " line one", "-line two", "+line three"], "similarity": 0.8333},
))


# ---------------------------------------------------------------- text/slug
class SlugInput(BaseModel):
    text: str
    separator: str = "-"


class SlugOutput(BaseModel):
    slug: str


def compute_slug(inp: SlugInput) -> SlugOutput:
    normalized = unicodedata.normalize("NFKD", inp.text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_text.lower()
    cleaned = re.sub(r"[^a-z0-9]+", inp.separator, lowered)
    slug = cleaned.strip(inp.separator)
    sep_escaped = re.escape(inp.separator)
    slug = re.sub(f"{sep_escaped}+", inp.separator, slug)
    if not slug:
        raise ComputeError("empty_result", "input produced an empty slug (no alphanumeric characters)")
    return SlugOutput(slug=slug)


register(ComputeSpec(
    slug="text/slug", price="$0.002", service_name="text-slug",
    description="Slugify text: strip accents, lowercase, replace non-alphanumerics with a separator.",
    tags=["slugify", "url slug", "text normalization", "seo"],
    input_model=SlugInput, output_model=SlugOutput, compute=compute_slug,
    sample_input={"text": "Café é la Mode!"},
    sample_output={"slug": "cafe-e-la-mode"},
))


# ----------------------------------------------------------- encoding/transcode
_ENCODINGS = ("utf-8", "base64", "hex", "url", "ascii")


class TranscodeInput(BaseModel):
    text: str
    from_encoding: str = Field(pattern="^(utf-8|base64|hex|url|ascii)$")
    to_encoding: str = Field(pattern="^(utf-8|base64|hex|url|ascii)$")


class TranscodeOutput(BaseModel):
    result: str


def _decode_to_bytes(text: str, encoding: str) -> bytes:
    try:
        if encoding == "utf-8":
            return text.encode("utf-8")
        if encoding == "ascii":
            return text.encode("ascii")
        if encoding == "base64":
            return base64.b64decode(text)
        if encoding == "hex":
            return binascii.unhexlify(text)
        if encoding == "url":
            return urllib.parse.unquote(text).encode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError, binascii.Error, ValueError) as exc:
        raise ComputeError("decode_error", f"could not decode input as {encoding}: {exc}") from exc


def _encode_from_bytes(data: bytes, encoding: str) -> str:
    if encoding == "utf-8":
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ComputeError("encode_error", f"result is not valid utf-8: {exc}") from exc
    if encoding == "ascii":
        try:
            return data.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ComputeError("encode_error", f"result is not valid ascii: {exc}") from exc
    if encoding == "base64":
        return base64.b64encode(data).decode("ascii")
    if encoding == "hex":
        return data.hex()
    if encoding == "url":
        return urllib.parse.quote(data)


def compute_transcode(inp: TranscodeInput) -> TranscodeOutput:
    data = _decode_to_bytes(inp.text, inp.from_encoding)
    return TranscodeOutput(result=_encode_from_bytes(data, inp.to_encoding))


register(ComputeSpec(
    slug="encoding/transcode", price="$0.002", service_name="encoding-transcode",
    description="Convert text between utf-8, base64, hex, URL-percent-encoding and ascii representations.",
    tags=["encoding", "base64", "hex", "url encoding", "transcode"],
    input_model=TranscodeInput, output_model=TranscodeOutput, compute=compute_transcode,
    sample_input={"text": "Hello", "from_encoding": "utf-8", "to_encoding": "base64"},
    sample_output={"result": "SGVsbG8="},
))


# ------------------------------------------------------------ encoding/detect
class DetectInput(BaseModel):
    hex_bytes: str


class DetectOutput(BaseModel):
    encoding: str
    has_bom: bool


def compute_detect(inp: DetectInput) -> DetectOutput:
    try:
        data = binascii.unhexlify(inp.hex_bytes)
    except (binascii.Error, ValueError) as exc:
        raise ComputeError("invalid_hex", f"{inp.hex_bytes!r} is not valid hex: {exc}") from exc

    if data.startswith(b"\xef\xbb\xbf"):
        return DetectOutput(encoding="utf-8-sig", has_bom=True)
    if data.startswith(b"\xff\xfe"):
        return DetectOutput(encoding="utf-16-le", has_bom=True)
    if data.startswith(b"\xfe\xff"):
        return DetectOutput(encoding="utf-16-be", has_bom=True)
    try:
        data.decode("ascii")
        return DetectOutput(encoding="ascii", has_bom=False)
    except UnicodeDecodeError:
        pass
    try:
        data.decode("utf-8")
        return DetectOutput(encoding="utf-8", has_bom=False)
    except UnicodeDecodeError:
        pass
    return DetectOutput(encoding="unknown-binary", has_bom=False)


register(ComputeSpec(
    slug="encoding/detect", price="$0.002", service_name="encoding-detect",
    description="Detect whether hex-encoded bytes are ASCII, UTF-8, or UTF-16 with a BOM, by decode-validity and BOM presence (not statistical language detection).",
    tags=["encoding detection", "charset", "bom", "utf-8", "ascii"],
    input_model=DetectInput, output_model=DetectOutput, compute=compute_detect,
    sample_input={"hex_bytes": "48656c6c6f"},
    sample_output={"encoding": "ascii", "has_bom": False},
))


# --------------------------------------------------------------------- hash/hash
class HashInput(BaseModel):
    text: str
    algorithm: str = Field(pattern="^(md5|sha1|sha256|sha512)$")


class HashOutput(BaseModel):
    hex_digest: str


def compute_hash(inp: HashInput) -> HashOutput:
    h = hashlib.new(inp.algorithm, inp.text.encode("utf-8"))
    return HashOutput(hex_digest=h.hexdigest())


register(ComputeSpec(
    slug="hash/hash", price="$0.002", service_name="hash-hash",
    description="Compute an MD5, SHA-1, SHA-256 or SHA-512 hex digest of text.",
    tags=["hash", "sha256", "md5", "sha1", "checksum", "digest"],
    input_model=HashInput, output_model=HashOutput, compute=compute_hash,
    sample_input={"text": "abc", "algorithm": "sha256"},
    sample_output={"hex_digest": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"},
))


# --------------------------------------------------------------------- hash/hmac
class HmacInput(BaseModel):
    text: str
    key: str
    algorithm: str = Field(default="sha256", pattern="^(md5|sha1|sha256|sha512)$")


class HmacOutput(BaseModel):
    hex_digest: str


def compute_hmac(inp: HmacInput) -> HmacOutput:
    digestmod = getattr(hashlib, inp.algorithm)
    mac = hmac_mod.new(inp.key.encode("utf-8"), inp.text.encode("utf-8"), digestmod)
    return HmacOutput(hex_digest=mac.hexdigest())


register(ComputeSpec(
    slug="hash/hmac", price="$0.002", service_name="hash-hmac",
    description="Compute an HMAC (MD5, SHA-1, SHA-256 or SHA-512) of text with a given key.",
    tags=["hmac", "hash", "authentication", "sha256", "message authentication code"],
    input_model=HmacInput, output_model=HmacOutput, compute=compute_hmac,
    sample_input={"text": "message", "key": "secret", "algorithm": "sha256"},
    sample_output={"hex_digest": "8b5f48702995c1598c573db1e21866a9b825d4a794d169d7060a03605796360b"},
))


# ---------------------------------------------------------- text/readability
_VOWELS = "aeiouy"


def _count_syllables(word: str) -> int:
    word = word.lower()
    if not word:
        return 0
    groups = re.findall(r"[aeiouy]+", word)
    count = len(groups)
    if word.endswith("e") and not word.endswith("le") and count > 1:
        count -= 1
    return max(count, 1)


class ReadabilityInput(BaseModel):
    text: str


class ReadabilityOutput(BaseModel):
    flesch_reading_ease: float
    flesch_kincaid_grade: float
    words: int
    sentences: int
    syllables: int


def compute_readability(inp: ReadabilityInput) -> ReadabilityOutput:
    words = re.findall(r"[A-Za-z']+", inp.text)
    sentences = [s for s in re.split(r"[.!?]+", inp.text) if s.strip()]
    if not words or not sentences:
        raise ComputeError("insufficient_text", "need at least one word and one sentence")

    syllables = sum(_count_syllables(w) for w in words)
    n_words, n_sentences = len(words), len(sentences)

    ease = 206.835 - 1.015 * (n_words / n_sentences) - 84.6 * (syllables / n_words)
    grade = 0.39 * (n_words / n_sentences) + 11.8 * (syllables / n_words) - 15.59
    return ReadabilityOutput(
        flesch_reading_ease=round(ease, 2), flesch_kincaid_grade=round(grade, 2),
        words=n_words, sentences=n_sentences, syllables=syllables,
    )


register(ComputeSpec(
    slug="text/readability", price="$0.001", service_name="text-readability",
    description="Flesch Reading Ease and Flesch-Kincaid Grade Level scores for English text (standard public formulas, heuristic syllable counting).",
    tags=["readability", "flesch reading ease", "flesch-kincaid", "text analysis"],
    input_model=ReadabilityInput, output_model=ReadabilityOutput, compute=compute_readability,
    sample_input={"text": "The cat sat on the mat. It was a sunny day."},
    sample_output={"flesch_reading_ease": 108.96, "flesch_kincaid_grade": -0.57, "words": 11, "sentences": 2, "syllables": 12},
))
