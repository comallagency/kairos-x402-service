"""20-case labeled quality gate for POST /sentiment (FR+EN,
easy+trap) - see the delivery-conformance batch this was built for
(2026-09-30). Real calls, no mocking - matches this repo's existing test
convention (see test_detect_language_query.py). Asserts an aggregate 90%
accuracy bar, not per-case, since this is a probabilistic classifier."""

import asyncio

from app.handlers.sentiment import _lookup

CASES = [
    ("I absolutely love this product, it's amazing!", "positive"),
    ("This is the worst service I've ever experienced.", "negative"),
    ("The meeting is scheduled for 3pm on Thursday.", "neutral"),
    ("Je suis vraiment ravi de cet achat, parfait !", "positive"),
    ("C'est une catastrophe, je suis tres decu.", "negative"),
    ("Le rapport sera envoye demain matin.", "neutral"),
    ("I can't say I'm unhappy with the results.", "positive"),
    ("Wow, another broken promise. Great job.", "negative"),
    ("The food was cold but the service was excellent overall.", "positive"),
    ("Je ne peux pas dire que je suis mecontent.", "positive"),
    ("Super, encore un retard, exactement ce qu'il me fallait.", "negative"),
    ("Thank you so much, this made my day!", "positive"),
    ("I'm extremely frustrated and disappointed.", "negative"),
    ("The document contains twelve pages.", "neutral"),
    ("Merci infiniment, c'est exactement ce que je voulais !", "positive"),
    ("Je suis furieux, ce produit est inutilisable.", "negative"),
    ("La reunion aura lieu dans la salle B.", "neutral"),
    ("It's okay, does the job, nothing special.", "neutral"),
    ("C'est correct, sans plus.", "neutral"),
    ("Well, that could have gone better.", "negative"),
]


def test_sentiment_accuracy_at_least_90_percent() -> None:
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
        f"sentiment accuracy {accuracy:.0%} ({correct}/{len(CASES)}) below the 90% gate - "
        f"wrong: {wrong}"
    )
