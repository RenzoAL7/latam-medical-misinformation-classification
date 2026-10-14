# Límites, duración y consumo del pipeline OCI

Este documento es la referencia operativa del pipeline. Describe los valores
configurados actualmente y los límites publicados por los proveedores. Un
tiempo indicado como `máximo` es un presupuesto interno de la Function; no es
una promesa de que el proveedor responderá en ese tiempo ni un SLA.

El ajuste de presupuesto de `fetch-newsdata` (`NEWSDATA_MAX_SECONDS`,
`NEWSDATA_TIME_BUFFER` y `elapsed_seconds`) está incluido en el PR de esta
documentación. Para que esos campos aparezcan en la respuesta de producción se
debe desplegar la nueva imagen después de fusionar el PR; la imagen que ya
estaba activa conserva su comportamiento anterior hasta ese despliegue.

La tabla de este documento refleja actualmente el contrato CSV desplegado. El
impacto del diseño objetivo multi-claim y JSONL se resume más abajo y se
especifica en [`multiclaim_jsonl_plan.md`](multiclaim_jsonl_plan.md).

## Resumen de una corrida

| Etapa | Disparador | Trabajo máximo por invocación | Presupuesto interno | Salida |
| --- | --- | ---: | ---: | --- |
| Recolección NewsData | Scheduler, 08:00 y 20:00 Lima | 50 filas nuevas; hasta 8 solicitudes con la configuración actual | 150 s + 10 s de margen | `bronze/newsdata_<run_id>.csv` |
| Extracción del cuerpo | Event `bronze/*.csv` + drenaje | 50 filas del Bronze disparador; drenaje global con payload vacío | 150 s + 10 s de margen | `silver/body/body_current_<source>_<hash>.csv` |
| Claims | Event `silver/body/*.csv` + drenaje | 25 filas del Body disparador; drenaje global con payload vacío; solicitudes Gemini de 10 filas | 150 s + 10 s de margen | `silver/claims/claims_current_<source>_<hash>.csv` |
| Evidence | Event `silver/claims/*.csv` + drenaje | 20 claims del Claims disparador; drenaje global con payload vacío; hasta 10 artículos PubMed por claim | 295 s + margen de 5 s | `silver/evidence/evidence_run_zz_current_<source>_<hash>.csv` |
| Etiquetado | Persona en Label Studio | No tiene timeout de OCI | Depende del equipo | Gold revisado |

Los eventos procesan solo el objeto recibido. El drenaje global se reserva para
invocaciones explícitas con payload vacío (`{}`), que seleccionan el pendiente
más antiguo y permiten recuperar reintentos sin mezclar corridas activas.
Los tamaños son límites superiores, no cantidades garantizadas. Si una etapa
se queda sin tiempo, escribe lo que terminó y deja el resto pendiente para la
siguiente activación de esa etapa. El `run_id`, el nombre del objeto, `row_count` y los campos
de auditoría son la fuente de verdad para saber cuánto se procesó realmente.

### Impacto del diseño multi-claim JSONL

El cambio de formato no debe ocultar el cambio de cardinalidad:

- Body sigue midiendo artículos procesados.
- Claims mide artículos de entrada y debe registrar también `claims_total`,
  `claims_count` por noticia y `claims_truncated`.
- Evidence mide claims elegibles y candidatos recuperados; `top_k` es por
  `claim_id`, no por noticia.
- Cada etapa escribe un `manifest.json` con artículos, claims, candidatos,
  estados terminales y `rows_left_for_next_run`.
- Una corrida JSONL es inmutable. Los reintentos actualizan el estado de la
  misma combinación de `run_id`, `record_id` y `claim_id` o generan una nueva
  corrida explícita; no mezclan resultados silenciosamente.

Por eso un lote de 25 noticias puede producir más de 25 claims y Evidence
puede hacer más de 25 búsquedas PubMed. El presupuesto se debe validar por
tokens, claims y candidatos, no únicamente por el número de artículos.

Con dos corridas de recolección al día, el objetivo normal es de hasta 100
filas nuevas diarias antes de descartar URLs repetidas, artículos sin cuerpo o
respuestas que no cumplan los filtros. La primera corrida puede producir menos
filas porque NewsData entrega una ventana reciente y Bronze ya puede contener
`record_id` conocidos.

### Duración de extremo a extremo

