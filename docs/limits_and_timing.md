# Límites, duración y consumo del pipeline v1

Esta es la referencia de operación del contrato multi-claim. Un presupuesto es
un límite interno de trabajo, no una garantía de respuesta de OCI, PubMed,
Gemini o NewsData.

## Activación segura

El despliegue de producción actual usa dos capturas aisladas por evento: 08:00
y 20:00 Lima. Ambas avanzan exclusivamente desde su propio objeto Bronze a
través de las reglas Events y el timer de Label Studio del proyecto
multi-claim. Los drenajes horarios permanecen inactivos porque su payload
global puede seleccionar trabajo histórico. Esta activación no autoriza borrar
ni reprocesar datos existentes.

## Resumen por corrida

| Etapa | Disparador | Trabajo v1 | Presupuesto | Salida |
| --- | --- | --- | ---: | --- |
| NewsData | Scheduler 08:00 Lima | hasta 50 noticias nuevas | 150 s + 10 s | `bronze/*.csv` |
| Body | Event Bronze | hasta 50 cuerpos | 150 s + 10 s | `silver/body/*.csv` |
| Claims | Event Body | todas las noticias válidas del objeto | 270 s + 20 s | `claims.jsonl` + manifest |
| Evidence | Event Claims JSONL | inventario completo + primer tramo de hasta 8 claims | 285 s + 10 s | `evidence.jsonl` + manifest |
| Continuación recuperación | Event `*.job.json` | hasta 8 claims pendientes, serial | 285 s + 10 s | Evidence final consolidado |
| Continuación traducción | Event `*.job.json` | hasta 4 claims ya recuperados | 285 s + 10 s | Evidence final consolidado |
| Revisión | Persona | una tarea por claim | sin timeout OCI | Gold por noticia |

Claims y Evidence no usan un drenaje de payload vacío en v1. Si un objeto de
Claims supera el tramo seguro, Evidence conserva cada claim en el padre y
encadena una recuperación serial desde ese mismo objeto inmutable. Si un
proveedor falla, mantiene el estado por artículo o claim y el manifest queda
`PARTIAL` o `FAILED`. La continuación de traducción se programa después de
que PubMed y Embed 4 terminaron, por lo que no los vuelve a consultar.

## Cardinalidad: qué se limita y qué no

Una noticia puede producir muchos claims. Para no ocultar uno potencialmente
decisivo, v1 exige:

```text
LLM_MAX_ROWS=0
MAX_CLAIMS_PER_ARTICLE=0
EVIDENCE_MAX_ROWS=0
```

El primer valor permite todas las noticias válidas del Body disparador; el
segundo conserva todos los claims detectados y el tercero conserva el
inventario de todos los claims elegibles de `claims.jsonl`. La recuperación se
serializa en tramos de ocho cuando hace falta, sin modificar esa cardinalidad.
Los lotes de cinco artículos para Gemini limitan solo el tamaño de una
solicitud, no la cantidad total de claims.

El ahorro se aplica a los candidatos:

```text
PUBMED_CANDIDATES_FETCHED=5
TRANSLATION_PARALLELISM=2
TRANSLATION_BATCH_SIZE=2
```

Cada claim consulta hasta cinco PMIDs y Embed 4 procesa todos los candidatos
recuperados que tengan abstract. El prefiltrado barato solo fija el orden de
presentación; no descarta candidatos. Si dos claims reciben exactamente el
mismo PMID+abstract, se calcula una sola representación del documento y una
sola traducción dentro de la Function; las similitudes coseno siguen siendo
una por claim porque cada query de claim es distinta.

La traducción puede agrupar hasta dos claims pequeños en una sola solicitud de
Gemini, con límites conservadores de 9,000 caracteres y seis abstracts por
lote. Si una fila supera esos límites, permanece sola: no se parte ni se
descarta evidencia.

## Claims y Gemini

