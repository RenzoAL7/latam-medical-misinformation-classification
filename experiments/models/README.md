# Experimento de modelo

Se conserva un solo modelo reproducible para la tesis:

```text
experiments/models/
└── 01_tfidf_naive_bayes/
    └── baseline.ipynb
```

El notebook usa `title + body` de una fila por noticia y entrena TF-IDF con
unigramas/bigramas seguido de `MultinomialNB`. No usa claims, evidencia,
fuente, país ni similitudes como features. La etiqueta binaria debe ser humana
y estar limitada a `RESPALDADA` y `REFUTADA`.

## Datos

Los artefactos de la corrida se conservan en Object Storage, por ejemplo:

```text
silver/annotations/labelstudio/project_7_<export_id>.json
silver/body/body_<run_id>.csv
silver/evidence/evidence_<run_id>.jsonl
gold/training/training_<YYYYMMDD>.csv
```

El notebook consume solamente `gold/training/training_<YYYYMMDD>.csv` o una
copia local equivalente descargada desde OCI. El snapshot
`training_20261019.csv` queda incluido para reproducir esta corrida.
`NO_DETERMINABLE`, `EXCLUIDA`, `PENDIENTE_REVISION` y
`PENDIENTE_EVIDENCIA` permanecen fuera del entrenamiento binario. El notebook
se detiene si no hay suficientes ejemplos de ambas clases; dos noticias
refutadas no son una base válida para entrenar.

La Function de OCI ya une Body, Evidence y anotaciones de la misma corrida
antes de publicar el CSV. No se debe subir directamente un JSONL Silver a Gold
ni fabricar etiquetas a partir de similitudes.

## Ejecución

Instala la familia de entrenamiento y abre el notebook desde la raíz del
repositorio:

```bash
python3 -m pip install -e '.[training]'
jupyter notebook experiments/models/01_tfidf_naive_bayes/baseline.ipynb
```

En la primera celda de configuración ajusta `GOLD_CSV`. El notebook valida las
columnas, muestra la distribución de clases, crea un split estratificado
70/15/15 y guarda en `artifacts/training_runs/notebook_tfidf_naive_bayes/`:

- `model.joblib`;
- `metrics.json`;
- `split_manifest.csv`;
- `predictions.csv`.

Las métricas deben interpretarse como piloto mientras el conjunto tenga pocos
casos `REFUTADA` o dependa de una sola fuente.
