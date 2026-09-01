#!/usr/bin/env python3
"""
Surveillance de nouveaux produits Pokemon sur Philibert et Strike Games.

- Recupere la liste des produits actuellement en ligne sur les deux sites
- Compare avec la liste sauvegardee lors du dernier passage (seen_products.json)
- Envoie une notification push (via ntfy.sh) pour chaque nouveau produit detecte
- Sauvegarde la nouvelle liste pour la prochaine execution

Configuration : variable d'environnement NTFY_TOPIC (voir README.md)
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

STATE_FILE = Path(__file__).parent / "seen_products.json"

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}" if NTFY_TOPIC else None

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

PHILIBERT_URL = "https://www.philibertnet.com/fr/212-pokemon/s-3/langues-francais"
STRIKEGAMES_JSON_URL = (
    "https://strikegames.shop/collections/tcg-pokemon-produit-en-francais/products.json"
    "?limit=250"
)
INVESTCOLLECT_BASE_URL = "https://investcollect.com/eshop/produits-scelles.html"
INVESTCOLLECT_MAX_PAGES = 10  # garde-fou pour ne jamais boucler a l'infini
MAISONDELAPRESSE_BASE_URL = (
    "https://www.maisondelapresse.com/jeux-jouets/cartes-collectionner/cartes-pokemon.html"
)
MAISONDELAPRESSE_MAX_PAGES = 15  # garde-fou pour ne jamais boucler a l'infini


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "philibert": {},
        "strikegames": {},
        "investcollect": {},
        "maisondelapresse": {},
    }


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)


def send_notification(title, message, url=None):
    if not NTFY_URL:
        print(f"[ntfy desactive] {title} - {message}")
        return
    try:
        requests.post(
            NTFY_URL,
            data=message.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
                "Priority": "default",
                **({"Click": url} if url else {}),
                "Tags": "pokeball",
            },
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"Erreur envoi notification ntfy: {e}", file=sys.stderr)


def fetch_philibert():
    """Retourne un dict {id_produit: {"title": ..., "url": ...}} pour Philibert."""
    resp = requests.get(PHILIBERT_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    products = {}
    # Les liens produits Pokemon suivent le format:
    # /fr/pokemon/<id>-<slug>.html
    pattern = re.compile(r"/fr/pokemon/(\d+)-[a-z0-9-]+\.html")

    for a in soup.find_all("a", href=True):
        m = pattern.search(a["href"])
        if not m:
            continue
        product_id = m.group(1)
        title = a.get_text(strip=True)
        url = a["href"]
        if not url.startswith("http"):
            url = "https://www.philibertnet.com" + url
        # On garde le premier titre non vide rencontre pour cet id
        if product_id not in products or (not products[product_id]["title"] and title):
            if title:
                products[product_id] = {"title": title, "url": url}
            elif product_id not in products:
                products[product_id] = {"title": "(titre indisponible)", "url": url}

    return products


def fetch_strikegames():
    """Retourne un dict {id_produit: {"title": ..., "url": ...}} pour Strike Games."""
    resp = requests.get(STRIKEGAMES_JSON_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    products = {}
    for p in data.get("products", []):
        product_id = str(p["id"])
        title = p.get("title", "(titre indisponible)")
        handle = p.get("handle", "")
        url = f"https://strikegames.shop/products/{handle}"
        products[product_id] = {"title": title, "url": url}

    return products


def fetch_investcollect():
    """Retourne un dict {id_produit: {"title": ..., "url": ...}} pour InvestCollect.

    Le catalogue est paginé (?p=1, ?p=2, ...). On parcourt les pages jusqu'à
    ce qu'une page ne ramène plus de nouveau produit (ou jusqu'au garde-fou
    INVESTCOLLECT_MAX_PAGES).
    """
    products = {}
    # Les liens produits suivent le format: /eshop/p/<slug>.html
    pattern = re.compile(r"/eshop/p/([a-z0-9\-_.]+)\.html")

    for page in range(1, INVESTCOLLECT_MAX_PAGES + 1):
        resp = requests.get(
            INVESTCOLLECT_BASE_URL, params={"p": page}, headers=HEADERS, timeout=30
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        found_on_this_page = 0
        for a in soup.find_all("a", href=True):
            m = pattern.search(a["href"])
            if not m:
                continue
            slug = m.group(1)
            title = a.get_text(strip=True)
            url = a["href"].split("?")[0]
            if not url.startswith("http"):
                url = "https://investcollect.com" + url

            if slug not in products:
                found_on_this_page += 1
                products[slug] = {
                    "title": title if title else "(titre indisponible)",
                    "url": url,
                }
            elif title and products[slug]["title"] == "(titre indisponible)":
                products[slug]["title"] = title

        if found_on_this_page == 0:
            # Page vide ou déjà entièrement vue : on a atteint la fin du catalogue
            break

    return products


def fetch_maisondelapresse():
    """Retourne un dict {id_produit: {"title": ..., "url": ...}} pour Maison de la Presse.

    Site Magento : on cible specifiquement la grille de produits de la
    categorie (<ol class="products list items product-items">), pas toute
    la page. Cela evite de capturer des widgets de recommandation
    ("vous aimerez aussi", "recemment consultes") qui utilisent la meme
    classe de lien mais changent de contenu a chaque requete sans lien
    avec le vrai catalogue.
    Pagination classique via ?p=2, ?p=3, etc.
    """
    products = {}

    for page in range(1, MAISONDELAPRESSE_MAX_PAGES + 1):
        resp = requests.get(
            MAISONDELAPRESSE_BASE_URL, params={"p": page}, headers=HEADERS, timeout=30
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Cible uniquement la grille de produits de la categorie (structure
        # Magento standard). Fallback sur le selecteur large si la grille
        # n'est pas trouvee, pour ne jamais tomber a zero silencieusement.
        links = soup.select("ol.products.list.items.product-items a.product-item-link")
        if not links:
            links = soup.select("a.product-item-link")

        if not links:
            # Page veritablement vide : fin du catalogue
            break

        for a in links:
            url = a.get("href", "").split("?")[0]
            if not url:
                continue
            title = a.get_text(strip=True)
            # Utilise l'URL elle-meme comme identifiant unique du produit
            if url not in products:
                products[url] = {
                    "title": title if title else "(titre indisponible)",
                    "url": url,
                }

    return products


def diff_and_notify(site_label, previous, current):
    """Compare les dicts previous/current, notifie les nouveaux, retourne current."""
    new_ids = [pid for pid in current if pid not in previous]

    if not previous:
        # Premiere execution pour ce site : on enregistre l'etat sans notifier
        # pour eviter un spam de toutes les alertes existantes.
        print(f"[{site_label}] Premiere execution : {len(current)} produits enregistres (pas de notif).")
        return current

    if new_ids:
        print(f"[{site_label}] {len(new_ids)} nouveau(x) produit(s) detecte(s).")
    else:
        print(f"[{site_label}] Aucun nouveau produit ({len(current)} produits suivis).")

    for pid in new_ids:
        item = current[pid]
        send_notification(
            title=f"Nouveau produit Pokemon - {site_label}",
            message=item["title"],
            url=item["url"],
        )
        time.sleep(1)  # petite pause pour ne pas spammer ntfy d'un coup

    return current


def main():
    state = load_state()

    try:
        philibert_current = fetch_philibert()
    except Exception as e:
        print(f"Erreur recuperation Philibert: {e}", file=sys.stderr)
        philibert_current = None

    try:
        strikegames_current = fetch_strikegames()
    except Exception as e:
        print(f"Erreur recuperation Strike Games: {e}", file=sys.stderr)
        strikegames_current = None

    try:
        investcollect_current = fetch_investcollect()
    except Exception as e:
        print(f"Erreur recuperation InvestCollect: {e}", file=sys.stderr)
        investcollect_current = None

    # DESACTIVE TEMPORAIREMENT : Maison de la Presse genere des notifications
    # en boucle sur les memes produits (probable widget de recommandation
    # capture par erreur). On coupe la detection le temps de diagnostiquer
    # avec le vrai code source de la page. Pour reactiver : decommenter les
    # 4 lignes ci-dessous.
    maisondelapresse_current = None
    # try:
    #     maisondelapresse_current = fetch_maisondelapresse()
    # except Exception as e:
    #     print(f"Erreur recuperation Maison de la Presse: {e}", file=sys.stderr)
    #     maisondelapresse_current = None

    if philibert_current is not None:
        state["philibert"] = diff_and_notify(
            "Philibert", state.get("philibert", {}), philibert_current
        )

    if strikegames_current is not None:
        state["strikegames"] = diff_and_notify(
            "Strike Games", state.get("strikegames", {}), strikegames_current
        )

    if investcollect_current is not None:
        state["investcollect"] = diff_and_notify(
            "InvestCollect", state.get("investcollect", {}), investcollect_current
        )

    if maisondelapresse_current is not None:
        state["maisondelapresse"] = diff_and_notify(
            "Maison de la Presse",
            state.get("maisondelapresse", {}),
            maisondelapresse_current,
        )

    save_state(state)


if __name__ == "__main__":
    main()
