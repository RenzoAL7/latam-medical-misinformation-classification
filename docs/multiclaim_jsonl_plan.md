# Contrato v1: múltiples claims y artefactos JSONL

> Estado de migración (2026-10-19): el contrato `claims.v1`/`evidence.v1`, las
> Functions x86 y las reglas Events Body -> Claims -> Translate -> Evidence
> están activas. La revisión de Label Studio se alimenta mediante importación
> manual; no se reactivan los drains históricos ni el schedule de las 20:00.
> No mezclar objetos CSV históricos con los selectores JSONL.

## Decisión de diseño

Una noticia puede producir cero, uno o varios claims. El resultado de la
extracción debe conservarlos como un arreglo `claims`, no sobrescribirlos en un
campo singular ni crear varias filas de entrenamiento para la misma noticia.

La etiqueta final sigue siendo de nivel noticia y la decide una persona. El
modelo binario actual continúa usando `title + body` como entrada y `label` como
objetivo; claims, PMIDs, embeddings y scores sirven para construir y auditar
esa etiqueta, no son variables predictoras del primer modelo.

## Organización por corrida

Cada etapa derivada debe conservar un `run_id` inmutable en el nombre del
archivo y no sobrescribir la salida de otra corrida:

```text
silver/
  body/body_<run_id>.csv
  claims/claims_<run_id>.jsonl
  claims/claims_<run_id>.manifest.json
  translate/translate_<run_id>.jsonl
  translate/translate_<run_id>.manifest.json
  evidence/evidence_<run_id>.jsonl
  evidence/evidence_<run_id>.manifest.json
  rejected/
    body/body_<run_id>.csv
    claims/claims_<run_id>.jsonl
    translate/translate_<run_id>.jsonl
    evidence/evidence_<run_id>.jsonl
```

