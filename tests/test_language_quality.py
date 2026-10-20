"""20-case labeled quality gate for POST /language (FR+EN,
easy+trap) - see the delivery-conformance batch this was built for
(2026-09-30). Real calls, no mocking - matches this repo's existing test
convention (see test_detect_language_query.py). Asserts an aggregate 90%
accuracy bar, not per-case, since this is a probabilistic classifier."""

import asyncio

from app.handlers.language import _lookup

CASES = [
    ("The weather is beautiful today.", "en"),
    ("Il fait tres beau aujourd'hui.", "fr"),
    ("El clima esta hermoso hoy.", "es"),
    ("Das Wetter ist heute wunderschoen.", "de"),
    ("Il tempo oggi e bellissimo.", "it"),
    ("O tempo esta lindo hoje.", "pt"),
    ("Het weer is vandaag prachtig.", "nl"),
    ("\u0421\u0435\u0433\u043e\u0434\u043d\u044f \u043f\u0440\u0435\u043a\u0440\u0430\u0441\u043d\u0430\u044f \u043f\u043e\u0433\u043e\u0434\u0430.", "ru"),
    ("\u4eca\u5929\u5929\u6c14\u5f88\u597d\u3002", "zh"),
    ("\u4eca\u65e5\u306f\u3068\u3066\u3082\u826f\u3044\u5929\u6c17\u3067\u3059\u3002", "ja"),
    ("\uc624\ub298 \ub0a0\uc528\uac00 \uc815\ub9d0 \uc88b\uc544\uc694.", "ko"),
    ("\u0627\u0644\u0637\u0642\u0633 \u062c\u0645\u064a\u0644 \u0627\u0644\u064a\u0648\u0645.", "ar"),
    ("\u0906\u091c \u092e\u094c\u0938\u092e \u092c\u0939\u0941\u0924 \u0938\u0941\u0902\u0926\u0930 \u0939\u0948\u0964", "hi"),
    ("Bugun hava cok guzel.", "tr"),
    ("Dzisiaj jest piekna pogoda.", "pl"),
    ("Hom nay thoi tiet rat dep.", "vi"),
    ("\u0e27\u0e31\u0e19\u0e19\u0e35\u0e49\u0e2d\u0e32\u0e01\u0e32\u0e28\u0e14\u0e35\u0e21\u0e32\u0e01", "th"),
    ("Cuaca hari ini sangat indah.", "id"),
    ("Vadret ar vackert idag.", "sv"),
    ("\u0421\u044c\u043e\u0433\u043e\u0434\u043d\u0456 \u043f\u0440\u0435\u043a\u0440\u0430\u0441\u043d\u0430 \u043f\u043e\u0433\u043e\u0434\u0430.", "uk"),
]


def test_language_accuracy_at_least_90_percent() -> None:
    correct = 0
    wrong = []
    for text, expected in CASES:
        result = asyncio.run(_lookup({"text": text}))
        if result["label"] == expected:
            correct += 1
        else:
            wrong.append((text, expected, result["label"]))
    accuracy = correct / len(CASES)
    assert accuracy >= 0.90, (
        f"language accuracy {accuracy:.0%} ({correct}/{len(CASES)}) below the 90% gate - "
        f"wrong: {wrong}"
    )
