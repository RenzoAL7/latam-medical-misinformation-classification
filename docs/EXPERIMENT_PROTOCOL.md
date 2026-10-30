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

El export de Label Studio se descarga en JSON. Un script selecciona con semilla
fija 300 tareas aptas con claim completo y texto fuente no duplicado, y escribe
un manifiesto final de `record_id`. El manifiesto asigna los splits fijos: 240
para train, 30 para validation y 30 para test. Las tareas no seleccionadas no
entran al Gold ni a las métricas. El manifiesto no se sobrescribe: se congela
antes de entrenar para impedir que el test se elija por sus resultados.

La anotación produce un claim central fiel a la fuente, no una opinión del
anotador, un veredicto, una consulta PubMed ni varios claims. Una noticia sin
claim médico se clasifica como no apta y se reemplaza antes de cerrar el Gold.
El script de entrenamiento rechaza claims vacíos y record_id duplicados.
Los cuatro modelos reciben el mismo `source_text`, construido con título y
cuerpo; la bajada se conserva como contexto de anotación, no como entrada del
modelo. Por ello el claim anotado debe quedar respaldado por ese texto de
entrada.

Sem1 no incluye revisión humana adicional, baselines extractivos ni ablations
de entrada. Su comparación se limita a los cuatro modelos bajo el mismo
conjunto de referencia y los mismos splits.

## 3. Modelos y adaptación

Los cuatro modelos se ajustan mediante fine-tuning supervisado sobre el mismo
contrato noticia→claim. Esta decisión sigue el enfoque supervisado de los
papers de Drive #49, #80 y #81 para pares de texto y claim anotados. No se
reporta una comparación zero-shot con checkpoints base: los enfoques zero-shot
de #49 usan LLMs instruccionales con prompting, no estos cuatro modelos base.

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
evaluación. La plataforma de anotación solo aporta un export JSON manual al
conversor local; no hay sincronización ni servicios externos durante el
entrenamiento o la inferencia.

## 4. Métricas

Con reference_claim del test se reportan exactamente tres métricas:

- METEOR: métrica primaria usada por los papers de Drive #49 y #80 en
  CheckThat! 2025.
- ROUGE-L F1: coincidencia de subsecuencia más larga.
- Exact match normalizado: coincidencia estricta después de normalizar espacios,
  mayúsculas y puntuación.

Sin reference_claim:

- tasa de salidas vacías;
- tasa de salidas de menos de cinco tokens;
- longitud media;
- tasa de duplicados.

Los diagnósticos sin referencia describen la ejecución, no calidad, fidelidad
ni exactitud factual. No se calcularán métricas de referencia contra claims
generados por otro modelo.

## 5. Fuera del alcance de Sem1

La revisión humana ciega, los baselines de título/primera oración, las
ablations de entrada, la extracción multi-claim, la traducción, generación de
consultas, recuperación PubMed y ranking se reservan para una fase posterior.
No se reportan como resultados de esta entrega.

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

