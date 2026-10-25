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
import unicodedata
from pathlib import Path
from urllib.parse import unquote

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

def fetch_shopify_raw(base_url, collection_handle):
    """Retourne la liste brute des produits d'une collection Shopify (API
    JSON native /products.json), avec tous leurs champs (tags, vendor...)."""
    url = f"{base_url}/collections/{collection_handle}/products.json?limit=250"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json().get("products", [])


def fetch_shopify(base_url, collection_handle):
    """Sites Shopify : on utilise l'API JSON native /products.json, beaucoup
    plus fiable qu'un scraping HTML (pas de classe CSS a deviner)."""
    products = {}
    for p in fetch_shopify_raw(base_url, collection_handle):
        product_id = str(p["id"])
        title = p.get("title", "(titre indisponible)")
        handle = p.get("handle", "")
        products[product_id] = {
            "title": title,
            "url": f"{base_url}/products/{handle}",
        }
    return products


JAPANESE_MARKERS = ("jp", "japonais", "japon", "jap ")


def _normalize(text):
    """Minuscules et sans accents (pour comparer 'Pokémon' et 'pokemon')."""
    text = unicodedata.normalize("NFD", str(text).lower())
    return "".join(c for c in text if unicodedata.category(c) != "Mn")


# Mentions d'une langue autre que le francais. Seuls les produits francais
# sont pris en compte : un produit dont le titre (ou a defaut l'URL) porte une
# de ces mentions n'est jamais enregistre ni notifie. Pas de "en" ni de "ko"
# seuls (mot francais / "Collection K.O.").
FOREIGN_LANGUAGE_PATTERN = re.compile(
    r"\b(chinois|chinoise|chinese|simplifie|traditionnel|cn|"
    r"japonais|japonaise|japanese|japon|jap|jp|jpn|"
    r"coreen|coreenne|korean|kor|kr|"
    r"anglais|anglaise|english|eng|us|uk|"
    r"allemand|allemande|german|deutsch|"
    r"italien|italienne|italian|espagnol|espagnole|spanish|"
    r"portugais|portuguese|neerlandais|dutch|"
    r"thai|thailandais|indonesien|indonesian)\b"
    r"|[\[(](?:en|de|it|es|pt|nl|ko)[\])]"
)
FRENCH_PATTERN = re.compile(r"\b(fr|vf|francais|francaise)\b")


def _language_text(text):
    return re.sub(r"[-_/]+", " ", _normalize(unquote(str(text or ""))))


def is_french_product(title, url):
    """Une mention de langue etrangere dans le titre -> pas francais ; une
    mention FR dans le titre -> francais ; sinon on regarde l'URL."""
    title_text = _language_text(title)
    if FOREIGN_LANGUAGE_PATTERN.search(title_text):
        return False
    if FRENCH_PATTERN.search(title_text):
        return True
    return not FOREIGN_LANGUAGE_PATTERN.search(_language_text(url))


# Autres jeux de cartes vendus par Pika-Boutique (a completer si un autre
# jeu s'infiltre dans les notifications).
OTHER_GAMES_PATTERN = re.compile(
    r"\b(one piece|riftbound|yu-?gi-?oh|lorcana|magic|mtg|dragon ball|digimon|"
    r"union arena|star wars|flesh and blood|naruto|weiss|gundam|final fantasy|"
    r"vanguard|grand archive|altered|metazoo|sorcery)\b"
)


def is_pokemon_product(p):
    """Decide si un produit Shopify est un produit Pokemon, a partir de son
    titre, sa marque, son type et ses tags. Regle : 'pokemon' present
    quelque part -> garde ; sinon un autre jeu cite -> ecarte ; sinon garde
    (pour ne pas rater un produit Pokemon dont le titre ne le mentionne pas,
    ex: 'Coffret ULTRA PREMIUM Terapagos EX [FR]')."""
    tags = p.get("tags", [])
    if isinstance(tags, str):
        tags = tags.split(",")
    text = _normalize(
        " ".join([p.get("title", ""), p.get("vendor", ""), p.get("product_type", "")] + list(tags))
    )
    if "pokemon" in text:
        return True
    if OTHER_GAMES_PATTERN.search(text):
        return False
    return True


def fetch_pikaboutique():
    """Pika-Boutique vend plusieurs jeux (Pokemon, One Piece, Riftbound...) et
    n'a pas de collection unique 'Pokemon FR' : on combine les 4 collections
    de scelle (le japonais a sa propre collection, exclue ici), puis on
    filtre (1) tout ce qui n'est pas Pokemon, (2) tout titre indiquant une
    version japonaise."""
    base_url = "https://pika-boutique.fr"
    collections = ["etb", "displays", "box-coffrets", "boosters"]
    products = {}

    for handle in collections:
        for p in fetch_shopify_raw(base_url, handle):
            title = p.get("title", "(titre indisponible)")
            if any(marker in title.lower() for marker in JAPANESE_MARKERS):
                continue
            if not is_pokemon_product(p):
                continue
            products[str(p["id"])] = {
                "title": title,
                "url": f"{base_url}/products/{p.get('handle', '')}",
            }

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


