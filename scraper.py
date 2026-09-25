#!/usr/bin/env python3
"""
Surveillant de logements étudiants — Fac-Habitat + CROUS Île-de-France
Envoie une alerte Telegram dès qu'un logement disponible apparaît.

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

# ── Sources à surveiller ───────────────────────────────────────
# Val-de-Marne (94) en priorité, puis petite couronne et Paris
FAC_HABITAT_PAGES = [
    # ── 94 Val-de-Marne ───────────────────────────────────────
    "https://logement.smerra.fr/ville/creteil/?availability=immediat%2Ca-venir&language=fr",
    "https://logement.smerra.fr/ville/ivry-sur-seine/?availability=immediat%2Ca-venir&language=fr",
    "https://logement.smerra.fr/ville/vitry-sur-seine/?availability=immediat%2Ca-venir&language=fr",
    "https://logement.smerra.fr/ville/alfortville/?availability=immediat%2Ca-venir&language=fr",
    "https://logement.smerra.fr/ville/villejuif/?availability=immediat%2Ca-venir&language=fr",
    "https://logement.smerra.fr/ville/maisons-alfort/?availability=immediat%2Ca-venir&language=fr",
    "https://logement.smerra.fr/ville/vincennes/?availability=immediat%2Ca-venir&language=fr",
    "https://logement.smerra.fr/ville/saint-maur-des-fosses/?availability=immediat%2Ca-venir&language=fr",
    "https://logement.smerra.fr/ville/fontenay-sous-bois/?availability=immediat%2Ca-venir&language=fr",
    "https://logement.smerra.fr/ville/champigny-sur-marne/?availability=immediat%2Ca-venir&language=fr",
    # ── 93 Seine-Saint-Denis ──────────────────────────────────
    "https://logement.smerra.fr/ville/aubervilliers/?availability=immediat%2Ca-venir&language=fr",
    "https://logement.smerra.fr/ville/saint-denis/?availability=immediat%2Ca-venir&language=fr",
    "https://logement.smerra.fr/ville/montreuil/?availability=immediat%2Ca-venir&language=fr",
    "https://logement.smerra.fr/ville/pantin/?availability=immediat%2Ca-venir&language=fr",
    # ── 92 Hauts-de-Seine ─────────────────────────────────────
    "https://logement.smerra.fr/ville/boulogne-billancourt/?availability=immediat%2Ca-venir&language=fr",
    "https://logement.smerra.fr/ville/nanterre/?availability=immediat%2Ca-venir&language=fr",
    "https://logement.smerra.fr/ville/issy-les-moulineaux/?availability=immediat%2Ca-venir&language=fr",
    # ── 91 Essonne ────────────────────────────────────────────
    "https://logement.smerra.fr/ville/massy/?availability=immediat%2Ca-venir&language=fr",
    "https://logement.smerra.fr/ville/evry-courcouronnes/?availability=immediat%2Ca-venir&language=fr",
    # ── Paris ─────────────────────────────────────────────────
    "https://logement.smerra.fr/ville/paris/?availability=immediat%2Ca-venir&language=fr",
]

CROUS_SEARCH_PAGES = [
    # ── 94 Val-de-Marne ───────────────────────────────────────
    "https://trouverunlogement.lescrous.fr/tools/47/search?city=Creteil",
    "https://trouverunlogement.lescrous.fr/tools/47/search?city=Creteil&page=2",
    "https://trouverunlogement.lescrous.fr/tools/47/search?city=Ivry-sur-Seine",
    "https://trouverunlogement.lescrous.fr/tools/47/search?city=Vitry-sur-Seine",
    "https://trouverunlogement.lescrous.fr/tools/47/search?city=Villejuif",
    "https://trouverunlogement.lescrous.fr/tools/47/search?city=Vincennes",
    # ── Paris ─────────────────────────────────────────────────
    "https://trouverunlogement.lescrous.fr/tools/47/search?city=Paris",
    "https://trouverunlogement.lescrous.fr/tools/47/search?city=Paris&page=2",
    "https://trouverunlogement.lescrous.fr/tools/47/search?city=Paris&page=3",
    # ── Versailles / autres ───────────────────────────────────
    "https://trouverunlogement.lescrous.fr/tools/47/search?city=Versailles",
    "https://trouverunlogement.lescrous.fr/tools/47/search?city=Nanterre",
    "https://trouverunlogement.lescrous.fr/tools/47/search?city=Massy",
]

# ── Technique ──────────────────────────────────────────────────
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

def format_prix_ligne(price: str) -> str:
    if price in ("N/A", "", None):
        return "💶 ⚠️ Prix non récupéré — vérifier sur le site"
    return f"💶 {price}"


def send_telegram(message: str, silent: bool = False) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
        "disable_notification": silent,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        print("  [Telegram] ✓ Message envoyé" + (" (silencieux)" if silent else ""))
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

        name_el = (
            link.find(["h2", "h3", "h4", "strong"])
            or container.find(["h2", "h3", "h4", "strong"])
        )
        name = (name_el.get_text(strip=True) if name_el else link.get_text(strip=True))[:120]
        if len(name) < 4:
            continue

        price = "N/A"
        for el in container.find_all(string=True):
            t = el.strip()
            if "€" in t and "partir" in t.lower():
                price = t
                break

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
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    residences = {}

    for link in soup.find_all("a", href=True):
        href = link["href"]
        if "/accommodations/" not in href:
            continue

        accom_id = href.rstrip("/").split("/")[-1]
        key = f"crous:{accom_id}"
        if key in residences:
            continue

        name_el = link.find("h3") or link.find("h2") or link.find("strong")
        if name_el is None:
            parent = link.parent
            name_el = parent.find("h3") or parent.find("h2") if parent else None
        name = name_el.get_text(strip=True) if name_el else f"Résidence CROUS #{accom_id}"
        name = name[:120]

        container = link.parent or link
        price = "N/A"
        for el in container.find_all(string=True):
            t = el.strip()
            if "€" in t and re.search(r"\d", t):
                price = t
                break

        addr = ""
        for el in container.find_all(string=True):
            t = el.strip()
            if re.match(r"^\d+[,\s]", t) and len(t) > 10:
                addr = t
                break

        full_url = (
            href if href.startswith("http")
            else "https://trouverunlogement.lescrous.fr" + href
        )

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

    send_telegram(
        f"🤖 <b>Run démarré</b> — {now}",
        silent=True,
    )

    state     = load_state()
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
            prev        = state.get(key, {})
            prev_status = prev.get("status", "unknown")
            curr_status = info["status"]
            new_state[key] = info

            icon = {"available": "✅", "coming_soon": "⏳", "full": "🔴", "unknown": "❓"}
            print(f"  {icon.get(curr_status,'?')} {info['name']} | {info['price']}")

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
            prev        = state.get(key, {})
            prev_status = prev.get("status", "unknown")
            new_state[key] = info

            print(f"  ✅ {info['name']} | {info['price']}")

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
        header = f"🏠 <b>Alerte logement étudiant</b> ({now})\n\n"
        send_telegram(header + "\n\n".join(alerts))
        print(f"\n→ {len(alerts)} alerte(s) Telegram envoyée(s) !")
    else:
        print(f"\n[{now}] Aucun nouveau logement détecté.")


# ╔══════════════════════════════════════════════════════════════╗
# ║                        ENTRÉE                               ║
# ╚══════════════════════════════════════════════════════════════╝

def main():
    parser = argparse.ArgumentParser(
        description="Surveille Fac-Habitat + CROUS IDF"
    )
    parser.add_argument(
        "--loop", type=int, default=0, metavar="MINUTES",
        help="Intervalle entre les vérifications (0 = une seule fois)"
    )
    args = parser.parse_args()

    print("🔍 Démarrage — toutes les résidences alertées, sans filtre de prix")
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
