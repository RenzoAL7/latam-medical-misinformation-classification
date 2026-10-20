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
unigramas y bigramas, y entrena `MultinomialNB`. Usa un único split
estratificado 70 % entrenamiento y 30 % prueba, con seed 42. Es suficiente
para explicar el baseline sin introducir rutas de modelos, validación ni
artefactos adicionales.

Para ejecutar el flujo técnico se requieren al menos dos noticias por clase:

```bash
python3 -m pip install -e '.[training]'
jupyter notebook experiments/models/01_tfidf_naive_bayes/baseline.ipynb
```

En la celda de configuración cambia `GOLD_CSV` a la ruta local del CSV. El
notebook muestra la distribución, el reporte de clasificación y la matriz de
confusión; no guarda un modelo ni escribe archivos derivados.

Dos noticias refutadas bastan para demostrar el flujo, pero no para evaluar un
detector de desinformación. Por ello el notebook muestra una advertencia si hay
menos de diez noticias en alguna clase y los resultados deben presentarse solo
como demostración técnica.
