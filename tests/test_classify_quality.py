"""20-case labeled quality gate for POST /classify against a fixed
caller-supplied label set (FR+EN, easy+trap) - see the delivery-conformance
batch this was built for (2026-09-30). Real calls, no mocking."""

import asyncio

from app.handlers.classify import _lookup

LABELS = ["billing", "technical", "shipping", "other"]

CASES = [
    ("I was charged twice for the same order.", "billing"),
    ("The app crashes every time I open it.", "technical"),
    ("My package hasn't arrived and it's been two weeks.", "shipping"),
    ("What are your business hours?", "other"),
    ("J'ai ete facture deux fois pour la meme commande.", "billing"),
    ("L'application plante a chaque ouverture.", "technical"),
    ("Mon colis n'est toujours pas arrive apres deux semaines.", "shipping"),
    ("Quels sont vos horaires d'ouverture ?", "other"),
    ("Can you refund the shipping fee I was overcharged?", "billing"),
    ("Pouvez-vous rembourser les frais de livraison qui ont ete surfactures ?", "billing"),
    ("The tracking number you gave me doesn't work on the carrier's website.", "shipping"),
    ("Le numero de suivi que vous m'avez donne ne fonctionne pas sur le site du transporteur.", "shipping"),
    ("My credit card was declined at checkout.", "billing"),
    ("Ma carte de credit a ete refusee au paiement.", "billing"),
    ("The website shows an error 500 when I try to log in.", "technical"),
    ("Le site affiche une erreur 500 quand j'essaie de me connecter.", "technical"),
    ("Can I change my delivery address after placing the order?", "shipping"),
    ("Puis-je changer mon adresse de livraison apres avoir passe la commande ?", "shipping"),
    ("Do you offer gift wrapping?", "other"),
    ("Proposez-vous l'emballage cadeau ?", "other"),
]


def test_classify_accuracy_at_least_90_percent() -> None:
    correct = 0
    wrong = []
    for text, expected in CASES:
        result = asyncio.run(_lookup({"text": text, "labels": LABELS}))
        if result["label"] == expected:
            correct += 1
        else:
            wrong.append((text, expected, result["label"]))
    accuracy = correct / len(CASES)
    assert accuracy >= 0.90, (
        f"classify accuracy {accuracy:.0%} ({correct}/{len(CASES)}) below the 90% gate - "
        f"wrong: {wrong}"
    )
