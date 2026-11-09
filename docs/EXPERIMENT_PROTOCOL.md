# Protocolo del experimento

## 1. Alcance de la entrega

Esta rama evalúa únicamente extracción y normalización de un claim central
desde una noticia médica en español. Traducción, generación de consultas,
recuperación PubMed y ranking se mantienen como fases posteriores.

La unidad de análisis es una noticia. Cada salida debe ser declarativa,
verificable y autocontenida. No es un veredicto de verdad, una traducción ni
una consulta de búsqueda.

La noticia conserva `record_id` y `source_hash`. Las predicciones conservan
además `model_id`, checkpoint y dispositivo. Los textos, anotaciones y
predicciones individuales se mantienen fuera de Git.

## 2. Datos y anotación

Desde el 08/11/2026 la recolección corre en OCI dos veces al día. El flujo
conserva noticias con cuerpo disponible, título y longitud mínima, descarta las
repetidas y carga las tareas en Label Studio; guarda en Object Storage las
seleccionadas y las descartadas con su motivo. El notebook 00, que armaba lotes
manuales, se retiró el 09/11/2026 y sigue en el historial de Git.

El export de Label Studio se descarga en CSV. El notebook 01 lo audita y el
notebook 02 selecciona con semilla fija 500 tareas aptas con afirmación completa
y texto fuente no duplicado, y escribe un manifiesto final de `record_id` en
`artifacts/02_conjuntos/claims_500/`. El manifiesto asigna los splits fijos: 400 para
train, 50 para validation y 50 para test. Las tareas no seleccionadas no entran
al conjunto de referencia ni a las
métricas. El conjunto final no se sobrescribe: se congela antes de entrenar para
impedir que el test se elija por sus resultados.

La anotación produce un claim central fiel a la fuente, no una opinión del
anotador, un veredicto, una consulta PubMed ni varios claims. Una noticia sin
claim médico se clasifica como no apta y se reemplaza antes de cerrar el conjunto de referencia.
El manifiesto excluye claims de menos de cinco o más de 80 palabras y evita
record_id, fuentes o targets duplicados entre los splits.
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

Para que la tabla compare arquitectura y preentrenamiento —no una cantidad
distinta de oportunidades de actualización— la corrida base usa un presupuesto
común: cinco épocas como máximo, batch efectivo de 8, learning rate de 3e-5, sin
warmup ni weight decay y decodificación determinista con cuatro beams. Cada
modelo conserva el checkpoint base y, en mBART-50, los códigos de idioma
español. El checkpoint con menor pérdida de validación dentro de esas cinco
épocas se evalúa una sola vez sobre el test. Las configuraciones de #49, #80 y
#81 son antecedentes para el diseño, no recetas trasladadas sin ajuste: esos
trabajos usan corpus, idiomas y tamaños de entrenamiento diferentes.

1. T5S: `vgaraujov/t5-base-spanish`, modelo encoder-decoder preentrenado en
   español.
2. BARTO: `vgaraujov/bart-base-spanish`, modelo BART preentrenado en español.
   Se ajusta con el mismo contrato source-to-claim y cuatro beams.
3. mBART-50: `facebook/mbart-large-50`, modelo multilingüe usado en trabajos
   de normalización multilingüe. Se establece `src_lang=es_XX`,
   `tgt_lang=es_XX` y `forced_bos_token_id` para que la salida sea española.
4. uMT5-Base: `google/umt5-base`, modelo multilingüe. En la corrida base se
   ejecuta bajo el mismo presupuesto controlado.

### 3.1 Variantes de entrenamiento (Acta de Asesoría N.° 1, 07/11/2026)

La asesora recomendó modificar la metodología de entrenamiento incorporando
Early Stopping y LoRA. Desde el 08/11/2026 los cuatro modelos pueden entrenarse
con las mismas particiones en estas variantes. La de 5 épocas es la corrida
base y las demás todavía no se han ejecutado sobre el conjunto completo.

| Variante | Entrenamiento | Antecedente en Drive |
| --- | --- | --- |
| `epocas_5` | Ajuste completo, 5 épocas; reutiliza `claims_500_v1` | #99 |
| `epocas_10` | Ajuste completo, 10 épocas | #100 |
| `epocas_20` | Ajuste completo, 20 épocas | #80 |
| `lora` | Solo adaptadores LoRA, r=8, alpha=32, dropout=0,1, 10 épocas, learning rate de 3e-4 | #75, #80, #100 |

- **Early Stopping:** se detiene tras tres épocas sin mejora en la pérdida de
  validación y se conserva el mejor checkpoint (#66). `epocas_5` se entrenó sin
  este mecanismo; como ningún modelo tuvo su mejor punto en la época 1, la
  paciencia de tres épocas no habría cambiado el checkpoint elegido.
- **LoRA:** se aplica a las capas de atención query, key, value y output (#75).
  El learning rate de 3e-4 coincide con #97 y se comprobó solo con validación
  en BARTO: tras dos épocas, la pérdida fue 2,26 con 3e-5 y 1,68 con 3e-4.
- **Selección:** para cada modelo se elige la variante con menor pérdida de
  validación. El test no interviene en esa elección; sus métricas solo se
  reportan.
- **Lectura de resultados:** cambiar épocas o aplicar LoRA modifica las
  oportunidades de actualización, así que la comparación entre variantes mide
  el efecto del entrenamiento dentro de cada modelo. Los antecedentes usan
  corpus, idiomas y tamaños diferentes, y no son recetas trasladadas sin ajuste.

La configuración base está en configs/models.json, las variantes en
`src/claim_extraction/workflow.py`, y ambas quedan registradas en el
manifiesto de cada corrida. Todos se ejecutan localmente con Transformers y
PyTorch; no se llama a una API generativa.
Los claims antiguos producidos por una API no forman parte del corpus ni de la
evaluación. La plataforma de anotación solo aporta un export CSV manual al
flujo local de notebooks; no hay sincronización ni servicios externos durante
el entrenamiento o la inferencia.

## 4. Métricas

Con reference_claim del test se reportan exactamente tres métricas:

- METEOR: métrica primaria usada por los papers de Drive #49 y #80 en
  CheckThat! 2025.
- ROUGE-L F1: coincidencia de subsecuencia más larga.
- BERTScore F1: similitud semántica contextual entre el claim generado y la
  referencia en español.

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

