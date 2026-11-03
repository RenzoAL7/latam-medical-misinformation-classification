# Datos locales

No se versionan cuerpos de noticias, URLs, exportaciones de Label Studio,
anotaciones, predicciones ni pesos. Todos esos insumos y salidas viven bajo
`artifacts/`, ignorado por Git, y se producen con los notebooks 00–04.

El contrato mínimo para entrenamiento y evaluación supervisados es:

```text
record_id,source_text,reference_claim
```

Los CSV finales añaden `source_hash`, `claim_hash` y `split` para comprobar que
no haya fugas entre entrenamiento, validación y prueba. `title` y `body` se
conservan para trazabilidad y para construir `source_text` cuando sea
necesario. mBART-50 recibe internamente los códigos de idioma españoles
`es_XX`.

El formato operativo es CSV: export de Label Studio, conjunto de referencia,
splits y predicciones. JSON solo se usa para metadatos pequeños de auditoría y
de corridas locales.
