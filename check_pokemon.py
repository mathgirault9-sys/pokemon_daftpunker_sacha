#!/usr/bin/env python3
"""
Surveillance de nouveaux produits Pokemon sur une liste de sites marchands.

- Recupere la liste des produits actuellement en ligne sur chaque site
- Compare avec la liste sauvegardee lors du dernier passage (seen_products.json)
- Envoie une notification push (via ntfy.sh) pour chaque nouveau produit detecte
- Sauvegarde la nouvelle liste (fusionnee avec l'ancienne) pour la prochaine execution

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

MAX_PAGES_DEFAULT = 15  # garde-fou pour ne jamais boucler a l'infini sur un site pagine


# ---------------------------------------------------------------------------
# Recuperateurs generiques (reutilisables pour plusieurs sites du meme type
# de plateforme e-commerce)
# ---------------------------------------------------------------------------

def fetch_shopify(base_url, collection_handle):
    """Sites Shopify : on utilise l'API JSON native /products.json, beaucoup
    plus fiable qu'un scraping HTML (pas de classe CSS a deviner)."""
    url = f"{base_url}/collections/{collection_handle}/products.json?limit=250"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    products = {}
    for p in data.get("products", []):
        product_id = str(p["id"])
        title = p.get("title", "(titre indisponible)")
        handle = p.get("handle", "")
        products[product_id] = {
            "title": title,
            "url": f"{base_url}/products/{handle}",
        }
    return products


