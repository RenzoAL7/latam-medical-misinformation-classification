# Esquema de datos v1

Object Storage es el espacio operativo del pipeline cloud. El flujo sigue una
organización Medallion con los prefijos `bronze/`, `silver/` y `gold/`; las
tablas de revisión pueden exportarse a Google Sheets o Label Studio sin perder
los identificadores de trazabilidad.

Bronze y Body siguen siendo CSV; Claims y Evidence v1 son JSONL inmutables por
corrida. El despliegue remoto se activa solo de forma coordinada con Events y
Label Studio; el contrato completo está en
[`multiclaim_jsonl_plan.md`](multiclaim_jsonl_plan.md).

## Bronze

Una fila representa una noticia candidata todavía no validada. Se mantienen diez campos:

| Campo | Uso |
| --- | --- |
| `record_id` | Hash estable de la URL canónica; se usa para deduplicar. |
| `source_name` | Medio o institución de procedencia. |
| `canonical_url` | URL normalizada que permite volver a la noticia original. |
| `published_at` | Fecha publicada cuando la fuente la entrega. |
| `title`, `subtitle_or_bajada` | Texto visible para decidir si la candidata merece revisión. |
| `topic` | Tema o archivo público que permitió descubrirla. |
| `selection_status` | `PENDIENTE`, `INCLUIR`, `EXCLUIR_NO_MEDICA`, `EXCLUIR_SIN_AFIRMACION` o `EXCLUIR_SIN_TEXTO`. |
| `retrieved_at` | Fecha UTC de recuperación. |
| `country` | Código ISO-2 canónico del país latinoamericano de la fuente, por ejemplo `pe` o `pr`. |

La Function elimina duplicados dentro de la corrida y vuelve a consultar los
`record_id` existentes antes de escribir el siguiente lote.

## Silver

`silver/body/` conserva el resultado técnico de extracción. `silver/claims/`
recibe cuerpos válidos y guarda todos los claims dentro de un registro JSONL
por noticia. `silver/evidence/` guarda un registro JSONL por claim con sus
candidatos PubMed. Los CSV de Bronze y Body son insumos operativos actuales;
Claims y Evidence v1 no se mezclan con el contrato CSV histórico.

| Campo | Uso |
| --- | --- |
| `record_id`, `source_name`, `topic`, `title`, `canonical_url` | Identidad y trazabilidad mínima de la noticia. |
| `body` | Texto principal descargado desde la URL pública. |
| `http_status` | Código HTTP obtenido. |
| `extraction_method` | Extracción HTML con BeautifulSoup o ausencia de extracción. |
| `extraction_status` | `OK`, `CUERPO_INSUFICIENTE` o error de descarga. |
| `extracted_at` | Fecha UTC de extracción. |
| `claim_id`, `claim_index` | Identidad estable de un claim dentro de su noticia y corrida. |
| `claim_text` | Afirmación candidata en español. |
| `claim_text_en` | Traducción fiel usada para buscar literatura biomédica. |
| `pubmed_query_en` | Consulta PubMed en inglés generada para el claim. |
| `query_status` | `OK`, `ERROR` o `SKIPPED_NOT_ELIGIBLE`. |
| `llm_key_slot` | Slot cargado de las claves Gemini 1/2 que produjo la fila; no contiene la clave. |
| `pubmed_results_json` | Hasta diez artículos candidatos con PMID, abstract original en inglés, traducción al español, URL, similitud de OCI Embed 4 y score TF-IDF auxiliar. |
| `evidence_status` | `OK`, `NO_RESULTS`, `NO_ABSTRACT` o `ERROR`. `OK` requiere LLM, embedding y traducción en `OK`. |
| `translation_status` | `OK`, `PARTIAL`, `ERROR`, `NO_ABSTRACTS` o estado de omisión. Un candidato puede marcarse `PARAPHRASED` si Gemini bloquea la traducción literal por recitación. |
| `translation_key_slot` | Slot cargado de las claves Gemini 3/4 que produjo la traducción; no contiene la clave. |
| `best_cosine_similarity` | Alias histórico del score coseno elegido para ordenar; con Embed 4 normalmente coincide con `best_embedding_similarity`, por lo que no es una tercera señal independiente. |
| `best_embedding_similarity` | Mejor similitud coseno calculada con `cohere.embed-v4.0`; es un apoyo de ranking y no una etiqueta. |
| `best_tfidf_similarity` | Mejor similitud TF-IDF entre claim y abstract traducido al español; sirve como score auxiliar. |
| `embedding_status` | Estado de la llamada a OCI Embed 4 (`OK`, `ERROR`, `NO_CANDIDATES` o `SKIPPED_NO_EVIDENCE`). |
| `embedding_model`, `embedding_query_field` | Modelo usado y campo del claim enviado a Embed 4 (`claim_text_en` o `claim_text`). |
| `ranking_method` | `oci_cohere_embed_v4` cuando Embed 4 rankea los candidatos; `tfidf_v1_es` si se usa el fallback. |
| `claim_materiality`, `claim_verdict` | Campos humanos de Label Studio; el primero determina si el claim influye en la etiqueta final. |
| `evidence_relation`, `verification_note` | Decisión y explicación humana sobre la evidencia. |

