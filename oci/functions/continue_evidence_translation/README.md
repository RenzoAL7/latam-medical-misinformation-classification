# `continue-evidence-translation`

Esta es una segunda OCI Function que usa **la misma imagen x86** de
`retrieve-pubmed-evidence`. No tiene una copia de `func.py`: se configura con
`PIPELINE_MODE=translation` y ejecuta únicamente la etapa de traducción
contenida en esa imagen. Mantener un único código evita que el contrato
`evidence.v1` se desalineen.

## Entrada y salida

La regla Events le entrega únicamente objetos:

```text
silver/translate/translate_<run_id>.jsonl
```

El JSONL contiene claims que ya pasaron Claims, PubMed y Embed 4, con sus
`candidates` y abstracts originales. La Function traduce solamente los
abstracts que aún no tienen `abstract_es` y escribe:

- `silver/evidence/evidence_<run_id>.jsonl` y su manifest;
- errores en `silver/rejected/translate/` y `silver/rejected/evidence/`.

No se crean jobs, staging ni archivos `part-*`.

No usa NewsData, Body, Claims, PubMed ni OCI Embed 4. Reintenta solo errores
temporales de Gemini hasta tres veces y conserva un claim como `ERROR` si no
se recupera.

## Configuración de la Function OCI

Clona la configuración no secreta de `retrieve-pubmed-evidence`, conserva el
acceso a los mismos secretos de traducción y aplica:

```text
PIPELINE_MODE=translation
PIPELINE_SCHEMA=v1
SILVER_TRANSLATE_PREFIX=silver/translate
SILVER_EVIDENCE_PREFIX=silver/evidence
SILVER_REJECTED_PREFIX=silver/rejected
TRANSLATION_MAX_SECONDS=285
TRANSLATION_TIME_BUFFER=10
TRANSLATION_PARALLELISM=2
```

La regla Events debe filtrar por el prefijo `silver/translate/` y el patrón
`translate_*.jsonl`. No debe escuchar `silver/evidence/` ni `silver/rejected/`
para no crear un bucle de invocaciones. Los errores conservan el registro del
claim, su `body` y los candidatos recibidos.
