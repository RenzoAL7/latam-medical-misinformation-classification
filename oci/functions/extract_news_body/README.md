# `extract-news-body`

Second OCI Function in the thesis pipeline. An Object Storage event wakes the
Function, which drains one bounded batch from the global `bronze/*.csv`
backlog, downloads the URLs, extracts the main HTML text, and updates one
canonical CSV per Bronze source under `silver/body/`. This lets a later retry
continue an older batch when an event was delayed or a previous invocation
reached its time budget.

## OCI configuration

The function uses the same resource principal and Object Storage permissions as
`fetch-newsdata`:

```text
OBJECT_STORAGE_NAMESPACE=<tenancy Object Storage namespace>
OBJECT_STORAGE_BUCKET=mednews-data
BRONZE_PREFIX=bronze
SILVER_BODY_PREFIX=silver/body
BODY_BATCH_SIZE=50
BODY_MAX_SECONDS=150
BODY_TIME_BUFFER=10
BODY_REQUEST_TIMEOUT=15
BODY_MAX_RETRIES=1
BODY_MAX_REDIRECTS=5
BODY_MAX_BYTES=1500000
BODY_MIN_WORDS=150
BODY_MIN_CHARS=800
BODY_MAX_TEXT_CHARS=200000
```

`BODY_BATCH_SIZE` is the maximum number of URLs attempted per invocation. The
Fetch Function emits at most 50 rows, but the Body Function can drain older
Bronze files as well. `BODY_MAX_SECONDS` leaves a safety margin below the
300-second OCI Functions limit. Rows not attempted before the deadline remain
pending; they are not incorrectly recorded as terminal failures.

The usual budget is 150 seconds with `BODY_TIME_BUFFER=10`; each URL has a
15-second request timeout and at most one retry. The elapsed time and pending
rows are visible in the invocation log. See the consolidated limits and
duration table in [`docs/limits_and_timing.md`](../../../docs/limits_and_timing.md).

The function follows up to five validated HTTP(S) redirects. Connection,
HTTP, extraction, and time-budget outcomes are persisted in the output CSV with
`extraction_status`. Permanent outcomes are skipped on later runs; transient
HTTP/network/time-budget outcomes remain retryable.

## Silver output

Each Bronze source has one deterministic output object:

```text
silver/body/body_current_<bronze-source>_<hash>.csv
```

Repeating an invocation updates that same object and merges by `record_id`; it
does not create another Body CSV for the same source. `rows_left_for_next_run`
in the response is the pending backlog count, not a hard-coded zero.

The output retains the Bronze columns and adds:

```text
body,http_status,extraction_method,extraction_status,
body_char_count,body_word_count,extracted_at,source_bronze_object,
extraction_run_id,extraction_error
```

`extraction_status` is `OK`, `CUERPO_INSUFICIENTE`, `HTTP_ERROR`, `TIMEOUT`,
`URL_ERROR`, or another explicit error status. A body is `OK` only when it has
at least 150 words and 800 characters, matching the project data schema. This
Function only extracts text; it never decides whether a claim is true or false.

## Manual drain invocation

```bash
oci fn function invoke \
  --function-id <extract-news-body-function-ocid> \
  --file - \
  --body '{}' \
  --region us-ashburn-1 \
  --read-timeout 360
```
