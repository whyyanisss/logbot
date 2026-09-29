#!/usr/bin/env python3
"""
Surveillant de logements étudiants — Fac-Habitat + CROUS Île-de-France
Envoie une alerte Telegram dès qu'un logement disponible apparaît.

Usage :
  python scraper.py                 # une seule vérification
  python scraper.py --loop 15       # boucle toutes les 15 minutes
  python scraper.py --reset         # efface state.json avant de commencer
  python scraper.py --debug         # sauvegarde le HTML des pages à 0 résultat
"""

import argparse
import html
import json
import os
import random
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

FH_SUFFIX = "?availability=immediat%2Ca-venir&language=fr"
FH_CITIES = [
    # 94
    "creteil", "ivry-sur-seine", "vitry-sur-seine", "alfortville", "villejuif",
    "maisons-alfort", "vincennes", "saint-maur-des-fosses",
    "fontenay-sous-bois", "champigny-sur-marne",
    # 93
    "aubervilliers", "saint-denis", "montreuil", "pantin",
    # 92
    "boulogne-billancourt", "nanterre", "issy-les-moulineaux",
    # 91
    "massy", "evry-courcouronnes",
    # Paris
    "paris",
]
FAC_HABITAT_PAGES = [f"https://logement.smerra.fr/ville/{c}/{FH_SUFFIX}" for c in FH_CITIES]

CROUS_BASE = "https://trouverunlogement.lescrous.fr/tools/47/search?city="
CROUS_SEARCH_PAGES = [
    CROUS_BASE + "Creteil",
    CROUS_BASE + "Creteil&page=2",
    CROUS_BASE + "Ivry-sur-Seine",
    CROUS_BASE + "Vitry-sur-Seine",
    CROUS_BASE + "Villejuif",
    CROUS_BASE + "Vincennes",
    CROUS_BASE + "Paris",
    CROUS_BASE + "Paris&page=2",
    CROUS_BASE + "Paris&page=3",
    CROUS_BASE + "Versailles",
    CROUS_BASE + "Nanterre",
    CROUS_BASE + "Massy",
]

STATE_FILE = Path(__file__).parent / "state.json"
DEBUG_DIR = Path(__file__).parent / "debug"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)
DEBUG = False

# ╔══════════════════════════════════════════════════════════════╗
# ║                      UTILITAIRES                            ║
# ╚══════════════════════════════════════════════════════════════╝

def pause() -> None:
    """Petit délai aléatoire pour ne pas se faire bloquer."""
    time.sleep(random.uniform(1, 3))


def format_prix_ligne(price: str) -> str:
    if price in ("N/A", "", None):
        return "💶 ⚠️ Prix non récupéré — vérifier sur le site"
    return f"💶 {html.escape(price)}"


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


def send_alerts(header: str, alerts: list) -> None:
    """Envoie les alertes en plusieurs messages si besoin (limite Telegram : 4096 car.)."""
    chunk, size = [], len(header)
    for a in alerts:
        if chunk and size + len(a) + 2 > 3800:
            send_telegram(header + "\n\n".join(chunk))
            chunk, size = [], len(header)
        chunk.append(a)
        size += len(a) + 2
    if chunk:
        send_telegram(header + "\n\n".join(chunk))


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception as e:
            print(f"⚠️ state.json illisible ({e}) — repartir de zéro")
    return {}


def save_state(state: dict) -> None:
    """Écriture atomique : pas de fichier corrompu si le script est interrompu."""
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    os.replace(tmp, STATE_FILE)


def dump_html(name: str, content: str) -> None:
    if not DEBUG:
        return
    DEBUG_DIR.mkdir(exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9]+", "_", name)[:80]
    (DEBUG_DIR / f"{safe}.html").write_text(content, encoding="utf-8")


def get(url: str) -> requests.Response:
    r = SESSION.get(url, timeout=20)
    r.raise_for_status()
    return r


# ╔══════════════════════════════════════════════════════════════╗
# ║                   SCRAPER FAC-HABITAT                       ║
# ╚══════════════════════════════════════════════════════════════╝

def _residence_slugs(el) -> set:
    slugs = set()
    for a in el.find_all("a", href=True):
        if "/residence-etudiante/" in a["href"]:
            s = a["href"].rstrip("/").split("/")[-1]
            if s:
                slugs.add(s)
    return slugs


