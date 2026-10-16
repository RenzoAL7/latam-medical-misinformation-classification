# `continue-evidence-translation`

Esta es una segunda OCI Function que usa **la misma imagen x86** de
`retrieve-pubmed-evidence`. No tiene una copia de `func.py`: se configura con
`PIPELINE_MODE=translation_continuation` y por eso ejecuta el handler de
continuación contenido en esa imagen. Mantener un único código evita que las
reglas de reintento y el contrato `evidence.v1` se desalineen.

## Entrada y salida

La regla Events le entrega únicamente objetos:

```text
silver/evidence-translation-jobs/run_id=<parent>/job-0001.job.json
```

El job contiene claims que ya pasaron Claims, PubMed y Embed 4, con sus
`candidates` y abstracts originales. La Function traduce solamente los
abstracts que aún no tienen `abstract_es`, hasta cuatro claims por invocación,
y escribe uno de estos resultados:

- otro `job-0002.job.json` si quedan claims o intentos pendientes;
- un `silver/evidence/run_id=<parent>_translation_final/evidence.jsonl` y su
  `manifest.json` si finalizó;
- un registro de staging para auditoría bajo
  `silver/evidence-translation-stage/`.

No usa NewsData, Body, Claims, PubMed ni OCI Embed 4. Reintenta solo errores
temporales de Gemini hasta tres veces y conserva un claim como `ERROR` si no
se recupera.

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

La regla Events debe filtrar por el prefijo de jobs y el sufijo `.job.json`.
No debe escuchar `silver/evidence/` ni el prefijo de staging para no crear un
bucle de invocaciones.
