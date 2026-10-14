# Plan objetivo: múltiples claims y artefactos JSONL

> Estado de migración (2026-10-13): este documento define el contrato objetivo
> para soportar varias afirmaciones por noticia. Las Functions y los scripts
> desplegados en este checkout todavía usan el contrato CSV descrito en sus
> secciones operativas. La migración de productores, consumidores y reglas de
> Events debe hacerse de forma coordinada; este documento no afirma que el
> cambio ya esté desplegado.

## Decisión de diseño

Una noticia puede producir cero, uno o varios claims. El resultado de la
extracción debe conservarlos como un arreglo `claims`, no sobrescribirlos en un
campo singular ni crear varias filas de entrenamiento para la misma noticia.

La etiqueta final sigue siendo de nivel noticia y la decide una persona. El
modelo binario actual continúa usando `title + body` como entrada y `label` como
objetivo; claims, PMIDs, embeddings y scores sirven para construir y auditar
esa etiqueta, no son variables predictoras del primer modelo.

## Organización por corrida

Cada etapa derivada debe conservar un `run_id` inmutable y no sobrescribir la
salida de otra corrida:

```text
silver/
  claims/
    run_id=<run_id>/
      claims.jsonl
      manifest.json
  evidence/
    run_id=<run_id>/
      evidence.jsonl
      manifest.json
  rejected/
    evidence/
      run_id=<run_id>/
        evidence.jsonl
```

`JSONL` significa que cada línea es un objeto JSON independiente. No se crea
un archivo por noticia ni por claim: se crea un archivo por etapa y corrida.
Esto permite reintentar una corrida, comparar versiones del prompt y mantener
la trazabilidad sin mezclar resultados de ejecuciones distintas.

## `claims.jsonl`

El archivo canónico contiene una línea por noticia procesada, incluso cuando
no se extrae ningún claim. El cuerpo completo permanece en el artefacto de
Body; Claims conserva la referencia al objeto fuente y un hash del texto para
evitar duplicar payload innecesariamente.

```json
{
  "schema_version": "claims.v1",
  "run_id": "20261013T080000Z",
  "record_id": "news_001",
  "source_body_object": "silver/body/body_current_abc.csv",
  "body_sha256": "...",
  "llm_status": "OK",
  "claims_count": 2,
  "claims_truncated": false,
  "claims": [
    {
      "claim_id": "news_001_c01",
      "claim_index": 1,
      "claim_text": "El tratamiento X cura la enfermedad Y.",
      "claim_text_en": "Treatment X cures disease Y.",
      "pubmed_query_en": "treatment X disease Y",
      "is_medical": true,
      "is_claim_eligible": true,
      "claim_type": "treatment",
      "claim_status": "PENDING_REVIEW"
    },
    {
      "claim_id": "news_001_c02",
      "claim_index": 2,
      "claim_text": "La enfermedad Y afecta principalmente a adultos.",
      "claim_text_en": "Disease Y mainly affects adults.",
      "pubmed_query_en": "disease Y adults",
      "is_medical": true,
      "is_claim_eligible": true,
      "claim_type": "epidemiology",
      "claim_status": "PENDING_REVIEW"
    }
  ]
}
```

Reglas mínimas:

- `claims` es siempre un arreglo; para una noticia sin claims se escribe
  `claims: []` y `claims_count: 0`.
- `claims_truncated=true` indica que el límite configurado impidió conservar
  todas las afirmaciones detectadas.
- `llm_status=OK` solo significa que la respuesta estructurada fue válida; no
  significa que la afirmación sea verdadera.
- `claim_id` debe ser único dentro de la corrida y relacionar Claims, Evidence,
  revisión humana y Gold.
- La extracción no asigna `RESPALDADA` ni `REFUTADA`.

## `evidence.jsonl`

El formato canónico conserva una línea por claim para que también queden
representados `NO_RESULTS`, `NO_ABSTRACT` y errores. Los artículos PubMed se
guardan dentro de `candidates`; si después se necesita analítica tabular, se
puede derivar un archivo plano con una línea por candidato sin cambiar la
fuente canónica.

```json
{
  "schema_version": "evidence.v1",
  "run_id": "20261013T080000Z",
  "record_id": "news_001",
  "claim_id": "news_001_c01",
  "evidence_status": "OK",
  "embedding_status": "OK",
  "translation_status": "OK",
  "embedding_model": "cohere.embed-v4.0",
  "embedding_query_field": "claim_text_en",
  "candidates": [
    {
      "pmid": "12345678",
      "rank": 1,
      "title": "Clinical study of treatment X",
      "abstract_en": "...",
      "abstract_es": "...",
      "similarity_score": 0.84,
      "evidence_relation": null
    }
  ]
}
```

`evidence_status=OK` indica que se recuperaron candidatos utilizables; no
indica que PubMed confirme el claim. `similarity_score` ordena evidencia y no
es una probabilidad de verdad. `evidence_relation` (`supports`, `contradicts` o
`unclear`) y la etiqueta de la noticia permanecen vacíos hasta la revisión
humana.

Los claims sin resultados o con abstracts inutilizables se conservan con su
estado terminal y una lista `candidates` vacía. Los fallos transitorios deben
conservarse en el objeto rechazado o reintentable sin perder el `record_id` ni
el `claim_id`.

## `manifest.json`

Cada corrida debe tener un manifest por etapa con, como mínimo:

- `schema_version`, `run_id`, `stage`, `source_object` y versión del código;
- `started_at`, `finished_at`, `elapsed_seconds` y estado de la corrida;
- `articles_processed`, `articles_with_claims`, `articles_without_claims`;
- `claims_total`, `claims_truncated` y conteos por estado;
- `evidence_claims_processed`, `candidates_total` y estados de PubMed,
  traducción y embeddings;
- `rows_left_for_next_run` y errores agregados sin incluir secretos.

El manifest es el lugar para auditar artículos sin claims o claims sin
evidencia; no se deben inferir esos casos contando únicamente las líneas de un
archivo de candidatos.

## Gold y entrenamiento

La revisión puede conservar una estructura de claims y relaciones de evidencia
por noticia, pero la salida compacta de entrenamiento continúa siendo una fila
por `record_id`:

```text
record_id,title,body,label,dataset_split
```

Solo `RESPALDADA` y `REFUTADA` entran al CSV binario. Una noticia con tres
claims no se duplica tres veces en el entrenamiento actual. Si en el futuro se
entrena un verificador de claims, será otro objetivo y otro dataset: una fila
por `claim_id`, con claim y evidencia como entrada.

## Migración segura

1. Implementar primero el esquema validado de `claims.jsonl` y pruebas para
   cero, uno, varios y claims truncados.
2. Cambiar `extract-claims` para escribir una salida por corrida y un manifest.
3. Cambiar el evento y `retrieve-pubmed-evidence` para consumir el objeto JSONL
   disparador y relacionar todo mediante `record_id` y `claim_id`.
4. Adaptar Label Studio y los sincronizadores para leer JSONL; mantener el
   CSV de Gold hasta validar una corrida completa.
5. Ejecutar una corrida de comparación antes de retirar el contrato CSV. No
   mezclar objetos CSV y JSONL bajo el mismo selector de Events.

La compatibilidad temporal debe ser explícita y con prefijos separados; los
artefactos locales generados y los manifiestos con datos no se versionan.