def _detect_status(container) -> str:
    """Cherche un badge de statut dans le conteneur (texte court uniquement)."""
    for el in container.find_all(string=True):
        t = el.strip().lower()
        if not t or len(t) > 40:
            continue
        if "complet" in t:
            return "full"
        if "venir" in t or "coming" in t:
            return "coming_soon"
        if "immédiat" in t or "immediat" in t or "disponible" in t or "available" in t:
            return "available"
    return "unknown"


def scrape_fac_habitat(url: str) -> dict:
    r = get(url)
    soup = BeautifulSoup(r.text, "html.parser")
    filtered = "availability=" in url

    residences = {}

    for link in soup.find_all("a", href=True):
        href = link["href"]
        if "/residence-etudiante/" not in href:
            continue
        slug = href.rstrip("/").split("/")[-1]
        if not slug or f"fh:{slug}" in residences:
            continue

        # Remonter jusqu'au plus petit conteneur, sans englober d'autres résidences
        container = link
        for _ in range(5):
            parent = container.parent
            if parent is None:
                break
            if len(_residence_slugs(parent)) > 1:
                break
            container = parent

        name_el = (
            link.find(["h2", "h3", "h4", "strong"])
            or container.find(["h2", "h3", "h4", "strong"])
        )
        name = (name_el.get_text(strip=True) if name_el else link.get_text(strip=True))[:120]
        if len(name) < 4:
            # lien "image" sans texte : on laissera un autre lien du même slug faire le travail
            continue

        price = "N/A"
        for el in container.find_all(string=True):
            t = el.strip()
            if "€" in t and "partir" in t.lower():
                price = t
                break

        status = _detect_status(container)
        if status == "unknown" and filtered:
            # Page déjà filtrée sur dispo immédiate / à venir : présent = disponible
            status = "available"

        full_url = href if href.startswith("http") else "https://logement.smerra.fr" + href

        residences[f"fh:{slug}"] = {
            "name": name,
            "url": full_url,
            "price": price,
            "status": status,
            "source": "Fac-Habitat",
        }

    if not residences:
        dump_html("fh_" + url, r.text)
    return residences


# ╔══════════════════════════════════════════════════════════════╗
# ║                     SCRAPER CROUS                           ║
# ╚══════════════════════════════════════════════════════════════╝

def scrape_crous(url: str) -> dict:
    r = get(url)
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
        if name_el is None and link.parent:
            name_el = link.parent.find("h3") or link.parent.find("h2")
        name = (name_el.get_text(strip=True) if name_el else f"Résidence CROUS #{accom_id}")[:120]

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

    if not residences:
        dump_html("crous_" + url, r.text)
    return residences


# ╔══════════════════════════════════════════════════════════════╗
# ║                   BOUCLE PRINCIPALE                         ║
# ╚══════════════════════════════════════════════════════════════╝

ICON = {"available": "✅", "coming_soon": "⏳", "full": "🔴", "unknown": "❓"}


