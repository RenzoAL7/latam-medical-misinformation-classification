# Flujo automático en OCI

Este código deja las noticias recolectadas listas para etiquetar. No extrae claims
ni evalúa veracidad.

| Carpeta | Dónde corre | Qué hace |
| --- | --- | --- |
| `prepare_labelstudio_batch/` | OCI Functions | Toma un CSV de `silver/body/`, elige hasta 50 noticias con cuerpo válido en round robin por país y descarta las repetidas |
| `labelstudio_batch_importer/` | VM, con un timer cada 15 minutos | Importa los lotes nuevos al proyecto de Label Studio sin repetir `record_id` |

La función guarda en `silver/labelstudio/<run_id>/` las tareas, las filas no usadas
con su motivo y un manifiesto. Un evento de Object Storage la dispara al aparecer un
CSV nuevo en `silver/body/`.

Las funciones previas, `fetch-newsdata` y `extract-news-body`, están desplegadas en
OCI y no se incluyen aquí.

## Configuración

Ningún valor de cuenta ni credencial se guarda en el repositorio.

| Componente | Variables obligatorias | Variables opcionales |
| --- | --- | --- |
| Función | `OBJECT_STORAGE_NAMESPACE`, `OBJECT_STORAGE_BUCKET` | `SILVER_BODY_PREFIX`, `LABELSTUDIO_PREFIX`, `LABELSTUDIO_TARGET_TASKS` |
| Importador | `OBJECT_STORAGE_NAMESPACE`, `LABEL_STUDIO_BATCH_PROJECT_ID`, `LABEL_STUDIO_BATCH_STATE`, `LABEL_STUDIO_REFRESH_TOKEN` | `OBJECT_STORAGE_BUCKET`, `LABEL_STUDIO_URL`, `OCI_REGION` |
