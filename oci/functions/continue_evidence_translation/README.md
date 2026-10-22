# `continue-evidence-translation`

Este directorio conserva un mapa histórico del modo de continuación de
`retrieve-pubmed-evidence`. La producción usa la Function existente y las
reglas Events la invocan directamente para los jobs de traducción; no se crea
ni se despliega una segunda OCI Function.

`func.yaml` y `requirements.txt` se conservan como referencia de compatibilidad.
El despliegue productivo reutiliza la imagen construida desde
`retrieve_pubmed_evidence`, sin crear una segunda imagen ni una Function
separada.

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

La entrada desplegada está en
`../retrieve_pubmed_evidence/func.py`, donde la ruta del job selecciona
`translation_continuation` sin modificar `PIPELINE_MODE`.

Para actualizar OCI, se construye la imagen desde
`oci/functions/retrieve_pubmed_evidence/` y se actualiza únicamente la Function
`retrieve-pubmed-evidence`; la regla de jobs de traducción apunta a esa misma
Function.