`extract-claims` recorta cada cuerpo según `LLM_MAX_BODY_CHARS` (24,000
caracteres por defecto) y pide al modelo
un arreglo estructurado de claims, no una única afirmación. Cada respuesta
incluye texto en español, traducción fiel en inglés y una consulta PubMed. La
Function usa un máximo interno de 270 segundos con un buffer de 20 segundos;
si una respuesta de varios artículos no respeta el arreglo `claims`, conserva
la validación estricta y reintenta solo esos artículos de forma individual. El
manifest deja auditables ambos contadores de recuperación.

Ante un 5xx transitorio de Gemini, rota entre las claves disponibles y aplica
backoff exponencial acotado antes de declarar el grupo como error. La cantidad
de reintentos y la pausa inicial se controlan con `LLM_MAX_RETRIES` y
`LLM_RETRY_BACKOFF_SECONDS`; no reducen `MAX_CLAIMS_PER_ARTICLE`.
Si la respuesta es inválida o el tiempo se agota, la noticia se conserva con
`llm_status=ERROR` y explicación.

Para recuperar de manera excepcional un cuerpo que antes fue recortado, se
puede activar temporalmente `CLAIMS_DEDUPLICATE_EXISTING_TEXTS=true`. Solo en
ese reintento compara `record_id` y texto exacto normalizado contra Claims v1
ya emitidos: conserva los claims nuevos del tramo recuperado y evita volver a
enviar a Label Studio los ya existentes. El manifest deja el conteo y el
criterio; la opción vuelve a `false` al terminar el reintento.

Un `llm_status=OK` confirma estructura válida, no veracidad. El modelo LLM no
asigna `RESPALDADA`, `REFUTADA` ni la etiqueta final de la noticia. El manifest
de Claims conserva los caracteres disponibles, enviados y omitidos por el
límite; el de Evidence registra PMIDs únicos, abstracts y caracteres de
traducción. Estas métricas permiten comparar costo/tiempo sin sacrificar
claims ni candidatos.

## Evidence, PubMed, embedding y traducción

Evidence exige `llm_status=OK` y consulta PubMed con `pubmed_query_en`. Un
resultado con título pero sin abstract se clasifica `NO_ABSTRACT`; no se crea
un candidato utilizable. Una vez recuperados los abstracts:

1. un filtro léxico barato ordena los cinco candidatos;
2. Embed 4 calcula vectores de `claim_text_en` y título+abstract de todos los
   candidatos recuperados con abstract;
3. la similitud coseno ordena los candidatos para la lectura humana;
4. Gemini traduce al español todos los abstracts candidatos;
5. si embedding o traducción falla, el estado final pasa a `ERROR`.

TF-IDF es un diagnóstico y fallback de ranking local; no es el TF-IDF del
modelo de clasificación de noticias. Ni TF-IDF ni embedding validan la verdad,
y ningún score tiene un umbral de aceptación antes de una calibración humana.

El Function respeta 0.4 s entre consultas PubMed sin API key. Embed 4 usa
dimensión 512 y trocea solicitudes por inputs/tokens. Con dos claves Gemini
independientes, la traducción procesa hasta dos lotes en paralelo
(`TRANSLATION_PARALLELISM=2`); esto reduce el tiempo de pared sin aumentar la
cantidad de abstracts ni el costo unitario. Las cuotas reales dependen del
tenancy y región; se revisan en OCI antes de elevar más el paralelismo o el
tamaño de lote.

## Continuación de recuperación y traducciones

`continue-evidence-retrieval` evita que un lote grande se agote antes de
consultar todos sus claims. El Evidence padre contiene una fila por claim: los
no iniciados quedan marcados como en cola y un job inmutable conserva solo sus
identidades. Cada invocación recupera, embebe y traduce como máximo ocho claims
y crea el siguiente job únicamente después de escribir su etapa. Al final
fusiona las etapas con el padre y publica un `evidence.jsonl` estándar. Los
reintentos de PubMed, red, Embed 4 o presupuesto se limitan a tres por claim;
una falla persistente sigue visible como `ERROR`.

## Continuación de traducciones

La continuación evita que un lote con muchos claims pierda evidencia por el
límite de cinco minutos. Evidence escribe un job solo para claims con
`translation_status=TIME_BUDGET`, `PARTIAL` o un error temporal de Gemini y
con `llm_status=OK` y `embedding_status=OK`. Cada job conserva los candidatos
con sus PMIDs, abstracts originales, scores y vectores ya calculados.

