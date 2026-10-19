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
4. `retrieve-pubmed-evidence` procesa todos los claims elegibles del
   `claims.jsonl` disparador. Recupera como máximo cinco candidatos PubMed por
   claim, ordena el resultado y hace Embed 4 sobre todos los abstracts
   utilizables; reutiliza la traducción y el embedding de un PMID/abstract
   idéntico dentro de la corrida.
   La salida es `silver/translate/translate_<run_id>.jsonl`, una línea JSONL
   por claim con sus candidatos y estados.
5. `continue-evidence-translation` consume ese JSONL, traduce los abstracts
   pendientes y publica `silver/evidence/evidence_<run_id>.jsonl`. No vuelve a
   consultar NewsData, Body, Claims, PubMed ni Embed 4.
6. Los fallos de Body, Claims, retrieval, traducción y Evidence se copian en
   `silver/rejected/<stage>/` con el mismo `run_id`; los artefactos normales
   conservan también el estado de cada fila.
7. Label Studio crea una tarea por `record_id + claim_id`. La persona revisora
   decide materialidad, relación y veredicto de cada claim; luego se agrega una
   única etiqueta de noticia para Gold y entrenamiento.

Los artefactos operativos se guardan en Object Storage: `bronze/`,
`silver/body/`, `silver/claims/`, `silver/translate/`, `silver/evidence/` y
`silver/rejected/`. Bronze y Body siguen siendo CSV; Claims, Translate y
Evidence usan nombres planos e inmutables por corrida:
`claims_<run_id>.jsonl`, `translate_<run_id>.jsonl` y
`evidence_<run_id>.jsonl`, con el manifest de la etapa cuando corresponde.
`evidence_status=OK` significa únicamente que la recuperación, el embedding y
la traducción produjeron candidatos utilizables; no es una etiqueta de verdad.

La extracción del cuerpo conserva estados `OK`, `CUERPO_INSUFICIENTE` y errores
HTTP para que una URL bloqueada no desaparezca del registro. El Function deja
un margen interno antes del límite de 300 segundos y continúa con las filas
pendientes en la siguiente ejecución.

## Multi-claim, JSONL y criterio de etiqueta

Una noticia puede tener cero, uno o muchos claims. No se recorta por defecto:
`MAX_CLAIMS_PER_ARTICLE=0` y `EVIDENCE_MAX_ROWS=0` obligan a conservar y
procesar todos los claims del objeto disparador. El límite de cinco minutos se
protege reduciendo candidatos por claim, no descartando claims. Los manifiestos
registran `COMPLETE`, `PARTIAL` o `FAILED`. Una falla de LLM, PubMed, embedding
o traducción queda explícita en el artefacto normal y se replica en
`silver/rejected/`; el estado `ERROR` no se reintenta implícitamente. La entrega
de los claims listos a Label Studio y la carga del export anotado a OCI se
realizan manualmente.

La agregación humana aplica esta regla explícita: un claim central o material
refutado vuelve la noticia `REFUTADA`; si todos los claims centrales/materiales
están respaldados, queda `RESPALDADA`; cualquier evidencia insuficiente o
conflicto queda `NO_DETERMINABLE`. Solo la primera y la segunda entran al
dataset binario, una vez por `record_id`.

El contrato, los ejemplos JSONL y la compatibilidad temporal están en
[`docs/multiclaim_jsonl_plan.md`](docs/multiclaim_jsonl_plan.md).

## Automatización OCI

Para una corrida v1 limpia, permanece activo solo el schedule de producción de
las 08:00 Lima junto con OCI Events. El schedule de las 20:00 y los drains
históricos siguen inactivos: no son pasos de la cadena normal y podrían
recorrer objetos históricos. La importación a Label Studio y la carga del
export humano a OCI se hacen manualmente. No se deben crear datos de prueba
productivos ni reprocesar CSV existentes durante la corrida.

Con la configuración v1, OCI Events encadena el proceso así:

```text
fetch-newsdata -> bronze/*.csv
                       |
                       v
               extract-news-body -> silver/body/*.csv
                                      |
                                      v
                              extract-claims -> silver/claims/claims_*.jsonl
                                                   |
                                                   v
                              retrieve-pubmed-evidence -> silver/translate/translate_*.jsonl
                                                   |
                                                   v
                              continue-evidence-translation -> silver/evidence/evidence_*.jsonl
```

Events procesa únicamente el objeto que lo disparó. Claims, retrieval y
traducción v1 no
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

Después de `silver/evidence/`, revisa los claims elegibles en el proyecto de
Label Studio. Exporta las anotaciones completadas y súbelas manualmente a
`silver/annotations/labelstudio/`. El Function `build-labelstudio-training`
genera el CSV binario de Gold en OCI; no se sube un JSONL Silver directamente a
Gold. La guía completa está en
[`labelstudio/README.md`](labelstudio/README.md).

Solo las filas revisadas por una persona, con evidencia trazable y etiqueta
binaria final, pasan a Gold y al split de entrenamiento.

El export crudo de Label Studio se conserva como respaldo en
`silver/annotations/labelstudio/`; no se copia dentro de `experiments/` ni se
usa directamente como dataset. La Function de Gold cruza Body, Evidence y
anotaciones de la misma corrida y publica `gold/training/`. En este repositorio
solo se conserva `gold/training/training_20261019.csv`; los demás artefactos se
descargan manualmente cuando se necesitan.

### Reintentos y recuperación

Las Functions conservan los errores técnicos en `silver/rejected/` y aplican
los reintentos internos definidos por cada etapa. No se mantienen scripts
locales de backfill, conversión o retry que puedan volver a publicar objetos y
duplicar la cadena de eventos. Para el modelo local se descarga directamente
el CSV Gold desde OCI y se abre el notebook.

## Archivos del repositorio

```text
experiments/models/
  README.md                       contrato del experimento reducido
  01_tfidf_naive_bayes/
    baseline.ipynb                único baseline reproducible
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
  README.md                      guía para el equipo
oci/functions/                   Functions desplegadas en OCI
```

Los CSV generados no se versionan por defecto. Este PR incluye únicamente el
snapshot Gold `20261019` para reproducir el notebook; las credenciales y los
artefactos temporales siguen fuera de Git.

## Primer modelo local

El entrenamiento empieza únicamente con el CSV compacto diario
`gold/training/training_<YYYYMMDD>.csv` descargado desde OCI, no con
`silver/evidence`. Ese CSV contiene
`record_id`, `title`, `body` y `label`; el notebook exige suficientes filas
binarias revisadas por una persona, con `title` y `body` completos. Usa
`title + body` como texto, `RESPALDADA` y `REFUTADA` como clases; no usa claims,
PMIDs, similitudes, fuente ni tema como variables predictoras.

En el MacBook instala las dependencias opcionales y ejecuta:

```bash
python3 -m pip install -e '.[training]'
jupyter notebook experiments/models/01_tfidf_naive_bayes/baseline.ipynb
```

En la primera celda se configura `GOLD_CSV`. El notebook crea
`model.joblib`, `metrics.json`, `split_manifest.csv` y `predictions.csv` en
`artifacts/training_runs/notebook_tfidf_naive_bayes/`, y muestra precisión,
recall, Macro-F1 y matriz de confusión por partición.

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
