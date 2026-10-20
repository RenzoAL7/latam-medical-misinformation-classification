# `retrieve-pubmed-evidence`

Fourth operational Function in the thesis pipeline. It reads a triggering
`claims.jsonl`, retrieves candidate biomedical evidence from PubMed for every
eligible claim, ranks candidates with OCI Generative AI `cohere.embed-v4.0`,
translates returned abstracts with a separate Gemini secret, and writes one
JSONL record per claim under `silver/evidence/`. An Object Storage event
processes only its triggering Claims object; v1 has no global drain.

The same x86 image is also deployed as the separate OCI Function
`continue-evidence-translation`, with `PIPELINE_MODE=translation_continuation`.
It is not another retrieval pass: it consumes only durable translation jobs
created by this Function after a bounded partial run.

This Function does not assign a truth label. `evidence_status=OK` means that
PubMed returned candidate records, not that the news claim is supported. The
relation (`supports`, `contradicts`, or `unclear`) and the final binary label
remain human decisions in the review workflow.

## Contrato `evidence.v1`

`claims.v1` contiene una noticia y un arreglo de claims; Evidence lo expande
en una fila de trabajo por `claim_id` y escribe
`silver/evidence/run_id=<run_id>/evidence.jsonl`. Cada registro mantiene el
sobre de la noticia, el claim, sus candidatos y los estados de PubMed,
embedding y traducción. Los casos `NO_RESULTS`, `NO_ABSTRACT` y `ERROR` se
mantienen incluso con `candidates: []`.

La Function exige `llm_status=OK`. En v1 una falla de Embed 4 o traducción
cambia `evidence_status` a `ERROR`, por lo que nunca se importa a Label Studio
como evidencia lista. Los scores solo ordenan candidatos: la relación
`supports`/`contradicts` y el veredicto siguen siendo decisiones humanas.
Si Gemini bloquea una traducción literal por `RECITATION`, la Function intenta
en la misma invocación un resumen fiel y lo deja auditado como
`translation_status=PARAPHRASED` a nivel candidato; si no puede recuperar todos
los abstracts, el claim sigue en `ERROR`.

## Configuration

```text
OBJECT_STORAGE_NAMESPACE=idur1kkrru56
OBJECT_STORAGE_BUCKET=mednews-data
SILVER_CLAIMS_PREFIX=silver/claims
SILVER_EVIDENCE_PREFIX=silver/evidence
PIPELINE_SCHEMA=v1
EVIDENCE_MAX_ROWS=0
PUBMED_CANDIDATES_FETCHED=5
PUBMED_EMAIL=<research contact email>
PUBMED_TOOL=mednews-thesis
PUBMED_REQUEST_DELAY=0.4
PUBMED_REQUEST_TIMEOUT=20
EVIDENCE_MAX_SECONDS=285
EVIDENCE_TIME_BUFFER=10
ABSTRACT_MAX_CHARS=1500
TRANSLATE_ABSTRACTS=true
GOOGLE_TRANSLATION_SECRET_OCIDS=<OCI Vault secret OCID API 3>,<OCI Vault secret OCID API 4>
# GOOGLE_TRANSLATION_SECRET_OCID remains supported for one key.
TRANSLATION_MODEL_ID=gemini-3.5-flash-lite
TRANSLATION_BATCH_SIZE=2
TRANSLATION_BATCH_MAX_INPUT_CHARS=9000
TRANSLATION_BATCH_MAX_ABSTRACTS=6
TRANSLATION_MAX_TOKENS=8000
TRANSLATION_REQUEST_TIMEOUT=30
TRANSLATION_PARALLELISM=2
TRANSLATION_CONTINUATION_ENABLED=true
SILVER_TRANSLATION_JOB_PREFIX=silver/evidence-translation-jobs
SILVER_TRANSLATION_STAGE_PREFIX=silver/evidence-translation-stage
TRANSLATION_CONTINUATION_MAX_CLAIMS=4
TRANSLATION_CONTINUATION_MAX_ATTEMPTS=3
TRANSLATION_CONTINUATION_MAX_SECONDS=285
TRANSLATION_CONTINUATION_TIME_BUFFER=10
TRANSLATION_CONTINUATION_PARALLELISM=1
EMBEDDINGS_ENABLED=true
GENAI_COMPARTMENT_ID=<OCI compartment OCID allowed to use Generative AI>
EMBEDDING_REGION=us-ashburn-1
EMBEDDING_MODEL_ID=cohere.embed-v4.0
EMBEDDING_OUTPUT_DIMENSIONS=512
EMBEDDING_MAX_CHARS=6000
EMBEDDING_MAX_INPUTS=96
EMBEDDING_MAX_INPUT_TOKENS=100000
EMBEDDING_REQUEST_TIMEOUT=45
```

