# Aurora France — station de prévision

Interface web de prévision météorologique sur la France, bâtie autour du modèle
de fondation [Aurora](https://github.com/microsoft/aurora) de Microsoft.
L'application tourne intégralement en local : un serveur FastAPI pilote le
modèle (chargement, déchargement, inférence) et sert une interface cartographique
animée sans aucune dépendance externe côté navigateur.

```
http://127.0.0.1:8077/        carte et prévisions
http://127.0.0.1:8077/admin   console d'administration
http://127.0.0.1:8077/api/docs  documentation de l'API
```

---

## Démarrage

```bash
./run.sh
```

Le script crée l'environnement virtuel, installe les dépendances légères
(FastAPI, uvicorn, numpy, psutil) et démarre le serveur. **Aucune installation de
PyTorch n'est nécessaire pour explorer l'interface** : un simulateur
atmosphérique local prend le relais tant qu'Aurora n'est pas chargé.

Variables d'environnement utiles :

| Variable | Défaut | Rôle |
| --- | --- | --- |
| `AURORA_HOST` | `0.0.0.0` | Interface d'écoute (`127.0.0.1` pour rester local) |
| `AURORA_PORT` | `8077` | Port |
| `AURORA_ADMIN_TOKEN` | *(vide)* | Jeton requis pour administrer depuis le réseau |
| `GITHUB_CLIENT_ID` | *(via `.env`)* | Identifiant OAuth pour la publication de démo |
| `AURORA_DEMO_ISSUE_TITLE` | `Public demo` | Titre de l'issue de démonstration |
| `AURORA_MAX_STORED_FORECASTS` | `40` | Nombre de prévisions conservées sur disque |
| `AURORA_DATA_DIR` | `./data` | Cache des poids et des données |
| `HF_HOME` | `./data/cache/huggingface` | Cache HuggingFace |

### Accès depuis le réseau local

Par défaut le serveur écoute sur toutes les interfaces : les URL d'accès sont
affichées au démarrage et rappelées dans la console d'administration.

L'API n'a pas de mécanisme d'authentification général. Les opérations sensibles —
chargement et déchargement de modèle, installation de dépendances, purge du cache —
sont donc filtrées :

| Origine de la requête | Consultation | Lancer une prévision | Administration |
| --- | --- | --- | --- |
| Machine hôte (`127.0.0.1`) | ✅ | ✅ | ✅ |
| Autre poste, sans jeton | ✅ | ✅ | ❌ `403` |
| Autre poste, avec jeton | ✅ | ✅ | ✅ |

Pour autoriser l'administration à distance :

```bash
AURORA_ADMIN_TOKEN="$(openssl rand -hex 24)" ./run.sh
```

La console d'administration réclame le jeton à la première action refusée et le
conserve dans le stockage local du navigateur.

Le trafic reste en HTTP clair et sans chiffrement : réservez cette exposition à un
réseau de confiance. Pour revenir à un fonctionnement strictement local :

```bash
AURORA_HOST=127.0.0.1 ./run.sh
```

---

## L'interface de prévision

<table>
<tr><td width="34%"><b>Carte animée</b></td><td>
Rendu canvas natif : champ continu interpolé, particules de vent advectées en
temps réel, isobares calculées par <i>marching squares</i>, contour de la France
en surbrillance et pays voisins atténués. Projection Mercator.
</td></tr>
<tr><td><b>Variables</b></td><td>
Température 2 m, vent moyen, rafales, pression au niveau de la mer,
précipitations, couverture nuageuse, humidité relative, température 850 hPa,
géopotentiel 500 hPa. Les variables réellement disponibles dépendent du modèle
chargé.
</td></tr>
<tr><td><b>Frise temporelle</b></td><td>
Lecture animée avec interpolation entre échéances (0,5× à 4×), repères
journaliers, navigation au clavier (<kbd>espace</kbd>, <kbd>←</kbd>,
<kbd>→</kbd>).
</td></tr>
<tr><td><b>Panneau ville</b></td><td>
45 villes de référence, météogramme (courbe de température, enveloppe
d'incertitude d'ensemble, barres de précipitations, vent), classement dynamique.
Un clic n'importe où sur la carte extrait la série du point.
</td></tr>
<tr><td><b>Historique</b></td><td>
Chaque prévision est écrite sur disque et rechargeable après redémarrage du
serveur. Suppression à l'unité ou vidage complet, depuis la carte comme depuis
la console d'administration.
</td></tr>
</table>

---

## La console d'administration

- **Ressources machine** — CPU, RAM, VRAM par GPU, disque, rafraîchies toutes
  les 5 s.
- **Catalogue Aurora** — les 9 versions publiées du modèle avec leurs
  caractéristiques (résolution, paramètres, taille des poids, VRAM requise) et
  un contrôle de pré-vol par carte : PyTorch présent ? mémoire suffisante ?
- **Chargement / déchargement** — sélection du périphérique (`cpu`, `cuda:N`),
  bascule LoRA, barre de progression du téléchargement des poids depuis
  HuggingFace, libération explicite de la mémoire (`empty_cache` inclus).
- **Installation des dépendances** — installe PyTorch, `microsoft-aurora` et
  optionnellement le client ERA5 dans l'environnement du serveur, avec sortie
  `pip` diffusée en direct.
- **Journal temps réel** — flux SSE de tous les événements du serveur.
- **Cache des poids** — inventaire et purge.

---

## Démonstration publique

La console d'administration comporte un parcours en trois étapes pour partager la
station avec l'extérieur.

### 1. Ouvrir un tunnel

Deux fournisseurs sont détectés automatiquement :

| Fournisseur | Compte requis | URL |
| --- | --- | --- |
| Cloudflare Quick Tunnel | non | `https://….trycloudflare.com` |
| Microsoft Dev Tunnels | oui, GitHub ou Microsoft | `https://….devtunnels.ms` |

Pour Dev Tunnels, la session est vérifiée à l'affichage et rappelée dans
l'interface. Si elle manque :

```bash
devtunnel user login -g
```

Les URL sont **éphémères** : elles changent à chaque réouverture et cessent de
fonctionner dès l'arrêt du tunnel.

### 2. Se connecter à GitHub

L'authentification utilise le *device flow* : l'application affiche un code à
saisir sur `github.com/login/device`. Aucun mot de passe ni secret client ne
transite par la station. Le jeton obtenu est conservé côté serveur dans
`data/state/github_token.json` en permissions `600` et **n'est jamais transmis au
navigateur**.

L'identifiant client se règle dans `.env` :

```
GITHUB_CLIENT_ID=Ov23li…
```

Le *device flow* doit être activé dans les réglages de l'application GitHub.

### 3. Publier l'URL dans un dépôt

Sélectionnez un dépôt (privés uniquement par défaut), puis **Publier** :

- si aucune issue intitulée `Public demo` n'existe, elle est créée ;
- sinon un commentaire y est ajouté, et l'issue est rouverte si elle était close.

Le corps du message reprend l'URL, le fournisseur de tunnel, le modèle chargé et
le nombre de prévisions en mémoire.

Le titre de l'issue est configurable via `AURORA_DEMO_ISSUE_TITLE`.

> Ouvrir un tunnel rend la station accessible à toute personne disposant du lien :
> consultation des prévisions et lancement de nouvelles exécutions. Le pilotage du
> modèle reste protégé par le mécanisme de jeton décrit plus haut.

---

## Modèles disponibles

| Modèle | Résolution | Poids | VRAM | Usage |
| --- | --- | --- | --- | --- |
| Simulateur local | 0.1° | — | — | démonstration hors ligne |
| Aurora 0.25° Small Pretrained | 0.25° | 0,5 Go | 4 Go | débogage |
| Aurora 0.25° Pretrained | 0.25° | 5,2 Go | 40 Go | ERA5, usage général |
| Aurora 0.25° Fine-Tuned | 0.25° | 5,2 Go | 40 Go | IFS HRES T0 |
| Aurora 1.5 | 0.25° | 5,5 Go | 32 Go | 26 variables, pas horaire |
| Aurora 1.5 Ensemble | 0.25° | 5,5 Go | 32 Go | prévision probabiliste |
| Aurora 0.25° 12 h | 0.25° | 5,2 Go | 40 Go | longues échéances |
| Aurora 0.1° Fine-Tuned | 0.1° | 5,3 Go | 80 Go | haute résolution |
| Aurora 0.4° Air Pollution | 0.4° | 5,4 Go | 40 Go | CAMS (données non fournies) |
| Aurora 0.25° Wave | 0.25° | 5,4 Go | 40 Go | HRES-WAM (données non fournies) |

---

## Sources de conditions initiales

**Atmosphère synthétique** (toujours disponible) — modèle barotrope
géostrophique embarqué : centres d'action mobiles recyclés en longitude, vent
déduit du gradient de pression avec friction différenciée terre/mer, advection
thermique, gradient adiabatique sur un relief analytique (Alpes, Pyrénées, Massif
central, Jura, Vosges, Corse), cycle diurne modulé par la nébulosité.
**Ce ne sont pas des observations : les champs sont fictifs.**

**ERA5 — Copernicus CDS** — réanalyse 0.25°, 13 niveaux de pression, exactement
le format attendu par les versions pré-entraînées d'Aurora. Requiert :

```bash
.venv/bin/pip install cdsapi xarray netCDF4
```

Les identifiants sont recherchés dans cet ordre :

1. les variables d'environnement `CDSAPI_URL` et `CDSAPI_KEY` ;
2. `.cdsapirc` à la **racine du projet** ;
3. `~/.cdsapirc` (emplacement par défaut de la bibliothèque `cdsapi`).

```
url: https://cds.climate.copernicus.eu/api
key: VOTRE_CLE
```

Ce fichier contient un secret : il est exclu du dépôt par `.gitignore` et doit
rester en permissions `600`. Il faut également accepter les conditions
d'utilisation des jeux de données `reanalysis-era5-single-levels` et
`reanalysis-era5-pressure-levels` sur le portail Copernicus.

---

## Exécuter réellement Aurora

1. Console d'administration → **Installer PyTorch + microsoft-aurora**
   (ou `.venv/bin/pip install -r requirements-model.txt`).
   Sur GPU NVIDIA, installez d'abord la variante PyTorch correspondant à votre
   version de CUDA — voir <https://pytorch.org/get-started/locally/>.
2. Configurez l'accès ERA5 (ci-dessus).
3. Chargez un modèle, en choisissant `cuda:0` si un GPU est disponible.
4. Revenez sur la carte, sélectionnez la source **ERA5**, un réseau synoptique
   (00/06/12/18 UTC) et lancez la prévision.

Le badge en haut à gauche de la carte indique sans ambiguïté si les champs
affichés proviennent de données réelles ou du simulateur.

---

## Architecture

```
backend/
  main.py           API HTTP, SSE, service des fichiers statiques
  model_manager.py  machine à états du modèle, pré-vol, cache, installation
  forecast.py       file de travaux, rollout, extraction France, quantification
  storage.py        persistance des prévisions (index, chargement, suppression)
  simulation.py     moteur atmosphérique local
  data_sources.py   ERA5/CDS, disponibilité des sources
  tunnel.py         tunnels publics (Cloudflare, Dev Tunnels)
  github_client.py  device flow GitHub, publication de l'issue de démo
  registry.py       catalogue des modèles et des variables
  geo.py            contours, villes, masques terre/mer, relief
  system_info.py    sonde CPU/RAM/GPU/dépendances/réseau
  events.py         bus d'événements temps réel
frontend/
  index.html        vue prévision
  admin.html        console d'administration
  js/mapview.js     moteur de rendu cartographique
  js/charts.js      météogrammes
  js/colormaps.js   palettes et LUT
  js/app.js         orchestration de la vue prévision
  js/admin.js       orchestration de l'administration
```

Les champs sont transmis quantifiés sur 16 bits en base64 (~47 ko par échéance
et par variable), décodés en `Float32Array` côté navigateur : l'animation reste
fluide sans WebGL ni bibliothèque tierce.

Sur disque, chaque prévision occupe `data/forecasts/{id}/` avec ses métadonnées
en JSON et ses champs en NPZ compressé (5 à 15 Mo selon l'échéance). L'écriture
est atomique — dossier temporaire puis renommage — pour qu'une interruption ne
laisse jamais d'entrée corrompue. Seuls les champs des quatre dernières
prévisions consultées restent en mémoire ; les autres sont relus à la demande.
La limite d'entrées conservées se règle avec `AURORA_MAX_STORED_FORECASTS`
(40 par défaut).

---

## Avertissement

Aurora est un modèle de **recherche**. Ses sorties ne constituent pas un service
météorologique opérationnel et ne doivent pas fonder de décision critique sans
validation par un expert du domaine. Le simulateur local intégré produit des
champs entièrement fictifs, destinés à la seule démonstration de l'interface.

Aurora est distribué par Microsoft sous sa propre licence ; consultez le
[dépôt officiel](https://github.com/microsoft/aurora) et l'article
[*A Foundation Model for the Earth System*](https://www.nature.com/articles/s41586-025-09005-y)
(Bodnar et al., Nature, 2025).
