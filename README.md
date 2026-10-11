# Corpus de noticias médicas en español de Latinoamérica

Este repositorio contiene las OCI Functions y las herramientas de revisión del
pipeline cloud. Descubre noticias médicas en español de Latinoamérica,
conserva su trazabilidad y prepara evidencia para revisión humana. No decide
automáticamente si una noticia es verdadera o falsa.

## Pipeline OCI en la nube

Las Functions desplegadas en OCI separan las etapas de datos:

1. `fetch-newsdata` consulta NewsData por países de Latinoamérica, incluye
   Puerto Rico (`pr`) y escribe lotes deduplicados en `bronze/`.
2. `extract-news-body` recorre los CSV Bronze en orden, combina archivos para
   formar lotes de hasta 50 filas pendientes y escribe los cuerpos en `silver/`.
3. `extract-claims` toma solo cuerpos válidos, genera una afirmación candidata
   en español y añade `claim_text_en` y `pubmed_query_en`. El modo
   `enrich_queries` migra CSV de claims creados antes de esos campos.
4. `retrieve-pubmed-evidence` consulta PubMed para cada claim elegible, guarda
   hasta diez artículos por fila en `silver/evidence/`, los ordena con
   `cohere.embed-v4.0` de OCI y conserva el TF-IDF en español como diagnóstico.
5. La revisión humana verifica la afirmación y la evidencia, asigna la etiqueta
   binaria y decide qué filas pasan a Gold.

Los artefactos operativos del pipeline se guardan en Object Storage:
`bronze/`, `silver/body/`, `silver/claims/` y `silver/evidence/`. Los nombres
son cortos y uniformes: `newsdata_<run_id>.csv`, `body_<run_id>.csv`,
`claims_<run_id>.csv` y `evidence_<run_id>.csv`. Los CSV de evidencia son
candidatos de recuperación, no etiquetas automáticas: `evidence_status=OK`
solo significa que PubMed devolvió artículos.

La extracción del cuerpo conserva estados `OK`, `CUERPO_INSUFICIENTE` y errores
HTTP para que una URL bloqueada no desaparezca del registro. El Function deja
un margen interno antes del límite de 300 segundos y continúa con las filas
pendientes en la siguiente ejecución.

## Automatización OCI

Resource Scheduler inicia `fetch-newsdata` a las 08:00 y 20:00, hora de Lima.
OCI Events encadena el proceso cuando aparece un CSV nuevo:

```text
fetch-newsdata -> bronze/*.csv
                       |
                       v
               extract-news-body -> silver/body/*.csv
                                      |
                                      v
                              extract-claims -> silver/claims/*.csv
                                                   |
                                                   v
                              retrieve-pubmed-evidence -> silver/evidence/*.csv
```

Los tres pasos Silver también tienen una invocación horaria de drenaje. Body
selecciona hasta 50 filas; claims intenta drenar todas las filas elegibles en
solicitudes Gemini de 10; evidence intenta drenar todas las claims elegibles y
conserva hasta 10 artículos PubMed por claim. Si una etapa llega a su presupuesto
de tiempo, cuota o error transitorio, la siguiente hora continúa con lo pendiente.

Las Functions mantienen el estado en los CSV y deduplican por `record_id`. Un
error temporal de red, PubMed o extracción queda auditado y puede reintentarse
en una ejecución posterior; un resultado terminal no se vuelve a procesar.

## Duración, límites y consumo

Los valores actuales son presupuestos por invocación: NewsData recolecta hasta
50 filas con un máximo normal de ocho solicitudes por corrida; body procesa 50
filas; claims intenta todas las filas pendientes en solicitudes de 10; evidence
intenta todas las claims pendientes y conserva hasta 10 artículos PubMed por
claim. Cada Function tiene un presupuesto interno de
aproximadamente 150 segundos y deja 10 segundos antes del límite síncrono de
OCI Functions y del límite práctico del Scheduler. Si una etapa no termina, escribe el trabajo
completado y el drenaje horario continúa con las filas pendientes.

El tiempo extremo a extremo no es un único timeout: un CSV Bronze de 50 filas
requiere hasta 5 solicitudes Gemini de claims. Evidence intenta trabajar todas
las claims resultantes en la misma invocación; si las búsquedas PubMed,
embeddings o traducciones no caben en el presupuesto, el drenaje horario cubre
lo que quede pendiente. La duración real se mide con `elapsed_seconds`,
`row_count` y los objetos escritos.

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

