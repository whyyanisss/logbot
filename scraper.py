#!/usr/bin/env python3
"""
Fac-Habitat / Smerra Logement — Surveillant de disponibilités
Scrape logement.smerra.fr et envoie une alerte Telegram
dès qu'un logement passe de "Complet" à disponible.

Usage :
  python scraper.py                  # une seule vérification
  python scraper.py --loop 15        # boucle toutes les 15 minutes
"""

import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ──────────────────────────────────────────────
# CONFIGURATION — à remplir avant de lancer
# ──────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "TON_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "TON_CHAT_ID")

# Pages à surveiller (décommente les villes qui t'intéressent)
PAGES = [
    "https://logement.smerra.fr/ville/paris/",
    # "https://logement.smerra.fr/ville/aubervilliers/",
    # "https://logement.smerra.fr/ville/massy/",
    # "https://logement.smerra.fr/ville/evry-courcouronnes/",
]

STATE_FILE = Path(__file__).parent / "state.json"
HEADERS    = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}
# ──────────────────────────────────────────────


def send_telegram(message: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        print("  [Telegram] ✓ Message envoyé")
    except Exception as e:
        print(f"  [Telegram] ✗ Erreur : {e}")


def parse_status(text: str) -> str:
    """Normalise le texte de statut en code interne."""
    t = text.strip().lower()
    if "complet" in t:
        return "full"
    if "venir" in t or "coming" in t:
        return "coming_soon"
    if "immédiate" in t or "disponible" in t or "available" in t:
        return "available"
    return "unknown"


def scrape_page(url: str) -> dict:
    """
    Retourne { slug : { name, url, price, status } }
    Structure HTML de logement.smerra.fr :
      Chaque résidence est un <article> ou <div> contenant :
        - un <a href="/residence-etudiante/..."> avec le nom
        - un prix "À partir de Xe"
        - un badge statut : "Dispo immédiate" / "Complet" / "Dispo à venir"
    """
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    residences = {}
    seen_slugs = set()

    # On cherche tous les conteneurs qui ont un lien vers une résidence
    # et un indicateur de disponibilité dans leur voisinage proche
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if "/residence-etudiante/" not in href:
            continue

        # Slug unique
        slug = href.rstrip("/").split("/")[-1]
        if slug in seen_slugs or not slug:
            continue
        seen_slugs.add(slug)

        # Remonter au conteneur parent (cherche jusqu'à 4 niveaux)
        container = link
        for _ in range(4):
            container = container.parent
            if container is None:
                break
            container_text = container.get_text(" ", strip=True).lower()
            # On cherche un indicateur de statut dans ce conteneur
            if any(k in container_text for k in ("complet", "immédiate", "à venir", "available")):
                break

        if container is None:
            continue

        # Nom de la résidence
        name_el = (
            link.find(["h2", "h3", "h4", "strong"])
            or container.find(["h2", "h3", "h4", "strong"])
        )
        name = name_el.get_text(strip=True) if name_el else link.get_text(strip=True)
        name = name[:120]  # sécurité
        if len(name) < 4:
            continue

        # Prix
        price = "N/A"
        for el in container.find_all(string=True):
            t = el.strip()
            if "€" in t and "partir" in t.lower():
                price = t
                break

        # Statut : cherche le badge explicite
        status_raw = ""
        for el in container.find_all(string=True):
            t = el.strip()
            if t.lower() in ("complet", "dispo immédiate", "dispo à venir",
                             "disponible", "available now", "coming soon"):
                status_raw = t
                break
            # variantes avec majuscules
            if t.lower() in ("dispo immédiate", "disponible immédiate"):
                status_raw = t
                break

        status = parse_status(status_raw) if status_raw else "unknown"

        full_url = href if href.startswith("http") else "https://logement.smerra.fr" + href

        residences[slug] = {
            "name": name,
            "url": full_url,
            "price": price,
            "status": status,
            "status_raw": status_raw,
        }

    return residences


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def check_all() -> None:
    state     = load_state()
    now       = datetime.now().strftime("%d/%m/%Y %H:%M")
    new_state = dict(state)   # on part de l'état existant
    alerts    = []

    for page_url in PAGES:
        print(f"\n[{now}] Scraping {page_url} …")
        try:
            residences = scrape_page(page_url)
        except Exception as e:
            print(f"  ✗ Erreur scraping : {e}")
            continue

        print(f"  {len(residences)} résidence(s) trouvée(s)")

        for slug, info in residences.items():
            prev_status = state.get(slug, {}).get("status", "unknown")
            curr_status = info["status"]

            new_state[slug] = info

            icon = {"available": "✅", "coming_soon": "⏳", "full": "🔴", "unknown": "❓"}
            print(f"  {icon.get(curr_status,'?')} {info['name']} — {curr_status} (avant: {prev_status})")

            # ── Alerte : passage de complet → disponible ──
            if curr_status in ("available", "coming_soon") and prev_status == "full":
                emoji = "🟢" if curr_status == "available" else "🟡"
                label = "DISPONIBLE MAINTENANT !" if curr_status == "available" else "BIENTÔT DISPONIBLE"
                alerts.append(
                    f"{emoji} <b>{label}</b>\n"
                    f"📍 {info['name']}\n"
                    f"💶 {info['price']}\n"
                    f"🔗 <a href=\"{info['url']}\">Voir la résidence</a>"
                )

            # ── Alerte : nouveau logement disponible jamais vu ──
            elif curr_status == "available" and prev_status == "unknown":
                alerts.append(
                    f"🆕 <b>NOUVEAU LOGEMENT DISPONIBLE</b>\n"
                    f"📍 {info['name']}\n"
                    f"💶 {info['price']}\n"
                    f"🔗 <a href=\"{info['url']}\">Voir la résidence</a>"
                )

    save_state(new_state)

    if alerts:
        header = f"🏠 <b>Fac-Habitat — Alerte logement</b> ({now})\n\n"
        send_telegram(header + "\n\n".join(alerts))
        print(f"\n→ {len(alerts)} alerte(s) Telegram envoyée(s) !")
    else:
        print("\n→ Aucun changement de disponibilité.")


def main():
    parser = argparse.ArgumentParser(description="Surveille les logements Fac-Habitat")
    parser.add_argument(
        "--loop", type=int, default=0, metavar="MINUTES",
        help="Intervalle entre les vérifications (0 = une seule fois)"
    )
    args = parser.parse_args()

    if args.loop > 0:
        print(f"🔄 Boucle active : vérification toutes les {args.loop} min. Ctrl+C pour arrêter.\n")
        while True:
            check_all()
            time.sleep(args.loop * 60)
    else:
        check_all()


if __name__ == "__main__":
    main()
