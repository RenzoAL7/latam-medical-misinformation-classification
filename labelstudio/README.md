# Revisión humana multi-claim con Label Studio

Label Studio contiene la revisión humana por claim. La similitud de Embed 4 y
TF-IDF solo ordena candidatos; no decide si una noticia es verdadera o falsa.
La etiqueta de entrenamiento se produce en OCI después de completar la
revisión.

## Proyecto

El proyecto actual es **MedNews LATAM — Multi-Claim**. El archivo
[`config_multiclaim.xml`](config_multiclaim.xml) se conserva únicamente como
referencia para recrear la interfaz. No se versionan tokens, archivos de
entorno ni servicios de la VM.

Cada tarea representa un `record_id::claim_id`. La persona revisora decide:

- `claim_materiality`: `central`, `material` o `secondary`;
- `evidence_relation` y `claim_verdict` para claims decisivos con evidencia;
- `NO_DETERMINABLE` para un claim decisivo sin resultados PubMed utilizables;
- una justificación y los PMID seleccionados cuando corresponda.

Los errores técnicos no se convierten en veredictos humanos.

## Flujo manual

1. En la consola de OCI, descarga el Evidence JSONL de la corrida que quieras
   revisar.
2. Trabaja en el proyecto de Label Studio que ya tienes configurado y completa
   la revisión por claim.
3. Usa **Export > JSON** o **JSONL** para descargar las anotaciones terminadas.
4. Sube manualmente ese export a:

   ```text
   silver/annotations/labelstudio/labelstudio_<utc_timestamp>.json
   ```

5. El Function `build-labelstudio-training` toma el export más reciente a las
   `14:00` (`America/Lima`) y publica:

   ```text
   gold/training/training_<YYYYMMDD>.csv
   silver/rejected/training/training_<YYYYMMDD>.jsonl
   ```

6. Descarga manualmente el CSV de `gold/training/` desde la consola de OCI y
   cópialo a `gold/training/` en este repositorio.
7. Abre [`baseline.ipynb`](../experiments/models/01_tfidf_naive_bayes/baseline.ipynb)
   y ajusta `GOLD_CSV` si cambiaste el nombre o ubicación local.

El notebook consume únicamente `record_id`, `title`, `body` y `label`; no usa
claims, PMIDs, similitudes ni fuentes como variables predictoras.
