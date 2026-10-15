# Automatización OCI del pipeline v1

Este documento describe la activación coordinada del pipeline multi-claim. Los
OCID concretos pertenecen a la configuración de OCI, no al repositorio.

## Estado operativo actual

Mientras se termina la migración, están pausados los dos schedules de NewsData,
los tres schedules históricos de drenaje y el timer de Label Studio. No se
deben borrar CSV ni reactivar esos recursos durante esta etapa. Las reglas
Events se conservan para no perder su configuración, pero la regla Claims →
Evidence debe cambiar junto con el despliegue de las imágenes v1.

## Cadena de objetos

```text
fetch-newsdata
  -> bronze/newsdata_<run_id>.csv
  -> extract-news-body
  -> silver/body/body_<run_id>.csv
  -> extract-claims (claims.v1)
  -> silver/claims/run_id=<run_id>/claims.jsonl + manifest.json
  -> retrieve-pubmed-evidence (evidence.v1)
  -> silver/evidence/run_id=<run_id>/evidence.jsonl + manifest.json
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

La tercera regla no debe seguir coincidiendo con `silver/claims/*.csv` después
de activar v1. La Function de Evidence rechaza un evento que no identifica
exactamente un `claims.jsonl`; por ello un selector amplio de prefijo causaría
errores al crear también `manifest.json`.

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
TRANSLATION_PARALLELISM=1
```

`MAX_CLAIMS_PER_ARTICLE=0` y `EVIDENCE_MAX_ROWS=0` son reglas de integridad:
no se descarta un claim por ser el sexto o por superar un lote arbitrario.
Cada claim recupera como máximo cinco PMIDs y se embeben todos los candidatos
con abstract. El embedding del query siempre es por claim; un mismo
PMID+abstract reaprovecha su embedding y traducción dentro de la misma
invocación.

## Presupuesto y errores

OCI Functions tiene un timeout de 300 segundos. Claims se detiene a 270 s con
20 s de margen y Evidence a 285 s con 10 s de margen. El límite se aplica a
la corrida, no a un claim: si una llamada falla o se acaba el tiempo, el claim
queda en `ERROR` dentro de `claims.jsonl` o `evidence.jsonl` y el manifest pasa
a `PARTIAL` o `FAILED`. No hay reintento automático, backlog ni modificación
de objetos históricos.

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

La VM privada de Label Studio usa Instance Principal para listar/leer Objects,
y un JWT refresh token local protegido para renovar su access token. El
sincronizador v1 solo consume manifests `evidence.manifest.v1` en estado
`COMPLETE`.

## Imágenes y activación segura

La forma de OCI Functions es `GENERIC_X86`; desde Apple Silicon la imagen debe
construirse para `linux/amd64`, por ejemplo con:

```bash
DOCKER_DEFAULT_PLATFORM=linux/amd64 fn deploy --local
```

El orden de activación es:

1. Ejecutar las pruebas locales y construir ambas imágenes x86.
2. Subir/desplegar las Functions con la configuración v1.
3. Actualizar el selector Claims → Evidence al sufijo `claims.jsonl`.
4. Crear un proyecto nuevo de Label Studio con `config_multiclaim.xml` y
   configurar su sincronizador en formato `jsonl`.
5. Hacer una corrida controlada nueva, inspeccionar ambos manifests y revisar
   tareas humanas de ejemplo.
6. Decidir explícitamente si se reactivan los schedules.

Nunca borres ni mezcles los CSV históricos como parte de esta activación. Su
ordenamiento, eliminación o enriquecimiento se decide después de evaluar la
corrida controlada.
