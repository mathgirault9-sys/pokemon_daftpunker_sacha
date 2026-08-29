# Surveillance de nouveaux produits Pokemon

Ce petit projet vérifie automatiquement (toutes les 20 minutes) si un
nouveau produit Pokémon apparaît sur :

- Philibert : https://www.philibertnet.com/fr/212-pokemon/s-3/langues-francais
- Strike Games : https://strikegames.shop/collections/tcg-pokemon-produit-en-francais

Dès qu'un nouveau produit est détecté, une **notification push** est
envoyée sur ton téléphone via [ntfy.sh](https://ntfy.sh) (gratuit, sans compte).

## Étape 1 — Installer l'app ntfy sur ton téléphone

1. Installe l'application **ntfy** :
   - Android : [Google Play](https://play.google.com/store/apps/details?id=io.heckel.ntfy)
   - iPhone : [App Store](https://apps.apple.com/us/app/ntfy/id1625396347)
2. Ouvre l'app, appuie sur **"+"** pour t'abonner à un topic.
3. Choisis un **nom de topic unique et difficile à deviner**
   (n'importe qui connaissant le nom peut s'y abonner ou t'envoyer des
   notifs). Par exemple : `pokemon-alertes-a8f3k2x9`
4. Abonne-toi à ce topic dans l'app. Garde ce nom de côté, tu en auras besoin
   à l'étape 3.

## Étape 2 — Créer le dépôt GitHub

1. Crée un compte gratuit sur [github.com](https://github.com) si tu n'en as pas.
2. Crée un **nouveau dépôt** (bouton vert "New"), par exemple nommé
   `pokemon-watcher`. Peut être privé ou public, peu importe.
3. Mets-y les fichiers de ce dossier (`check_pokemon.py`, `requirements.txt`,
   `seen_products.json`, le dossier `.github/`) — soit en les glissant
   directement dans l'interface GitHub ("Add file" → "Upload files"),
   soit via `git push` si tu es à l'aise avec Git.

## Étape 3 — Configurer le secret NTFY_TOPIC

1. Dans ton dépôt GitHub, va dans **Settings** → **Secrets and variables**
   → **Actions**.
2. Clique sur **New repository secret**.
3. Nom : `NTFY_TOPIC`
   Valeur : le nom de topic choisi à l'étape 1 (ex: `pokemon-alertes-a8f3k2x9`)
4. Sauvegarde.

## Étape 4 — Lancer une première fois

1. Va dans l'onglet **Actions** de ton dépôt.
2. Clique sur le workflow **"Surveillance Pokemon"**, puis **"Run workflow"**
   pour le lancer manuellement une première fois.
3. Ce premier passage sert juste à **enregistrer les produits déjà en ligne**
   (aucune notification n'est envoyée à ce moment, pour éviter d'être
   inondé d'alertes sur des produits qui existent déjà).
4. À partir de la deuxième exécution (automatique, 20 min plus tard), toute
   nouveauté déclenchera une notification sur ton téléphone.

## C'est tout !

Le workflow tourne désormais tout seul toutes les 20 minutes, gratuitement
(GitHub Actions offre largement assez d'heures gratuites pour ce genre de
tâche légère). Tu peux ajuster la fréquence dans
`.github/workflows/check.yml` (ligne `cron: "*/20 * * * *"`) — attention,
en dessous de 5-10 minutes, GitHub peut ignorer certaines exécutions
programmées en période de forte charge.

## Dépannage

- **Pas de notif alors qu'un produit a été ajouté ?** Va dans l'onglet
  Actions et regarde les logs de la dernière exécution — les erreurs
  éventuelles (site indisponible, structure de page changée) y sont affichées.
- **Le site a changé de structure et le script ne détecte plus rien ?**
  Il faudra ajuster le code d'extraction dans `check_pokemon.py`
  (fonctions `fetch_philibert` et `fetch_strikegames`).
- **Tu veux surveiller d'autres pages Pokémon ?** Duplique la logique
  dans `check_pokemon.py` avec une nouvelle URL.
