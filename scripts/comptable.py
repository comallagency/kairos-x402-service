#!/usr/bin/env python3
"""Comptable - mécanique, zéro appel de modèle. Lit la chaîne, jamais les
compteurs des tiers : synchronise chain_payments.py (déjà fonctionnel,
inchangé) puis calcule la répartition par route (paiements, acheteurs
distincts, taux de retour, part mécanique) en corrélant chaque transfert
on-chain avec nos propres lignes `requests` - voir db.route_chain_stats()
pour la méthode et ses limites (deux routes au même prix, appariement au
plus proche dans une fenêtre de 5 minutes).

Remplace l'ancien cron horaire de chain_payments.py (toujours utilisable
seul si besoin d'une resynchro manuelle) :
    0 * * * * cd /opt/x402/app && docker compose run --rm x402 python -m scripts.comptable >> logs/comptable.log 2>&1
"""
import asyncio

from app import config, db
from chain_payments import sync as chain_sync


async def run(trigger: str = "cron") -> str:
    """Reusable core - called by the CLI below and by the /admin/usine
    "Lancer" button (app/admin.py, in-process asyncio task)."""
    await chain_sync()

    network = config.X402_NETWORK
    stats = db.route_chain_stats(network)
    summary_bits = db.chain_summary(network)

    if not stats:
        summary = f"0 paiement on-chain attribuable par route ({summary_bits['total_payments']} transferts totaux)"
    else:
        parts = [
            f"{route}: {s['payments']} paiement(s), {s['distinct_buyers']} acheteur(s), "
            f"retour {s['return_rate']:.0%}, mécanique {s['mechanical_share']:.0%}"
            for route, s in sorted(stats.items())
        ]
        summary = "; ".join(parts)

    db.add_journal_entry("comptable", "run", summary[:500], trigger_kind=trigger)
    return summary


async def main() -> None:
    summary = await run()
    print(summary)


if __name__ == "__main__":
    asyncio.run(main())
