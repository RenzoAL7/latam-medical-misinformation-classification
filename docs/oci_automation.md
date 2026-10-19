# Automatización OCI del pipeline v1

Este documento describe la activación coordinada del pipeline multi-claim. Los
OCID concretos pertenecen a la configuración de OCI, no al repositorio.

## Activación segura para una corrida v1

La corrida limpia activa únicamente el schedule de NewsData de las 08:00 Lima
y las tres reglas Events. El schedule de las 20:00 y los tres schedules
históricos de drenaje permanecen inactivos: un drain usa un payload global y
podría volver a recorrer objetos históricos, mientras que Claims y Evidence v1
deben procesar el objeto que disparó el evento. La importación a Label Studio y
la carga del export humano se hacen manualmente. No borres ni reproceses CSV
históricos como parte de la activación.

## Cadena de objetos

```text
fetch-newsdata
  -> bronze/newsdata_<run_id>.csv
  -> extract-news-body
  -> silver/body/body_<run_id>.csv
  -> extract-claims (claims.v1)
  -> silver/claims/claims_<run_id>.jsonl + manifest.json
  -> retrieve-pubmed-evidence (retrieval.v1)
  -> silver/translate/translate_<run_id>.jsonl + manifest.json
  -> continue-evidence-translation (mismo image, modo translation)
  -> silver/evidence/evidence_<run_id>.jsonl + manifest.json
  -> revisión humana por claim en Label Studio
  -> export JSON manual en silver/annotations/labelstudio/
  -> Function build-labelstudio-training
  -> una fila de Gold por noticia en gold/training/
```

Bronze y Body continúan siendo CSV. Claims y Evidence no usan un CSV plano
porque una noticia puede tener muchos claims y cada claim muchos candidatos.

## Reglas Events

| Evento Object Storage | Selector | Function destino |
| --- | --- | --- |
| `CreateObject` | `bronze/*.csv` | `extract-news-body` |
| `CreateObject` | `silver/body/*.csv` | `extract-claims` |
| `CreateObject` | `silver/claims/claims_*.jsonl` | `retrieve-pubmed-evidence` |
| `CreateObject` | `silver/translate/translate_*.jsonl` | `continue-evidence-translation` |

La tercera regla no debe seguir coincidiendo con `silver/claims/*.csv` después
de activar v1. La Function de retrieval rechaza un evento que no identifica
exactamente un `claims_<run_id>.jsonl`; por ello un selector amplio de prefijo
causaría errores al crear también el manifest.

