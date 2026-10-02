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
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
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


JAPANESE_MARKERS = ("jp", "japonais", "japon", "jap ")


def fetch_pikaboutique():
    """Pika-Boutique n'a pas de collection unique couvrant tout le scelle
    francais : on combine les 4 collections qui, ensemble, representent le
    catalogue FR (le japonais a sa propre collection separee, 'Japonais',
    volontairement exclue ici). On filtre aussi par securite tout produit
    dont le titre indique explicitement une version japonaise, au cas ou
    un article JP se retrouverait mal classe dans une des 4 collections."""
    base_url = "https://pika-boutique.fr"
    collections = ["etb", "displays", "box-coffrets", "boosters"]
    products = {}

    for handle in collections:
        batch = fetch_shopify(base_url, handle)
        for product_id, item in batch.items():
            title_lower = item["title"].lower()
            if any(marker in title_lower for marker in JAPANESE_MARKERS):
                continue
            products[product_id] = item

    return products


def fetch_prestashop_id_pattern(category_url, id_pattern, base_url, max_pages=MAX_PAGES_DEFAULT, warmup_url=None):
    """Sites PrestaShop : les liens produits contiennent un identifiant
    numerique stable dans l'URL (ex: /fr/pokemon/12345-nom-du-produit.html).
    Pagination classique via ?page=N.

    Si warmup_url est fourni, on visite d'abord cette page avec une session
    (pour recuperer d'eventuels cookies anti-bot) avant de requeter la page
    cible, avec un Referer. Cela peut aider a passer des protections legeres,
    mais ne garantit rien contre une protection basee sur l'empreinte TLS
    (Cloudflare/DataDome), que la librairie requests ne peut pas reproduire.
    """
    products = {}
    pattern = re.compile(id_pattern)

    session = requests.Session()
    session.headers.update(HEADERS)
    request_headers = {}
    if warmup_url:
        session.get(warmup_url, timeout=30)
        request_headers["Referer"] = warmup_url

    for page in range(1, max_pages + 1):
        resp = session.get(category_url, params={"page": page}, headers=request_headers, timeout=30)
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


def fetch_auxtroiskoalas():
    """Site Odoo (verifie directement, HTML propre genere cote serveur).
    Pagination particuliere : via un suffixe de chemin /page/N (pas un
    parametre de requete ?page=N comme les sites PrestaShop)."""
    base_category_url = "https://www.auxtroiskoalas.fr/shop/category/pokemon-coffrets-428"
    id_pattern = re.compile(r"/shop/pokemon-coffrets-428/[a-z0-9\-]+-(\d+)")
    products = {}

    for page in range(1, MAX_PAGES_DEFAULT + 1):
        url = base_category_url if page == 1 else f"{base_category_url}/page/{page}"
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        found_on_this_page = 0
        for a in soup.find_all("a", href=True):
            m = id_pattern.search(a["href"])
            if not m:
                continue
            product_id = m.group(1)
            title = a.get_text(strip=True)
            link = a["href"].split("?")[0]
            if not link.startswith("http"):
                link = "https://www.auxtroiskoalas.fr" + link

            if product_id not in products:
                found_on_this_page += 1
                products[product_id] = {
                    "title": title if title else "(titre indisponible)",
                    "url": link,
                }
            elif title and products[product_id]["title"] == "(titre indisponible)":
                products[product_id]["title"] = title

        if found_on_this_page == 0:
            break

    return products


# ---------------------------------------------------------------------------
# Configuration de tous les sites suivis. Chaque entree : cle d'etat,
# libelle pour les notifs, fonction de recuperation.
# ---------------------------------------------------------------------------

SITES = [
    ("philibert", "Philibert", fetch_philibert),
    ("strikegames", "Strike Games", fetch_strikegames),
    ("investcollect", "InvestCollect", fetch_investcollect),
    ("auxtroiskoalas", "Aux Trois Koalas", fetch_auxtroiskoalas),
    ("arakemon", "Arakemon", lambda: fetch_prestashop_id_pattern(
        category_url="https://www.arakemon.com/coffrets-pokemon",
        id_pattern=r"/(product-page/[^?\s\"']+)",
        base_url="https://www.arakemon.com",
    )),
    ("pikaboutique", "Pika-Boutique", fetch_pikaboutique),
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