El tiempo de una Function y el tiempo de todo el pipeline son cosas distintas.
Events puede iniciar la siguiente etapa poco después del `PUT`, pero el
servicio es asíncrono. Para un
CSV Bronze de 50 filas, claims avanza en lotes de 25 y hace como máximo 3
solicitudes Gemini de 10 filas por invocación. Evidence avanza en lotes de 20:
hace una búsqueda PubMed por claim, agrupa el EFetch, trocea los embeddings y
traduce abstracts en grupos de 2. Si ese trabajo no cabe dentro del presupuesto,
la fila queda reintentable y una activación posterior del drenaje continúa. El
tiempo observado depende de la cola de
OCI, Events, URLs, PubMed, cuotas y errores; se debe reportar desde
`elapsed_seconds` y los nombres de objetos, no inferirlo solo desde la hora del
Scheduler.

## Duración y límites de OCI Functions

OCI permite configurar el timeout síncrono de una Function hasta 300 segundos.
El pipeline no espera hasta ese borde: cada etapa termina su trabajo normal
alrededor de los 150 segundos y conserva un margen de 10 segundos para
serializar y escribir el CSV. Esto deja holgura frente al límite práctico de
espera de Resource Scheduler. El valor se controla con:

```text
NEWSDATA_MAX_SECONDS=150
NEWSDATA_TIME_BUFFER=10
BODY_MAX_SECONDS=150
BODY_TIME_BUFFER=10
LLM_MAX_SECONDS=150
LLM_TIME_BUFFER=10
EVIDENCE_MAX_SECONDS=270
```

El timeout de la llamada `oci fn function invoke` debe ser mayor que el
timeout de la Function, por ejemplo `--read-timeout 360`. Eso solo cambia la
espera del cliente; no aumenta el máximo permitido por OCI.

La imagen desplegada usa `512 MB`. OCI ofrece otros tamaños de memoria, pero
subir la memoria no convierte una ejecución síncrona en ilimitada y puede
cambiar el costo. La concurrencia regional y los límites de invocación son
cuotas del tenancy; deben consultarse en **Governance & Administration >
Limits, Quotas and Usage** antes de aumentar paralelismo. Si se cambia la
forma de la Function, la imagen de este repositorio debe construirse para
`linux/amd64`/`GENERIC_X86`, porque esa es la forma usada en OCI.

