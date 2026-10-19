# Revisión humana multi-claim con Label Studio

Label Studio decide el veredicto humano de cada claim. `evidence.v1` recupera
y ordena literatura, pero Embed 4 y TF-IDF no prueban que una noticia sea
verdadera o falsa. La unidad de revisión es un claim; la unidad del dataset de
entrenamiento es una noticia.

```text
silver/evidence/evidence_<run_id>.jsonl -> Label Studio (una tarea por record_id + claim_id)
       |                 |
       |                 v
       +-----> Evidence JSONL + export manual JSON/JSONL + Body CSV de la misma corrida
                         |
                         v
           scripts/04_build_training_dataset.py
                         |
                         +-> claim_reviews.csv       (auditoría por claim)
                         +-> article_labels.csv      (una fila por noticia)
                         +-> training.csv            (binario: título + body)
```

## Proyecto e importación manual

Usa [`config_multiclaim.xml`](config_multiclaim.xml) en el proyecto
**MedNews LATAM — Multi-Claim**. Cada tarea requiere una decisión humana de
materialidad y una justificación. Si el claim es central/material, también
requiere relación, veredicto y PMID(s) cuando hay candidatos utilizables. Si es
secundario, se conserva en `claim_reviews.csv`, pero no necesita veredicto ni
PMID y no afecta la etiqueta del artículo.

- `claim_materiality`: `central`, `material` o `secondary`;
- para un claim central/material con evidencia `OK`: `evidence_relation`,
  `claim_verdict`, PMID(s) y justificación humana;
- para un claim central/material con `NO_RESULTS` o `NO_ABSTRACT`: solo se
  permite `unclear` + `NO_DETERMINABLE`, con justificación y sin PMID;
- los fallos técnicos transitorios no se convierten en veredictos.

La importación se realiza manualmente desde el JSONL de Evidence descargado de
OCI. El importador acepta claims con `llm_status=OK` y evidencia completa
(`evidence_status=OK`, embeddings/traducciones `OK`) o resultados terminales de
PubMed (`NO_RESULTS`/`NO_ABSTRACT`) con sus etapas marcadas
`SKIPPED_NO_EVIDENCE`. Excluye fallos técnicos transitorios y deduplica por
`record_id::claim_id`.

La etapa de traducción consume exactamente
`silver/translate/translate_<run_id>.jsonl` y publica el Evidence final. Si una
traducción falla, el claim permanece en `silver/rejected/` y no se importa como
evidencia lista. El importador deduplica por `record_id::claim_id`, conserva
cualquier anotación humana existente y no crea tareas por reintentos.

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

Una tarea pendiente, un claim central/material con un fallo técnico de Evidence,
un PMID/justificación faltante, una relación incoherente o un Body ausente deja
la noticia fuera de `training.csv`; el script termina con código 2 y deja el
motivo en `rejected.csv`. Un claim secundario revisado no bloquea la noticia por
carecer de PMID. Un claim central/material sin resultados PubMed solo puede
quedar como `NO_DETERMINABLE`, nunca `RESPALDADO` o `REFUTADO`. Esto evita
entrenar con Gold parcial. El Evidence JSONL es obligatorio: registra tanto
los resultados terminales de PubMed, que necesitan triage humano, como los
fallos técnicos que pueden bloquear la noticia. Por sí solo no sirve para
crear el CSV porque no contiene la anotación humana ni repite el body para
cada claim.

Si se descargan la salida parcial y su salida final de continuación en la misma
carpeta, `scripts/04_build_training_dataset.py` consolida revisiones por
`record_id::claim_id`: prefiere la revisión más nueva que sí sea revisable y
registra el número de revisiones sustituidas en `summary.json`. Así el
inventario sigue teniendo una sola decisión por claim.

La agregación es determinista una vez que todas las tareas de la noticia están
completas y válidas:

- algún claim `central` o `material` `REFUTADO` -> `REFUTADA`;
- todos los claims `central` o `material` `RESPALDADOS` -> `RESPALDADA`;
- algún claim central/material `NO_DETERMINABLE`, o sin evidencia terminal ->
  `NO_DETERMINABLE`;
- los claims `secondary` se conservan en la auditoría y no alteran la regla de
  etiqueta.

Claims, PMIDs y similitudes se guardan en los reportes de auditoría. El modelo
base usa únicamente `title + body` como entrada y `label` como objetivo.

## Gold en OCI y descarga manual

Cuando termines la revisión humana, usa **Export > JSON** o **JSONL** en Label
Studio y sube manualmente el archivo crudo a OCI Object Storage:

```text
silver/annotations/labelstudio/labelstudio_<utc_timestamp>.json
```

El Function `build-labelstudio-training` corre diariamente a las `14:00`
(`America/Lima`) y toma el export más reciente para escribir un único CSV por
día:

```text
gold/training/training_<YYYYMMDD>.csv
silver/rejected/training/training_<YYYYMMDD>.jsonl
```

Si se reintenta el mismo día, el CSV diario se reemplaza; no se generan
fragmentos ni archivos `summary.json` en Gold.

Después descarga manualmente `gold/training/training_<YYYYMMDD>.csv` desde OCI
para usarlo en el notebook local. No se requieren servicios `systemd`, timers
ni tokens de Label Studio en la VM para este flujo.
