# Automatización OCI del pipeline

Este documento describe los recursos que deben existir en el tenancy. Los
OCID concretos se mantienen en la configuración de OCI y no en el repositorio.

La automatización que ya está desplegada usa objetos CSV. El plan objetivo para
varios claims y JSONL está separado en
[`multiclaim_jsonl_plan.md`](multiclaim_jsonl_plan.md); no se deben activar
selectores CSV y JSONL sobre el mismo prefijo durante la transición.

## Frecuencia

Resource Scheduler inicia `fetch-newsdata` dos veces al día, a las 08:00 y
20:00 en `America/Lima`. Resource Scheduler interpreta el cron en UTC, por
eso las expresiones configuradas son `0 13 * * *` y `0 1 * * *`. La Function
solicita hasta 50 filas nuevas por corrida; si la API devuelve menos, escribe
el CSV disponible. El siguiente paso no depende de que el lote llegue
exactamente a 50.

Para controlar el consumo de NewsData, la configuración de producción usa
`NEWSDATA_SIZE=10` y `NEWSDATA_MAX_PAGES=2`. Como hay cuatro grupos de países,
el recolector puede hacer como máximo ocho solicitudes por corrida. Después de
consultar los grupos disponibles, selecciona las filas en round-robin por país
y conserva hasta 50 filas nuevas. Cada solicitud adicional consume un crédito
de NewsData, por lo que no se dejan más de dos páginas por grupo como valor
predeterminado.

Las etapas posteriores se encadenan mediante OCI Events: cada nueva entrada
Bronze activa Body, cada salida de Body activa Claims y cada salida de Claims
activa Evidence. Los tres pasos Silver conservan además un drenaje horario con
payload vacío (`{}`) para recuperar trabajo transitorio y avanzar los lotes que
superan el tope de una invocación.

## Permisos y activación de los schedules

Cada schedule que invoca una Function necesita una identidad propia en un
dynamic group que coincida con el OCID exacto del `resourceschedule`. Esa
identidad debe tener, en el compartimento de las Functions:

```text
read fn-app
read fn-function
use fn-invocation
```

El último permiso puede restringirse a la Function destino. No basta con
otorgar únicamente `use fn-invocation`: Resource Scheduler también resuelve la
aplicación y la Function antes de la invocación. Las reglas de OCI Events usan
su propia identidad y conservan su permiso `use fn-invocation` hacia la etapa
siguiente.

La hora `time-starts` debe ser anterior al siguiente tick cron deseado. Por
ejemplo, para ejecutar `0 13 * * *` a las 08:00 de Lima, no se debe crear el
schedule con inicio exactamente a las 13:00 UTC: OCI puede calcular la primera
recurrencia para el día siguiente. Verificar siempre `time-next-run` después de
crear o actualizar un schedule.

Las Functions ejecutan como `mednews-functions`; esa identidad debe leer cada
secret bundle configurado para NewsData, claims y traducción de abstracts,
incluidas las dos claves de Gemini usadas por Evidence.

## Presupuesto de tiempo

El timeout síncrono máximo de OCI Functions es de 300 segundos. Resource
Scheduler marca como fallida una invocación síncrona que se acerca a tres
minutos aunque la Function siga escribiendo resultados; por ello las cuatro
Functions tienen un presupuesto interno de 150 segundos y un margen de 10
segundos para terminar la escritura del objeto:

```text
NEWSDATA_MAX_SECONDS=150       NEWSDATA_TIME_BUFFER=10
BODY_MAX_SECONDS=150           BODY_TIME_BUFFER=10
LLM_MAX_SECONDS=150            LLM_TIME_BUFFER=10
EVIDENCE_MAX_SECONDS=270
```

Los valores son límites de trabajo, no duraciones garantizadas. El lote real
puede ser menor si las URLs son lentas, si el proveedor agota la cuota o si ya
existe el `record_id`. Cada Function escribe las filas que alcanzó a completar
y los estados transitorios continúan en la siguiente activación aplicable. Para invocación manual usa
`--read-timeout 360`; ese parámetro pertenece al cliente OCI y no amplía el
límite de 300 segundos de la Function.

