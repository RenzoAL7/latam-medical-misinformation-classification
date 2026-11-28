# Extracción de afirmaciones médicas centrales en español

Este repositorio contiene el flujo local de Seminario 1 para transformar una
noticia médica en español de Latinoamérica en una única **afirmación médica
central**, declarativa, verificable y autocontenida.

La tesis hasta Seminario 1 termina en dos resultados: las métricas de los
cuatro modelos de deep learning que extraen la afirmación y la clasificación de
las noticias por tema. No plantea hipótesis de investigación. Desde el
27/11/2026 incluye, como análisis exploratorio, dos líneas base sin
entrenamiento y una prueba de Wilcoxon con corrección de Holm sobre METEOR
(sección 4.2 del protocolo).

Los resultados agregados están en
[Resultados de Seminario 1: `claims_1271`](#resultados-de-seminario-1-claims_1271).

No clasifica noticias como verdaderas o falsas. Tampoco recupera evidencia,
genera consultas, hace ranking ni extrae múltiples afirmaciones. La
`reference_claim` es una afirmación de referencia anotada para entrenar y
evaluar el modelo; no equivale a una validación de veracidad médica.

## Flujo operativo: notebooks 01 → 05

El flujo se ejecuta en Jupyter. Los notebooks 01 a 04 son secuenciales; el 05
solo necesita los splits del 02.

| Paso | Notebook | Entrada | Salida local |
| --- | --- | --- | --- |
| 01 | `01_auditar_export_labelstudio.ipynb` | export CSV más reciente de Label Studio | auditoría y candidatas aptas |
| 02 | `02_construir_splits_train_val_test.ipynb` | candidatas auditadas | conjunto cerrado de 1271: 1017 train, 127 validation, 127 test |
| 03 | `03_entrenar_modelos.ipynb` | los tres CSV cerrados | checkpoints y predicciones locales de test |
| 04 | `04_evaluar_y_visualizar_modelos.ipynb` | predicciones y `test.csv` | métricas de cada variante y resultados finales de los cuatro modelos |
| 05 | `05_clasificar_temas.ipynb` | los tres CSV cerrados | clasificador de temas elegido y su evaluación |

Los notebooks 01–02 son los únicos que se necesitan antes de entrenar. Los
notebooks 03–04 reutilizan funciones comprobables de `src/claim_extraction/`,
pero la secuencia de trabajo y sus parámetros se ven directamente en cada
notebook.

## Conjunto vigente: claims_1271 y clasificación de temas

**Estado (26/11/2026):** las 3500 tareas del proyecto 9 están anotadas en una
hoja de cálculo (`src/claim_extraction/anotacion_csv.py` la valida y la convierte
al formato del export que lee el notebook 01). Resultaron 1295 aptas con claim y
tema; tras quitar 24 claims repetidos quedan 1271 pares únicos. Como no se
llegó a las 1500 previstas, el conjunto se cerró con las 1271 en la misma
proporción 80/10/10 del diseño original. Las 16 combinaciones de modelo y
variante se entrenaron con este conjunto el 26 y el 27/11/2026 (ver «Variantes de
entrenamiento»).

El flujo agrega un paso secundario para identificar el tema de cada noticia:

```text
1. Recolección
2. Anotación: aptitud, tema y claim
3. Conjunto final: 1017 train / 127 validation / 127 test
   ├── 4A. Clasificación de temas
   └── 4B. Entrenamiento de claims
5. Evaluación: METEOR global y por tema
```

El conjunto se guarda como `claims_1271`. `claims_1500` sigue definido en el código, pero no existe.
La extracción de claims sigue siendo el aporte principal.

La clasificación de temas asigna una de siete categorías: Cáncer,
Cardiometabólicas, Nutrición y suplementos, Salud mental y neurología,
Infecciosas e inmunidad, Sistema de salud y políticas, y Otra. No usa
fine-tuning ni evalúa la veracidad de la noticia. Su diseño es:

- **Representación del texto:** `paraphrase-multilingual-MiniLM-L12-v2`,
  `paraphrase-multilingual-mpnet-base-v2` y TF-IDF como línea base.
- **Clasificadores:** regresión logística y SVM lineal, ambos con pesos por
  clase porque los temas están desbalanceados.
- **Entrada:** título y comienzo del cuerpo.
- **Selección:** la mejor de las seis combinaciones por F1 macro medio en una
  validación cruzada de 5 folds sobre train y validation.

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

## Recolección de noticias

Las noticias llegan solas a Label Studio. A las 08:00 y 20:00 de Lima, un flujo
en OCI consulta NewsData, extrae los cuerpos, descarta las repetidas y deja
hasta 50 tareas por corrida en Object Storage. Un timer de la VM las importa al
proyecto de Label Studio. El código propio de la selección de lotes y del importador
está en [`oci_pipeline/`](oci_pipeline/README.md).

El notebook 00, que armaba lotes manuales, se retiró el 09/11/2026 y sigue
disponible en el historial de Git.

## 01–02. Export, auditoría y splits cerrados

Cuando termines de anotar, descarga el export CSV de Label Studio y cópialo a:

```text
artifacts/01_labelstudio/exports/
```

El notebook 01 detecta automáticamente el CSV más reciente y conserva una
auditoría con su nombre y hash. El notebook 02 usa las filas aptas con
`reference_claim`, descarta duplicados exactos de texto fuente o afirmación,
envía a train las noticias casi duplicadas y crea, sin fechas en el nombre, estas
rutas fijas:

```text
artifacts/02_conjuntos/claims_1271/
├── reference_claims.csv
├── train.csv
├── validation.csv
├── test.csv
├── manifest_final_1271.csv
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
pérdida de `validation.csv` y recién entonces genera predicciones para los
casos de `test.csv`. Cada corrida se nombra por conjunto y variante, todas con la
misma semilla, y el notebook se detiene si se intenta reanudar una corrida hecha con
otros datos. De cada modelo se conserva solo el mejor checkpoint y su historial de
pérdidas.

Con *Run All*, el notebook 03 entrena las cuatro variantes de los cuatro modelos
en un solo kernel y libera la memoria del acelerador entre un modelo y otro. Si
un modelo falla, por ejemplo por falta de memoria, el error queda en el
`run_status.json` de su variante, el notebook sigue con los demás y la última
celda lista los fallos. Al volver a ejecutarlo con `RESUME = True` se reutiliza
lo terminado y solo se reintenta lo que falta. `VARIANTS_TO_RUN` y
`MODELS_TO_RUN` permiten limitar la ejecución.

El notebook 04 también se ejecuta completo con *Run All* y tiene dos partes. La
parte A recorre las variantes que ya tienen predicciones completas y guarda,
dentro de la corrida de cada una:

- METEOR (métrica principal),
- ROUGE-L precisión, recall y F1,
- BERTScore precisión, recall y F1,
- diagnósticos de salidas vacías, muy cortas y repetidas,
- métricas por tema anotado,
- curvas de entrenamiento y validación.

La parte B corre sola cuando las cuatro variantes están evaluadas; si falta alguna,
se detiene e indica cuál. Toma cada modelo con la variante elegida por validación y guarda en
`artifacts/05_resultados_finales/` las tablas y figuras de la tesis:

```text
artifacts/05_resultados_finales/claims_1271/
├── tablas/
│   ├── 01_distribucion_particiones.csv
│   ├── 02_variantes_elegidas.csv
│   ├── 03_metricas_finales.csv
│   ├── 04_metricas_por_tema.csv
│   ├── 05_diagnosticos_generacion.csv
│   └── 06_resumen_tokens.csv
└── figuras/
    ├── 01_comparacion_metricas.png
    ├── 02_curvas_entrenamiento_validacion.png
    ├── 03_distribucion_longitudes.png
    ├── 04_tokens_noticias.png
    └── 05_tokens_afirmaciones.png
```

La elección de variantes y la semilla están fijadas en la sección 4.2 de
[`docs/EXPERIMENT_PROTOCOL.md`](docs/EXPERIMENT_PROTOCOL.md). Las diferencias
entre modelos se describen con las medias. Solo las siete comparaciones
exploratorias de esa sección se someten a una prueba de Wilcoxon con Holm.

Estos puntajes miden similitud entre una predicción y la afirmación de
referencia. No prueban exactitud clínica ni veracidad de la noticia. Los
cuerpos, anotaciones, predicciones individuales y pesos permanecen bajo
`artifacts/`, que está ignorado por Git. Solo los agregados revisados pueden
publicarse en `docs/results/`.

### Variantes de entrenamiento

El notebook 03 permite entrenar los cuatro modelos con las mismas particiones
en cuatro variantes, definidas en `src/claim_extraction/workflow.py`:

| Variante | Entrenamiento | Learning rate |
| --- | --- | --- |
| `epocas_5` | Ajuste completo, 5 épocas | 3e-5 |
| `epocas_10` | Ajuste completo, 10 épocas | 3e-5 |
| `epocas_20` | Ajuste completo, 20 épocas | 3e-5 |
| `lora` | Solo adaptadores LoRA (r=8, alpha=32, dropout 0,1), 10 épocas | 3e-4 |

Early Stopping detiene el entrenamiento tras tres épocas sin mejora en la
pérdida de validación. Para cada modelo se conserva la variante con menor
pérdida de validación; `test.csv` no interviene en esa elección. Con
`claims_1271` se entrenaron las 16 combinaciones: las 12 de ajuste completo el
26/11/2026 y las 4 de LoRA el 27/11/2026. La validación eligió 20 épocas para T5S
y uMT5-Base, 5 épocas para BARTO y LoRA para mBART-50. Esas corridas se hicieron
en otra PC y sus artefactos no se copiaron a `artifacts/03_corridas/` de esta
copia. El detalle está en la sección 3.1 de
[`docs/EXPERIMENT_PROTOCOL.md`](docs/EXPERIMENT_PROTOCOL.md).

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

## Resultados de Seminario 1: `claims_1271`

Resultados agregados del conjunto final (1017 train / 127 validation / 127
test). Las 16 combinaciones de modelo y variante se entrenaron el 26 y el
27/11/2026, con la semilla 20261103, en una PC con una GPU NVIDIA RTX 5070 de
12 GB, precisión mixta bfloat16 y batch efectivo 8. Las noticias, anotaciones,
predicciones y pesos no se publican; esta sección solo reporta agregados.

### Pérdida de validación por variante

La variante de cada modelo se elige por la menor pérdida de validación (en
negrita); el test no interviene en esa elección. Entre paréntesis, la época con
menor pérdida.

| Modelo | 5 épocas | 10 épocas | 20 épocas | LoRA |
| --- | ---: | ---: | ---: | ---: |
| T5S | 0.934 (5) | 0.810 (10) | **0.795 (9)** | 0.827 (9) |
| BARTO | **0.710 (2)** | 0.720 (2) | 0.723 (2) | 0.741 (7) |
| mBART-50 | 0.722 (3) | 0.731 (2) | 0.716 (2) | **0.658 (5)** |
| uMT5-Base | 1.438 (5) | 0.971 (10) | **0.841 (18)** | 0.910 (8) |

El early stopping (paciencia 3) detuvo BARTO y mBART-50 en la quinta época en
las variantes de 10 y 20 épocas. Las 12 combinaciones de ajuste completo
sumaron 169 minutos de entrenamiento y las 4 de LoRA, 43. LoRA entrenó entre el
0.30 % (uMT5-Base) y el 0.71 % (T5S) de los parámetros.

### Métricas en test

Medias sobre las 127 noticias de test, con la variante elegida de cada modelo.
Las dos líneas base no se entrenan: copian el título o la primera oración del
cuerpo.

| Sistema | Variante | METEOR | ROUGE-L F1 | BERTScore F1 |
| --- | --- | ---: | ---: | ---: |
| mBART-50 | LoRA | **0.528** | **0.517** | **0.926** |
| BARTO | 5 épocas | 0.511 | 0.501 | 0.924 |
| T5S | 20 épocas | 0.466 | 0.467 | 0.918 |
| uMT5-Base | 20 épocas | 0.427 | 0.415 | 0.910 |
| Primera oración | — | 0.280 | 0.220 | 0.856 |
| Título | — | 0.241 | 0.276 | 0.869 |

La desviación estándar de METEOR entre noticias va de 0.27 a 0.29 en los cuatro
modelos, así que la variación entre noticias es mayor que la diferencia entre
modelos.

### Prueba de Wilcoxon (exploratoria)

Wilcoxon de rangos con signo, bilateral, sobre el METEOR por noticia, con
corrección de Holm sobre siete comparaciones. Se decidió después de ver test,
así que se reporta como análisis exploratorio (sección 4.2 del protocolo).
«Gana / pierde / empata» cuenta noticias desde el primer sistema; r es la
correlación biserial de rangos.

| Comparación | Diferencia media | Gana / pierde / empata | r | p ajustado (Holm) |
| --- | ---: | :---: | ---: | ---: |
| mBART-50 vs. primera oración | +0.248 | 94 / 32 / 1 | 0.72 | < 0.001 |
| BARTO vs. primera oración | +0.232 | 95 / 30 / 2 | 0.69 | < 0.001 |
| T5S vs. primera oración | +0.187 | 89 / 38 / 0 | 0.56 | < 0.001 |
| uMT5-Base vs. primera oración | +0.147 | 83 / 44 / 0 | 0.45 | < 0.001 |
| mBART-50 vs. uMT5-Base | +0.101 | 65 / 42 / 20 | 0.36 | 0.004 |
| mBART-50 vs. T5S | +0.062 | 63 / 43 / 21 | 0.28 | 0.022 |
| mBART-50 vs. BARTO | +0.016 | 52 / 50 / 25 | 0.10 | 0.379 |

Los cuatro modelos superan a la primera oración. mBART-50 supera a uMT5-Base y
a T5S, y no difiere significativamente de BARTO.

### Clasificación de temas

Validación cruzada de 5 folds sobre las 1144 noticias de train y validation:

| Representación | Clasificador | F1 macro (media ± DE) | Exactitud media |
| --- | --- | ---: | ---: |
| mpnet multilingüe | SVM lineal | **0.777 ± 0.028** | 0.809 |
| TF-IDF | SVM lineal | 0.771 ± 0.046 | 0.813 |
| mpnet multilingüe | Regresión logística | 0.754 ± 0.020 | 0.799 |
| TF-IDF | Regresión logística | 0.746 ± 0.034 | 0.786 |
| MiniLM multilingüe | SVM lineal | 0.724 ± 0.034 | 0.781 |
| MiniLM multilingüe | Regresión logística | 0.711 ± 0.018 | 0.760 |

La combinación elegida, mpnet con SVM lineal, se evaluó una sola vez en test:
F1 macro de 0.644 y exactitud de 0.756.

| Tema | n en test | F1 |
| --- | ---: | ---: |
| Cáncer | 21 | 0.905 |
| Infecciosas e inmunidad | 30 | 0.885 |
| Nutrición y suplementos | 11 | 0.741 |
| Cardiometabólicas | 20 | 0.727 |
| Salud mental y neurología | 16 | 0.688 |
| Otra | 27 | 0.565 |
| Sistema de salud y políticas | 2 | 0.000 |

Sistema de salud y políticas tiene solo 2 noticias en test, así que su F1 no es
estable.

Estos puntajes miden similitud con la afirmación de referencia y acuerdo con el
tema anotado. No prueban exactitud clínica ni veracidad de las noticias.

## Resultados anteriores: corrida controlada de 400

La corrida histórica `sem1_20261102_400_claims_controlled_5ep_v2` evaluó los
cuatro modelos sobre 40 noticias de prueba, a partir de un conjunto cerrado de
400 noticias (320 de entrenamiento, 40 de validación y 40 de prueba). Todos
usaron hasta cinco épocas, batch efectivo 8, learning rate 3e-5, sin warmup ni
weight decay y cuatro beams; el checkpoint se escogió por pérdida de validación.

| Modelo | METEOR | ROUGE-L F1 | BERTScore F1 |
| --- | ---: | ---: | ---: |
| T5S | 0.1735 | 0.2209 | 0.8246 |
| BARTO | 0.3150 | 0.3258 | 0.8997 |
| mBART-50 | 0.2786 | 0.2941 | 0.8974 |
| uMT5-Base | 0.2160 | 0.1981 | 0.7427 |

En ese piloto BARTO obtuvo el valor más alto en las tres métricas. Los puntajes
miden similitud entre la predicción y la afirmación de referencia: no prueban
veracidad médica ni exactitud clínica. Consulta los
[resultados agregados y sus figuras](docs/results/sem1_20261102_400_claims_controlled_5ep_v2/README.md)
para el detalle. Esta corrida de 400 no se combina ni se compara directamente
con el piloto `claims_500_v1` de 500 claims.

## Estructura del repositorio

```text
configs/                 Registro de modelos y configuración controlada
docs/                    Protocolo y resultados agregados históricos
notebooks/               Flujo operativo 01–05
oci_pipeline/            Selección de lotes en OCI e importador a Label Studio
src/claim_extraction/    Lógica reusable y comprobable
tests/                   Pruebas sin corpus ni pesos locales
artifacts/               Datos, anotaciones, modelos y resultados locales (ignorado)
  01_labelstudio/        Exports CSV de Label Studio y auditoría del notebook 01
  02_conjuntos/          Conjunto cerrado vigente: claims_1271
  03_corridas/           Modelos, predicciones y evaluación de cada variante
  04_temas/              Comparación y evaluación del clasificador de temas
  05_resultados_finales/ Tablas y figuras de la tesis con la variante elegida
```

Los resultados existentes en `docs/results/` son evidencia agregada de
corridas históricas. La comparación controlada
`sem1_20261102_400_claims_controlled_5ep_v2` corresponde al conjunto previo de
400 claims; no se debe mezclar con el piloto `claims_500_v1` de 500 claims ni
con la de `claims_1271`, ni usar la corrida preliminar no controlada
para comparar arquitecturas.