Referencias oficiales: [personalización y timeout de OCI
Functions](https://docs.oracle.com/en-us/iaas/Content/Functions/Tasks/functionscustomizing.htm),
[invocación síncrona y
asíncrona](https://docs.oracle.com/en-us/iaas/Content/Functions/Tasks/functionsinvokingfunctions.htm),
[disponibilidad y escalado](https://docs.oracle.com/en-us/iaas/Content/Functions/Concepts/functionsavailability.htm)
y [límites generales del servicio](https://docs.oracle.com/en-us/iaas/Content/General/service-limits/default.htm).

## NewsData: créditos, paginación y repetición

La Function solicita `language=es`, `category=health`, una consulta médica de
menos de 100 caracteres y grupos de hasta cinco códigos de país. La
configuración de producción usa `size=10` y `NEWSDATA_MAX_PAGES=2`:

- hay cuatro grupos para los 19 códigos LATAM configurados;
- el máximo normal es `4 grupos × 2 páginas = 8 solicitudes` por corrida;
- la Function consulta los grupos disponibles y selecciona hasta 50 filas con
  una ronda por país para evitar que los países al final de la secuencia sean
  excluidos por el corte del lote;
- cada página adicional es otra llamada y puede consumir otro crédito;
- con dos corridas programadas, el techo normal es 16 solicitudes diarias,
  antes de considerar reintentos o fallos que no lleguen a completar una
  página;
- `request_meta.requests_made`, `pages_fetched`, `total_results` y
  `articles_received` registran el consumo y el resultado de cada corrida.

`totalResults` no es el número de filas guardadas: es una estimación del
universo que la API encontró. Las filas guardadas son `row_count` después de
filtrar país, URL inválida y `record_id` existente. La API de últimas noticias
trabaja sobre una ventana reciente (en el plan y endpoint usados, normalmente
las últimas 48 horas), por lo que dos corridas pueden devolver las mismas
URLs. La deduplicación de Bronze por `record_id` evita pagar y almacenar la
misma noticia otra vez cuando la API la vuelve a mostrar.

La cuota y el tamaño de página dependen del plan. La documentación de
NewsData describe créditos por solicitud, `nextPage` para paginar, `size` de
1--10 en planes gratuitos y hasta 50 en planes que lo permiten, y límites de
consulta por plan. El código mantiene el límite conservador de 100 caracteres
porque la API ya devolvió el error `NEWSDATA_QUERY cannot be longer than 100
characters` en este proyecto. Verifica el panel de la cuenta antes de cambiar
`size`, `max_pages` o la frecuencia.

La misma documentación muestra 200 créditos diarios para el plan gratuito en
su tabla actual; el saldo y el límite del plan contratado son los que mandan en
la cuenta real. Un crédito no representa necesariamente una noticia: depende
del endpoint, del tamaño solicitado y de la paginación.

Referencia: [NewsData API documentation](https://newsdata.io/documentation).

NewsData solo descubre candidatas. No es una fuente de veracidad médica; el
cuerpo, PubMed y la revisión humana son etapas posteriores.

## Body: por qué el lote es 50

`extract-news-body` procesa las filas en orden de objeto y no asume que un CSV
contiene exactamente 50 filas. Puede tomar las filas restantes de un segundo
CSV para llenar el lote. Cada URL tiene timeout de 15 s y un reintento para
errores transitorios. Una página bloqueada, un paywall o un cuerpo muy corto
queda en el CSV con `extraction_status`. Los cuerpos insuficientes son
terminales. Timeouts, throttling, errores de conexión, HTTP 403 y HTTP 5xx
quedan pendientes para el siguiente drenaje o evento de Body; los códigos 4xx
fuera de la lista reintentable, como 406, quedan como errores terminales.

Así se evita que una corrida de 50 URLs exceda los 300 s de OCI. No se elimina
una fila sin explicación: `HTTP_ERROR`, `TIMEOUT`, `URL_ERROR` y
`CUERPO_INSUFICIENTE` permanecen auditables.

## Claims y Gemini

Claims procesa hasta 25 filas elegibles de una fuente Body por invocación. Los
eventos se limitan al Body disparador y el drenaje horario selecciona una fuente
pendiente. Agrupa diez filas por solicitud Gemini y continúa con la siguiente
solicitud hasta terminar o alcanzar el presupuesto. Usa las claves Gemini 1 y 2
de proyectos distintos, pero
la cuota de Gemini se mide por proyecto y modelo en RPM, TPM y RPD; cuatro
claves del mismo proyecto no multiplicarían la cuota. La Function rota entre
los proyectos configurados y usa el siguiente slot solo como failover cuando
hay cuota, autenticación o rate limit. La clave se carga desde OCI Vault en
tiempo de ejecución y nunca se escribe en un CSV, una imagen o Git. Los
contadores de RPD se reinician según la zona horaria indicada por Google
(actualmente, medianoche del Pacífico); la consola de AI Studio es la fuente
de verdad para el proyecto y el modelo seleccionados.

El límite práctico de una corrida lo determinan los tokens enviados (cuerpo
recortado a `LLM_MAX_BODY_CHARS`), la respuesta JSON y la ventana de cuota, no
solo el número de filas. Por eso el lote de 10 es el tamaño de cada solicitud,
no el máximo de claims de la invocación. `LLM_MAX_ROWS=0` deja el máximo sin
tope explícito; el presupuesto de tiempo sigue siendo el límite efectivo.
Una respuesta incompleta o JSON inválido deja esas filas pendientes; no se
escribe un claim parcialmente asignado a otro `record_id`.

Referencias: [Gemini API rate limits](https://ai.google.dev/gemini-api/docs/rate-limits?hl=en)
y [API key security](https://ai.google.dev/gemini-api/docs/api-key).

## Evidence, PubMed y OCI Embed 4

Evidence intenta tomar todas las claims elegibles pendientes por invocación.
Para cada claim hace una búsqueda PubMed en inglés, recupera los abstracts de
los PMIDs devueltos y conserva hasta 10 candidatos dentro de
`pubmed_results_json`; no crea 50 filas nuevas por claim.
La configuración incluye una pausa de 0.4 s entre solicitudes, un ESearch por
claim y un EFetch agrupado para los PMIDs de ese lote. Los abstracts se
traducen en grupos de 2 con las claves Gemini 3 y 4. Si falla la traducción,
se conserva el abstract inglés y el estado del candidato explica el motivo.

El ranking usa `cohere.embed-v4.0` de OCI Embed 4 sobre el claim en inglés y
el título+abstract en inglés. Guarda `embedding_similarity`,
`best_cosine_similarity`, `embedding_status`, `embedding_model` y
`ranking_method`. El score es una medida de similitud para ordenar candidatos,
no una probabilidad de que la noticia sea verdadera. La evidencia pasa a Gold
solo después de la decisión humana (`supports`, `contradicts` o `unclear` y la
etiqueta final).

En el formato objetivo JSONL, `evidence.jsonl` conserva una línea por claim
con su arreglo `candidates`, incluidos los casos sin resultados. Así se evita
que `NO_RESULTS` o `NO_ABSTRACT` desaparezcan por no tener un PMID que contar.
Si se necesita una tabla plana para análisis, puede derivarse una línea por
candidato manteniendo `record_id` y `claim_id` como claves.

Embed 4 admite hasta 128.000 tokens de entrada total por llamada en API/SDK y
dimensiones 256, 512, 1024 o 1536; el pipeline usa 512 y limita el texto por
carácter para no acercarse al borde. La disponibilidad del modelo, las cuotas
de Generative AI y el endpoint dependen de la región y del tenancy.

PubMed pide no superar 3 solicitudes por segundo sin clave y permite hasta
10 solicitudes por segundo con una API key registrada (la cuota puede variar).
El Function conserva 0.4 s de separación y recomienda configurar `PUBMED_EMAIL`
y `PUBMED_TOOL`. Si se añade una clave NCBI, debe configurarse en el entorno,
nunca en el repositorio.

Referencias: [OCI Cohere Embed 4](https://docs.oracle.com/en-us/iaas/Content/generative-ai/cohere-embed-4.htm),
[límites de OCI Generative AI](https://docs.oracle.com/en-us/iaas/Content/generative-ai/limits.htm)
y [NCBI E-utilities usage guidelines](https://www.ncbi.nlm.nih.gov/books/NBK25497/?report=printable).

## Scheduler, Events y Object Storage

Resource Scheduler evalúa los cron en UTC. Las expresiones `0 13 * * *` y
`0 1 * * *` representan 08:00 y 20:00 en Lima mientras la conversión local sea
la esperada. Scheduler no cambia automáticamente el cron por horario de
verano y su intervalo mínimo es de una hora; por eso Body, Claims y Evidence
usan el drenaje `0 * * * *` con payload vacío.
La hora de inicio puede retrasarse por la cola del servicio.

Object Storage usa prefijos, no carpetas POSIX. Un archivo `bronze/foo.csv`
es un objeto cuyo nombre contiene `/`; crear carpetas vacías no cambia el
procesamiento. El límite de tamaño de un objeto es muy superior al de estos
CSV: OCI documenta hasta 10 TiB por objeto, 50 GiB para una llamada `PutObject`
y partes de hasta 50 GiB en multipart (máximo 10.000 partes). El costo depende
también de llamadas, versiones, retención y lifecycle. El pipeline escribe
pequeños CSV auditables y evita reescribir todo el histórico.

Events es asíncrono: la creación de un objeto puede disparar la siguiente
Function después de que el PUT terminó y una entrega puede necesitar reintento.
Por eso cada etapa es idempotente por `record_id`; los eventos procesan solo su
objeto disparador y los drains con payload vacío recorren una fuente pendiente a
la vez. No se debe usar el timestamp del evento como garantía de que la etapa
terminó: para terminar una colección grande se necesita una activación
recurrente de drenaje mientras `rows_left_for_next_run` sea mayor que cero.

Referencias: [OCI Resource Scheduler](https://docs.oracle.com/en-us/iaas/Content/resource-scheduler/tasks/create-manage.htm)
y [OCI Object Storage overview](https://docs.oracle.com/en-us/iaas/Content/Object/Concepts/objectstorageoverview.htm).

## Cómo auditar una corrida

1. Revisar la respuesta de la invocación: `run_id`, `object_name`, `row_count`
   y `request_meta`.
2. En Object Storage contar los CSV bajo cada prefijo y comparar los
   `record_id` entre capas.
3. En los logs mirar `elapsed_seconds`, `rows_left`, estados terminales y
   slots de clave utilizados.
4. Revisar en OCI Resource Scheduler los work requests y en Events las reglas
   activas.
5. Revisar en AI Studio/OCI Generative AI la cuota del proyecto y en NewsData
   el saldo de créditos antes de subir lotes o frecuencia.
6. Importar evidence a Label Studio con el manifest. El manifest y las tareas
   existentes impiden duplicar `record_id`.

## Calibración y límites metodológicos

No existe un umbral universal como `0.87` que se pueda copiar entre modelos de
embeddings. Se conservan los diez candidatos para revisión y se elige un
umbral solo después de una muestra humana suficiente, midiendo precisión y
recall. Una similitud baja o alta nunca sustituye la lectura del abstract y
la decisión humana. Solo las filas revisadas, con evidencia trazable y una
etiqueta binaria válida, pasan a Gold.
