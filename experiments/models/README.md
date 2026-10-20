# Único notebook de modelo

Para la exposición se conserva un solo baseline reproducible:

```text
experiments/models/
└── 01_tfidf_naive_bayes/
    └── baseline.ipynb
```

El notebook usa `title + body` de una fila por noticia y entrena TF-IDF con
unigramas/bigramas seguido de `MultinomialNB`. No usa claims, evidencia,
fuente, país ni similitudes como features. La etiqueta binaria debe ser humana
y estar limitada a `RESPALDADA` y `REFUTADA`.

El flujo se explica en cinco pasos: cargar Gold, preparar el texto, separar
70/30, entrenar y revisar las métricas. No guarda modelos ni genera artefactos
adicionales: es deliberadamente un baseline sencillo para mostrar el proceso.

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
columnas, muestra la distribución de clases, crea un split estratificado 70/30
y muestra el reporte de clasificación y la matriz de confusión.

Las métricas son solo demostrativas mientras el conjunto tenga pocos casos
`REFUTADA` o dependa de una sola fuente. El snapshot actual tiene dos ejemplos
`REFUTADA`: alcanza para ejecutar el flujo técnico, pero no para afirmar que el
modelo sea confiable.
