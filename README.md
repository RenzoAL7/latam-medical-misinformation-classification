# Extracción de afirmaciones médicas centrales en español

Este repositorio contiene el flujo local de Seminario 1 para transformar una
noticia médica en español de Latinoamérica en una única **afirmación médica
central**, declarativa, verificable y autocontenida.

No clasifica noticias como verdaderas o falsas. Tampoco recupera evidencia,
genera consultas, hace ranking ni extrae múltiples afirmaciones. La
`reference_claim` es una afirmación de referencia anotada para entrenar y
evaluar el modelo; no equivale a una validación de veracidad médica.

## Flujo operativo: notebooks 00 → 04

El flujo se ejecuta en Jupyter mediante cinco notebooks secuenciales.

| Paso | Notebook | Entrada | Salida local |
| --- | --- | --- | --- |
| 00 | `00_recolectar_noticias.ipynb` | NewsData API y `.env` | candidatos, cuerpos extraídos, balance y tareas para Label Studio |
| 01 | `01_auditar_export_labelstudio.ipynb` | export CSV más reciente de Label Studio | auditoría y candidatas aptas |
| 02 | `02_construir_splits_train_val_test.ipynb` | candidatas auditadas | conjunto cerrado de 400: 320 train, 40 validation, 40 test |
| 03 | `03_entrenar_modelos.ipynb` | los tres CSV cerrados | checkpoints y predicciones locales de test |
| 04 | `04_evaluar_y_visualizar_modelos.ipynb` | predicciones y `test.csv` | métricas, tablas y figuras locales |

Los notebooks 00–02 son los únicos que se necesitan antes de entrenar. Los
notebooks 03–04 reutilizan funciones comprobables de `src/claim_extraction/`,
pero la secuencia de trabajo y sus parámetros se ven directamente en cada
notebook.

## Preparación local

Requiere Python 3.11 o superior. Después de clonar el repositorio, desde su
raíz:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m nltk.downloader wordnet omw-1.4
```

`requirements.txt` instala el proyecto con todos los grupos necesarios:
Jupyter, análisis, entrenamiento, evaluación y pruebas. No subas `.venv` a
Git: contiene binarios y rutas específicas de cada computadora.

Para comprobar la lógica local sin descargar modelos ni corpus:

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider
```

En equipos Apple Silicon, PyTorch usa MPS cuando está disponible; CUDA se usa
cuando existe. Los pesos base se descargan a la caché local de Hugging Face y
no se versionan.

## 00. Recolección de noticias

Crea un archivo `.env` local —ignorado por Git— con:

```text
NEWSDATA_API_KEY=tu_clave
```

El notebook consulta noticias de salud en español, extrae cuerpos con
BeautifulSoup y aplica round robin entre países/territorios de LATAM para
completar las cuotas configuradas. Solo genera `labelstudio_tasks.csv` cuando
cumple el objetivo de balance de esa corrida. El inventario y los cuerpos son
artefactos de trazabilidad, no datos de entrenamiento.

En Label Studio importa `labelstudio_tasks.csv` como **List of tasks**.

## 01–02. Export, auditoría y splits cerrados

Cuando termines de anotar, descarga el export CSV de Label Studio y cópialo a:

```text
artifacts/labelstudio/exports/
```

El notebook 01 detecta automáticamente el CSV más reciente y conserva una
auditoría con su nombre y hash. El notebook 02 usa las filas aptas con
`reference_claim`, descarta duplicados exactos de texto fuente o afirmación y
crea, sin fechas en el nombre, estas rutas fijas:

```text
artifacts/sem1/final/
├── reference_claims.csv
├── train.csv
├── validation.csv
├── test.csv
├── manifest_final_400.csv
├── reserve_candidates.csv
└── split_summary.json
```

El notebook 02 no sobrescribe un conjunto final existente. Así el conjunto de
prueba no puede cambiar después de mirar resultados. Si se necesita comenzar
una nueva selección, se debe conservar la anterior y cambiar explícitamente el
destino en el notebook; no reemplazarla de forma silenciosa.

Cada CSV de split tiene, como mínimo:

```text
record_id,source_text,reference_claim,source_hash,claim_hash,split
```

No se necesita JSONL para este flujo: CSV es el formato de intercambio de
Label Studio, de los splits y de las predicciones locales. Los JSON pequeños
solo registran manifiestos y estado de una corrida.

## 03–04. Entrenamiento y evaluación

El notebook 03 valida que los splits no compartan `record_id`, `source_hash` o
`claim_hash`. Entrena cada modelo con `train.csv`, escoge el checkpoint por
pérdida de `validation.csv` y recién entonces genera predicciones para los 40
casos de `test.csv`.

El notebook 04 calcula:

- METEOR (métrica principal),
- ROUGE-L precisión, recall y F1,
- BERTScore precisión, recall y F1,
- diagnósticos de salidas vacías, muy cortas y repetidas.

Estos puntajes miden similitud entre una predicción y la afirmación de
referencia. No prueban exactitud clínica ni veracidad de la noticia. Los
cuerpos, anotaciones, predicciones individuales y pesos permanecen bajo
`artifacts/`, que está ignorado por Git. Solo los agregados revisados pueden
publicarse en `docs/results/`.

## Modelos y configuración controlada

La configuración ejecutable está en `configs/models.json`. La comparación usa
los mismos datos, splits y presupuesto para:

1. T5S — `vgaraujov/t5-base-spanish`
2. BARTO — `vgaraujov/bart-base-spanish`
3. mBART-50 — `facebook/mbart-large-50`, configurado para `es_XX`
4. uMT5-Base — `google/umt5-base`

El diseño controlado fija hasta cinco épocas, batch efectivo 8, learning rate
3e-5, sin warmup ni weight decay y decodificación con cuatro beams. Consulta
[`docs/EXPERIMENT_PROTOCOL.md`](docs/EXPERIMENT_PROTOCOL.md) para el alcance y
las reglas metodológicas completas.

## Estructura del repositorio

```text
configs/                 Registro de modelos y configuración controlada
docs/                    Protocolo y resultados agregados históricos
notebooks/               Flujo operativo 00–04
src/claim_extraction/    Lógica reusable y comprobable
tests/                   Pruebas sin corpus ni pesos locales
artifacts/               Datos, anotaciones, modelos y resultados locales (ignorado)
```

Los resultados existentes en `docs/results/` son evidencia agregada de
corridas históricas. La comparación controlada vigente es
`sem1_20261102_400_claims_controlled_5ep_v2`; no se deben mezclar con una
corrida nueva ni usar la corrida preliminar no controlada para comparar
arquitecturas.
