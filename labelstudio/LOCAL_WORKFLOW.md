# Flujo local: revisión por claim a dataset binario

Todo artefacto descargado o generado vive fuera de Git, por ejemplo:

```text
labelstudio/artifacts/
  evidence.jsonl                 # salida evidence.v1 de una corrida
  body/                           # CSV Body que permite restaurar title/body
  labelstudio_export.json         # export descargado de Label Studio
  claim_reviews.csv               # una revisión humana por claim
  article_labels.csv              # una fila agregada por noticia
  gold_reviewed.csv
  gold_training.csv
```

## 1. Importar tareas por claim

Crea un proyecto limpio con `config_multiclaim.xml` y usa
`import_evidence.py` sobre `evidence.jsonl`. Solo se importan claims que tienen
los cuatro estados listos: LLM, evidencia, embedding y traducción en `OK`.

```bash
export LABEL_STUDIO_TOKEN='token-local'
python3 labelstudio/import_evidence.py \
  --project-id <id> \
  --input labelstudio/artifacts/evidence.jsonl \
  --manifest labelstudio/artifacts/import_manifest.csv
```

El identificador de deduplicación es `claim_key = record_id::claim_id`; por eso
una noticia con tres claims crea tres tareas y ninguna tarea se repite.

## 2. Exportar revisiones humanas

Desde la interfaz de Label Studio usa **Export > JSON** y guárdalo como
`labelstudio/artifacts/labelstudio_export.json`.

```bash
python3 labelstudio/export_annotations.py \
  --mode multiclaim \
  --input-json labelstudio/artifacts/labelstudio_export.json \
  --output labelstudio/artifacts/claim_reviews.csv
```

El resultado sigue siendo una fila por claim. No lo entregues aún al modelo de
clasificación de noticias.

## 3. Agregar una sola etiqueta por noticia

```bash
python3 labelstudio/aggregate_article_labels.py \
  --claims-input labelstudio/artifacts/claim_reviews.csv \
  --body-input labelstudio/artifacts/body/ \
  --output labelstudio/artifacts/article_labels.csv
```

La regla es: algún claim central/material refutado → `REFUTADA`; todos los
claims centrales/materiales respaldados → `RESPALDADA`; de otro modo →
`NO_DETERMINABLE`. La salida conserva auditoría (`claims_total`, razones y
anotaciones), pero no duplica artículos.

## 4. Validar Gold

```bash
python3 labelstudio/validate_gold.py \
  --input labelstudio/artifacts/article_labels.csv \
  --reviewed-output labelstudio/artifacts/gold_reviewed.csv \
  --training-output labelstudio/artifacts/gold_training.csv \
  --rejected-output labelstudio/artifacts/gold_rejected.csv \
  --fail-on-reject
```

`gold_training.csv` contiene solo `record_id`, `title`, `body`, `label` y
`dataset_split`. El primer modelo aprende de `title + body`; claims,
evidencia, PMIDs y similitudes se conservan fuera como auditoría.