`JSONL` significa que cada línea es un objeto JSON independiente. No se crea
un archivo por noticia ni por claim: se crea un archivo por etapa y corrida.
Esto permite comparar versiones del prompt sin mezclar resultados de
ejecuciones distintas. La v1 no hace drenajes globales: todo el objeto
disparador se procesa en una sola invocación y cualquier degradación queda
explícita en el manifest y en `rejected/`. La etapa Translate usa el JSONL ya
persistido y no vuelve a procesar Claims, PubMed ni embeddings.

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
  "source_body_object": "silver/body/body_run_20261019T080000Z_ab12cd34.csv",
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
      "claim_status": "PENDING_EVIDENCE"
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
      "claim_status": "PENDING_EVIDENCE"
    }
  ]
}
```

Reglas mínimas:

- `claims` es siempre un arreglo; para una noticia sin claims se escribe
  `claims: []` y `claims_count: 0`.
- `MAX_CLAIMS_PER_ARTICLE=0` es el valor obligatorio de v1: no se descarta un
  sexto claim potencialmente decisivo. `claims_truncated=true` solo aparece si
  se configura de forma explícita un límite de emergencia.
- `llm_status=OK` solo significa que la respuesta estructurada fue válida; no
  significa que la afirmación sea verdadera.
- La extracción usa una política de alta cobertura: revisa título, subtítulo,
  cuerpo y citas atribuidas, e incluye afirmaciones de medicina, salud pública
  y sistema sanitario aunque parezcan verdaderas, negativas, polémicas o estén
  atribuidas a otra persona. Conserva negación, modalidad, cifras, causalidad
  y polaridad; no convierte el texto en una versión más positiva.
- `claims: []` requiere haber revisado todo el artículo y significa únicamente
  que no quedó una afirmación factual elegible. No es un veredicto de verdad.
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
  "run_id": "run_20261013T080000Z_a1b2c3d4",
  "evidence_run_id": "run_20261013T080000Z_a1b2c3d4",
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
      "pubmed_rank": 1,
      "title": "Clinical study of treatment X",
      "abstract_en": "...",
      "abstract_es": "...",
      "embedding_similarity": 0.84,
      "translation_status": "OK"
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
estado terminal y una lista `candidates` vacía. Una falla de LLM, Embed 4 o
traducción hace que `evidence_status=ERROR`, por lo que ese claim no llega a
Label Studio como si tuviera evidencia lista. Una respuesta Gemini bloqueada
por `RECITATION` puede usar, dentro de la etapa de traducción y con presupuesto
acotado, un resumen fiel marcado `PARAPHRASED`. Los fallos se copian en el
subdirectorio correspondiente de `silver/rejected/`; no se crean jobs ni
staging.

## `manifest.json`

Cada corrida debe tener un manifest por etapa con, como mínimo:

- `schema_version`, `run_id`, `stage`, `source_object` y versión del código;
- `started_at`, `finished_at`, `elapsed_seconds` y estado de la corrida;
- `articles_processed`, `articles_with_claims`, `articles_without_claims`;
- `coverage.medical_articles_with_claims`,
  `coverage.medical_articles_without_claims` y
  `coverage.nonmedical_articles_without_claims`;
- `claims_total`, `claims_truncated` y conteos por estado;
- `evidence_claims_processed`, `candidates_total` y estados de PubMed,
  traducción y embeddings;
- configuración efectiva (candidatos recuperados y confirmación de que todos
  los abstracts utilizables fueron embedidos),
  estadísticas de caché y errores agregados sin incluir secretos.

El manifest es el lugar para auditar artículos sin claims o claims sin
evidencia; no se deben inferir esos casos contando únicamente las líneas de un
archivo de candidatos.

## Gold y entrenamiento

Label Studio importa una tarea por `record_id + claim_id`. La persona revisora
marca `claim_materiality` (`central`, `material` o `secondary`) y deja una
justificación. Si es `secondary`, no necesita adjudicar evidencia, PMID ni
veredicto; el claim permanece en la auditoría y no determina la etiqueta. Si
es `central` o `material`, también revisa `evidence_relation` y
`claim_verdict`. Para `NO_RESULTS`/`NO_ABSTRACT`, un claim decisivo solo puede
recibir `NO_DETERMINABLE`. Los errores técnicos no se convierten en veredictos.
Después, `scripts/04_build_training_dataset.py` genera una sola fila por
`record_id` solo cuando reúne el Evidence JSONL completo, la exportación de
Label Studio y el Body de la misma corrida. Un resultado terminal
`NO_RESULTS`/`NO_ABSTRACT` sí se puede revisar para clasificar materialidad y,
si el claim es decisivo, adjudicar `NO_DETERMINABLE`. Un fallo técnico de un
claim decisivo, una tarea/revisión humana faltante u otro dato inválido deja la
noticia fuera del CSV binario. Con el lote completo, aplica esta regla:

- algún claim `central`/`material` `REFUTADO` → `REFUTADA`;
- todos los claims `central`/`material` `RESPALDADOS` → `RESPALDADA`;
- al menos un claim decisivo `NO_DETERMINABLE` → `NO_DETERMINABLE`;
- los claims `secondary` quedan en el registro de revisión pero no alteran la
  agregación.

Esta relajación reduce la búsqueda manual de evidencia por claims secundarios;
no reduce por sí sola las consultas a PubMed, porque la Function de Evidence
actual todavía recibe y procesa todos los claims elegibles.

La salida compacta de entrenamiento continúa siendo una fila por noticia:

```text
record_id,title,body,label,dataset_split
```

Solo `RESPALDADA` y `REFUTADA` entran al CSV binario. Una noticia con tres
claims no se duplica tres veces en el entrenamiento actual. Si en el futuro se
entrena un verificador de claims, será otro objetivo y otro dataset: una fila
por `claim_id`, con claim y evidencia como entrada.

## Activación coordinada

1. Mantener inactivos el schedule de las 20:00 y los drains históricos. Para
   una corrida v1 limpia, activar solo el schedule de las 08:00 Lima y las
   reglas Events; la revisión de Label Studio se opera manualmente.
2. Construir y subir las Functions para `linux/amd64`/`GENERIC_X86`; configurar
   `PIPELINE_SCHEMA=v1`, `LLM_MAX_ROWS=0`, `EVIDENCE_MAX_ROWS=0`,
   `PUBMED_CANDIDATES_FETCHED=5`.
3. Mantener la regla Events Claims → retrieval para
   `silver/claims/claims_*.jsonl` y la regla Translate → Evidence para
   `silver/translate/translate_*.jsonl`; Body → Claims mantiene el CSV de Body
   como entrada.
4. Crear o migrar explícitamente un proyecto limpio de Label Studio con
   `config_multiclaim.xml`; importar manualmente el Evidence JSONL en formato
   `jsonl`.
5. Ejecutar una corrida controlada nueva, revisar ambos manifests y completar
   la revisión humana. Exportar después las anotaciones y subirlas manualmente
   a `silver/annotations/labelstudio/`. El resultado define si se mantiene esa
   activación, no si se habilitan automáticamente los schedules históricos.

La compatibilidad temporal debe ser explícita y con prefijos separados; los
artefactos locales generados y los manifiestos con datos no se versionan.