La tabla completa, incluida la duración de cada solicitud, el consumo y la
matriz de estados, está en
[`limits_and_timing.md`](limits_and_timing.md).

## Encadenamiento por eventos

OCI Events escucha la creación de estos objetos y llama a una Function:

| Evento | Prefijo creado | Function llamada |
| --- | --- | --- |
| Object Storage `CreateObject` | `bronze/*.csv` | `extract-news-body` |
| Object Storage `CreateObject` | `silver/body/*.csv` | `extract-claims` |
| Object Storage `CreateObject` | `silver/claims/*.csv` | `retrieve-pubmed-evidence` |

Cada regla filtra también `bucketName=mednews-data`. No se crea una regla para
`silver/evidence/`, porque la revisión de etiquetas permanece bajo control
humano en Label Studio.

### Encadenamiento objetivo JSONL

Después de la migración, las reglas de las etapas nuevas deberán distinguir el
objeto de corrida y el sufijo JSONL:

```text
silver/body/*.csv
        |
        v
extract-claims -> silver/claims/run_id=<run_id>/claims.jsonl
                              |
                              v
retrieve-pubmed-evidence -> silver/evidence/run_id=<run_id>/evidence.jsonl
```

El evento de Evidence deberá consumir únicamente el `claims.jsonl` que lo
disparó. Un drenaje explícito podrá seleccionar una corrida pendiente, pero el
manifest y el `run_id` deben mantenerse juntos. La etiqueta final seguirá
siendo humana y no habrá un evento automático desde Evidence hacia Gold.

## Orden y tamaño de los lotes

| Capa | Function | Máximo por invocación | Resultado |
| --- | --- | ---: | --- |
| Bronze | `fetch-newsdata` | 50 noticias nuevas | `bronze/newsdata_<run_id>.csv` |
| Silver body | `extract-news-body` | 50 filas del Bronze disparador; drenaje global con payload vacío | `silver/body/body_current_<source>_<hash>.csv` |
| Silver claims | `extract-claims` | 25 filas del Body disparador; drenaje global con payload vacío; solicitudes Gemini de 10 filas | `silver/claims/claims_current_<source>_<hash>.csv` |
| Silver evidence | `retrieve-pubmed-evidence` | 20 claims del Claims disparador; drenaje global con payload vacío; hasta 10 artículos por claim | `silver/evidence/evidence_run_zz_current_<source>_<hash>.csv` |

`extract-news-body` debe usar el prefijo específico `SILVER_BODY_PREFIX=silver/body`.
No se debe configurar el prefijo genérico `SILVER_PREFIX=silver`: produciría
`silver/body_<run_id>.csv`, que no coincide con la regla Events
`silver/body/*.csv` y corta la cadena automática.

Cada evento procesa únicamente el objeto que lo disparó, evitando que una
reintención antigua desplace la corrida actual. Una invocación explícita con
payload vacío (`{}`) activa el drenaje global, siempre en lotes acotados y hasta
el presupuesto interno. Las solicitudes individuales siguen siendo pequeñas:
Claims envía 10 filas por llamada Gemini y Evidence traduce en grupos de 2
abstracts. Los handlers dejan `rows_left_for_next_run` real y los schedules de
drenaje deben volver a activarlos hasta llegar a cero; no se debe asumir que una
sola entrega de Events vacía un corpus de 1000 noticias.

Evidence guarda hasta 10 candidatos PubMed dentro de
`pubmed_results_json`. Eso son diez candidatos por claim, no diez filas
adicionales. La puntuación `embedding_similarity` de OCI Embed 4 ordena la
lista; el valor no es una probabilidad de veracidad. La etiqueta final se
asigna en Label Studio.

En el contrato objetivo JSONL, esta lista dejará de viajar en una columna
`pubmed_results_json`: se conservará dentro del objeto de Evidence asociado a
cada `claim_id`, junto con `evidence_status`, `translation_status` y
`embedding_status`. El cambio no altera el significado de los scores ni la
necesidad de revisión humana.

## Restricciones externas que afectan la automatización

- **NewsData:** `size=10` y dos páginas por grupo producen como máximo ocho
  solicitudes por corrida con la configuración actual. Cada página puede
  consumir un crédito. `totalResults` describe resultados disponibles y no
  equivale a `row_count`; la ventana reciente puede repetir URLs. La consulta
  se mantiene por debajo de 100 caracteres por el error observado en esta
  cuenta. Revisar el saldo y el límite del plan antes de aumentar frecuencia o
  páginas.
