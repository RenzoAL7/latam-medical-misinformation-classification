# `extract-claims`

Third OCI Function in the thesis pipeline. It reads the triggering Body CSV,
skips rows without a body or with `extraction_status != OK`, and extracts every
distinct, verifiable medical claim from each remaining news article with OCI
Generative AI or Gemini. The Google API key is read at invocation time from OCI
Vault and is never stored in the image or repository.

The model is used only to structure the article into a candidate claim and to
prepare a faithful English rendering plus a PubMed search query. It does not
decide whether the claim is true or false. Human review and evidence are
required later.

## Contrato `claims.v1`

La salida es `silver/claims/run_id=<run_id>/claims.jsonl`: una línea por
noticia con `claims: []` o un arreglo de claims. Cada elemento recibe
`claim_id`, `claim_index`, `claim_text`, `claim_text_en`, `pubmed_query_en`,
`claim_type` y `claim_status=PENDING_EVIDENCE`. El mismo directorio contiene
`manifest.json` con conteos, duración y fallos. `llm_status=OK` solo confirma
JSON estructurado válido: jamás asigna `RESPALDADA` ni `REFUTADA`.

En v1 `LLM_MAX_ROWS=0` y `MAX_CLAIMS_PER_ARTICLE=0` son obligatorios: una
invocación procesa todas las noticias válidas del objeto de Body y conserva
todos sus claims. Si se agota tiempo o el proveedor falla, cada noticia queda
representada con `llm_status=ERROR` en la misma salida; no existe drenaje ni
reintento automático por payload vacío.

## Configuration

```text
OBJECT_STORAGE_NAMESPACE=idur1kkrru56
OBJECT_STORAGE_BUCKET=mednews-data
SILVER_BODY_PREFIX=silver/body
SILVER_CLAIMS_PREFIX=silver/claims
PIPELINE_SCHEMA=v1
LLM_MODEL_ID=google.gemini-2.5-flash
LLM_REGION=us-ashburn-1
GENAI_COMPARTMENT_ID=<Cloud compartment OCID>
LLM_MAX_ROWS=0
MAX_CLAIMS_PER_ARTICLE=0
LLM_REQUEST_BATCH_SIZE=5
LLM_MAX_SECONDS=270
LLM_TIME_BUFFER=20
LLM_MAX_BODY_CHARS=12000
LLM_MAX_TOKENS=8000
LLM_REQUEST_TIMEOUT=45
LLM_TEMPERATURE=0.0
LLM_TOP_P=0.9
LLM_MAX_RETRIES=0
```

When OCI Generative AI model quota is unavailable, use the Google provider:

```text
LLM_PROVIDER=google
LLM_MODEL_ID=gemini-3.5-flash-lite
GOOGLE_GEMINI_SECRET_OCIDS=<OCI Vault secret OCID API 1>,<OCI Vault secret OCID API 2>
# GOOGLE_GEMINI_SECRET_OCID remains supported for one key.
```

Each secret must contain one Gemini API key as plain text. The Function reads
both secrets at invocation time, rotates them round-robin and fails over to the
other project on quota, authentication, or rate-limit errors. The dynamic
group must be allowed to read both secrets. The request uses the
`generateContent` endpoint and sends the key in the `x-goog-api-key` header.
`llm_key_slot` records which loaded slot produced each row without exposing the
key.

An Object Storage Event is scoped to its triggering `silver/body/*.csv` object;
it never imports an older Body source. `LLM_REQUEST_BATCH_SIZE` limits only the
number of articles in an individual LLM prompt; it does not cap claims or
articles in the invocation. Eligible claims include `claim_text_en` and
`pubmed_query_en`, so Evidence can query English PubMed abstracts without a
second extraction call. HTTP, quota and malformed-JSON failures produce an
auditable `llm_status=ERROR` for every affected article.

The recommended request batch is five articles. The actual time is dominated
by body length, the number of claims returned, response tokens and project
quota. See
[`docs/limits_and_timing.md`](../../../docs/limits_and_timing.md) for the
provider, quota and retry matrix.

## Output

```text
silver/claims/run_id=<run_id>/claims.jsonl
silver/claims/run_id=<run_id>/manifest.json
```

The article envelope retains the metadata needed to join the Body CSV without
duplicating its full text. The `claims` array holds the many-to-one relation;
the manifest retains articles with no claims and all LLM error counts.

## Manual invocation

```bash
oci fn function invoke \
  --function-id <extract-claims-function-ocid> \
  --file - \
  --body '{"data":{"resourceName":"silver/body/body_<run>.csv"}}' \
  --region us-ashburn-1 \
  --read-timeout 360
```
