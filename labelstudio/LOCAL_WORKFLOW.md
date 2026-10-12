# Flujo local: JSON de Label Studio a primer modelo

Este flujo se ejecuta localmente desde la raíz de este repositorio.

Solo necesitas una carpeta local. No subas sus archivos a GitHub:

```text
labelstudio/artifacts/
  labelstudio_export.json       # lo descargas de Label Studio
  silver_evidence_*.csv         # Evidence con el body de las noticias
  gold_labels.csv               # lo genera el exportador
  gold_reviewed.csv             # lo genera el validador
  gold_training.csv             # lo genera el validador
```

`labelstudio/artifacts/` ya está ignorada por Git. Solo se suben a `main` los
scripts Python y esta guía.

## 1. Descargar el JSON

En Label Studio usa **Export > JSON** y copia el archivo descargado a:

```text
labelstudio/artifacts/labelstudio_export.json
```

Coloca también en esa misma carpeta el CSV de Evidence correspondiente. El
JSON de Label Studio no contiene el `body` completo porque se excluyó para que
la interfaz sea más ligera. El exportador busca automáticamente los CSV cuyo
nombre contiene `evidence`.

## 2. Convertir JSON a CSV

Desde la raíz del repo ejecuta:

```bash
python3 labelstudio/export_annotations.py \
  --input-json labelstudio/artifacts/labelstudio_export.json \
  --output labelstudio/artifacts/gold_labels.csv \
  --evidence-input labelstudio/artifacts/
```

El script toma la última anotación completada por `record_id`, conserva la
etiqueta y la relación de evidencia, y escribe una fila por noticia. Revisa
el resultado impreso: si `missing_body` es mayor que cero, falta el CSV de
Evidence y todavía no se debe entrenar.

## 3. Validar las etiquetas humanas

```bash
python3 labelstudio/validate_gold.py \
  --input labelstudio/artifacts/gold_labels.csv \
  --reviewed-output labelstudio/artifacts/gold_reviewed.csv \
  --training-output labelstudio/artifacts/gold_training.csv \
  --rejected-output labelstudio/artifacts/gold_rejected.csv \
  --fail-on-reject
```

Para entrenamiento solo pasan `RESPALDADA` y `REFUTADA`. Las filas
`NO_DETERMINABLE`, `EXCLUIDA` o incompletas quedan fuera del CSV binario.

## 4. Entrenar el primer modelo

Instala las dependencias una vez:

```bash
python3 -m pip install scikit-learn joblib
```

Con al menos 100 filas binarias validadas:

```bash
python3 scripts/03_train_baseline.py \
  --input labelstudio/artifacts/gold_training.csv \
  --output-dir labelstudio/artifacts \
  --run-name pilot_100 \
  --limit 100 \
  --min-rows 100 \
  --seed 42
```

El modelo usa únicamente `title + body`. Claims, PMIDs, similitudes, fuente y
tema se conservan como metadata de auditoría, pero no son variables de entrada
del primer modelo.
