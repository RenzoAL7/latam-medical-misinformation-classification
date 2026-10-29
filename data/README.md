# Datos locales

No se versionan en este repositorio cuerpos de noticias, URLs, exportaciones,
anotaciones humanas ni salidas generadas. Mantén los insumos en data/raw o
fuera del checkout y usa rutas explícitas al ejecutar los scripts.

El formato mínimo para entrenamiento y evaluación supervisados es:

    record_id,source_text,reference_claim

Para los cuatro modelos se aceptan también `title` y `body`; el flujo forma
`source_text` cuando sea necesario y mantiene el mismo contrato noticia a
claim. mBART-50 añade internamente los códigos de idioma españoles
`es_XX`.

