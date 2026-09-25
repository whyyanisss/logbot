#!/usr/bin/env python3
"""
Surveillant de logements étudiants — Fac-Habitat + CROUS Île-de-France
Envoie une alerte Telegram dès qu'un logement disponible passe sous le seuil de prix.

Usage :
  python scraper.py                  # une seule vérification
  python scraper.py --loop 15        # boucle toutes les 15 minutes
"""

import argparse
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ╔══════════════════════════════════════════════════════════════╗
# ║                     CONFIGURATION                           ║
# ╚══════════════════════════════════════════════════════════════╝

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "TON_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "TON_CHAT_ID")

# ── Filtre de prix ─────────────────────────────────────────────
# Seuls les logements dont le loyer minimum est <= cette valeur
# déclenchent une alerte. Modifie uniquement cette ligne.
PRIX_MAX_EUROS = 600

# ── Sources à surveiller ───────────────────────────────────────
# Fac-Habitat / Smerra : villes à surveiller
FAC_HABITAT_PAGES = [
    "https://logement.smerra.fr/ville/paris/",
    "https://logement.smerra.fr/ville/aubervilliers/",
    "https://logement.smerra.fr/ville/massy/",
    "https://logement.smerra.fr/ville/evry-courcouronnes/",
]

# CROUS : URL de recherche Île-de-France, triée par prix croissant
# L'API accepte city=Paris et les pages se paginent avec ?page=N
CROUS_SEARCH_PAGES = [
    "https://trouverunlogement.lescrous.fr/tools/47/search?city=Paris",
    "https://trouverunlogement.lescrous.fr/tools/47/search?city=Paris&page=2",
    "https://trouverunlogement.lescrous.fr/tools/47/search?city=Versailles",
    "https://trouverunlogement.lescrous.fr/tools/47/search?city=Creteil",
]

# ── Technique ─────────────────────────────────────────────────
STATE_FILE = Path(__file__).parent / "state.json"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# ╔══════════════════════════════════════════════════════════════╗
# ║                      UTILITAIRES                            ║
# ╚══════════════════════════════════════════════════════════════╝

def extract_prix_min(text: str) -> float | None:
    """
    Extrait le prix minimum depuis une chaîne comme :
      "À partir de 548,88€"  → 548.88
      "de 323,31 à 646,61 €" → 323.31
      "403,59 €"             → 403.59
    Retourne None si aucun nombre trouvé.
    """
    # Tous les nombres avec virgule ou point dans le texte
    nombres = re.findall(r"\d[\d\s]*[,\.]\d{2}", text.replace("\xa0", ""))
    if not nombres:
        # Entiers simples
        nombres = re.findall(r"\d{3,}", text)
    if not nombres:
        return None
    # On prend le plus petit (= loyer minimum)
    valeurs = []
    for n in nombres:
        try:
            valeurs.append(float(n.replace(" ", "").replace(",", ".")))
        except ValueError:
            pass
    return min(valeurs) if valeurs else None


def prix_ok(prix_str: str) -> bool:
    """Retourne True si le prix minimum extrait est <= PRIX_MAX_EUROS."""
    if prix_str in ("N/A", "", None):
        return True  # prix inconnu → on alerte quand même
    p = extract_prix_min(prix_str)
    if p is None:
        return True
    return p <= PRIX_MAX_EUROS


def format_prix_ligne(price: str) -> str:
    """
    Formate la ligne prix pour le message Telegram.
    - Prix connu et sous le seuil  → "💶 À partir de 465€ (≤ 600€)"
    - Prix inconnu / non parseable → "💶 ⚠️ Prix non récupéré — vérifier sur le site"
    """
    p = extract_prix_min(price) if price not in ("N/A", "", None) else None
    if p is None:
        return f"💶 ⚠️ Prix non récupéré — vérifier sur le site"
    return f"💶 {price} (≤ {PRIX_MAX_EUROS}€)"


def send_telegram(message: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
          "chat_id": TELEGRAM_CHAT_ID,
          "text": message,
          "parse_mode": "HTML",
          "disable_notification": silent,   # ← ajouter cette ligne
      }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        print("  [Telegram] ✓ Message envoyé")
    except Exception as e:
        print(f"  [Telegram] ✗ Erreur : {e}")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


# ╔══════════════════════════════════════════════════════════════╗
# ║                   SCRAPER FAC-HABITAT                       ║
# ╚══════════════════════════════════════════════════════════════╝

def parse_status_fh(text: str) -> str:
    t = text.strip().lower()
    if "complet" in t:
        return "full"
    if "venir" in t or "coming" in t:
        return "coming_soon"
    if "immédiate" in t or "disponible" in t or "available" in t:
        return "available"
    return "unknown"


