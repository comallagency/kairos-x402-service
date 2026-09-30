"""20-case labeled quality gate for POST /spam-check (FR+EN,
easy+trap) - see the delivery-conformance batch this was built for
(2026-09-30). Real calls, no mocking - matches this repo's existing test
convention (see test_detect_language_query.py). Asserts an aggregate 90%
accuracy bar, not per-case, since this is a probabilistic classifier."""

import asyncio

from app.handlers.spam_check import _lookup

CASES = [
    ("CONGRATULATIONS! You've won a free iPhone! Click here now!!!", "spam"),
    ("Hi Mom, just checking in, call me when you can.", "not_spam"),
    ("FELICITATIONS ! Vous avez gagne un iPhone gratuit ! Cliquez ici !", "spam"),
    ("Salut, je confirme notre rendez-vous de demain.", "not_spam"),
    ("Buy cheap watches now, 90% off, limited time offer, act fast!", "spam"),
    ("Achetez des montres pas cheres maintenant, -90%, offre limitee !", "spam"),
    ("The quarterly report is attached, let me know your thoughts.", "not_spam"),
    ("Le rapport trimestriel est en piece jointe, dites-moi ce que vous en pensez.", "not_spam"),
    ("Reminder: your dentist appointment is tomorrow at 10am.", "not_spam"),
    ("Rappel : votre rendez-vous chez le dentiste est demain a 10h.", "not_spam"),
    ("Your account has been suspended, verify your password immediately at this link.", "spam"),
    ("Votre compte a ete suspendu, verifiez votre mot de passe immediatement sur ce lien.", "spam"),
    ("Hey, are we still on for lunch?", "not_spam"),
    ("Salut, on se voit toujours pour le dejeuner ?", "not_spam"),
    ("Make $5000 a week working from home, no experience needed, sign up now!", "spam"),
    ("Gagnez 5000 euros par semaine depuis chez vous, aucune experience requise, inscrivez-vous !", "spam"),
    ("Please find attached the minutes from yesterday's meeting.", "not_spam"),
    ("Veuillez trouver ci-joint le compte-rendu de la reunion d'hier.", "not_spam"),
    ("URGENT: Please review and sign the contract before end of day.", "not_spam"),
    ("URGENT : merci de relire et signer le contrat avant la fin de journee.", "not_spam"),
]


def test_spam_check_accuracy_at_least_90_percent() -> None:
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
        f"spam_check accuracy {accuracy:.0%} ({correct}/{len(CASES)}) below the 90% gate - "
        f"wrong: {wrong}"
    )
