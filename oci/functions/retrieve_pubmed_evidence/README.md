# `retrieve-pubmed-evidence`

Fourth operational Function in the thesis pipeline. It reads a triggering flat
`claims_<run_id>.jsonl`, retrieves candidate biomedical evidence from PubMed
for every eligible claim, ranks candidates with OCI Generative AI
`cohere.embed-v4.0`, and writes one JSONL record per claim under
`silver/translate/`. Translation is a separate event-driven Function that
consumes this artifact and publishes final Evidence. An Object Storage event
processes only its triggering Claims object; v1 has no global drain.

The same x86 image is also deployed as the separate OCI Function
`continue-evidence-translation`, with `PIPELINE_MODE=translation`. It is not
another retrieval pass: it consumes only the durable flat Translate JSONL
created by this Function.

This Function does not assign a truth label. `evidence_status=OK` means that
PubMed returned candidate records, not that the news claim is supported. The
relation (`supports`, `contradicts`, or `unclear`) and the final binary label
remain human decisions in the review workflow.

## Contrato `evidence.v1`

`claims.v1` contiene una noticia y un arreglo de claims; retrieval lo expande
en una fila de trabajo por `claim_id` y escribe
`silver/translate/translate_<run_id>.jsonl`. Cada registro mantiene el sobre de
la noticia, incluido `body`, el claim, sus candidatos y los estados de PubMed y
embedding. La Function de traducción publica después
`silver/evidence/evidence_<run_id>.jsonl`. Los casos `NO_RESULTS`,
`NO_ABSTRACT` y `ERROR` se mantienen incluso con `candidates: []`.

La Function exige `llm_status=OK`. En v1 una falla de Embed 4
cambia `evidence_status` a `ERROR`, por lo que nunca se importa a Label Studio
como evidencia lista. Los scores solo ordenan candidatos: la relación
`supports`/`contradicts` y el veredicto siguen siendo decisiones humanas.

## Configuration

```text
OBJECT_STORAGE_NAMESPACE=idur1kkrru56
OBJECT_STORAGE_BUCKET=mednews-data
SILVER_CLAIMS_PREFIX=silver/claims
SILVER_TRANSLATE_PREFIX=silver/translate
SILVER_REJECTED_PREFIX=silver/rejected
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
runtime by `continue-evidence-translation` from OCI Vault and are never stored
in the image or repository.

## Selección, costo y límite temporal

An Object Storage Event is scoped to its triggering
`silver/claims/claims_<run_id>.jsonl` object. The Function requires
`is_claim_eligible=true`, `llm_status=OK`, `claim_text` and
`pubmed_query_en`. `EVIDENCE_MAX_ROWS=0` is mandatory: every eligible claim
from that object is represented in the resulting JSONL, even if it terminates
in `ERROR`.

The 285-second work budget leaves ten seconds to persist JSONL and the
manifest before OCI's 300-second limit. It gets at most five PubMed PMIDs per
claim, and embeds every returned candidate with a usable abstract. The query
embedding remains per claim, while a repeated identical PMID+abstract reuses
one document embedding inside the invocation. It waits 0.4 seconds between
PubMed requests. The Embed 4 requests
are chunked by input count and a conservative 100,000-token estimate so a
large set of claims does not exceed the model input limit. See
[`docs/limits_and_timing.md`](../../../docs/limits_and_timing.md) for the
complete duration, PubMed and Embed 4 limits.

La segunda Function puede agrupar hasta dos *small* claim rows en una petición
Gemini. Ese límite y los estados de error se documentan en
`continue_evidence_translation/README.md`; retrieval solo produce el input
Translate y nunca crea jobs ni staging.

## Output

Every input claim is written in the immutable Translate output:

```text
silver/translate/translate_<run_id>.jsonl
silver/translate/translate_<run_id>.manifest.json
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
`embedding_status`, `embedding_model` and `embedding_query_field` make a
failed or skipped embedding call visible. The obsolete `ranking_method`
column is intentionally not emitted; the scores and statuses are enough for
diagnosis without adding a repeated display column.

## Manual invocation

```bash
oci fn function invoke \
  --function-id <retrieve-pubmed-evidence-function-ocid> \
  --file - \
  --body '{"data":{"resourceName":"silver/claims/claims_<run_id>.jsonl"}}' \
  --region us-ashburn-1 \
  --read-timeout 360
```
