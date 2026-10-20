# Corpus de noticias médicas en español de Latinoamérica

Este repositorio contiene las OCI Functions y las herramientas de revisión del
pipeline cloud. Descubre noticias médicas en español de Latinoamérica,
conserva su trazabilidad y prepara evidencia para revisión humana. No decide
automáticamente si una noticia es verdadera o falsa.

## Pipeline OCI en la nube

Las Functions desplegadas en OCI separan las etapas de datos:

1. `fetch-newsdata` consulta NewsData por países de Latinoamérica, incluye
   Puerto Rico (`pr`) y escribe lotes deduplicados en `bronze/`.
2. `extract-news-body` procesa el objeto Bronze que disparó el evento y escribe
   el cuerpo técnico en `silver/body/`.
3. `extract-claims` toma cada cuerpo válido y genera **todos** los claims
   médicos verificables: una línea JSONL por noticia con un arreglo `claims`.
   Cada claim tiene `claim_id`, texto en español e inglés y su consulta PubMed.
4. `retrieve-pubmed-evidence` conserva el inventario completo del
   `claims.jsonl` disparador y recupera el primer tramo seguro de claims.
   Recupera como máximo cinco candidatos PubMed por claim, ordena el resultado
   y hace Embed 4 sobre todos los abstracts utilizables.
5. Si quedan claims, `continue-evidence-retrieval` toma un job JSON inmutable,
   procesa un tramo serial de hasta ocho claims y encadena el siguiente hasta
   publicar un Evidence final consolidado. No se descarta ningún claim ni se
   disparan búsquedas PubMed en paralelo.
6. Si solo falla o se agota el tiempo de **traducción**,
   `continue-evidence-translation` reutiliza exactamente los candidatos/PMIDs
   y embeddings ya guardados, traduce como máximo cuatro claims por invocación
   y encadena el siguiente job. No vuelve a consultar NewsData, Body, Claims,
   PubMed ni Embed 4.
7. Label Studio crea una tarea por `record_id + claim_id`. La persona revisora
   decide materialidad, relación y veredicto de cada claim; luego se agrega una
   única etiqueta de noticia para Gold y entrenamiento.

Los artefactos operativos se guardan en Object Storage: `bronze/`,
`silver/body/`, `silver/claims/` y `silver/evidence/`. Bronze y Body siguen
siendo CSV; Claims y Evidence usan rutas inmutables por corrida:
`silver/claims/run_id=<run_id>/claims.jsonl` y
`silver/evidence/run_id=<run_id>/evidence.jsonl`, cada una con su
`manifest.json`. `evidence_status=OK` significa únicamente que la recuperación,
el embedding y la traducción produjeron candidatos utilizables; no es una
etiqueta de verdad.

La extracción del cuerpo conserva estados `OK`, `CUERPO_INSUFICIENTE` y errores
HTTP para que una URL bloqueada no desaparezca del registro. El Function deja
un margen interno antes del límite de 300 segundos y continúa con las filas
pendientes en la siguiente ejecución.

## Multi-claim, JSONL y criterio de etiqueta

Una noticia puede tener cero, uno o muchos claims. No se recorta por defecto:
`MAX_CLAIMS_PER_ARTICLE=0` y `EVIDENCE_MAX_ROWS=0` obligan a conservar y
procesar todos los claims del objeto disparador. El límite de cinco minutos se
protege encadenando tramos seriales de recuperación, no descartando claims ni
reduciendo candidatos. Los manifiestos registran `COMPLETE`, `PARTIAL` o
`FAILED`. La recuperación reintenta únicamente errores transitorios; una falla
permanente de LLM, PubMed o embedding sigue quedando explícita. Mientras tanto,
el sincronizador puede entregar a Label Studio los claims ya listos.

La agregación humana aplica esta regla explícita: un claim central o material
refutado vuelve la noticia `REFUTADA`; si todos los claims centrales/materiales
están respaldados, queda `RESPALDADA`; cualquier evidencia insuficiente o
conflicto queda `NO_DETERMINABLE`. Solo la primera y la segunda entran al
dataset binario, una vez por `record_id`.

El contrato, los ejemplos JSONL y la compatibilidad temporal están en
[`docs/multiclaim_jsonl_plan.md`](docs/multiclaim_jsonl_plan.md).

## Automatización OCI

El despliegue de producción actual captura a las 08:00 y a las 20:00 Lima.
Ambos schedules solo crean su propio objeto Bronze y OCI Events avanza desde
ese objeto, sin mezclar corridas. Los drains horarios históricos permanecen
inactivos porque sí usan un payload global y podrían recorrer objetos
anteriores. No se deben crear datos de prueba productivos ni reprocesar CSV
existentes durante una corrida.

Con la configuración v1, OCI Events encadena el proceso así:

```text
fetch-newsdata -> bronze/*.csv
                       |
                       v
               extract-news-body -> silver/body/*.csv
                                      |
                                      v
                              extract-claims -> silver/claims/run_id=*/claims.jsonl
                                                   |
                                                   v
                              retrieve-pubmed-evidence -> Evidence padre completo
                                                   |
                              (claims pendientes, serial)
                                                   v
                              continue-evidence-retrieval -> Evidence final consolidado
                                                   |
                              (solo traducción pendiente)
                                                   v
                              continue-evidence-translation -> evidencia final de traducción
```

Events procesa únicamente el objeto que lo disparó. Claims y Evidence v1 no
aceptan un payload vacío como drenaje global: escriben resultados y errores
auditables en la misma corrida, sin mezclar objetos históricos ni reintentar
automáticamente por horario.

## Duración, límites y consumo

Los presupuestos son por invocación: NewsData recolecta hasta 50 noticias y
Body procesa hasta 50 filas. Claims v1 procesa todas las filas válidas del
Body disparador y Evidence v1 todos sus claims, con un presupuesto interno de
270--285 segundos y margen de escritura. Cada claim consulta cinco PMIDs como
máximo y se embeben todos los candidatos con abstract; los objetos compartidos
se cachean dentro de la invocación. La duración real se audita con el manifest,
`elapsed_seconds`, conteos de claims y candidatos, no por un número fijo de
filas.

NewsData trabaja con una ventana reciente, por lo que la misma URL puede
aparecer en dos corridas. Bronze deduplica por `record_id`; `row_count` y
`request_meta` indican cuántas filas se guardaron y cuántas solicitudes se
realizaron. `totalResults` de la API no equivale al número de filas del CSV.
Gemini aplica cuotas por proyecto y modelo (RPM, TPM y RPD), no una cuota
ilimitada por cada clave; las cuatro claves se usan como rotación/failover.
PubMed se limita con una pausa de 0.4 segundos y Embed 4 ordena candidatos sin
convertir la similitud en una etiqueta de verdad.

La tabla completa de duraciones, límites, reintentos, créditos y auditoría está
en [`docs/limits_and_timing.md`](docs/limits_and_timing.md). Incluye enlaces a
la documentación oficial de OCI Functions, Scheduler, Events, Object Storage,
Generative AI, NewsData, Gemini y PubMed.

El recolector registra `elapsed_seconds`; los presupuestos y su operación se
documentan en [`docs/limits_and_timing.md`](docs/limits_and_timing.md).

## Cobertura de NewsData

`fetch-newsdata` solicita `language=es`, `category=health` y una consulta médica
corta compatible con el límite de 100 caracteres de NewsData. La secuencia
actual cubre estos 19 códigos de Latinoamérica: `ar`, `bo`, `cl`, `co`, `cr`,
`cu`, `do`, `ec`, `gt`, `hn`, `mx`, `ni`, `pa`, `pe`, `pr`, `py`, `sv`, `uy` y
`ve`. España y Guinea Ecuatorial quedan fuera del alcance de este corpus; Puerto
Rico se mantiene visible como `pr`.

Cada ejecución intenta hasta 50 noticias nuevas. Usa `size=10` y como máximo dos
páginas por cada uno de los cuatro grupos de países (ocho solicitudes como tope),
consulta los grupos disponibles y selecciona las filas mediante una ronda por
país antes de ordenar el CSV. Así cada país con candidatas tiene una oportunidad
antes de completar el objetivo. Luego deduplica con `record_id` contra todo
`bronze/`. Si las últimas 48 horas no ofrecen 50 URLs nuevas o la API devuelve
menos resultados, se guarda el lote real disponible. No se inventan filas ni se
reutilizan noticias ya almacenadas.

La cobertura geográfica no implica que todos los países tengan una noticia en
cada corrida: NewsData puede no devolver resultados, repetir fuentes o entregar
una noticia cuya URL ya existe. `request_meta` conserva países consultados,
filas candidatas, páginas y slots de API utilizados para auditar cada ejecución.

## Revisión humana y Gold

Después de `silver/evidence/`,
[`labelstudio/import_evidence.py`](labelstudio/import_evidence.py) importa los
claims con evidencia completa (`evidence_status`, `llm_status`,
`embedding_status` y `translation_status` en `OK`) y los resultados terminales
de PubMed (`NO_RESULTS` o `NO_ABSTRACT` con embedding/traducción
`SKIPPED_NO_EVIDENCE`) para triage humano. Excluye fallos técnicos transitorios
y deduplica por `claim_key` (`record_id::claim_id`), no por noticia. Exporta las
anotaciones completadas desde Label Studio y coloca ese archivo junto al
`evidence.jsonl` completo y al Body CSV de la misma corrida. Un solo comando local,
[`scripts/04_build_training_dataset.py`](scripts/04_build_training_dataset.py),
genera los CSV por claim, por noticia y el CSV binario de entrenamiento. Nunca
se sube un JSONL Silver directamente a Gold. La guía completa está en
[`labelstudio/README.md`](labelstudio/README.md).

Solo las filas revisadas por una persona, con evidencia trazable y etiqueta
binaria final, pasan a Gold y al split de entrenamiento.

