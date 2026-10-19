# build-labelstudio-training

Scheduled Gold builder for the private Label Studio installation.

Label Studio listens only on `127.0.0.1:18080` inside `PUB_VM`, so this
Function does not receive or store a Label Studio token. A VM-side exporter
uses the protected token and writes one raw export to:

```text
silver/annotations/labelstudio/labelstudio_<utc_timestamp>.json
```

The Function uses the `evidence_run_id` and `source_body_object` references in
that export/Evidence to download only the matching inputs. It writes exactly
one training artifact per Lima calendar day; a retry on the same day replaces
that day's CSV:

```text
gold/training/training_<YYYYMMDD>.csv
silver/rejected/training/training_<YYYYMMDD>.jsonl  # only if needed
```

The date is evaluated in `America/Lima`. There are no `run_id=` directories,
timestamp suffixes or `part-0001` fragments. The scheduled empty payload means
“use the newest Label Studio export”; an Object Storage event may identify a
specific export object.

Required Function configuration:

```text
OBJECT_STORAGE_NAMESPACE=idur1kkrru56
OBJECT_STORAGE_BUCKET=mednews-data
LABELSTUDIO_EXPORT_PREFIX=silver/annotations/labelstudio
SILVER_EVIDENCE_PREFIX=silver/evidence
SILVER_BODY_PREFIX=silver/body
GOLD_TRAINING_PREFIX=gold/training
SILVER_REJECTED_PREFIX=silver/rejected/training
```
