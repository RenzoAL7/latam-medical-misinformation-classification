# Protocolo del experimento

## 1. Alcance de la entrega

Esta rama evalúa únicamente extracción y normalización de un claim central
desde una noticia médica en español. Traducción, generación de consultas,
recuperación PubMed y ranking se mantienen como fases posteriores.

La unidad de análisis es una noticia. Cada salida debe ser declarativa,
verificable y autocontenida. No es un veredicto de verdad, una traducción ni
una consulta de búsqueda.

La noticia conserva record_id y source_hash. Las predicciones conservan además
model_id, checkpoint, texto de entrada, dispositivo, parámetros de generación
y fecha de ejecución. Los textos y anotaciones generados se mantienen fuera de
Git.

## 2. Datos y anotación

El CSV de MedNews se filtra a filas con body_available=SI,
extraction_status=OK, título no vacío y cuerpo de al menos 400 caracteres. Se
deduplica por record_id. La salida es un JSONL local de candidatos.

El export no contiene reference_claim. Por tanto, antes de entrenar o reportar
METEOR/ROUGE se debe anotar un subconjunto y dividirlo en train y validation.
La anotación debe producir un claim central fiel a la fuente, no una opinión del
anotador ni un veredicto. El script de entrenamiento rechaza claims vacíos y
record_id duplicados.

No se exige revisión humana adicional para la comparación automática si ya
existe un conjunto de referencia anotado con el contrato anterior. La revisión
ciega es opcional y solo se añade si se desean medir las propiedades humanas
evaluadas por CLAIMGEN-BART.

## 3. Modelos y adaptación

1. T5S: `vgaraujov/t5-base-spanish`, modelo encoder-decoder preentrenado en
   español. Se ajusta con entrada `source_text`, cinco épocas, learning rate
   3e-5 y cuatro beams.
2. BARTO: `vgaraujov/bart-base-spanish`, modelo BART preentrenado en español.
   Se ajusta con el mismo contrato source-to-claim y cuatro beams.
3. mBART-50: `facebook/mbart-large-50`, modelo multilingüe usado en trabajos
   de normalización multilingüe. Se establece `src_lang=es_XX`,
   `tgt_lang=es_XX` y `forced_bos_token_id` para que la salida sea española.
4. uMT5-Base: se conserva la configuración española de MMA at CheckThat! 2025:
   ajuste post-a-claim, 20 épocas, learning rate 5e-4, weight decay 0.01,
   warmup de 90 pasos y cinco beams. La acumulación de gradientes conserva el
   batch efectivo mientras se adapta a la memoria local.

La configuración ejecutable está en configs/models.json. Todos se ejecutan
localmente con Transformers y PyTorch; no se llama a una API generativa.
Los claims antiguos producidos por una API no forman parte del corpus ni de la
evaluación. Label Studio tampoco forma parte del flujo de esta rama.

## 4. Métricas

Con reference_claim:

- METEOR: métrica primaria compatible con la comparación de los trabajos de
  CheckThat!.
- ROUGE-L F1: coincidencia de subsecuencia más larga.
- Exact match normalizado: diagnóstico complementario.

Sin reference_claim:

- tasa de salidas vacías;
- tasa de salidas de menos de cinco tokens;
- longitud media;
- tasa de duplicados.

Los diagnósticos sin referencia describen la ejecución, no calidad, fidelidad
ni exactitud factual. No se calcularán métricas de referencia contra claims
generados por otro modelo.

## 5. Revisión humana opcional

Si se activa, se congela una muestra común con predicciones de los cuatro
modelos. La hoja ciega asigna códigos M01, M02, etc., mezcla las filas y
separa model_key.json. Se puede puntuar en escala 1 a 5:

- atomicidad;
- fluidez;
- descontextualización;
- fidelidad a la noticia.

El script no convierte estas puntuaciones en métricas automáticas ni las
presenta como independientes si el mismo investigador realizó toda la revisión.
Si solo se necesita la evaluación automática de la fase del sábado, esta hoja
no es necesaria.

## 6. Regla de reproducibilidad

No se ejecuta entrenamiento sobre el CSV de noticias como si ya tuviera labels.
No se llama fine-tuned a un checkpoint base. Cada corrida conserva la
configuración del registro, la semilla y el dispositivo. Los modelos se
descargan a la caché local y no se versionan sus pesos.

## 7. Fuentes primarias

- https://aclanthology.org/2024.lrec-main.1283/
- https://huggingface.co/vgaraujov/t5-base-spanish
- https://huggingface.co/vgaraujov/bart-base-spanish
- https://huggingface.co/facebook/mbart-large-50
- https://arxiv.org/abs/2509.11496
- https://ceur-ws.org/Vol-4038/paper_88.pdf

