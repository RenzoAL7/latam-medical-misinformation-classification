# Baseline de entrenamiento local

## Contrato de entrada

El archivo debe ser `output/training.csv`, producido por
`scripts/04_build_training_dataset.py` después de reunir Evidence, las
anotaciones humanas y el Body de la misma corrida. Debe contener, como mínimo:

| Columna | Papel |
| --- | --- |
| `record_id` | Identificador único y trazabilidad; no es una feature. |
| `title` | Primera parte del texto de entrada. |
| `body` | Segunda parte del texto de entrada. |
| `label` | Objetivo: `RESPALDADA` o `REFUTADA`. |
| `dataset_split` | Se completa en el manifiesto de la corrida. |

El builder conserva los claims en `claim_reviews.csv` y la decisión por noticia
en `article_labels.csv`, pero el CSV `training.csv` que consume este script
queda reducido a esas cinco columnas. `NO_DETERMINABLE`, `EXCLUIDA` y
`PENDIENTE_REVISION` y `PENDIENTE_EVIDENCIA` nunca entran al entrenamiento
binario.

## Modelo piloto

`scripts/03_train_baseline.py` concatena `title` y `body`, transforma el texto
con TF-IDF de unigramas y bigramas, y entrena una regresión logística con pesos
balanceados. El split fijo es 70 % train, 15 % validation y 15 % test, con seed
42. También calcula una línea base de clase mayoritaria para contextualizar las
métricas.

Con 100 filas binarias validadas:

```bash
python3 -m pip install -e '.[training]'
python3 scripts/03_train_baseline.py \
  --input /ruta/privada/gold_training.csv \
  --output-dir artifacts/training_runs \
  --run-name pilot_100 \
  --limit 100 \
  --min-rows 100 \
  --seed 42
```

La salida queda en `artifacts/training_runs/pilot_100/` y contiene:

- `model.joblib`: pipeline TF-IDF + regresión logística;
- `metrics.json`: configuración, conteos, métricas y matriz de confusión;
- `split_manifest.csv`: asignación reproducible de cada `record_id`;
- `predictions.csv`: predicción por fila para revisar errores.

El resultado del piloto sirve para la presentación y para detectar problemas
de datos. No debe reportarse como desempeño final con un test de solo 15 filas.