La extracción del cuerpo no asigna una etiqueta. Si el texto no alcanza el
mínimo, `silver/body/` conserva una fila con `CUERPO_INSUFICIENTE` para la
auditoría y no se crea una fila posterior en `silver/claims/`; Bronze puede
marcarse como `EXCLUIR_SIN_TEXTO` durante la revisión. Las decisiones médicas
y de veracidad siguen siendo humanas y basadas en evidencia.

### JSONL por corrida

`silver/claims/run_id=<run_id>/claims.jsonl` tiene una línea por noticia y
`claims: []` cuando no hay afirmaciones. Cada claim lleva `claim_id` y la
consulta PubMed. Evidence escribe
`silver/evidence/run_id=<run_id>/evidence.jsonl`, una línea por `claim_id`,
con candidatos, recuperación, traducción, embedding y ranking. Ambos directorios
incluyen un `manifest.json` con conteos, duración, configuración y errores.

Todos los claims del objeto de entrada se procesan en la misma invocación v1.
Cada claim recupera como máximo `PUBMED_CANDIDATES_FETCHED=5` PMIDs y genera
embeddings y traducciones para todos los candidatos con abstract utilizable.
La caché por PMID+abstract reduce duplicados dentro de la corrida; no se
omiten claims ni candidatos recuperados para cumplir el tiempo.

El contrato JSONL no crea varias filas de entrenamiento para una noticia. La
relación entre `record_id`, `claim_id` y candidatos se conserva para auditoría;
la agregación humana transforma las tareas por claim en una sola etiqueta por
noticia antes de `gold/training`.

## Gold

Es una salida generada y no debe editarse manualmente. Solo incluye noticias de `Silver` que cumplen simultáneamente:

- `validation_status=COMPLETADA`;
- etiqueta binaria `RESPALDADA` o `REFUTADA`;
- `body`, `claim_text` y `best_pmid` no vacíos;
- relación de evidencia, nota y metadatos de anotación completos.

Una fila por `record_id` conserva la trazabilidad de la noticia y de sus
claims revisados. El CSV `gold/reviewed` incluye como mínimo `record_id`, `country`,
`source_name`, `topic`, `canonical_url`, `published_at`, `title`, `body`,
`claim_text`, `best_pmid`, `best_embedding_similarity`,
`best_cosine_similarity`, `best_tfidf_similarity`, `ranking_method`,
`embedding_model`, `evidence_status`, `evidence_run_id`, `evidence_relation`,
`label`, `verification_note`, `annotation_id`, `annotator`, `annotated_at`,
`claims_total`, `decisive_claims_total`, `aggregation_status`,
`aggregation_reason`, `final_training_eligible`, `validation_status` y
`dataset_split`.

`gold/reviewed` conserva también las filas válidas `NO_DETERMINABLE` y
`EXCLUIDA` para auditoría; `gold/training` deja solamente `RESPALDADA` y
`REFUTADA` y tiene únicamente cinco columnas: `record_id`, `title`, `body`,
`label` y `dataset_split`. Para entrenar, el script local concatena `title +
body` como una sola entrada y usa `label` como objetivo binario
(`RESPALDADA=1`, `REFUTADA=0`). Las similitudes, claim, PMID, fuente y tema
quedan en `gold/reviewed`/Silver para trazabilidad y análisis, no en el CSV
que consume el modelo. `dataset_split` permanece vacío en Gold hasta que una
corrida de entrenamiento genere su manifiesto estratificado 70/15/15.

Con el formato multi-claim, el artefacto de revisión puede conservar una lista
de claims y relaciones de evidencia por `record_id`; el CSV compacto de
`gold/training` no cambia. Si se entrena un verificador de claims en el futuro,
deberá definirse como un dataset separado y no reinterpretar el CSV binario de
noticias.
