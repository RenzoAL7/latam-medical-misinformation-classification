# `continue-evidence-translation`

Esta es una segunda OCI Function que usa **la misma imagen x86** de
`retrieve-pubmed-evidence`. El repositorio tiene ahora una entrada explícita en
`func.py`, pero esa entrada delega al único código fuente compartido de
`retrieve-pubmed-evidence` y fija `PIPELINE_MODE=translation_continuation`. Así las seis
Functions desplegadas quedan representadas en el repo sin mantener dos copias
de la lógica de PubMed y traducción que puedan desalinearse.

`func.yaml` y `requirements.txt` documentan la identidad y las dependencias de
esta Function. El despliegue productivo sigue reutilizando la imagen construida
desde `retrieve_pubmed_evidence`, tal como está configurado en OCI; no se debe
crear una segunda imagen con una copia de `retrieve_pubmed_evidence/func.py`.

## Entrada y salida

La regla Events le entrega únicamente objetos de job inmutables:

```text
silver/evidence-translation-jobs/run_id=<parent>/job-0001.job.json
```

El job conserva los claims que ya pasaron Claims, PubMed y Embed 4, con sus
`candidates`, abstracts originales, el Evidence padre y los objetos de staging
ya procesados. La Function traduce solamente los abstracts que aún no tienen
`abstract_es`, procesa como máximo cuatro claims por invocación y escribe:

- una etapa auditable en `silver/evidence-translation-stage/`;
- el siguiente `*.job.json` si quedan claims o reintentos transitorios;
- al final, `silver/evidence/run_id=<parent>_translation_final/evidence.jsonl`
  y su manifest estándar.

No usa NewsData, Body, Claims, PubMed ni OCI Embed 4 durante la continuación.
Reintenta solo errores transitorios de Gemini hasta tres veces y conserva un
claim como `ERROR` si no se recupera. El Evidence final mantiene la misma
identidad `record_id::claim_id`, por lo que el sincronizador de Label Studio lo
puede consumir sin una ruta especial.

## Configuración de la Function OCI

Clona la configuración no secreta de `retrieve-pubmed-evidence`, conserva el
acceso a los mismos secretos de traducción y aplica:

```text
PIPELINE_MODE=translation_continuation
PIPELINE_SCHEMA=v1
SILVER_EVIDENCE_PREFIX=silver/evidence
SILVER_TRANSLATION_JOB_PREFIX=silver/evidence-translation-jobs
SILVER_TRANSLATION_STAGE_PREFIX=silver/evidence-translation-stage
TRANSLATION_CONTINUATION_MAX_CLAIMS=4
TRANSLATION_CONTINUATION_MAX_ATTEMPTS=3
TRANSLATION_CONTINUATION_MAX_SECONDS=285
TRANSLATION_CONTINUATION_TIME_BUFFER=10
TRANSLATION_CONTINUATION_PARALLELISM=1
```

La regla Events debe filtrar por el prefijo
`silver/evidence-translation-jobs/` y el sufijo `.job.json`. No debe escuchar
el prefijo de staging ni `silver/evidence/` para no crear un bucle de
invocaciones. Los jobs y etapas conservan el registro del claim y sus
candidatos recibidos.

## Fuente compartida y despliegue

`func.py` carga el módulo hermano
`../retrieve_pubmed_evidence/func.py` únicamente para exponer la misma entrada
de Function dentro de este directorio. La variable `PIPELINE_MODE` se fuerza a
`translation_continuation` durante cada invocación y se restaura al terminar.

Para actualizar OCI, primero se construye la imagen desde
`oci/functions/retrieve_pubmed_evidence/` y después se actualiza la Function
`continue-evidence-translation` para que use esa misma imagen y sus variables
de entorno de traducción. Este directorio sirve como mapa de código y contrato
de la sexta Function; no se duplica el código de retrieval.