def fetch_prestashop_id_pattern(category_url, id_pattern, base_url, max_pages=MAX_PAGES_DEFAULT):
    """Sites PrestaShop : les liens produits contiennent un identifiant
    numerique stable dans l'URL (ex: /fr/pokemon/12345-nom-du-produit.html).
    Pagination classique via ?page=N."""
    products = {}
    pattern = re.compile(id_pattern)

    for page in range(1, max_pages + 1):
        resp = requests.get(category_url, params={"page": page}, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        found_on_this_page = 0
        for a in soup.find_all("a", href=True):
            m = pattern.search(a["href"])
            if not m:
                continue
            product_id = m.group(1)
            title = a.get_text(strip=True)
            url = a["href"].split("?")[0]
            if not url.startswith("http"):
                url = base_url + url

            if product_id not in products:
                found_on_this_page += 1
                products[product_id] = {
                    "title": title if title else "(titre indisponible)",
                    "url": url,
                }
            elif title and products[product_id]["title"] == "(titre indisponible)":
                products[product_id]["title"] = title

        if found_on_this_page == 0:
            break

    return products


def fetch_url_pattern(page_url, id_pattern, base_url, params_key=None, max_pages=1):
    """Generique : extrait les liens produits correspondant a un pattern
    d'URL donne (regex avec un groupe = identifiant). Utilise pour les sites
    dont on a confirme le pattern manuellement mais qui ne suivent ni
    Shopify ni PrestaShop pile-poil (ex: play-in.com, lecoindesbarons.com).
    Si params_key est fourni, pagine via ce parametre de requete (?param=N).
    """
    products = {}
    pattern = re.compile(id_pattern)

    for page in range(1, max_pages + 1):
        params = {params_key: page} if params_key else None
        resp = requests.get(page_url, params=params, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        found_on_this_page = 0
        for a in soup.find_all("a", href=True):
            m = pattern.search(a["href"])
            if not m:
                continue
            product_id = m.group(1)
            title = a.get_text(strip=True)
            url = a["href"].split("?")[0]
            if not url.startswith("http"):
                url = base_url + url

            if product_id not in products:
                found_on_this_page += 1
                products[product_id] = {
                    "title": title if title else "(titre indisponible)",
                    "url": url,
                }
            elif title and products[product_id]["title"] == "(titre indisponible)":
                products[product_id]["title"] = title

        if params_key and found_on_this_page == 0:
            break

    return products


# ---------------------------------------------------------------------------
# Recuperateurs specifiques (logique propre a un site precis, deja testee)
# ---------------------------------------------------------------------------

def fetch_philibert():
    return fetch_prestashop_id_pattern(
        category_url="https://www.philibertnet.com/fr/212-pokemon/s-3/langues-francais",
        id_pattern=r"/fr/pokemon/(\d+)-[a-z0-9-]+\.html",
        base_url="https://www.philibertnet.com",
    )


def fetch_strikegames():
    return fetch_shopify("https://strikegames.shop", "tcg-pokemon-produit-en-francais")


def fetch_investcollect():
    return fetch_prestashop_id_pattern(
        category_url="https://investcollect.com/eshop/produits-scelles.html",
        id_pattern=r"/eshop/p/([a-z0-9\-_.]+)\.html",
        base_url="https://investcollect.com",
    )


def fetch_maisondelapresse():
    """Cas particulier : on cible specifiquement la grille de produits de la
    categorie (<ol class="products list items product-items">), pas toute la
    page, pour eviter de capturer des widgets de recommandation qui changent
    de contenu a chaque requete sans lien avec le vrai catalogue."""
    base_url = "https://www.maisondelapresse.com/jeux-jouets/cartes-collectionner/cartes-pokemon.html"
    products = {}

    for page in range(1, MAX_PAGES_DEFAULT + 1):
        resp = requests.get(base_url, params={"p": page}, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        links = soup.select("ol.products.list.items.product-items a.product-item-link")
        if not links:
            links = soup.select("a.product-item-link")
        if not links:
            break

        for a in links:
            url = a.get("href", "").split("?")[0]
            if not url:
                continue
            title = a.get_text(strip=True)
            if url not in products:
                products[url] = {
                    "title": title if title else "(titre indisponible)",
                    "url": url,
                }

    return products


def fetch_playin():
    return fetch_url_pattern(
        page_url="https://www.play-in.com/fr/gamme/3/pokemon/catalogue",
        id_pattern=r"/fr/produit/(\d+)/[a-z0-9\-]+",
        base_url="https://www.play-in.com",
    )


def fetch_lecoindesbarons():
    return fetch_url_pattern(
        page_url="https://lecoindesbarons.com/les-tcg/cartes-pokemon/",
        id_pattern=r"(/tradingcard-game/cartes-pokemon/[a-z0-9\-]+/)$",
        base_url="https://lecoindesbarons.com",
        params_key="paged",
        max_pages=5,
    )


# ---------------------------------------------------------------------------
# Configuration de tous les sites suivis. Chaque entree : cle d'etat,
# libelle pour les notifs, fonction de recuperation.
# ---------------------------------------------------------------------------

SITES = [
    ("philibert", "Philibert", fetch_philibert),
    ("strikegames", "Strike Games", fetch_strikegames),
    ("investcollect", "InvestCollect", fetch_investcollect),
    ("maisondelapresse", "Maison de la Presse", fetch_maisondelapresse),
    ("tradingcardsxxx", "Tradingcardsxxx", lambda: fetch_shopify("https://tradingcardsxxx.fr", "pokemon")),
    ("lebordelmagique", "Le Bordel Magique", lambda: fetch_shopify("https://lebordelmagique.com", "pokemon-fr")),
    ("relictcg", "RelicTCG", lambda: fetch_shopify("https://www.relictcg.com", "pokemon")),
    ("kairyu", "Kairyu", lambda: fetch_shopify("https://kairyu.fr", "frjap-scelle")),
    ("masterset", "Masterset", lambda: fetch_shopify("https://masterset.store", "pokemon")),
    ("kingdultes", "Kingdultes", lambda: fetch_shopify("https://www.kingdultes.com", "tcg-pokemon")),
    ("ludiworld", "Ludiworld", lambda: fetch_prestashop_id_pattern(
        category_url="https://www.ludiworld.com/148-pokemon",
        id_pattern=r"/(\d+)-[a-z0-9-]+\.html",
        base_url="https://www.ludiworld.com",
    )),
    ("ludocortex", "Ludocortex", lambda: fetch_prestashop_id_pattern(
        category_url="https://www.ludocortex.fr/58-pokemon-tcg",
        id_pattern=r"/(\d+)-[a-z0-9-]+\.html",
        base_url="https://www.ludocortex.fr",
    )),
    ("bcdjeux", "BCD Jeux", lambda: fetch_prestashop_id_pattern(
        category_url="https://www.bcd-jeux.fr/511809-pokemon-tcg",
        id_pattern=r"/(\d+)-[a-z0-9-]+\.html",
        base_url="https://www.bcd-jeux.fr",
    )),
    ("lesgentlemendujeu", "Les Gentlemen du Jeu", lambda: fetch_prestashop_id_pattern(
        category_url="https://lesgentlemendujeu.com/14-cartes-a-collectionner/s-6/cartes_a_collectionner-pokemon",
        id_pattern=r"/(\d+)-[a-z0-9_-]+\.html",
        base_url="https://lesgentlemendujeu.com",
    )),
    ("playin", "Playin", fetch_playin),
    ("lecoindesbarons", "Le Coin des Barons", fetch_lecoindesbarons),
]


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {key: {} for key, _, _ in SITES}


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


def diff_and_notify(site_label, previous, current):
    """Compare les dicts previous/current, notifie les nouveaux, retourne
    l'etat fusionne (jamais un simple remplacement, voir explication ci-dessous).

    IMPORTANT : on fusionne previous et current (au lieu de remplacer par
    current) pour ne jamais "oublier" un produit qui aurait disparu
    temporairement d'un passage a l'autre (variation d'affichage cote site,
    decalage de stock, etc.). Sans cette fusion, un produit qui reapparait
    apres une absence momentanee serait injustement considere comme
    "nouveau" et redeclencherait une notification.
    """
    new_ids = [pid for pid in current if pid not in previous]

    if not previous:
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
        time.sleep(1)

    merged = dict(previous)
    merged.update(current)
    return merged


def main():
    state = load_state()

    for key, label, fetch_fn in SITES:
        try:
            current = fetch_fn()
        except Exception as e:
            print(f"Erreur recuperation {label}: {e}", file=sys.stderr)
            continue

        state[key] = diff_and_notify(label, state.get(key, {}), current)

    save_state(state)


if __name__ == "__main__":
    main()
