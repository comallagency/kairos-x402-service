"""20-case labeled quality gate for POST /toxicity (FR+EN,
easy+trap) - see the delivery-conformance batch this was built for
(2026-09-30). Real calls, no mocking - matches this repo's existing test
convention (see test_detect_language_query.py). Asserts an aggregate 90%
accuracy bar, not per-case, since this is a probabilistic classifier."""

import asyncio

from app.handlers.toxicity import _lookup

CASES = [
    ("You're a disgusting piece of garbage and everyone hates you.", "toxic"),
    ("I think we should reconsider this approach.", "not_toxic"),
    ("Tu es une ordure et tout le monde te deteste.", "toxic"),
    ("Je pense qu'on devrait reconsiderer cette approche.", "not_toxic"),
    ("Shut up, nobody wants to hear your stupid opinion.", "toxic"),
    ("Ferme-la, personne ne veut entendre ton avis stupide.", "toxic"),
    ("I strongly disagree with your point, but I see where you're coming from.", "not_toxic"),
    ("Je ne suis pas du tout d'accord, mais je comprends ton point de vue.", "not_toxic"),
    ("This code is badly written and needs a complete rewrite.", "not_toxic"),
    ("Ce code est mal ecrit et doit etre entierement reecrit.", "not_toxic"),
    ("I will find you and make you regret this.", "toxic"),
    ("Je vais te retrouver et te faire regretter ca.", "toxic"),
    ("That was a pretty dumb mistake, honestly.", "not_toxic"),
    ("C'etait vraiment une erreur idiote, honnetement.", "not_toxic"),
    ("Great effort, but there's room for improvement.", "not_toxic"),
    ("Beau travail, mais il y a encore des points a ameliorer.", "not_toxic"),
    ("People like you don't deserve to exist.", "toxic"),
    ("Les gens comme toi ne meritent pas d'exister.", "toxic"),
    ("Oh wow, what a genius idea, truly the dumbest thing I've ever heard.", "not_toxic"),
    ("Oh genial, quelle idee de genie, vraiment la chose la plus stupide que j'aie entendue.", "not_toxic"),
]


def test_toxicity_accuracy_at_least_90_percent() -> None:
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
        f"toxicity accuracy {accuracy:.0%} ({correct}/{len(CASES)}) below the 90% gate - "
        f"wrong: {wrong}"
    )
