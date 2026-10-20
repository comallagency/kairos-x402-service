"""20-case labeled quality gate for POST /pii-check (FR+EN,
easy+trap) - see the delivery-conformance batch this was built for
(2026-09-30). Real calls, no mocking - matches this repo's existing test
convention (see test_detect_language_query.py). Asserts an aggregate 90%
accuracy bar, not per-case, since this is a probabilistic classifier."""

import asyncio

from app.handlers.pii_check import _lookup

CASES = [
    ("My name is John Smith, email john.smith@gmail.com, phone 555-234-5678.", "pii_detected"),
    ("The quarterly revenue increased by 15% compared to last year.", "no_pii"),
    ("Je m'appelle Marie Dupont, mon email est marie.dupont@gmail.com.", "pii_detected"),
    ("Le chiffre d'affaires trimestriel a augmente de 15%.", "no_pii"),
    ("Please send the check to 123 Main Street, Springfield, account number 4829371.", "pii_detected"),
    ("Merci d'envoyer le cheque au 123 rue Principale, compte numero 4829371.", "pii_detected"),
    ("Our new product launch is scheduled for next quarter.", "no_pii"),
    ("Le lancement de notre nouveau produit est prevu le trimestre prochain.", "no_pii"),
    ("Smith and Jones LLP is a well-known law firm.", "no_pii"),
    ("Dupont et Martin SARL est un cabinet bien connu.", "no_pii"),
    ("The internal product code for this item is 555-123-4567.", "no_pii"),
    ("Le code produit interne de cet article est le 555-123-4567.", "no_pii"),
    ("Social Security Number: 123-45-6789.", "pii_detected"),
    ("Numero de securite sociale : 1 85 12 75 108 001 42.", "pii_detected"),
    ("The meeting will focus on marketing strategy for Q3.", "no_pii"),
    ("La reunion portera sur la strategie marketing du troisieme trimestre.", "no_pii"),
    ("Please update my address to 456 Oak Avenue, Apt 3B, and my number to 555-987-6543.", "pii_detected"),
    ("Merci de mettre a jour mon adresse au 456 avenue des Chenes et mon numero au 0612345678.", "pii_detected"),
    ("The president gave a speech at the White House today.", "no_pii"),
    ("Le president a prononce un discours a l'Elysee aujourd'hui.", "no_pii"),
]


def test_pii_check_accuracy_at_least_90_percent() -> None:
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
        f"pii_check accuracy {accuracy:.0%} ({correct}/{len(CASES)}) below the 90% gate - "
        f"wrong: {wrong}"
    )