El nuevo presupuesto interno del recolector y su campo `elapsed_seconds` quedan
listos en el PR de documentación; después de fusionarlo hay que desplegar la
imagen de `fetch-newsdata` para que la Function activa los use.

## Flujo local histórico (no es el flujo operativo)

Los scripts y registros de la primera exploración se conservan para
reproducibilidad. El dataset nuevo se recolecta en OCI y no depende de Colab ni
de Google Sheets. Si se ejecuta una prueba local, su salida debe mantenerse
fuera del bucket `mednews-data`.

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

Después de `silver/evidence/`, importa las filas `evidence_status=OK` a Label
Studio con [`labelstudio/import_evidence.py`](labelstudio/import_evidence.py).
El script consulta las tareas existentes y no duplica un `record_id`. Tu amigo
puede usar el mismo proyecto, revisar la relación `supports/contradicts/unclear`
y la etiqueta final, y exportar con
[`labelstudio/export_annotations.py`](labelstudio/export_annotations.py). Antes
de publicar a Object Storage,
[`labelstudio/validate_gold.py`](labelstudio/validate_gold.py) separa el
conjunto auditado de `gold/reviewed/` y el binario apto para entrenamiento de
`gold/training/`; nunca se sube un CSV Silver directamente a Gold. La guía
completa está en [`labelstudio/README.md`](labelstudio/README.md).

Solo las filas revisadas por una persona, con evidencia trazable y etiqueta
binaria final, pasan a Gold y al split de entrenamiento.

## Archivos del repositorio

```text
configs/
  batch.yaml                     fuentes, temas y límites de la corrida
scripts/
  01_collect_newsdata_urls.py    recolector local histórico
  03_train_baseline.py           baseline local TF-IDF + regresión logística
src/peruvian_medical_misinformation/
  __init__.py                    definición del paquete
  newsdata.py                    recolección, normalización y deduplicación
tests/
  test_newsdata_batch.py         pruebas del recolector
  test_batch_contract.py         protege las fuentes y la meta global del batch
  test_repository_hygiene.py     evita versionar datos CSV
docs/
  data_schema.md                 columnas de Bronze, Silver, claims, evidencia y Gold
  limits_and_timing.md            duración, cuotas, límites y operación del pipeline
  oci_automation.md               Scheduler, Events, lotes y reintentos
  methodology.md                 metodología implementada
  training_baseline.md            contrato y ejecución del primer modelo
data/00_control/                 registro temporal de corridas
data/01_candidates/              CSV temporal de cada corrida
reports/runs/                    diagnóstico JSON temporal de cada corrida
labelstudio/
  config.xml                     interfaz de revisión humana
  import_evidence.py             importa evidencia sin duplicar
  export_annotations.py          exporta etiquetas Gold
  validate_gold.py               valida y separa auditoría/entrenamiento Gold
  README.md                      guía para el equipo
```

Los CSV, reportes, credenciales y cuerpos de noticias no se versionan en Git.

## Primer modelo local

El entrenamiento empieza únicamente con el CSV compacto `gold/training`
generado por `validate_gold.py`, no con `silver/evidence`. Ese CSV contiene
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

## Ejecución local opcional

El flujo oficial se ejecuta en OCI. Estos comandos sirven para verificar el
recolector y las herramientas de revisión localmente.

```bash
make setup
make test
make plan
```

Para una corrida local, copia `.env.example` como `.env`, agrega `NEWSDATA_API_KEY` y ejecuta:

```bash
make run EXISTING="/ruta/Corpus noticias medicas.xlsx"
```

La opción `EXISTING` debe apuntar a un Excel o CSV con una columna `record_id`. El archivo se usa solo para excluir noticias conocidas.

## Límites metodológicos

- NewsData y los archivos periodísticos son mecanismos de descubrimiento, no autoridades médicas.
- `topic` conserva el tema o archivo que permitió descubrir la candidata.
- En `Bronze`, `selection_status` resume la decisión de incluir o excluir y su motivo principal.
- La afirmación, URL de evidencia, nota de verificación y etiqueta se completan y validan en `Silver`.
- `Gold` contiene únicamente registros con cuerpo, afirmación, evidencia, etiqueta `RESPALDADA` o `REFUTADA` y validación completada.
- Para entrenar, la entrada será `title + body` y el objetivo será `label`; `source_name` y `topic` se conservan solo para análisis y auditoría.
- El historial de fuentes aceptadas y descartadas está documentado en `docs/source_audit.md`.
