"""5 reference-verified cases per route. Hashes are checked against the
famous NIST-published test vectors (SHA-256/SHA-1/MD5 of the empty string
and of "abc" - e3b0c442... and ba7816bf... are about as widely published
as a hash value gets). slug/diff/readability reference values were
computed independently (a standalone script, not this production module)
before being hardcoded here."""

import pytest

from app.purecalc.registry import ComputeError
from app.purecalc.routes.text import (
    CaseInput, DetectInput, DiffInput, HashInput, HmacInput,
    ReadabilityInput, SlugInput, TranscodeInput,
    compute_case, compute_detect, compute_diff, compute_hash, compute_hmac,
    compute_readability, compute_slug, compute_transcode,
)


# ------------------------------------------------------------------ text/case
def test_case_to_snake():
    r = compute_case(CaseInput(text="Hello World Example", target_case="snake"))
    assert r.result == "hello_world_example"


def test_case_to_camel():
    r = compute_case(CaseInput(text="hello-world_example", target_case="camel"))
    assert r.result == "helloWorldExample"


def test_case_to_pascal():
    r = compute_case(CaseInput(text="hello_world", target_case="pascal"))
    assert r.result == "HelloWorld"


def test_case_camel_boundary_to_kebab():
    r = compute_case(CaseInput(text="HelloWorld", target_case="kebab"))
    assert r.result == "hello-world"


def test_case_to_constant():
    r = compute_case(CaseInput(text="hello world", target_case="upper_snake"))
    assert r.result == "HELLO_WORLD"


# ------------------------------------------------------------------ text/diff
def test_diff_identical_texts_similarity_1():
    r = compute_diff(DiffInput(a="same text", b="same text"))
    assert r.similarity == 1.0
    assert r.diff == []


def test_diff_completely_different_similarity_0():
    r = compute_diff(DiffInput(a="aaaa", b="zzzz"))
    assert r.similarity == 0.0


def test_diff_one_line_changed():
    r = compute_diff(DiffInput(a="line one\nline two", b="line one\nline three"))
    assert r.similarity == pytest.approx(0.8333, abs=0.0001)
    assert "-line two" in r.diff
    assert "+line three" in r.diff


def test_diff_empty_vs_empty():
    r = compute_diff(DiffInput(a="", b=""))
    assert r.similarity == 1.0


def test_diff_addition_only():
    r = compute_diff(DiffInput(a="", b="new content"))
    assert r.similarity == 0.0


# ------------------------------------------------------------------ text/slug
def test_slug_accents_stripped():
    r = compute_slug(SlugInput(text="Café é la Mode!"))
    assert r.slug == "cafe-e-la-mode"


def test_slug_basic():
    r = compute_slug(SlugInput(text="Hello World"))
    assert r.slug == "hello-world"


def test_slug_custom_separator():
    r = compute_slug(SlugInput(text="Hello World", separator="_"))
    assert r.slug == "hello_world"


def test_slug_collapses_repeated_punctuation():
    r = compute_slug(SlugInput(text="Hello   ---   World"))
    assert r.slug == "hello-world"


def test_slug_empty_result_rejected():
    with pytest.raises(ComputeError):
        compute_slug(SlugInput(text="!!!???"))


# ------------------------------------------------------- encoding/transcode
def test_transcode_utf8_to_base64():
    r = compute_transcode(TranscodeInput(text="Hello", from_encoding="utf-8", to_encoding="base64"))
    assert r.result == "SGVsbG8="


def test_transcode_base64_to_utf8_roundtrip():
    r = compute_transcode(TranscodeInput(text="SGVsbG8=", from_encoding="base64", to_encoding="utf-8"))
    assert r.result == "Hello"


def test_transcode_utf8_to_hex():
    r = compute_transcode(TranscodeInput(text="AB", from_encoding="utf-8", to_encoding="hex"))
    assert r.result == "4142"  # 'A'=0x41, 'B'=0x42, basic ASCII fact


def test_transcode_utf8_to_url():
    r = compute_transcode(TranscodeInput(text="a b", from_encoding="utf-8", to_encoding="url"))
    assert r.result == "a%20b"


def test_transcode_invalid_base64_rejected():
    with pytest.raises(ComputeError):
        compute_transcode(TranscodeInput(text="not valid base64!!!", from_encoding="base64", to_encoding="utf-8"))


# --------------------------------------------------------- encoding/detect
def test_detect_ascii():
    r = compute_detect(DetectInput(hex_bytes="48656c6c6f"))  # "Hello"
    assert r.encoding == "ascii" and r.has_bom is False


