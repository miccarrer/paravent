# Paravent

Paravent range des documents personnels (courriers, relevés, attestations…) sous forme d'un
**corpus Markdown** lisible par un humain, et en tire un **miroir anonymisé** que l'on peut
confier à une IA. Les documents restent derrière le paravent : seul le miroir passe devant.

Tout tourne en local, sur Windows et Linux, sans carte graphique ni IA installée.

> **État : V0a.** Seule la conversion d'un document en Markdown est en place, avec la mise à
> jour automatique. Le corpus et le miroir anonymisé viennent ensuite.

## Ce qui marche aujourd'hui

```sh
paravent convert courrier.pdf -o courrier.md   # PDF (texte ou scanné) ou image → Markdown
paravent update                                # vérifie et installe la dernière version
paravent --version
```

- Les pages PDF qui ont une couche texte sont lues directement. Les autres (scans, photos)
  passent par l'OCR ([RapidOCR](https://github.com/RapidAI/RapidOCR)). Ses modèles sont fournis
  avec l'installation : rien n'est téléchargé à l'usage.
- Chaque page est précédée d'un repère `<!-- page N · ocr · confiance min 0.94 -->`. Une ligne
  que l'OCR lit mal est signalée comme telle ; elle n'est jamais gardée sans avertissement.

## Installation

Paravent s'installe avec [uv](https://docs.astral.sh/uv/), qui installe lui-même Python :

```sh
uv tool install --python 3.12 https://github.com/miccarrer/paravent/releases/latest/download/<wheel>
```

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