`PUBMED_API_KEY` is optional. Without it, the Function keeps the request rate
below the NCBI unauthenticated limit. It makes one ESearch call per claim and
one grouped EFetch call for the returned PMIDs. The Gemini keys are read at
runtime from OCI Vault and are never stored in the image or repository. The
two translation keys rotate round-robin and fail over on quota, authentication,
or rate-limit errors. `translation_key_slot` records the loaded slot used for a
row.

## Selección, costo y límite temporal

An Object Storage Event is scoped to its triggering
`silver/claims/run_id=*/claims.jsonl` object. The Function requires
`is_claim_eligible=true`, `llm_status=OK`, `claim_text` and
`pubmed_query_en`. `EVIDENCE_MAX_ROWS=0` is mandatory: every eligible claim
from that object is represented in the resulting JSONL, even if it terminates
in `ERROR`.

The 285-second work budget leaves ten seconds to persist JSONL and the
manifest before OCI's 300-second limit. It gets at most five PubMed PMIDs per
claim, and embeds and translates every returned candidate with a usable
abstract. The query embedding remains per claim, while a repeated identical
PMID+abstract reuses one document embedding and one translation inside the
invocation. It waits 0.4 seconds between PubMed requests. The Embed 4 requests
are chunked by input count and a conservative 100,000-token estimate so a
large set of claims does not exceed the model input limit. See
[`docs/limits_and_timing.md`](../../../docs/limits_and_timing.md) for the
complete duration, PubMed and Embed 4 limits.

PubMed ESearch and EFetch send their URL-encoded parameters in an HTTP POST
body. This keeps long claim queries out of the request URI and avoids NCBI
HTTP 414 failures while retaining the same bounded query and fetch contract.

Translation may group up to two *small* claim rows in one Gemini request. It
never splits a claim or removes one of its candidates: the 9,000-character and
six-abstract ceilings force a larger claim to remain in a batch of one. The
manifest records the effective batch limits, request/cache statistics, and
abstract character counts, so a future tuning decision can use observed work
rather than lower `TRANSLATION_MAX_TOKENS` blindly.

## Continuación de traducción sin recomputar evidencia

Después de escribir el resultado normal, Evidence identifica solamente los
claims con `llm_status=OK`, `embedding_status=OK` y una traducción transitoria
(`TIME_BUDGET`, `PARTIAL` o error temporal). Para ellos escribe un único job
inmutable en:

```text
silver/evidence-translation-jobs/run_id=<evidence_run>/job-0001.job.json
```

La regla Object Storage correspondiente invoca
`continue-evidence-translation`. Cada invocación procesa como máximo
`TRANSLATION_CONTINUATION_MAX_CLAIMS=4` claims, conserva los PMIDs,
`candidates`, scores y embeddings presentes en el job y llama solo a Gemini
para los abstracts que aún no tienen `abstract_es`. La siguiente invocación se
encadena mediante otro `*.job.json`; los resultados intermedios se guardan bajo
`silver/evidence-translation-stage/` y no son leídos por Label Studio.

Al terminar, se publica un `silver/evidence/run_id=<parent>_translation_final/`
normal, con `evidence.jsonl` y `manifest.json`. El sincronizador existente lo
consume como cualquier otro manifest: la deduplicación por `record_id::claim_id`
evita duplicados y las tareas ya anotadas no se reemplazan. Errores de PubMed,
LLM o Embed 4 no entran en este mecanismo y nunca se enmascaran como una
traducción pendiente.

## Output

Every input claim is written in the immutable run output:

```text
silver/evidence/run_id=<run_id>/evidence.jsonl
silver/evidence/run_id=<run_id>/manifest.json
```

The `candidates` array contains every usable abstract among the at-most-five
PubMed candidates, sorted by a lexical diagnostic before semantic ranking,
with PMID, title, original English `abstract_excerpt`, translated
`abstract_es`, journal, publication date, DOI, PubMed URL, rank, translation
status, OCI embedding cosine similarity and Spanish TF-IDF diagnostic score.
The embedding query is `claim_text_en` and the document input is the English
title plus abstract. Scores are ranking aids, never truth probabilities or
automatic labels. The original English abstract remains for audit.

`best_embedding_similarity` is the OCI Embed 4 cosine score of the PMID that
heads the ranking. `best_tfidf_similarity` is the maximum post-translation
TF-IDF score across candidates, so it can belong to a different PMID and must
not be compared numerically with the embedding score. The
`embedding_status`, `embedding_model`, `embedding_query_field`, and
`ranking_method` columns make a failed or skipped embedding call visible
instead of silently mixing ranking methods.
When Gemini's recitation filter blocks a literal translation, the Function
retries that PMID with a faithful Spanish paraphrase and marks the nested
candidate `translation_status` as `PARAPHRASED`; the English source remains
unchanged for review.

## Manual invocation

```bash
oci fn function invoke \
  --function-id <retrieve-pubmed-evidence-function-ocid> \
  --file - \
  --body '{"data":{"resourceName":"silver/claims/run_id=<run>/claims.jsonl"}}' \
  --region us-ashburn-1 \
  --read-timeout 360
```