def scrape_fac_habitat(url: str) -> dict:
    """
    Scrape une page ville Smerra/Fac-Habitat.
    Retourne { "fh:<slug>" : { name, url, price, status, source } }
    """
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    residences = {}
    seen = set()

    for link in soup.find_all("a", href=True):
        href = link["href"]
        if "/residence-etudiante/" not in href:
            continue
        slug = href.rstrip("/").split("/")[-1]
        if not slug or slug in seen:
            continue
        seen.add(slug)

        # Remonter jusqu'au conteneur portant un statut
        container = link
        for _ in range(4):
            container = container.parent
            if container is None:
                break
            ct = container.get_text(" ", strip=True).lower()
            if any(k in ct for k in ("complet", "immédiate", "à venir", "available")):
                break
        if container is None:
            continue

        # Nom
        name_el = (
            link.find(["h2", "h3", "h4", "strong"])
            or container.find(["h2", "h3", "h4", "strong"])
        )
        name = (name_el.get_text(strip=True) if name_el else link.get_text(strip=True))[:120]
        if len(name) < 4:
            continue

        # Prix
        price = "N/A"
        for el in container.find_all(string=True):
            t = el.strip()
            if "€" in t and "partir" in t.lower():
                price = t
                break

        # Statut
        status_raw = ""
        for el in container.find_all(string=True):
            t = el.strip()
            if t.lower() in ("complet", "dispo immédiate", "dispo à venir",
                             "disponible", "available now", "coming soon"):
                status_raw = t
                break

        status = parse_status_fh(status_raw) if status_raw else "unknown"
        full_url = href if href.startswith("http") else "https://logement.smerra.fr" + href

        residences[f"fh:{slug}"] = {
            "name": name,
            "url": full_url,
            "price": price,
            "status": status,
            "source": "Fac-Habitat",
        }

    return residences


# ╔══════════════════════════════════════════════════════════════╗
# ║                     SCRAPER CROUS                           ║
# ╚══════════════════════════════════════════════════════════════╝

def scrape_crous(url: str) -> dict:
    """
    Scrape une page de résultats trouverunlogement.lescrous.fr.
    Les logements listés sont tous disponibles (le site ne montre que le dispo).
    Retourne { "crous:<id>" : { name, url, price, status, source } }
    """
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    residences = {}

    # Chaque logement est dans un <li> avec un lien vers /tools/47/accommodations/<id>
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if "/accommodations/" not in href:
            continue

        # ID unique
        accom_id = href.rstrip("/").split("/")[-1]
        key = f"crous:{accom_id}"
        if key in residences:
            continue

        # Nom (balise h3 dans le lien ou son parent)
        name_el = link.find("h3") or link.find("h2") or link.find("strong")
        if name_el is None:
            parent = link.parent
            name_el = parent.find("h3") or parent.find("h2") if parent else None
        name = name_el.get_text(strip=True) if name_el else f"Résidence CROUS #{accom_id}"
        name = name[:120]

        # Prix : cherche dans le conteneur parent
        container = link.parent or link
        price = "N/A"
        for el in container.find_all(string=True):
            t = el.strip()
            if "€" in t and re.search(r"\d", t):
                price = t
                break

        # Adresse (optionnel, pour enrichir la notif)
        addr = ""
        for el in container.find_all(string=True):
            t = el.strip()
            # Cherche une chaîne qui ressemble à une adresse (numéro + rue)
            if re.match(r"^\d+[,\s]", t) and len(t) > 10:
                addr = t
                break

        full_url = (
            href if href.startswith("http")
            else "https://trouverunlogement.lescrous.fr" + href
        )

        # Les logements listés sur le CROUS sont toujours disponibles
        residences[key] = {
            "name": name,
            "url": full_url,
            "price": price,
            "address": addr,
            "status": "available",
            "source": "CROUS",
        }

    return residences


# ╔══════════════════════════════════════════════════════════════╗
# ║                   BOUCLE PRINCIPALE                         ║
# ╚══════════════════════════════════════════════════════════════╝