### Reproceso histórico sin volver a extraer noticias

Para recuperar claims de un lote anterior, se reutiliza el Body ya guardado y
se empieza directamente en Claims. El script
[`scripts/05_prepare_claims_backfill.py`](scripts/05_prepare_claims_backfill.py)
recibe los `record_id` de un export de Label Studio, selecciona solo los Body
con `extraction_status=OK` y prepara CSVs pequeños para subir uno por uno a
`silver/body/`. No consulta NewsData ni vuelve a visitar URLs; el evento inicia
Claims → Evidence → sincronización automática al proyecto Multi-Claim. El
`campaign.json` conserva cualquier noticia faltante o sin Body válido para no
ocultar exclusiones.

Si Evidence agota tiempo únicamente para algunos abstracts, usa
[`scripts/07_prepare_translation_continuation_job.py`](scripts/07_prepare_translation_continuation_job.py)
sobre el `evidence.jsonl` y `manifest.json` parcial. Genera un solo job para
subir a `silver/evidence-translation-jobs/run_id=<run>/job-0001.job.json`; la
Function de continuación no repite PubMed ni embeddings. El helper histórico
[`scripts/06_prepare_evidence_retry.py`](scripts/06_prepare_evidence_retry.py)
queda reservado para fallas anteriores a la traducción, cuando sí se decide
ejecutar Evidence de nuevo de forma explícita.

## Archivos del repositorio

```text
scripts/
  03_train_baseline.py           baseline local TF-IDF + regresión logística
  04_build_training_dataset.py   Evidence + export de Label Studio + Body a CSVs Gold
  05_prepare_claims_backfill.py  Body histórico a lotes controlados de Claims
  06_prepare_evidence_retry.py   Claims fallidos de Evidence a reintento mínimo
  07_prepare_translation_continuation_job.py  job sin PubMed/Embed para traducciones pendientes
tests/
  ...                             pruebas automatizadas
docs/
  data_schema.md                 columnas de Bronze, Silver, claims, evidencia y Gold
  multiclaim_jsonl_plan.md        contrato multi-claim JSONL y migración coordinada
  limits_and_timing.md            duración, cuotas, límites y operación del pipeline
  oci_automation.md               Scheduler, Events, lotes y reintentos
  training_baseline.md            contrato y ejecución del primer modelo
labelstudio/
  config_multiclaim.xml          interfaz de revisión por claim
  import_evidence.py             importa JSONL sin duplicar claim_key
  export_annotations.py          parser interno de revisiones por claim
  aggregate_article_labels.py    regla de agregación reutilizada por el builder
  validate_gold.py               validación reutilizada por el builder
  README.md                      guía para el equipo
oci/functions/                   Functions desplegadas en OCI
```

Los CSV, credenciales y artefactos generados no se versionan en Git.

## Primer modelo local

El entrenamiento empieza únicamente con el CSV compacto `training.csv`
generado por `04_build_training_dataset.py`, no con `silver/evidence`. Ese CSV contiene
`record_id`, `title`, `body`, `label` y `dataset_split`; el piloto exige al menos 100
filas binarias revisadas por una persona, con `title` y `body` completos. Usa
`title + body` como texto, `RESPALDADA=1` y `REFUTADA=0`; no usa claims,
PMIDs, similitudes, fuente ni tema como variables predictoras.

En el MacBook instala las dependencias opcionales y ejecuta:

```bash
python3 -m pip install -e '.[training]'
python3 scripts/03_train_baseline.py \
  --input /ruta/privada/gold_training_2026-10-11.csv \
  --output-dir artifacts/training_runs \
  --run-name pilot_100 \
  --limit 100 \
  --min-rows 100 \
  --seed 42
```

La corrida crea `model.joblib`, `metrics.json`, `split_manifest.csv` y
`predictions.csv` dentro de un directorio ignorado por Git. Reporta el baseline
de mayoría, precisión, recall, Macro-F1, F1 por clase, ROC-AUC cuando ambas
clases aparecen en el split y matriz de confusión. Con 100 filas, la partición
queda en 70 train, 15 validation y 15 test; los resultados deben presentarse
como piloto por el tamaño pequeño del test.

## Verificación local

El flujo operativo se ejecuta en OCI. Para instalar las dependencias de
desarrollo y validar las Functions y herramientas del repositorio:

```bash
make setup
make test
```

## Límites metodológicos

- NewsData y los archivos periodísticos son mecanismos de descubrimiento, no autoridades médicas.
- `topic` conserva el tema o archivo que permitió descubrir la candidata.
- En `Bronze`, `selection_status` resume la decisión de incluir o excluir y su motivo principal.
- La afirmación, URL de evidencia, nota de verificación y etiqueta se completan y validan en `Silver`.
- `Gold` contiene únicamente registros con cuerpo, afirmación, evidencia, etiqueta `RESPALDADA` o `REFUTADA` y validación completada.
- Para entrenar, la entrada será `title + body` y el objetivo será `label`; `source_name` y `topic` se conservan solo para análisis y auditoría.
