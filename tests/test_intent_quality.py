"""20-case labeled quality gate for POST /intent (FR+EN,
easy+trap) - see the delivery-conformance batch this was built for
(2026-09-30). Real calls, no mocking - matches this repo's existing test
convention (see test_detect_language_query.py). Asserts an aggregate 90%
accuracy bar, not per-case, since this is a probabilistic classifier."""

import asyncio

from app.handlers.intent import _lookup

CASES = [
    ("What time does the store close?", "question"),
    ("Please send me the invoice by Friday.", "request"),
    ("This is unacceptable, my order never arrived.", "complaint"),
    ("Your support team was fantastic, thank you!", "compliment"),
    ("Quelle est la date limite pour postuler ?", "question"),
    ("Pourriez-vous m'envoyer le contrat signe ?", "request"),
    ("Je suis tres mecontent, mon colis est perdu.", "complaint"),
    ("Bravo pour votre excellent service !", "compliment"),
    ("The sky is blue today.", "other"),
    ("Il fait beau aujourd'hui.", "other"),
    ("Can you send me my order status?", "request"),
    ("Why does this always happen to me?", "complaint"),
    ("Pourquoi est-ce que ca ne marche jamais ?", "complaint"),
    ("Cancel my subscription immediately.", "request"),
    ("Annulez mon abonnement immediatement.", "request"),
    ("I've been waiting on hold for an hour, this is ridiculous.", "complaint"),
    ("J'attends depuis une heure, c'est ridicule.", "complaint"),
    ("I just wanted to say your app is incredibly well designed.", "compliment"),
    ("Je voulais juste dire que votre application est superbement concue.", "compliment"),
    ("What are your opening hours?", "question"),
]


def test_intent_accuracy_at_least_90_percent() -> None:
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
        f"intent accuracy {accuracy:.0%} ({correct}/{len(CASES)}) below the 90% gate - "
        f"wrong: {wrong}"
    )
