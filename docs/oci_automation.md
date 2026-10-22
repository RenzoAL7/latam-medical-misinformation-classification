# Automatización OCI del pipeline v1

Este documento describe la activación coordinada del pipeline multi-claim. Los
OCID concretos pertenecen a la configuración de OCI, no al repositorio.

## Activación segura para una corrida v1

El despliegue de producción actual activa los schedules de NewsData de las
08:00 y 20:00 Lima, las cinco reglas Events y el timer del proyecto multi-claim
de Label Studio. Cada schedule crea su propio Bronze y cada Function procesa
exclusivamente el objeto que disparó el evento. Los tres schedules históricos
de drenaje permanecen inactivos: un drain usa un payload global y podría volver
a recorrer objetos históricos. No borres ni reproceses CSV históricos como
parte de la activación.

## Cadena de objetos

```text
fetch-newsdata
  -> bronze/newsdata_<run_id>.csv
  -> extract-news-body
  -> silver/body/body_<run_id>.csv
  -> extract-claims (claims.v1)
  -> silver/claims/run_id=<run_id>/claims.jsonl + manifest.json
  -> retrieve-pubmed-evidence (evidence.v1)
  -> silver/evidence/run_id=<run_id>/evidence.jsonl + manifest.json (inventario completo)
  -> [claims pendientes] silver/evidence-retrieval-jobs/*.job.json
  -> retrieve-pubmed-evidence (misma Function, modo de continuación)
  -> silver/evidence/run_id=<parent>_retrieval_final/evidence.jsonl + manifest.json
  -> [solo traducción pendiente] silver/evidence-translation-jobs/*.job.json
  -> retrieve-pubmed-evidence (misma Function, modo de continuación)
  -> silver/evidence/run_id=<parent>_translation_final/evidence.jsonl + manifest.json
  -> revisión humana por claim en Label Studio
  -> una fila de Gold por noticia
```

Bronze y Body continúan siendo CSV. Claims y Evidence no usan un CSV plano
porque una noticia puede tener muchos claims y cada claim muchos candidatos.

## Reglas Events

| Evento Object Storage | Selector | Function destino |
| --- | --- | --- |
| `CreateObject` | `bronze/*.csv` | `extract-news-body` |
| `CreateObject` | `silver/body/*.csv` | `extract-claims` |
| `CreateObject` | `silver/claims/run_id=*/claims.jsonl` | `retrieve-pubmed-evidence` |
| `CreateObject` | `silver/evidence-retrieval-jobs/*.job.json` | `retrieve-pubmed-evidence` |
| `CreateObject` | `silver/evidence-translation-jobs/*.job.json` | `retrieve-pubmed-evidence` |

La tercera regla no debe seguir coincidiendo con `silver/claims/*.csv` después
de activar v1. La Function de Evidence rechaza un evento que no identifica
exactamente un `claims.jsonl`; por ello un selector amplio de prefijo causaría
errores al crear también `manifest.json`.

La cuarta regla debe limitarse al prefijo
`silver/evidence-retrieval-jobs/` y al sufijo `.job.json`. Invoca la Function
`retrieve-pubmed-evidence` existente, que reconoce la ruta y entra en modo de
continuación. Lee el Claims JSONL inmutable, procesa un tramo serial y escribe
el siguiente job solo cuando el actual ya quedó auditado. No debe escuchar el
prefijo de staging ni `silver/evidence/`.

La quinta regla debe limitarse al prefijo
`silver/evidence-translation-jobs/` y al sufijo `.job.json`. Invoca la misma
Function `retrieve-pubmed-evidence`, que reconoce la ruta y entra en modo de
continuación de traducción. No debe apuntar al prefijo de staging ni a
`silver/evidence/`, porque esos objetos no son entrada de recuperación.

Cada Function recibe el objeto que disparó el evento. `extract-claims` y
`retrieve-pubmed-evidence` v1 no aceptan `{}` como drenaje global: procesan
todo el objeto fuente y materializan los estados y errores en su propio JSONL.

## Configuración v1 necesaria

En `extract-claims`:

```text
PIPELINE_SCHEMA=v1
LLM_MAX_ROWS=0
MAX_CLAIMS_PER_ARTICLE=0
LLM_REQUEST_BATCH_SIZE=5
LLM_MAX_SECONDS=270
LLM_TIME_BUFFER=20
```

En `retrieve-pubmed-evidence`:

```text
PIPELINE_SCHEMA=v1
EVIDENCE_MAX_ROWS=0
PUBMED_CANDIDATES_FETCHED=5
EVIDENCE_MAX_SECONDS=285
EVIDENCE_TIME_BUFFER=10
TRANSLATION_PARALLELISM=2
TRANSLATION_CONTINUATION_ENABLED=true
SILVER_TRANSLATION_JOB_PREFIX=silver/evidence-translation-jobs
SILVER_TRANSLATION_STAGE_PREFIX=silver/evidence-translation-stage
TRANSLATION_CONTINUATION_MAX_CLAIMS=4
TRANSLATION_CONTINUATION_MAX_ATTEMPTS=3
TRANSLATION_CONTINUATION_MAX_SECONDS=285
TRANSLATION_CONTINUATION_TIME_BUFFER=10
TRANSLATION_CONTINUATION_PARALLELISM=1
RETRIEVAL_CONTINUATION_ENABLED=true
SILVER_RETRIEVAL_JOB_PREFIX=silver/evidence-retrieval-jobs
SILVER_RETRIEVAL_STAGE_PREFIX=silver/evidence-retrieval-stage
RETRIEVAL_CONTINUATION_MAX_CLAIMS=8
RETRIEVAL_CONTINUATION_MAX_ATTEMPTS=3
RETRIEVAL_CONTINUATION_MAX_SECONDS=285
RETRIEVAL_CONTINUATION_TIME_BUFFER=10
```

