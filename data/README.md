# Datos locales

No se versionan en este repositorio cuerpos de noticias, URLs, exportaciones,
anotaciones humanas ni salidas generadas. Mantén los insumos en data/raw o
fuera del checkout y usa rutas explícitas al ejecutar los scripts.

El formato mínimo para entrenamiento y evaluación supervisados es:

    record_id,source_text,reference_claim

Para CLAIMGEN-BART adaptado también se aceptan title y body; el script forma
una entrada controlada de contexto y entradilla. Esa adaptación se documenta
como transferencia y no como reproducción literal de citancias científicas.
