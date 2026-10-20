# Baseline de entrenamiento local

## Contrato de entrada

El notebook
[`experiments/models/01_tfidf_naive_bayes/baseline.ipynb`](../experiments/models/01_tfidf_naive_bayes/baseline.ipynb)
consume `gold/training/training_<YYYYMMDD>.csv`, producido después de reunir Evidence,
anotaciones humanas y Body de la misma corrida. Debe contener, como mínimo:

| Columna | Papel |
| --- | --- |
| `record_id` | Identificador único; no es una feature. |
| `title` | Primera parte del texto de entrada. |
| `body` | Segunda parte del texto de entrada. |
| `label` | `RESPALDADA` o `REFUTADA`, decidida por humanos. |

El export crudo de Label Studio se conserva en `silver/annotations/` como
respaldo. No se usa directamente para entrenar: el ensamblador debe combinarlo
con Evidence y Body, validar la misma corrida y producir Gold.

`NO_DETERMINABLE`, `EXCLUIDA`, `PENDIENTE_REVISION` y
`PENDIENTE_EVIDENCIA` permanecen en los reportes de auditoría, pero nunca
entran al entrenamiento binario.

## Modelo piloto

El notebook concatena `title` y `body`, transforma el texto con TF-IDF de
unigramas y bigramas, y entrena `MultinomialNB`. Usa un split estratificado
70 % train, 15 % validation y 15 % test, con seed 42. Se detiene si no hay
suficientes ejemplos de ambas clases para que el resultado no sea una métrica
engañosa.

Con al menos diez noticias por clase:

```bash
python3 -m pip install -e '.[training]'
jupyter notebook experiments/models/01_tfidf_naive_bayes/baseline.ipynb
```

En la celda de configuración cambia `GOLD_CSV` a la ruta local del CSV. La
salida queda en `artifacts/training_runs/notebook_tfidf_naive_bayes/` y contiene:

- `model.joblib`;
- `metrics.json`;
- `split_manifest.csv`;
- `predictions.csv`.

Dos noticias refutadas no son suficientes para entrenar ni para evaluar un
detector de desinformación. El notebook conserva la distribución real en Gold,
pero exige una cantidad mínima de ambas clases antes de ejecutar.
