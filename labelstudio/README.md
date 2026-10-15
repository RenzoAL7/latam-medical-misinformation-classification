# Revisión humana multi-claim con Label Studio

Label Studio es la decisión humana del pipeline. `evidence.v1` recupera y
ordena literatura; no prueba que una noticia sea verdadera o falsa. La unidad
de revisión es un claim, pero la unidad del dataset binario sigue siendo una
noticia.

```text
evidence.jsonl (una línea por claim utilizable)
        |
        v
Label Studio (una tarea por record_id + claim_id)
        |
        v
claim_reviews.csv
        |
        v
aggregate_article_labels.py (una noticia, una etiqueta)
        |
        v
validate_gold.py -> gold/reviewed + gold/training
```

Para una corrida v1 validada, el sincronizador automático puede permanecer
activo solo para el proyecto multi-claim y el formato `jsonl`. No importes los
CSV históricos en ese proyecto ni cambies la configuración de un proyecto que
ya tenga anotaciones con el XML antiguo.

## 1. Crear un proyecto limpio

Crea un proyecto nuevo en Label Studio y pega el contenido de
[`config_multiclaim.xml`](config_multiclaim.xml). Esta interfaz pide, por cada
claim:

- `claim_materiality`: `central`, `material` o `secondary`;
- `evidence_relation`: `supports`, `contradicts` o `unclear`;
- `claim_verdict`: `RESPALDADO`, `REFUTADO` o `NO_DETERMINABLE`;
- PMID(s) usados y una justificación humana.

No contiene `final_label`: la etiqueta de noticia se calcula después de que
todos sus claims centrales o materiales fueron revisados.

## 2. Importar Evidence JSONL

Solo llegan a revisión los registros con `llm_status=OK`,
`evidence_status=OK`, `embedding_status=OK` y `translation_status=OK`. El
importador deduplica por `claim_key` (`record_id::claim_id`) y no duplica tareas
cuando una noticia tiene muchos claims.

```bash
export LABEL_STUDIO_TOKEN='token-local-de-label-studio'
python3 labelstudio/import_evidence.py \
  --project-id <proyecto-nuevo> \
  --input /ruta/a/evidence.jsonl \
  --manifest labelstudio/artifacts/import_manifest.csv
```

Para actualizar texto o candidatos de tareas existentes sin borrar sus
anotaciones, añade `--refresh-existing`. Guarda tokens, manifests y exportes
en `labelstudio/artifacts/`, que está ignorado por Git.

## 3. Revisar un claim

Lee el claim y los abstracts PubMed. Embed 4 y TF-IDF solo ordenan los
candidatos: no son scores de verdad. Marca `central` cuando el claim sostiene
la conclusión de la noticia, `material` cuando cambia de forma importante su
interpretación y `secondary` cuando solo aporta contexto. El PMID y la nota
deben permitir que otra persona audite la decisión.

## 4. Exportar y agregar una etiqueta por noticia

En Label Studio usa **Export > JSON**. Luego ejecuta:

```bash
python3 labelstudio/export_annotations.py \
  --mode multiclaim \
  --input-json labelstudio/artifacts/labelstudio_export.json \
  --output labelstudio/artifacts/claim_reviews.csv

python3 labelstudio/aggregate_article_labels.py \
  --claims-input labelstudio/artifacts/claim_reviews.csv \
  --body-input /ruta/a/silver/body/ \
  --output labelstudio/artifacts/article_labels.csv
```

La agregación es determinista:

- algún claim `central` o `material` `REFUTADO` → `REFUTADA`;
- todos los claims `central` o `material` `RESPALDADOS` → `RESPALDADA`;
- cualquier caso restante → `NO_DETERMINABLE`.

Una noticia nunca se duplica por tener varios claims. El cuerpo se recupera
desde el CSV de Body porque no se incluye en la tarea de Label Studio para
mantener la interfaz ligera.

## 5. Validar Gold y preparar entrenamiento

```bash
python3 labelstudio/validate_gold.py \
  --input labelstudio/artifacts/article_labels.csv \
  --reviewed-output labelstudio/artifacts/gold_reviewed.csv \
  --training-output labelstudio/artifacts/gold_training.csv \
  --rejected-output labelstudio/artifacts/gold_rejected.csv \
  --fail-on-reject
```

`gold/reviewed` conserva la auditoría de la agregación. `gold/training` solo
incluye `RESPALDADA` y `REFUTADA`, una fila por `record_id`, con `title + body`
como entrada del modelo de clasificación de noticias. Claims, PMIDs y
similitudes quedan como trazabilidad, no como variables del primer modelo.

## Sincronización desde Object Storage

En una VM privada, `sync_evidence_from_object_storage.py --format jsonl`
acepta únicamente manifests `evidence.manifest.v1` con estado `COMPLETE`,
descarga el `evidence.jsonl` referido y aplica `config_multiclaim.xml` antes de
importar. Usa un JWT refresh token de Label Studio en un archivo protegido y
el Instance Principal de la VM para Object Storage; nunca guardes access tokens
ni secretos en Git.

El unit de systemd describe este modo. Una vez habilitado tras la validación,
el timer sincroniza los manifests `COMPLETE` cada cinco minutos; no sustituye
las reglas Events ni justifica activar los drains históricos.
