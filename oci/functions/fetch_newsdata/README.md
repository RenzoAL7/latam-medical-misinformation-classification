# `fetch-newsdata`

First OCI Function in the thesis pipeline. It reads the NewsData API key from
OCI Vault, requests Spanish health news country by country in a fixed order,
and writes one deduplicated CSV per run to the flat `bronze/` prefix in Object
Storage. The collection scope is Spanish-speaking Latin America, including
Puerto Rico (`pr`) and excluding Spain and Equatorial Guinea. The CSV follows
the Bronze columns used by the thesis spreadsheet and includes the canonical
ISO-2 publisher country. It can rotate across multiple Vault secrets so one
exhausted or unavailable NewsData key does not stop the batch.

## Required function configuration

Set these application/function environment variables in OCI:

```text
NEWSDATA_SECRET_OCIDS=<OCID of key 1>,<OCID of key 2>
# NEWSDATA_SECRET_OCID remains supported when only one key is configured.
OBJECT_STORAGE_NAMESPACE=<tenancy Object Storage namespace>
OBJECT_STORAGE_BUCKET=mednews-data
BRONZE_PREFIX=bronze
BRONZE_DEDUP_ENABLED=1
NEWSDATA_LANGUAGE=es
NEWSDATA_CATEGORY=health
NEWSDATA_SIZE=10
NEWSDATA_MAX_PAGES=2
NEWSDATA_TARGET_ROWS=50
NEWSDATA_REQUIRE_TARGET_ROWS=0
NEWSDATA_COUNTRY_GROUP_SIZE=5
NEWSDATA_COUNTRY_DELAY=1
NEWSDATA_PAGE_DELAY=1
NEWSDATA_MAX_RETRIES=2
NEWSDATA_MAX_SECONDS=150
NEWSDATA_TIME_BUFFER=10
NEWSDATA_REMOVEDUPLICATE=1
NEWSDATA_VIDEO=0
NEWSDATA_COUNTRY_SEQUENCE=ar,bo,cl,co,cr,cu,do,ec,gt,hn,mx,ni,pa,pe,pr,py,sv,uy,ve
NEWSDATA_QUERY=(salud OR medicina OR médico OR enfermedad OR vacuna OR tratamiento OR cáncer OR diabetes)
```

`NEWSDATA_QUERY` and `NEWSDATA_ENDPOINT` remain optional filters. The function
splits `NEWSDATA_COUNTRY_SEQUENCE` into groups of up to five countries (the
NewsData limit), requests each group in order, and sorts the resulting rows
back into the configured country order before writing the CSV. It appends new
rows until `NEWSDATA_TARGET_ROWS` is reached. Puerto Rico (`pr`) is
intentionally included as a separate territory. The function rejects a
configuration containing a non-LATAM code and filters the API response again,
so an article reported with a country outside the requested group cannot enter
the Bronze CSV. The order restarts at `ar` on the next invocation; no cursor or
state JSON is needed.

The default medical query includes general terms such as `salud`, `médico`,
`medicina`, and `enfermedad` in addition to specific terms such as `cáncer`,
`diabetes`, `vacuna`, and `tratamiento`. It stays below NewsData's 100-character
query limit.

With `BRONZE_DEDUP_ENABLED=1`, each run reads prior CSVs under `bronze/` and
skips any `record_id` already stored there and also removes duplicates between
countries in the current run. This prevents the same canonical URL from
returning in multiple runs while the API's latest feed covers overlapping
48-hour windows. If a run finds no new rows, it returns `no_new_rows` and does
not create an empty Bronze CSV. `NEWSDATA_MAX_PAGES` controls pages per country group. The production value is
`2`: with `NEWSDATA_COUNTRY_GROUP_SIZE=5`, a full 19-country pass makes at most
8 NewsData requests (four country groups x two pages), while the function stops
early as soon as it has 50 new rows. When two Vault keys are configured, groups
use the keys in round-robin order and fail over to another key if a request
fails. Each extra page is another API credit. `NEWSDATA_COUNTRY_DELAY` spaces country
group requests, while `NEWSDATA_PAGE_DELAY` spaces pages within a country group
and `NEWSDATA_MAX_RETRIES` retries temporary HTTP 429 responses. With
`NEWSDATA_REQUIRE_TARGET_ROWS=0` writes the available new rows even when the
batch has fewer than 50; set it to `1` only when an incomplete CSV must be
rejected. A target of 50 keeps the Bronze batch small enough for the next body
extraction step to run within the OCI Functions timeout. The query broadens the
medical vocabulary and must remain at most 100 characters because that is the
NewsData API limit. `NEWSDATA_MAX_SECONDS` is an internal 150-second budget and
`NEWSDATA_TIME_BUFFER` reserves 10 seconds before OCI's 300-second synchronous
limit and the Resource Scheduler's shorter synchronous wait. If the budget is reached, the function writes the rows collected so far
when `NEWSDATA_REQUIRE_TARGET_ROWS=0` and leaves the remaining countries for a
later scheduled run. Increase `NEWSDATA_TARGET_ROWS` or
`NEWSDATA_MAX_PAGES` only after measuring elapsed time and checking the account's
credit balance.

The request metadata records `requests_made`, `pages_fetched`,
`articles_received`, `total_results`, `elapsed_seconds`,
`time_budget_reached`, and the key slots used. `total_results` is not the CSV
row count: the latter is reduced by country
validation and `record_id` deduplication. NewsData's latest endpoint can expose
the same article again during its recent-news window, so the Bronze history is
always consulted before a row is written. See the full operational limits in
[`docs/limits_and_timing.md`](../../../docs/limits_and_timing.md) and the
[NewsData documentation](https://newsdata.io/documentation).

The function writes objects like:

```text
bronze/newsdata_20261004T000000Z_ab12cd34.csv
```

The CSV columns are:

```text
record_id,source_name,canonical_url,published_at,title,
subtitle_or_bajada,topic,selection_status,retrieved_at,country
```

No API key is stored in the CSV. The Function must use a resource principal
with permission to read every Vault secret listed in `NEWSDATA_SECRET_OCIDS`
and create objects in `mednews-data` whose name matches `bronze/*`.