DESTOCKTCG_STATUS_LABELS = {
    "precommande": "en précommande",
    "reassort": "en réassort",
    "disponible": "disponible",
}


# DestockTCG : on ne garde que les produits scelles contenant des cartes.
# Un titre doit citer un format de cartes (liste blanche) et ne doit pas
# designer un accessoire (liste noire, prioritaire : "Protection pour Coffret
# Dresseur d'Elite" cite "coffret" mais n'a pas de cartes).
CARD_PRODUCT_PATTERN = re.compile(
    r"\b(booster|boosters|display|coffret|coffrets|etb|bundle|pokebox|tin|"
    r"mini tin|deck|decks|artset|tripack|duopack|blister|blisterbooster|"
    r"collection|valisette|pack)\b"
)
ACCESSORY_PATTERN = re.compile(
    r"\b(protection|protections|protege|sleeve|sleeves|pochette|pochettes|"
    r"toploader|binder|portfolio|tapis|playmat|deck ?box|etui|rangement|"
    r"acrylique|plexi|boitier|presentoir|support|des a jouer|compteur|"
    r"peluche|carte cadeau)\b"
)


def is_card_product(title):
    text = _normalize(title)
    if ACCESSORY_PATTERN.search(text):
        return False
    return bool(CARD_PRODUCT_PATTERN.search(text))


def fetch_destocktcg():
    """DestockTCG (site maison, HTML genere cote serveur). Chaque vignette
    produit porte un drapeau de langue ('Langue : fr') et eventuellement une
    etiquette d'etat : classe 'preorder' (precommande) ou 'outofstock' (en
    reassort). Sans etiquette (ou avec une etiquette promo), le produit est
    commandable. Pagination via ?page=N.

    Le site renvoie une erreur 403 a la librairie requests (protection
    anti-bot basee sur l'empreinte TLS) : on passe donc par curl_cffi, qui
    imite l'empreinte d'un vrai navigateur Chrome."""
    from curl_cffi import requests as browser_requests

    base_url = "https://www.destocktcg.fr"
    category_url = f"{base_url}/jeux-de-cartes-a-collectionner/pokemon/"
    id_pattern = re.compile(r"/product/[a-z0-9\-]+-(\d+)$")
    products = {}

    for page in range(1, MAX_PAGES_DEFAULT + 1):
        resp = browser_requests.get(
            category_url, params={"page": page}, impersonate="chrome", timeout=30
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "html.parser")

        found_on_this_page = 0
        for card in soup.select(".product-item-info"):
            a = card.find("a", href=id_pattern)
            if not a:
                continue
            link = a["href"].split("?")[0]
            product_id = id_pattern.search(link).group(1)
            if product_id in products:
                continue
            if not link.startswith("http"):
                link = base_url + link

            flag = card.select_one(".product-thumb-flag")
            if flag and not _normalize(flag.get("alt", "")).endswith(": fr"):
                continue

            title = a.get("title") or a.get_text(strip=True) or "(titre indisponible)"
            if not is_card_product(title):
                continue

            label = card.select_one(".product-item-label")
            label_classes = label.get("class", []) if label else []
            if "preorder" in label_classes:
                status = "precommande"
            elif "outofstock" in label_classes:
                status = "reassort"
            else:
                status = "disponible"

            found_on_this_page += 1
            products[product_id] = {
                "title": title,
                "url": link,
                "status": status,
            }

        if found_on_this_page == 0:
            break

    return products


def diff_status_and_notify(site_label, previous, current):
    """Comme diff_and_notify, mais previent aussi quand un produit deja vu
    devient commandable : passage a 'disponible' (depuis precommande ou
    reassort) ou a 'precommande' (depuis reassort)."""
    merged = diff_and_notify(site_label, previous, current)
    if not previous:
        return merged

    for pid, item in current.items():
        old_status = previous.get(pid, {}).get("status")
        new_status = item["status"]
        if pid not in previous or old_status == new_status or new_status == "reassort":
            continue
        if old_status is None and new_status == "precommande":
            continue
        print(f"[{site_label}] {item['title']} : {old_status} -> {new_status}")
        send_notification(
            title=f"Pokemon {DESTOCKTCG_STATUS_LABELS[new_status]} - {site_label}",
            message=item["title"],
            url=item["url"],
        )
        time.sleep(1)

    return merged


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
    ("destocktcg", "DestockTCG", fetch_destocktcg),
]

# Sites dont on suit aussi l'etat (precommande / reassort / disponible)
STATUS_TRACKED_SITES = {"destocktcg"}


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
            current = {
                pid: item for pid, item in fetch_fn().items()
                if is_french_product(item["title"], item["url"])
            }
        except Exception as e:
            print(f"Erreur recuperation {label}: {e}", file=sys.stderr)
            continue

        diff_fn = diff_status_and_notify if key in STATUS_TRACKED_SITES else diff_and_notify
        state[key] = diff_fn(label, state.get(key, {}), current)

    save_state(state)


if __name__ == "__main__":
    main()
