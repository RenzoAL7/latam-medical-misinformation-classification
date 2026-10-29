# Fixture sintético de desarrollo

`claims_predictions_100.jsonl` contiene **100 casos sintéticos** y una salida
simulada de cada uno de los cuatro extractores locales: 400 filas en total.

Sirve únicamente para desarrollar y probar la fase de traducción y generación
de consultas antes de recibir las predicciones reales. No contiene noticias
reales, no fue generado por los modelos y no debe usarse para reportar métricas
de la tesis.

Para regenerarlo:

```bash
.venv/bin/python scripts/create_synthetic_claim_predictions.py
```

Todas las filas incluyen `synthetic: true` y `checkpoint: synthetic-fixture`.
Cuando lleguen las predicciones reales, reemplaza este archivo por los JSONL
producidos por `scripts/run_inference.py`; no mezcles ambos conjuntos.