`continue-evidence-translation` procesa un máximo de cuatro claims por
invocación, con una sola ola de Gemini (`TRANSLATION_CONTINUATION_PARALLELISM=1`)
para reducir errores 429/503. Si todavía hay trabajo, escribe un único job
siguiente; si termina, fusiona sus resultados con el Evidence padre y publica
un nuevo `evidence.jsonl` estándar bajo
`run_id=<parent>_translation_final`. Los intentos se limitan a tres por claim;
si los tres fallan, el claim sigue visible como `ERROR`, no se afirma que esté
verificado.

Los costos adicionales son exclusivamente de Gemini para abstracts que no
tengan `abstract_es`; no hay nueva búsqueda PubMed ni nuevas llamadas Embed 4.
Los objetos intermedios bajo `silver/evidence-translation-stage/` son de
auditoría y no llegan a Label Studio.

## Qué llega a Label Studio

Un claim se importa si cumple una de estas dos rutas revisables:

```text
# evidencia utilizable
llm_status=OK
evidence_status=OK
embedding_status=OK
translation_status=OK

# resultado terminal de PubMed para triage humano
llm_status=OK
evidence_status=NO_RESULTS o NO_ABSTRACT
embedding_status=SKIPPED_NO_EVIDENCE
translation_status=SKIPPED_NO_EVIDENCE
```

La segunda ruta nunca se presenta como evidencia que apoya o contradice el
claim; permite que la persona revisora determine materialidad y, si es central,
registre `NO_DETERMINABLE`. Esto evita tratar un error de infraestructura como
evidencia útil. Un manifest `PARTIAL` no bloquea los demás claims: el
sincronizador importa solo las filas revisables y conserva las fallidas fuera
de Label Studio. Cuando aparece el manifest final de continuación, el
importador deduplica por `record_id::claim_id`: añade solo los claims
recuperados y no duplica ni sobrescribe una anotación humana existente.
Label Studio no recibe un `final_label`; la persona clasifica materialidad,
relación de evidencia y veredicto del claim. Al construir Gold,
`build-labelstudio-training` usa el Evidence JSONL como inventario
de todos los claims: si uno no fue revisable o no tiene revisión humana, bloquea
la noticia. Solo entonces aplica la regla artículo: un claim central/material
refutado implica `REFUTADA`; todos respaldados implica `RESPALDADA`; los
otros casos quedan `NO_DETERMINABLE`.

## Dataset y modelo

Gold conserva auditoría por noticia y por revisión humana, pero
`gold/training` mantiene una sola fila por `record_id`:

```text
record_id,title,body,label,dataset_split
```

El modelo baseline recibe `title + body` y devuelve una clase binaria
`RESPALDADA`/`REFUTADA` (más sus probabilidades o scores de clasificación según
el algoritmo). Claims, PMIDs, embeddings y TF-IDF no son entradas de ese
modelo: se usan para construir y auditar la etiqueta humana.

## Imagen x86 y verificación

La forma de OCI Functions es `GENERIC_X86`; las imágenes deben compilarse para
`linux/amd64` desde un Mac ARM. Antes de desplegar se ejecutan `make test`, una
prueba local de ambos handlers con Object Storage/PubMed/Embed/Traducción
simulados y una inspección de arquitectura de la imagen. La prueba local no
sustituye una corrida controlada remota: la verificación final requiere un
objeto nuevo, manifests reales y tareas creadas en un proyecto limpio de Label
Studio.

Referencias: [OCI Functions](https://docs.oracle.com/en-us/iaas/Content/Functions/Tasks/functionscustomizing.htm),
[OCI Cohere Embed 4](https://docs.oracle.com/en-us/iaas/Content/generative-ai/cohere-embed-4.htm),
[NCBI E-utilities](https://www.ncbi.nlm.nih.gov/books/NBK25497/?report=printable)
y [Gemini API rate limits](https://ai.google.dev/gemini-api/docs/rate-limits?hl=en).
