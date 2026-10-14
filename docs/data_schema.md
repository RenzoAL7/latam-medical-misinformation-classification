# Esquema de datos vigente

Object Storage es el espacio operativo del pipeline cloud. El flujo sigue una
organización Medallion con los prefijos `bronze/`, `silver/` y `gold/`; las
tablas de revisión pueden exportarse a Google Sheets o Label Studio sin perder
los identificadores de trazabilidad.

Este documento conserva el contrato CSV actualmente desplegado y añade el
contrato objetivo JSONL para la migración multi-claim. La diferencia es
intencional: hasta que se actualicen las Functions y los consumidores, los
objetos operativos siguen siendo CSV. El diseño objetivo completo está en
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
recibe únicamente cuerpos válidos y añade el claim candidato. `silver/evidence/`
guarda candidatos PubMed para los claims elegibles. Conserva la trazabilidad
anterior y añade:

| Campo | Uso |
| --- | --- |
| `record_id`, `source_name`, `topic`, `title`, `canonical_url` | Identidad y trazabilidad mínima de la noticia. |
| `body` | Texto principal descargado desde la URL pública. |
| `http_status` | Código HTTP obtenido. |
| `extraction_method` | `trafilatura`, respaldo con BeautifulSoup o ausencia de extracción. |
| `extraction_status` | `OK`, `CUERPO_INSUFICIENTE` o error de descarga. |
| `extracted_at` | Fecha UTC de extracción. |
| `claim_text` | Afirmación candidata en español. |
| `claim_text_en` | Traducción fiel usada para buscar literatura biomédica. |
| `pubmed_query_en` | Consulta PubMed en inglés generada para el claim. |
| `query_status` | `OK`, `ERROR` o `SKIPPED_NOT_ELIGIBLE`. |
| `llm_key_slot` | Slot cargado de las claves Gemini 1/2 que produjo la fila; no contiene la clave. |
| `pubmed_results_json` | Hasta diez artículos candidatos con PMID, abstract original en inglés, traducción al español, URL, similitud de OCI Embed 4 y score TF-IDF auxiliar. |
| `evidence_status` | `OK`, `NO_RESULTS`, `NO_ABSTRACT` o `ERROR`. |
| `translation_status` | `OK`, `PARTIAL`, `ERROR`, `NO_ABSTRACTS` o estado de omisión. Un candidato puede marcarse `PARAPHRASED` si Gemini bloquea la traducción literal por recitación. |
| `translation_key_slot` | Slot cargado de las claves Gemini 3/4 que produjo la traducción; no contiene la clave. |
| `best_cosine_similarity` | Alias histórico del score coseno elegido para ordenar; con Embed 4 normalmente coincide con `best_embedding_similarity`, por lo que no es una tercera señal independiente. |
| `best_embedding_similarity` | Mejor similitud coseno calculada con `cohere.embed-v4.0`; es un apoyo de ranking y no una etiqueta. |
| `best_tfidf_similarity` | Mejor similitud TF-IDF entre claim y abstract traducido al español; sirve como score auxiliar. |
| `embedding_status` | Estado de la llamada a OCI Embed 4 (`OK`, `ERROR`, `NO_CANDIDATES` o `SKIPPED_NO_EVIDENCE`). |
| `embedding_model`, `embedding_query_field` | Modelo usado y campo del claim enviado a Embed 4 (`claim_text_en` o `claim_text`). |
| `ranking_method` | `oci_cohere_embed_v4` cuando Embed 4 rankea los candidatos; `tfidf_v1_es` si se usa el fallback. |
| `main_medical_claim` | Afirmación médica principal delimitada manualmente. |
| `label` | `PENDIENTE`, `RESPALDADA`, `REFUTADA`, `NO_DETERMINABLE` o `EXCLUIDA`. |
| `evidence_url` | Fuente especializada usada para contrastar la afirmación. |
| `verification_note` | Explicación breve de por qué la evidencia respalda, refuta o no permite concluir. |
| `reviewer`, `validation_status` | Responsable y estado `PENDIENTE` o `COMPLETADA`. |

La extracción del cuerpo no asigna una etiqueta. Si el texto no alcanza el
mínimo, `silver/body/` conserva una fila con `CUERPO_INSUFICIENTE` para la
auditoría y no se crea una fila posterior en `silver/claims/`; Bronze puede
marcarse como `EXCLUIR_SIN_TEXTO` durante la revisión. Las decisiones médicas
y de veracidad siguen siendo humanas y basadas en evidencia.

### Contrato objetivo JSONL

En la migración, `silver/claims/run_id=<run_id>/claims.jsonl` tendrá una línea
por noticia y `claims: []` cuando no se produzcan afirmaciones. Cada elemento
llevará un `claim_id` y los campos necesarios para consultar PubMed. La etapa
Evidence escribirá `silver/evidence/run_id=<run_id>/evidence.jsonl`, una línea
por claim con sus candidatos y estados de recuperación, traducción y
embedding. Los manifests conservarán los conteos que no pueden inferirse al
contar únicamente candidatos.

El contrato JSONL no crea varias filas de entrenamiento para una noticia. La
relación entre `record_id`, `claim_id` y candidatos de evidencia se conserva
para auditoría y revisión humana; `gold/training` sigue siendo una fila por
noticia.

## Gold

Es una salida generada y no debe editarse manualmente. Solo incluye noticias de `Silver` que cumplen simultáneamente:

- `validation_status=COMPLETADA`;
- etiqueta binaria `RESPALDADA` o `REFUTADA`;
- `body`, `claim_text` y `best_pmid` no vacíos;
- relación de evidencia, nota y metadatos de anotación completos.

Una fila por `record_id` conserva la trazabilidad de la noticia y de su
revisión. El CSV `gold/reviewed` incluye como mínimo `record_id`, `country`,
`source_name`, `topic`, `canonical_url`, `published_at`, `title`, `body`,
`claim_text`, `best_pmid`, `best_embedding_similarity`,
`best_cosine_similarity`, `best_tfidf_similarity`, `ranking_method`,
`embedding_model`, `evidence_status`, `evidence_run_id`, `evidence_relation`,
`label`, `verification_note`, `annotation_id`, `annotator`, `annotated_at`,
`final_training_eligible`,
`validation_status` y `dataset_split`.

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