def check_all() -> None:
    now = datetime.now().strftime("%d/%m/%Y %H:%M")

    # Heartbeat silencieux — désactive avec disable_notification: True
    send_telegram(
        f"🤖 <b>Run démarré</b> — {now}\n"
        f"💶 Filtre : ≤ {PRIX_MAX_EUROS}€",
        silent=True   # notif muette, pas de son
    )
    state     = load_state()
    now       = datetime.now().strftime("%d/%m/%Y %H:%M")
    new_state = dict(state)
    alerts    = []

    # ── Fac-Habitat ───────────────────────────────────────────
    for page_url in FAC_HABITAT_PAGES:
        print(f"\n[Fac-Habitat] {page_url}")
        try:
            residences = scrape_fac_habitat(page_url)
        except Exception as e:
            print(f"  ✗ Erreur : {e}")
            continue

        print(f"  {len(residences)} résidence(s) trouvée(s)")

        for key, info in residences.items():
            prev  = state.get(key, {})
            prev_status = prev.get("status", "unknown")
            curr_status = info["status"]
            new_state[key] = info

            icon = {"available":"✅","coming_soon":"⏳","full":"🔴","unknown":"❓"}
            prix_filtre = f"{'✓' if prix_ok(info['price']) else '✗ hors budget'}"
            print(f"  {icon.get(curr_status,'?')} {info['name']} | {info['price']} {prix_filtre}")

            # Alerte seulement si le prix passe le filtre
            if not prix_ok(info["price"]):
                continue

            if curr_status in ("available", "coming_soon") and prev_status == "full":
                emoji = "🟢" if curr_status == "available" else "🟡"
                label = "DISPONIBLE MAINTENANT !" if curr_status == "available" else "BIENTÔT DISPONIBLE"
                alerts.append(
                    f"{emoji} <b>[Fac-Habitat] {label}</b>\n"
                    f"📍 {info['name']}\n"
                    f"{format_prix_ligne(info['price'])}\n"
                    f"🔗 <a href=\"{info['url']}\">Voir la résidence</a>"
                )
            elif curr_status == "available" and prev_status == "unknown":
                alerts.append(
                    f"🆕 <b>[Fac-Habitat] NOUVEAU LOGEMENT DISPONIBLE</b>\n"
                    f"📍 {info['name']}\n"
                    f"{format_prix_ligne(info['price'])}\n"
                    f"🔗 <a href=\"{info['url']}\">Voir la résidence</a>"
                )

    # ── CROUS ─────────────────────────────────────────────────
    for page_url in CROUS_SEARCH_PAGES:
        print(f"\n[CROUS] {page_url}")
        try:
            residences = scrape_crous(page_url)
        except Exception as e:
            print(f"  ✗ Erreur : {e}")
            continue

        print(f"  {len(residences)} logement(s) trouvé(s)")

        for key, info in residences.items():
            prev = state.get(key, {})
            prev_status = prev.get("status", "unknown")
            new_state[key] = info

            prix_filtre = f"{'✓' if prix_ok(info['price']) else '✗ hors budget'}"
            print(f"  ✅ {info['name']} | {info['price']} {prix_filtre}")

            # Filtre prix
            if not prix_ok(info["price"]):
                continue

            # Alerte seulement si c'est un nouveau logement jamais vu
            if prev_status == "unknown":
                addr_line = f"\n📮 {info['address']}" if info.get("address") else ""
                alerts.append(
                    f"🏛️ <b>[CROUS] NOUVEAU LOGEMENT DISPONIBLE</b>\n"
                    f"📍 {info['name']}{addr_line}\n"
                    f"{format_prix_ligne(info['price'])}\n"
                    f"🔗 <a href=\"{info['url']}\">Voir le logement</a>"
                )

    save_state(new_state)

    if alerts:
        header = (
            f"🏠 <b>Alerte logement étudiant</b> ({now})\n"
            f"💶 Filtre actif : loyer ≤ {PRIX_MAX_EUROS}€\n\n"
        )
        send_telegram(header + "\n\n".join(alerts))
        print(f"\n→ {len(alerts)} alerte(s) Telegram envoyée(s) !")
    else:
        print(f"\n[{now}] Aucun nouveau logement sous {PRIX_MAX_EUROS}€.")


# ╔══════════════════════════════════════════════════════════════╗
# ║                        ENTRÉE                               ║
# ╚══════════════════════════════════════════════════════════════╝

def main():
    parser = argparse.ArgumentParser(
        description=f"Surveille Fac-Habitat + CROUS IDF (filtre ≤ {PRIX_MAX_EUROS}€)"
    )
    parser.add_argument(
        "--loop", type=int, default=0, metavar="MINUTES",
        help="Intervalle entre les vérifications (0 = une seule fois)"
    )
    args = parser.parse_args()

    print(f"🔍 Démarrage — filtre prix : ≤ {PRIX_MAX_EUROS}€/mois")
    print(f"   Sources : Fac-Habitat ({len(FAC_HABITAT_PAGES)} pages) + CROUS ({len(CROUS_SEARCH_PAGES)} pages)\n")

    if args.loop > 0:
        print(f"🔄 Boucle : toutes les {args.loop} min. Ctrl+C pour arrêter.\n")
        while True:
            check_all()
            time.sleep(args.loop * 60)
    else:
        check_all()


if __name__ == "__main__":
    main()
