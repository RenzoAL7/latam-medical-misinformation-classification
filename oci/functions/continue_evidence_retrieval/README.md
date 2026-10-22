# `continue-evidence-retrieval`

Este directorio conserva un mapa histórico del modo de continuación de
`retrieve-pubmed-evidence`. La producción usa la Function existente y las
reglas Events la invocan directamente para los jobs de recuperación; no se crea
ni se despliega una segunda OCI Function.

## Entrada y salida

La regla Events entrega a `retrieve-pubmed-evidence` solo objetos de este patrón:

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
al finalizar la recuperación se dejan para la continuación de traducción de
`retrieve-pubmed-evidence`.

## Configuración OCI

La Function desplegada conserva su configuración no secreta, sus permisos y los
secretos de Gemini. El modo se decide por la ruta del objeto; no se debe fijar
`PIPELINE_MODE=retrieval_continuation` para la producción actual.

```text
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
`silver/evidence-retrieval-jobs/` y el sufijo `.job.json`, y apuntar a
`retrieve-pubmed-evidence`. No debe escuchar el prefijo de staging ni
`silver/evidence/`; así cada tramo se encadena una sola vez y no se crean
bucles.