La cuarta regla debe limitarse al prefijo `silver/translate/` y al patrón
`translate_*.jsonl`. No debe escuchar `silver/evidence/` ni
`silver/rejected/`, porque esos objetos son salidas. El destino usa la misma
imagen x86 que retrieval pero tiene `PIPELINE_MODE=translation`.

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
SILVER_TRANSLATE_PREFIX=silver/translate
SILVER_REJECTED_PREFIX=silver/rejected
EMBEDDINGS_ENABLED=true
```

En `continue-evidence-translation`:

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

`MAX_CLAIMS_PER_ARTICLE=0` y `EVIDENCE_MAX_ROWS=0` son reglas de integridad:
no se descarta un claim por ser el sexto o por superar un lote arbitrario.
Cada claim recupera como máximo cinco PMIDs y se embeben todos los candidatos
con abstract. El embedding del query siempre es por claim; un mismo
PMID+abstract reaprovecha su embedding y traducción dentro de la misma
invocación. Con las dos claves Gemini independientes configuradas, la
traducción puede atender dos filas de claim en paralelo; no cambia el número
de abstracts traducidos ni el límite de cinco PMIDs por claim.

## Presupuesto y errores

OCI Functions tiene un timeout de 300 segundos. Claims se detiene a 270 s con
20 s de margen y Evidence a 285 s con 10 s de margen. El límite se aplica a
la corrida, no a un claim: si una llamada falla o se acaba el tiempo, el claim
queda en `ERROR` dentro del JSONL de la etapa y el manifest pasa a `PARTIAL` o
`FAILED`. Además se escribe una copia en el subdirectorio correspondiente de
`silver/rejected/`. Retrieval nunca traduce; deja el candidato en
`silver/translate/` para que la segunda Function lo procese sin consultar
PubMed ni Embed 4 nuevamente.

`evidence_status=OK` solo se conserva si `llm_status`, `embedding_status` y
`translation_status` son `OK`. `NO_RESULTS` y `NO_ABSTRACT` también son
resultados explícitos y quedan en la salida para auditoría, aunque no lleguen
a Label Studio.

## Permisos

La identidad de Resource Scheduler requiere `read fn-app`, `read fn-function`
y `use fn-invocation` sobre la Function correspondiente. Las reglas Events
requieren su propio `use fn-invocation`. La identidad de las Functions debe
leer los secret bundles de NewsData, claims y traducción de abstracts; las
claves se cargan en tiempo de ejecución y nunca se incorporan a la imagen.

La VM privada de Label Studio no necesita servicios ni credenciales locales del
repositorio para este flujo manual. La persona revisora trabaja en el proyecto
existente, exporta las anotaciones terminadas y sube ese JSON a
`silver/annotations/labelstudio/`. El builder de Gold conserva la compuerta
por claim: los cuatro estados deben estar en `OK`.

## Imágenes y activación segura

La forma de OCI Functions es `GENERIC_X86`; desde Apple Silicon la imagen debe
construirse para `linux/amd64`, por ejemplo con:

```bash
DOCKER_DEFAULT_PLATFORM=linux/amd64 fn deploy --no-bump
```

El orden de activación es:

1. Ejecutar las pruebas locales y construir la imagen x86.
2. Subir/desplegar retrieval y actualizar `continue-evidence-translation` con
   esa misma imagen y `PIPELINE_MODE=translation`.
3. Mantener el selector Claims → retrieval en `claims_*.jsonl` y el selector
   Translate → Evidence en `translate_*.jsonl`.
4. Crear un proyecto nuevo de Label Studio con `config_multiclaim.xml`.
5. Importar manualmente el Evidence JSONL y revisar las tareas.
6. Exportar las anotaciones terminadas y subir el JSON a
   `silver/annotations/labelstudio/`.
7. Hacer una corrida controlada nueva e inspeccionar ambos manifests y el Gold.

Nunca borres ni mezcles los CSV históricos como parte de esta activación. Su
ordenamiento, eliminación o enriquecimiento se decide después de evaluar la
corrida controlada.

## Reintentos y recuperación

Las salidas con errores se conservan por etapa en `silver/rejected/body/`,
`silver/rejected/claims/`, `silver/rejected/translate/` y
`silver/rejected/evidence/`. La recuperación se hace sobre esos objetos y los
reintentos internos de las Functions, con el `run_id` y los manifests como
control de duplicados. No se usan scripts locales de backfill o retry para
volver a publicar lotes; así una recuperación no vuelve a disparar la cadena
completa por accidente.

Para entrenar localmente después de la revisión humana, se descarga el único
CSV Gold desde `gold/training/` y se abre el notebook del baseline. No se
ensamblan CSV localmente.

## Gold diario y descarga manual

Después de completar la revisión, se exportan las anotaciones desde Label
Studio y se suben manualmente a `silver/annotations/labelstudio/`. No se
mantienen servicios `systemd`, timers ni credenciales locales de Label Studio
en este repositorio.

El horario OCI
`mednews-build-labelstudio-training-14-lima` ejecuta el Function a las
`14:00` (`America/Lima`) con payload vacío. El Function toma el export más
reciente, resuelve únicamente los Evidence y Body referenciados por ese
export, y publica `gold/training/training_<YYYYMMDD>.csv` usando la fecha de
Lima. Si se reintenta el mismo día, reemplaza ese mismo objeto. Las filas pendientes,
inconsistentes o sin Body se conservan en
`silver/rejected/training/`. El export debe existir previamente porque se carga
manualmente a `silver/annotations/labelstudio/`; no hay un servicio local ni
un fallback de token administrado por este repositorio.
