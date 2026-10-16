# Revisión humana multi-claim con Label Studio

Label Studio decide el veredicto humano de cada claim. `evidence.v1` recupera
y ordena literatura, pero Embed 4 y TF-IDF no prueban que una noticia sea
verdadera o falsa. La unidad de revisión es un claim; la unidad del dataset de
entrenamiento es una noticia.

```text
evidence.jsonl -> Label Studio (una tarea por record_id + claim_id)
       |                 |
       |                 v
       +-----> Evidence JSONL + Export JSON/JSONL + Body CSV de la misma corrida
                         |
                         v
           scripts/04_build_training_dataset.py
                         |
                         +-> claim_reviews.csv       (auditoría por claim)
                         +-> article_labels.csv      (una fila por noticia)
                         +-> training.csv            (binario: título + body)
```

## Proyecto y sincronización

Usa [`config_multiclaim.xml`](config_multiclaim.xml) en el proyecto
**MedNews LATAM — Multi-Claim**. Cada tarea requiere:

- `claim_materiality`: `central`, `material` o `secondary`;
- `evidence_relation`: `supports`, `contradicts` o `unclear`;
- `claim_verdict`: `RESPALDADO`, `REFUTADO` o `NO_DETERMINABLE`;
- PMID(s) y justificación humana.

El sincronizador de la VM acepta únicamente manifests `evidence.manifest.v1`
`COMPLETE` o `PARTIAL` y el importador acepta solamente claims con
`llm_status=OK`, `evidence_status=OK`, `embedding_status=OK` y
`translation_status=OK`. Deduplica por `record_id::claim_id`.

Si una corrida Evidence deja traducciones pendientes, puede aparecer primero un
manifest parcial y después un manifest final
`run_id=<parent>_translation_final`. Ambos describen los mismos claims; el
importador deduplica por `record_id::claim_id`, conserva cualquier anotación
humana existente y añade únicamente los claims que la continuación dejó listos.
No crea una tarea nueva por cada intento de traducción.

```bash
export LABEL_STUDIO_TOKEN='token-local-de-label-studio'
python3 labelstudio/import_evidence.py \
  --project-id <proyecto> \
  --input /ruta/a/evidence.jsonl \
  --manifest labelstudio/artifacts/import_manifest.csv
```

## Un único comando para producir el dataset

Cuando hayas terminado de revisar un lote, en Label Studio usa **Export > JSON**
o **JSONL**. Crea una carpeta local ignorada por Git con esta estructura:

```text
labelstudio/artifacts/lote_2026-10-15/
  labelstudio_export.json         # o labelstudio_export.jsonl
  evidence.jsonl                  # todos los claims Evidence de ese lote
  body.csv                         # Body descargado de silver/body/ de esa corrida
```

También se acepta un directorio `body/` con varios CSV Body. Luego ejecuta:

```bash
python3 scripts/04_build_training_dataset.py \
  labelstudio/artifacts/lote_2026-10-15
```

El script escribe `output/claim_reviews.csv`, `output/article_labels.csv`,
`output/training.csv`, `output/rejected.csv` y `output/summary.json`.

`training.csv` contiene solo una fila por noticia y solo las etiquetas
`RESPALDADA` o `REFUTADA`:

```text
record_id,title,body,label,dataset_split
```

Una tarea pendiente, un claim que Evidence no pudo hacer revisable, un
PMID/justificación faltante, una relación incoherente o un Body ausente deja la
noticia fuera de `training.csv`; el script termina con código 2 y deja el
motivo en `rejected.csv`. Esto evita entrenar con Gold parcial. El Evidence
JSONL es obligatorio: permite detectar claims que nunca llegaron a Label
Studio por `NO_RESULTS` o un error. Por sí solo no sirve para crear el CSV
porque no contiene la anotación humana ni repite el body para cada claim.

Si se descargan la salida parcial y su salida final de continuación en la misma
carpeta, `scripts/04_build_training_dataset.py` consolida revisiones por
`record_id::claim_id`: prefiere la revisión más nueva que sí sea revisable y
registra el número de revisiones sustituidas en `summary.json`. Así el
inventario sigue teniendo una sola decisión por claim.

La agregación es determinista una vez que todas las tareas de la noticia están
completas:

- algún claim `central` o `material` `REFUTADO` -> `REFUTADA`;
- todos los claims `central` o `material` `RESPALDADOS` -> `RESPALDADA`;
- cualquier otro caso -> `NO_DETERMINABLE`.

Claims, PMIDs y similitudes se guardan en los reportes de auditoría. El modelo
base usa únicamente `title + body` como entrada y `label` como objetivo.