def check_all() -> None:
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    state = load_state()
    seen: dict = {}          # tout ce qui a été vu pendant ce run
    alerts: list = []
    alerted: set = set()
    all_ok = True            # False si au moins une page a échoué
    counts = {"Fac-Habitat": 0, "CROUS": 0}

    # ── Fac-Habitat ───────────────────────────────────────────
    for page_url in FAC_HABITAT_PAGES:
        print(f"\n[Fac-Habitat] {page_url}")
        try:
            residences = scrape_fac_habitat(page_url)
        except Exception as e:
            print(f"  ✗ Erreur : {e}")
            all_ok = False
            pause()
            continue

        counts["Fac-Habitat"] += len(residences)
        print(f"  {len(residences)} résidence(s) trouvée(s)")

        for key, info in residences.items():
            prev_status = state.get(key, {}).get("status", "unknown")
            curr_status = info["status"]
            seen[key] = info
            print(f"  {ICON.get(curr_status, '?')} {info['name']} | {curr_status} | {info['price']}")

            if (
                curr_status in ("available", "coming_soon")
                and prev_status not in ("available", "coming_soon")
                and key not in alerted
            ):
                alerted.add(key)
                if curr_status == "available":
                    emoji, label = "🟢", "DISPONIBLE MAINTENANT !"
                else:
                    emoji, label = "🟡", "BIENTÔT DISPONIBLE"
                alerts.append(
                    f"{emoji} <b>[Fac-Habitat] {label}</b>\n"
                    f"📍 {html.escape(info['name'])}\n"
                    f"{format_prix_ligne(info['price'])}\n"
                    f"🔗 <a href=\"{html.escape(info['url'])}\">Voir la résidence</a>"
                )
        pause()

    # ── CROUS ─────────────────────────────────────────────────
    for page_url in CROUS_SEARCH_PAGES:
        print(f"\n[CROUS] {page_url}")
        try:
            residences = scrape_crous(page_url)
        except Exception as e:
            print(f"  ✗ Erreur : {e}")
            all_ok = False
            pause()
            continue

        counts["CROUS"] += len(residences)
        print(f"  {len(residences)} logement(s) trouvé(s)")

        for key, info in residences.items():
            seen[key] = info
            print(f"  ✅ {info['name']} | {info['price']}")

            if key not in state and key not in alerted:
                alerted.add(key)
                addr_line = f"\n📮 {html.escape(info['address'])}" if info.get("address") else ""
                alerts.append(
                    f"🏛️ <b>[CROUS] NOUVEAU LOGEMENT DISPONIBLE</b>\n"
                    f"📍 {html.escape(info['name'])}{addr_line}\n"
                    f"{format_prix_ligne(info['price'])}\n"
                    f"🔗 <a href=\"{html.escape(info['url'])}\">Voir le logement</a>"
                )
        pause()

    # ── Mise à jour de l'état ─────────────────────────────────
    if all_ok:
        # Tout a été scrapé : on ne garde que ce qui est encore visible,
        # ainsi un logement qui disparaît puis revient re-déclenche une alerte.
        new_state = seen
    else:
        # Une page a échoué : on conserve l'ancien état pour éviter de fausses alertes
        new_state = {**state, **seen}
    save_state(new_state)

    # ── Alertes ───────────────────────────────────────────────
    if alerts:
        send_alerts(f"🏠 <b>Alerte logement étudiant</b> ({now})\n\n", alerts)
        print(f"\n→ {len(alerts)} alerte(s) Telegram envoyée(s) !")
    else:
        print(f"\n[{now}] Aucun nouveau logement détecté.")

    # Avertissement silencieux si un site ne renvoie plus rien (structure changée ?)
    for source, n in counts.items():
        if n == 0:
            send_telegram(
                f"⚠️ {source} : 0 résultat sur toutes les pages ({now}). "
                f"Le site a peut-être changé ou bloque le script.",
                silent=True,
            )


# ╔══════════════════════════════════════════════════════════════╗
# ║                        ENTRÉE                               ║
# ╚══════════════════════════════════════════════════════════════╝

def main():
    global DEBUG
    parser = argparse.ArgumentParser(description="Surveille Fac-Habitat + CROUS IDF")
    parser.add_argument("--loop", type=int, default=0, metavar="MINUTES",
                        help="Intervalle entre les vérifications (0 = une seule fois)")
    parser.add_argument("--reset", action="store_true", help="Supprime state.json au démarrage")
    parser.add_argument("--debug", action="store_true",
                        help="Sauvegarde le HTML des pages qui renvoient 0 résultat dans ./debug")
    args = parser.parse_args()
    DEBUG = args.debug

    if args.reset and STATE_FILE.exists():
        STATE_FILE.unlink()
        print("🧹 state.json supprimé")

    print("🔍 Démarrage — toutes les résidences alertées, sans filtre de prix")
    print(f"   Sources : Fac-Habitat ({len(FAC_HABITAT_PAGES)} pages) + CROUS ({len(CROUS_SEARCH_PAGES)} pages)\n")

    # Message de démarrage envoyé UNE seule fois
    send_telegram(
        f"🤖 <b>Surveillance lancée</b> — {datetime.now():%d/%m/%Y %H:%M}",
        silent=True,
    )

    if args.loop > 0:
        print(f"🔄 Boucle : toutes les {args.loop} min. Ctrl+C pour arrêter.\n")
        while True:
            try:
                check_all()
            except Exception as e:
                print(f"✗ Erreur inattendue : {e}")
            time.sleep(args.loop * 60)
    else:
        check_all()


if __name__ == "__main__":
    main()
