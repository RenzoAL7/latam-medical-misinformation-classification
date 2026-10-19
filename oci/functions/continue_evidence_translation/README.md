# `continue-evidence-translation`

Esta es una segunda OCI Function que usa **la misma imagen x86** de
`retrieve-pubmed-evidence`. El repositorio tiene ahora una entrada explícita en
`func.py`, pero esa entrada delega al único código fuente compartido de
`retrieve-pubmed-evidence` y fija `PIPELINE_MODE=translation`. Así las seis
Functions desplegadas quedan representadas en el repo sin mantener dos copias
de la lógica de PubMed y traducción que puedan desalinearse.

`func.yaml` y `requirements.txt` documentan la identidad y las dependencias de
esta Function. El despliegue productivo sigue reutilizando la imagen construida
desde `retrieve_pubmed_evidence`, tal como está configurado en OCI; no se debe
crear una segunda imagen con una copia de `retrieve_pubmed_evidence/func.py`.

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

## Fuente compartida y despliegue

`func.py` carga el módulo hermano
`../retrieve_pubmed_evidence/func.py` únicamente para exponer la misma entrada
de Function dentro de este directorio. La variable `PIPELINE_MODE` se fuerza a
`translation` durante cada invocación y se restaura al terminar.

Para actualizar OCI, primero se construye la imagen desde
`oci/functions/retrieve_pubmed_evidence/` y después se actualiza la Function
`continue-evidence-translation` para que use esa misma imagen y sus variables
de entorno de traducción. Este directorio sirve como mapa de código y contrato
de la sexta Function; no se duplica el código de retrieval.
