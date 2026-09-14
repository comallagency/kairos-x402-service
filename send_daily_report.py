#!/usr/bin/env python3
"""Daily recap email of the /admin dashboard numbers, same idea as the existing
MyHermes Surveillant. Cron once a day:

    0 7 * * * cd /opt/x402/app && .venv/bin/python send_daily_report.py >> logs/daily_report.log 2>&1

Needs SMTP_USER, SMTP_APP_PASSWORD (a Gmail app password, not the account
password) and REPORT_EMAIL_TO in .env - none of these were provided yet, this
script is wired but will exit with a clear error until they're set.
"""
import asyncio
import os
import smtplib
import sys
from email.mime.text import MIMEText

from app.admin import collect_dashboard_data, collect_usine_data
from app.db import init_db

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_APP_PASSWORD = os.getenv("SMTP_APP_PASSWORD", "")
REPORT_EMAIL_TO = os.getenv("REPORT_EMAIL_TO", "")


def render_usine_text(usine: dict) -> str:
    lines = ["", "--- Usine (voir /admin/usine) ---", ""]
    lines.append(f"Règlements tentés (total) : {usine['settlement_attempts_total']}")
    lines.append("")
    lines.append("Agents :")
    for a in usine["agents"]:
        lines.append(f"  {a['label']:12s} {a['last_run'] or 'jamais lancé':30s} {a['summary']}")
    lines.append("")
    lines.append("Chaîne de production :")
    for f in usine["funnel"]:
        lines.append(f"  {f['stage']:28s} {f['count'] if f['count'] is not None else '—'}")
    condemned = [r for r in usine["routes"] if r["state"] != "vivante"]
    if condemned:
        lines.append("")
        lines.append("Routes en sursis ou condamnées :")
        for r in condemned:
            lines.append(f"  /{r['slug']:15s} {r['state']} (0 paiement en {r['age_days']} j)")
    return "\n".join(lines)


def render_text(data: dict) -> str:
    routes = data["calls_by_route"]
    lines = [
        "AgentIndex x402 - recap quotidien",
        f"Genere le {data['generated_at']}",
        "",
        "Revenu USDC:",
        f"  24h : ${data['revenue_usdc']['24h']:.2f}",
        f"  7j  : ${data['revenue_usdc']['7d']:.2f}",
        f"  30j : ${data['revenue_usdc']['30d']:.2f}",
        "",
        "Appels par route (24h) - payes / non-convertis / erreurs / capacite atteinte:",
    ]
    for route in ("search", "translate", "jobs"):
        c = routes.get(route, {})
        lines.append(
            f"  {route:10s} {c.get('paid', 0):4d} / {c.get('unpaid', 0):4d} / "
            f"{c.get('error', 0):4d} / {c.get('capacity_reached', 0):4d}"
        )
    mechanical_share = data["chain"]["mechanical_share"]
    mechanical_str = (
        "N/A (pas de donnees on-chain)" if mechanical_share is None else f"{mechanical_share*100:.1f}%"
    )
    availability = data["availability_7d_pct"]
    availability_str = "N/A" if availability is None else f"{availability*100:.1f}%"

    lines += [
        "",
        f"Acheteurs distincts : {data['buyers']['distinct']} "
        f"(taux de retour {data['buyers']['return_rate']*100:.1f}%)",
        f"Part payeurs mecaniques : {mechanical_str}",
        f"Disponibilite 7j : {availability_str}",
        f"Dernier paiement recu : {data['last_payment_at'] or 'aucun'}",
        "",
        "File des jobs : "
        f"{data['jobs_queue']['queued']} en attente, {data['jobs_queue']['running']} en cours, "
        f"{data['jobs_queue']['done_24h']} termines (24h), {data['jobs_queue']['failed_24h']} echecs (24h)",
        "",
        "OpenRouter (usage $, jour/semaine/mois) : "
        + (
            "indisponible"
            if data["openrouter"]["error"]
            else f"{data['openrouter']['usage_daily']} / "
            f"{data['openrouter']['usage_weekly']} / {data['openrouter']['usage_monthly']}"
        ),
    ]
    return "\n".join(lines)


def send_email(subject: str, body: str) -> None:
    if not (SMTP_USER and SMTP_APP_PASSWORD and REPORT_EMAIL_TO):
        print(
            "SMTP_USER / SMTP_APP_PASSWORD / REPORT_EMAIL_TO not set - "
            "report generated but not sent:\n",
            file=sys.stderr,
        )
        print(body)
        return

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = REPORT_EMAIL_TO

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_APP_PASSWORD)
        server.send_message(msg)


async def main() -> None:
    init_db()
    data = await collect_dashboard_data()
    usine = await collect_usine_data()
    body = render_text(data) + "\n" + render_usine_text(usine)
    send_email("AgentIndex x402 - recap quotidien", body)


if __name__ == "__main__":
    asyncio.run(main())