- **Gemini:** RPM, TPM y RPD son límites por proyecto y modelo. Las cuatro
  claves pertenecen a proyectos diferentes y se usan como failover; no se
  asume que una clave adicional multiplica la cuota de un proyecto. Las claves
  solo se leen desde Vault durante la invocación.
- **PubMed:** la Function respeta una pausa de 0.4 s y agrupa los PMIDs en
  EFetch. Mantener `PUBMED_EMAIL` y `PUBMED_TOOL` configurados y no enviar
  consultas en paralelo sin revisar las reglas de NCBI.
- **OCI Embed 4:** el modelo `cohere.embed-v4.0` usa 512 dimensiones en este
  pipeline y se limita por caracteres antes de crear embeddings. La API/SDK
  admite hasta 128.000 tokens de entrada total por ejecución; las cuotas y la
  disponibilidad dependen de la región.
- **Scheduler:** interpreta cron en UTC, tiene intervalo mínimo de una hora y
  no ajusta automáticamente el horario de verano. Las horas de inicio pueden
  retrasarse por la cola del servicio.
- **Events y Object Storage:** Events es asíncrono y puede entregar un evento
  después de un retry; Object Storage usa prefijos, no carpetas POSIX. La
  deduplicación por `record_id` y los drenajes de Body/Claims/Evidence hacen segura la repetición.

Enlaces oficiales y detalles de cada límite: [OCI Functions](https://docs.oracle.com/en-us/iaas/Content/Functions/Tasks/functionscustomizing.htm),
[Resource Scheduler](https://docs.oracle.com/en-us/iaas/Content/resource-scheduler/tasks/create-manage.htm),
[Object Storage](https://docs.oracle.com/en-us/iaas/Content/Object/Concepts/objectstorageoverview.htm),
[OCI Generative AI](https://docs.oracle.com/en-us/iaas/Content/generative-ai/limits.htm),
[NewsData](https://newsdata.io/documentation) y
[NCBI E-utilities](https://www.ncbi.nlm.nih.gov/books/NBK25497/?report=printable).

## Despliegue de la imagen

Las Functions se ejecutan con la forma `GENERIC_X86` y memoria de 512 MB. Al
construir desde Apple Silicon, conserva la plataforma de OCI, por ejemplo:

```bash
DOCKER_DEFAULT_PLATFORM=linux/amd64 fn deploy --local
```

El comando exacto depende de la aplicación y el OCIR configurados; no se deben
subir claves ni archivos `.env` a la imagen.

## Claves Gemini

| Secretos de Vault | Stage | Uso |
| --- | --- | --- |
| `google-gemini-api-key`, `google-gemini-api-key-2` | claims | extracción del claim, traducción fiel a inglés y `pubmed_query_en` |
| `google-gemini-api-key-3`, `google-gemini-api-key-4` | evidence | traducción de abstracts a español |

Cada Function carga los secretos durante la invocación y alterna entre ellos.
Si un proyecto responde por cuota, autenticación o rate limit, prueba el
siguiente slot. Las claves nunca entran al CSV, la imagen ni el repositorio.

## Reintentos y duplicados

El estado se deriva de `record_id`. Un resultado terminal (`OK`,
`CUERPO_INSUFICIENTE`, `NO_RESULTS`, `NO_ABSTRACT`, o un error permanente de
URL) no se vuelve a procesar. Un timeout, error de red, HTTP 429 o HTTP 5xx se
conserva para auditoría y se reintenta en la siguiente ejecución.

## Similitud y umbral

No se fija un umbral universal como `0.87` antes de calibrar. Scores de
embeddings distintos no son comparables: la escala depende del modelo y del
texto, y `0.3683` puede ser un score TF-IDF o un score de otro modelo. Se
guardan los 10 candidatos para revisión; después de etiquetar una muestra de
al menos 30 claims, se elige un umbral con una tabla de precisión/recall y se
documenta la decisión en la metodología. Una fila no entra a Gold por superar
una puntuación automática.
