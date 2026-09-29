# Paravent

Paravent range des documents personnels (courriers, relevés, attestations…) sous forme d'un
**corpus Markdown** lisible par un humain, et en tire un **miroir anonymisé** que l'on peut
confier à une IA. Les documents restent derrière le paravent : seul le miroir passe devant.

Tout tourne en local, sur Windows et Linux, sans carte graphique. Une IA locale (Ollama) peut
faire des propositions si elle est configurée ; rien n'en dépend.

> **État : V1 en cours.** La conversion en Markdown, le corpus (import, dédoublonnage, reprise)
> et le rangement sont en place, avec la mise à jour automatique. Le miroir anonymisé vient
> ensuite.

## Ce qui marche aujourd'hui

```sh
paravent init ~/MonCorpus                  # crée un corpus dans un dossier vide
cd ~/MonCorpus
paravent import ~/Téléchargements/scans    # fichiers ou dossiers (PDF, images, Word, e-mails)
paravent etat                              # à faire · fait · échec · à vérifier
paravent mesures                           # chiffres sur la conversion, sans aucun nom
paravent ranger                            # range les documents arrivés dans corpus/_a-ranger
paravent convert courrier.pdf -o courrier.md   # un document seul, hors corpus
paravent update                            # vérifie et installe la dernière version
```

Un corpus contient trois dossiers :

```text
MonCorpus/
├── originaux/   copies des documents importés, dédoublonnées, en lecture seule
├── corpus/      les documents en Markdown, rangés comme vous voulez ; les nouveaux arrivent dans _a-ranger/
└── .corpus/     l'état des imports — jamais partagé
```

- Les pages PDF qui ont une couche texte sont lues directement. Les autres (scans, photos)
  passent par l'OCR ([RapidOCR](https://github.com/RapidAI/RapidOCR)). Ses modèles sont fournis
  avec l'installation : rien n'est téléchargé à l'usage.
- Un document Word (`.docx`) garde ses titres, listes et tableaux. Un e-mail (`.eml`) donne
  ses en-têtes (de, à, date, objet) et son texte ; chaque pièce jointe devient un document à part,
  avec un lien vers le mail d'origine (`piece_jointe_de`).
- Les lignes d'un même paragraphe sont recollées, d'après leur place sur la page : une ligne
  continue la précédente quand elle la suit de près et que son premier mot n'aurait pas tenu au
  bout. Une adresse, une liste, un champ de formulaire (« IBAN : … ») ou une ligne de tableau
  restent à part.
- Chaque page est précédée d'un repère `<!-- page N · ocr · 40 lignes · confiance min 0.94 -->`.
  Un passage que l'OCR lit mal est **surligné** (`==…==`, en jaune dans Obsidian) et suivi de sa
  confiance et de sa place dans la page (`<!-- illisible ? confiance 0.61 · ligne 12/40 -->`) ;
  il n'est ni gardé sans avertissement, ni jeté (RapidOCR écarte d'ordinaire les lignes sous
  0,5 : pas ici). Un
  document qui a une page vide ou peu sûre passe « à vérifier ». Une page blanche (un verso) est
  reconnue comme telle et ne compte pas : il faut que l'OCR n'y lise rien et qu'elle n'ait
  presque pas d'encre, si bien qu'une signature ou un mot au crayon la laissent « à vérifier ».
- Les documents à vérifier sont listés dans `À vérifier.md`, à la racine du corpus (hors de
  `corpus/`, donc jamais dans le miroir), avec un lien vers le Markdown et un vers l'original.
  Cochez la case d'un document une fois relu (dans Obsidian, par exemple) : le prochain
  `paravent etat` ou `paravent import` le passe à « fait ». Les échecs de conversion y sont
  listés à part.
- `paravent mesures` ne donne que des chiffres (documents par format et par état, pages lues
  par couche texte ou par OCR, tranches de confiance, pages dont la couche texte est très courte,
  et les lignes illisibles décrites sans leur texte : longueur, confiance, place dans la page,
  concentration par document) : on peut les partager pour se faire aider sans rien dévoiler des
  documents.
- Un import interrompu reprend là où il s'était arrêté : relancez la même commande.
- `paravent init` refuse un dossier synchronisé par OneDrive : les documents partiraient en clair
  dans le cloud.

## Avec Obsidian (facultatif)

Le dossier du corpus peut s'ouvrir tel quel comme coffre [Obsidian](https://obsidian.md) :
rangement à la souris, recherche, et l'original PDF à côté du Markdown (le champ `original` des
Propriétés est un lien). `.corpus/` y reste invisible. Paravent retrouve un document par ce champ,
où que vous l'ayez déplacé.

- `paravent init` règle Obsidian pour que les nouvelles notes aillent dans `corpus/`, désactive
  les modules Sync et Publish (Obsidian les active par défaut ; sans compte ils n'envoient rien,
  mais une connexion enverrait tout), et refuse de créer un corpus à l'intérieur d'un autre coffre.
- `paravent etat` avertit si le coffre a des plugins tiers (ils peuvent lire tous les documents),
  ou si les modules Sync ou Publish y sont activés.

## IA locale (facultative)

Si une IA tourne sur la machine ou le réseau local ([Ollama](https://ollama.com)), `paravent
ranger` lui demande une proposition pour chaque document : type, émetteur, date, titre et dossier.
Vous acceptez, corrigez ou passez. Sans IA, `ranger` demande simplement le dossier et le nom.

Pour l'activer, créez `config.toml` dans `~/.config/paravent/` (Linux) ou
`%APPDATA%\paravent\` (Windows) :

```toml
[ia]
url = "http://localhost:11434"
modele = "gemma4:8b-16k"
```

`paravent ia` vérifie que le serveur répond et que le modèle est installé. Les règles :

- **Uniquement locale.** L'IA lit les documents *non anonymisés* : une adresse qui ne désigne pas
  cette machine ou le réseau local est refusée.
- **Elle propose, le code vérifie, vous décidez.** Réponse en JSON contrôlée, date vérifiée,
  dossier existant ou signalé comme nouveau, nom de fichier construit par Paravent.
- **Elle ne réécrit jamais un document** (pas de « correction » de l'OCR : elle inventerait ce
  qui est illisible).

## Installation

Paravent s'installe avec [uv](https://docs.astral.sh/uv/), qui installe lui-même Python :

```sh
uv tool install --python 3.12 https://github.com/miccarrer/paravent/releases/download/v0.1.0/paravent-0.1.0-py3-none-any.whl
```

(adresse du paquet de la version voulue : voir la page [Releases](https://github.com/miccarrer/paravent/releases)).

Les mises à jour passent ensuite par `paravent update`. Chaque version publiée fournit un
paquet (wheel) et un fichier `latest.json` qui donne sa version, son adresse et son empreinte
SHA-256. Le paquet téléchargé est vérifié avant d'être installé. Sous Windows, l'installation se
termine juste après la fermeture de Paravent, parce qu'un programme en cours d'exécution ne
peut pas être remplacé.

## Développement

```sh
uv sync
uv run pytest
uv run python scripts/ci_update_scenario.py   # installation → conversion → mise à jour, isolé dans un dossier temporaire
```

La CI (GitHub Actions) exécute les tests et ce scénario sous **Linux et Windows**. Pousser un
tag `vX.Y.Z` publie la version, mais seulement si la CI passe sur les deux systèmes.

**Aucun document réel dans ce dépôt.** Les documents de test (`tests/fixtures/`) sont inventés
et régénérables avec `tests/fixtures/make_fixtures.py`.

## Licence

MIT.