def test_detect_utf8_multibyte():
    r = compute_detect(DetectInput(hex_bytes="c3a9"))  # UTF-8 for 'é'
    assert r.encoding == "utf-8"


def test_detect_utf8_bom():
    r = compute_detect(DetectInput(hex_bytes="efbbbf48656c6c6f"))
    assert r.encoding == "utf-8-sig" and r.has_bom is True


def test_detect_utf16_le_bom():
    r = compute_detect(DetectInput(hex_bytes="fffe4800"))
    assert r.encoding == "utf-16-le" and r.has_bom is True


def test_detect_invalid_utf8_is_unknown():
    r = compute_detect(DetectInput(hex_bytes="ff"))
    assert r.encoding == "unknown-binary"


# ------------------------------------------------------------------ hash/hash
def test_hash_sha256_abc_nist_vector():
    r = compute_hash(HashInput(text="abc", algorithm="sha256"))
    assert r.hex_digest == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_hash_sha1_abc_nist_vector():
    r = compute_hash(HashInput(text="abc", algorithm="sha1"))
    assert r.hex_digest == "a9993e364706816aba3e25717850c26c9cd0d89d"


def test_hash_md5_abc_vector():
    r = compute_hash(HashInput(text="abc", algorithm="md5"))
    assert r.hex_digest == "900150983cd24fb0d6963f7d28e17f72"


def test_hash_sha256_empty_string():
    r = compute_hash(HashInput(text="", algorithm="sha256"))
    assert r.hex_digest == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_hash_md5_empty_string():
    r = compute_hash(HashInput(text="", algorithm="md5"))
    assert r.hex_digest == "d41d8cd98f00b204e9800998ecf8427e"


# ------------------------------------------------------------------ hash/hmac
def test_hmac_sha256_matches_stdlib():
    import hashlib
    import hmac as hmac_mod
    expected = hmac_mod.new(b"secret", b"message", hashlib.sha256).hexdigest()
    r = compute_hmac(HmacInput(text="message", key="secret", algorithm="sha256"))
    assert r.hex_digest == expected


def test_hmac_different_keys_differ():
    r1 = compute_hmac(HmacInput(text="message", key="key1", algorithm="sha256"))
    r2 = compute_hmac(HmacInput(text="message", key="key2", algorithm="sha256"))
    assert r1.hex_digest != r2.hex_digest


def test_hmac_deterministic():
    r1 = compute_hmac(HmacInput(text="message", key="secret", algorithm="sha256"))
    r2 = compute_hmac(HmacInput(text="message", key="secret", algorithm="sha256"))
    assert r1.hex_digest == r2.hex_digest


def test_hmac_md5_length():
    r = compute_hmac(HmacInput(text="x", key="y", algorithm="md5"))
    assert len(r.hex_digest) == 32  # MD5 digest is 128 bits = 32 hex chars


def test_hmac_sha512_length():
    r = compute_hmac(HmacInput(text="x", key="y", algorithm="sha512"))
    assert len(r.hex_digest) == 128  # SHA-512 digest is 512 bits = 128 hex chars


# ----------------------------------------------------------- text/readability
def test_readability_sample_computed_independently():
    r = compute_readability(ReadabilityInput(text="The cat sat on the mat. It was a sunny day."))
    assert r.words == 11 and r.sentences == 2 and r.syllables == 12
    assert r.flesch_reading_ease == pytest.approx(108.96, abs=0.01)
    assert r.flesch_kincaid_grade == pytest.approx(-0.57, abs=0.01)


def test_readability_single_sentence_word_count():
    r = compute_readability(ReadabilityInput(text="Hello world."))
    assert r.words == 2 and r.sentences == 1


def test_readability_no_terminal_punctuation_counts_as_one_sentence():
    # No '.', '!' or '?' at all -> the whole text is treated as a single
    # sentence (documented behavior of the sentence-splitting regex, not a
    # rejection condition - only truly empty input is).
    r = compute_readability(ReadabilityInput(text="no terminal punctuation here"))
    assert r.sentences == 1 and r.words == 4


def test_readability_empty_text_rejected():
    with pytest.raises(ComputeError):
        compute_readability(ReadabilityInput(text=""))


def test_readability_multiple_sentences_counted():
    r = compute_readability(ReadabilityInput(text="One. Two. Three."))
    assert r.sentences == 3