`MAX_CLAIMS_PER_ARTICLE=0` y `EVIDENCE_MAX_ROWS=0` son reglas de integridad:
no se descarta un claim por ser el sexto o por superar un lote arbitrario. Si
el objeto excede un tramo seguro, Recovery lo deja explícito en el Evidence
padre y continúa por jobs seriales de ocho claims. Cada claim recupera como
máximo cinco PMIDs y se embeben todos los candidatos
con abstract. El embedding del query siempre es por claim; un mismo
PMID+abstract reaprovecha su embedding y traducción dentro de la misma
invocación. Con las dos claves Gemini independientes configuradas, la
traducción puede atender dos filas de claim en paralelo; no cambia el número
de abstracts traducidos ni el límite de cinco PMIDs por claim.

## Presupuesto y errores

OCI Functions tiene un timeout de 300 segundos. Claims se detiene a 270 s con
20 s de margen y cada tramo de Evidence a 285 s con 10 s de margen. Ante un
objeto grande, el Evidence padre conserva todos los claims y deja un job para
los no iniciados; la continuación de `retrieve-pubmed-evidence` los procesa
serialmente hasta publicar la consolidación. Un timeout o error transitorio dentro de un tramo se
reintenta hasta tres veces; una falla permanente queda como `ERROR` y el
manifest como `PARTIAL` o `FAILED`. Para `RECITATION`, Evidence puede usar en
la misma invocación un fallback acotado de resumen fiel. Si tras ello solo queda
traducción transitoria, deja un job que no consulta nuevamente PubMed ni Embed
4.

`evidence_status=OK` solo se conserva si `llm_status`, `embedding_status` y
`translation_status` son `OK`. `NO_RESULTS` y `NO_ABSTRACT` también son
resultados explícitos: cuando `llm_status=OK` y embedding/traducción son
`SKIPPED_NO_EVIDENCE`, llegan a Label Studio para triage humano sin presentarse
como evidencia que apoya o contradice el claim. Los fallos técnicos
transitorios no se importan.

## Permisos

La identidad de Resource Scheduler requiere `read fn-app`, `read fn-function`
y `use fn-invocation` sobre la Function correspondiente. Las reglas Events
requieren su propio `use fn-invocation`. La identidad de las Functions debe
leer los secret bundles de NewsData, claims y traducción de abstracts; las
claves se cargan en tiempo de ejecución y nunca se incorporan a la imagen.

La VM privada de Label Studio usa Instance Principal para listar/leer Objects,
y un JWT refresh token local protegido para renovar su access token. El
sincronizador v1 consume manifests `evidence.manifest.v1` `COMPLETE` o
`PARTIAL`, pero el importador conserva la compuerta por claim: los cuatro
estados deben estar en `OK`, o el claim debe ser un resultado terminal
`NO_RESULTS`/`NO_ABSTRACT` con LLM en `OK` y embedding/traducción en
`SKIPPED_NO_EVIDENCE`.

La llegada de Evidence a Label Studio es automática; la salida humana es un
export explícito en `silver/annotations/labelstudio/`. El Function
`build-labelstudio-training` toma ese export y los objetos Evidence/Body que
referencia para construir `gold/training/training_<YYYYMMDD>.csv`. Un error o
una revisión incompleta se publica como rechazo y nunca se infiere una etiqueta
binaria desde similitud o desde el JSONL técnico.

## Imágenes y activación segura

La forma de OCI Functions es `GENERIC_X86`; desde Apple Silicon la imagen debe
construirse para `linux/amd64`, por ejemplo con:

```bash
DOCKER_DEFAULT_PLATFORM=linux/amd64 fn deploy --no-bump
```

El orden de activación es:

1. Ejecutar las pruebas locales y construir la imagen x86.
2. Actualizar las Functions existentes y crear solo las reglas Events de jobs,
   ambas apuntando a `retrieve-pubmed-evidence`; no se crean Functions nuevas.
3. Mantener el selector Claims → Evidence al sufijo `claims.jsonl` y añadir
   una regla Jobs → continuación por cada prefijo de jobs y sufijo `.job.json`.
4. Crear un proyecto nuevo de Label Studio con `config_multiclaim.xml` y
   configurar su sincronizador en formato `jsonl`.
5. Hacer una corrida controlada nueva, inspeccionar ambos manifests y revisar
   tareas humanas de ejemplo.
6. Decidir explícitamente si se reactivan los schedules.

Nunca borres ni mezcles los CSV históricos como parte de esta activación. Su
ordenamiento, eliminación o enriquecimiento se decide después de evaluar la
corrida controlada.

## Backfill histórico controlado (solo Claims y Evidence)

Cuando se decida recuperar noticias históricas, no se vuelve a ejecutar
NewsData ni `extract-news-body`. Primero se exporta el listado exacto de
`record_id` del proyecto histórico de Label Studio y se cruza con los Body CSV
ya almacenados. La campaña debe registrar los IDs solicitados, Bodies válidos,
faltantes y excepciones antes de crear un único objeto Body de entrada. El
evento normal ejecuta **solo** Claims, después Evidence y finalmente el
sincronizador de Multi-Claim. Se revisan ambos manifests antes del lote
siguiente; una fila sin Body válido queda como excepción, nunca se vuelve a
rastrear automáticamente. Los drains con payload global siguen inactivos.
