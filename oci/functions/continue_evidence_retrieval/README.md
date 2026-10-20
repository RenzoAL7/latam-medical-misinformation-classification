# `continue-evidence-retrieval`

Esta segunda Function reutiliza la misma imagen x86 de
`retrieve-pubmed-evidence`, pero se configura con
`PIPELINE_MODE=retrieval_continuation`. Atiende únicamente los objetos de cola
inmutables que deja Evidence cuando un `claims.jsonl` contiene más claims de
los que es seguro recuperar dentro de los 300 segundos de OCI Functions.

## Entrada y salida

La regla Events debe entregar solo objetos de este patrón:

```text
silver/evidence-retrieval-jobs/run_id=<parent>/job-0001.job.json
```

El job conserva las identidades `record_id::claim_id`, la referencia al
`claims.jsonl` inmutable y al Evidence padre; no duplica artículos ni cuerpos.
Cada invocación vuelve a leer esa fuente, recupera como máximo
`RETRIEVAL_CONTINUATION_MAX_CLAIMS=8` claims, y escribe una etapa auditable bajo
`silver/evidence-retrieval-stage/`. Si quedan claims, crea exactamente un
`job-0002.job.json`; al terminar, fusiona las etapas con el inventario completo
del Evidence padre y publica un Evidence final estándar.

Los reintentos se limitan a tres para errores transitorios de PubMed, red,
Embed 4 o presupuesto. Los errores de traducción no vuelven a ejecutar PubMed:
al finalizar la recuperación se dejan para `continue-evidence-translation`.

## Configuración OCI

Clona la configuración no secreta y los permisos de
`retrieve-pubmed-evidence`, conserva los secretos de Gemini y aplica:

```text
PIPELINE_MODE=retrieval_continuation
PIPELINE_SCHEMA=v1
SILVER_EVIDENCE_PREFIX=silver/evidence
SILVER_RETRIEVAL_JOB_PREFIX=silver/evidence-retrieval-jobs
SILVER_RETRIEVAL_STAGE_PREFIX=silver/evidence-retrieval-stage
RETRIEVAL_CONTINUATION_MAX_CLAIMS=8
RETRIEVAL_CONTINUATION_MAX_ATTEMPTS=3
RETRIEVAL_CONTINUATION_MAX_SECONDS=285
RETRIEVAL_CONTINUATION_TIME_BUFFER=10
```

La regla Events debe filtrar simultáneamente por el prefijo
`silver/evidence-retrieval-jobs/` y el sufijo `.job.json`. No debe escuchar el
prefijo de staging ni `silver/evidence/`; así cada tramo se encadena una sola
vez y no se crean bucles.
