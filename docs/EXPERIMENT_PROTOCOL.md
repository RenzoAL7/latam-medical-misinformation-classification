# Protocolo del experimento

## 1. Alcance de la entrega

Esta rama evalúa únicamente extracción y normalización de un claim central
desde una noticia médica en español. Traducción, generación de consultas,
recuperación PubMed y ranking se mantienen como fases posteriores.

Desde el 25/11/2026, la tesis hasta Seminario 1 cubre dos resultados y termina
en ellos:

1. Las métricas de los cuatro modelos de deep learning (sección 3) sobre el
   test: METEOR, ROUGE-L F1 y BERTScore F1, con sus diagnósticos y su desglose
   por tema (sección 4).
2. La clasificación de las noticias por tema (sección 4.1).

No se plantean hipótesis de investigación. El 27/11/2026, después de ver test,
se agregaron dos líneas base sin entrenamiento y una prueba de Wilcoxon con
corrección de Holm sobre METEOR (sección 4.2). Esos análisis se reportan como
exploratorios.

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

El export de Label Studio se descarga en CSV. Desde el 26/11/2026 la anotación
también puede hacerse en una hoja de cálculo: `src/claim_extraction/anotacion_csv.py`
la valida con los valores de la interfaz y la convierte a las columnas del export.
El notebook 01 lo audita y el notebook 02 ordena con semilla fija las tareas aptas
con afirmación completa, tema y texto fuente no duplicado, y escribe un manifiesto
final de `record_id` en `artifacts/02_conjuntos/claims_1271/`. El manifiesto
asigna los splits fijos, estratificados por tema: 1017 para train, 127 para
validation y 127 para test. El diseño original preveía 1500 noticias (1200/150/150);
la anotación de las 3500 tareas dio 1295 aptas y 1271 pares únicos, y el conjunto
se cerró con todas ellas en la misma proporción 80/10/10 (registro de la sección
4.2). `claims_500` (400/50/50) fue un piloto y sus archivos locales se retiraron el
26/11/2026. Las tareas no seleccionadas no
entran al conjunto de referencia ni a las métricas. El conjunto final no se
sobrescribe: se congela antes de entrenar para impedir que el test se elija por
sus resultados.

Desde el 09/11/2026 el notebook 02 también detecta casi-duplicados: dos noticias
se enlazan si la similitud de Jaccard de sus trigramas de palabras, o la de los
bigramas de sus claims, es de al menos 0,5. Todas las noticias de un grupo van a
train, de modo que validation y test no tengan una versión parecida en otro
split. En `claims_500`, que no aplicó esta regla, 2 de 50 noticias de test y 2 de
50 de validation superaban ese umbral en su texto frente a train.

La anotación produce un claim central fiel a la fuente, no una opinión del
anotador, un veredicto, una consulta PubMed ni varios claims. Los criterios para
decidir si una noticia es médica, marcar la oración central y sus oraciones de
contexto, asignar el tema y medir el acuerdo entre anotadores están en
`docs/GUIA_ANOTACION.md` (v1 desde el 24/11/2026; v1.1, que quita las reglas sin
respaldo en los papers, desde el 26/11/2026); la interfaz de Label Studio, en
`configs/labelstudio_proyecto9.xml`. Una noticia sin
claim médico se clasifica como no apta y se reemplaza antes de cerrar el conjunto de referencia.
El manifiesto excluye claims de menos de cinco o más de 80 palabras y evita
record_id, fuentes o targets duplicados entre los splits.
Los cuatro modelos reciben el mismo `source_text`, construido con título y
cuerpo; la bajada no es entrada del modelo y, desde la guía v1.1, tampoco se muestra
al anotar. Por ello el claim anotado debe quedar respaldado por ese texto de
entrada.

Sem1 no incluye revisión humana adicional ni ablations de entrada. Compara los
cuatro modelos bajo el mismo conjunto de referencia y los mismos splits. Desde el
27/11/2026 la comparación incluye además dos líneas base sin entrenamiento: el
título y la primera oración del cuerpo (sección 4.2).

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

**Limitación de longitud de entrada.** Los cuatro modelos reciben como máximo 512
tokens de título y cuerpo; el resto se trunca. En `claims_500`, entre el 84 % y el
91 % de las noticias supera ese límite según el tokenizador. La proporción de
palabras de contenido del claim presentes en el texto baja de 75 % en la noticia
completa a 60–64 % en la parte que recibe el modelo. Es una medida léxica
aproximada, pero indica que parte de los claims se apoya en texto que el modelo
no ve.

### 3.1 Variantes de entrenamiento (Acta de Asesoría N.° 1, 07/11/2026)

La asesora recomendó modificar la metodología de entrenamiento incorporando
Early Stopping y LoRA. Desde el 08/11/2026 los cuatro modelos pueden entrenarse
con las mismas particiones en estas variantes. En `claims_1271` se entrenaron las
16 combinaciones (4 modelos × 4 variantes): las 12 de ajuste completo el
26/11/2026 y las 4 de LoRA el 27/11/2026.

| Variante | Entrenamiento | Antecedente en Drive |
| --- | --- | --- |
| `epocas_5` | Ajuste completo, 5 épocas; solo en `claims_500` reutiliza `claims_500_v1` | #99 |
| `epocas_10` | Ajuste completo, 10 épocas | #100 |
| `epocas_20` | Ajuste completo, 20 épocas | #80 |
| `lora` | Solo adaptadores LoRA, r=8, alpha=32, dropout=0,1, 10 épocas, learning rate de 3e-4 | #75, #80, #100 |

- **Early Stopping:** se detiene tras tres épocas sin mejora en la pérdida de
  validación y se conserva el mejor checkpoint (#66). En `claims_500`, `epocas_5`
  se entrenó sin este mecanismo; como ningún modelo tuvo su mejor punto en la
  época 1, la paciencia de tres épocas no habría cambiado el checkpoint elegido.
  En `claims_1271` todas las variantes se entrenan desde cero con early stopping.
- **Precisión y microbatch (26/11/2026):** las corridas de `claims_1271` se
  entrenan en GPU CUDA con precisión mixta bfloat16 y microbatch 2 con acumulación
  de 4 pasos, de modo que el batch efectivo sigue siendo 8. La precisión, el
  microbatch y el dispositivo quedan en los manifiestos de cada corrida y modelo.
- **Corridas:** cada corrida se nombra por conjunto y variante, y todas usan la
  misma semilla (sección 4.2). Su manifiesto registra una huella de los `record_id`
  de cada split, y los notebooks 03 y 04 se detienen si se intenta reanudar o
  evaluar una corrida hecha con otros datos o con otra semilla. Al terminar cada
  modelo se conserva solo el mejor checkpoint (`best/`) y su historial de pérdidas
  (`trainer_state.json`).
- **LoRA:** se aplica a las capas de atención query, key, value y output (#75).
  El learning rate de 3e-4 coincide con #97 y se comprobó solo con validación
  en BARTO: tras dos épocas, la pérdida fue 2,26 con 3e-5 y 1,68 con 3e-4.
  En `claims_1271` la primera corrida de LoRA no terminó. Se repitió el
  27/11/2026 con la misma semilla y la misma configuración, después de pasar
  `use_reentrant=False` al gradient checkpointing en
  `src/claim_extraction/workflow.py`. Ese ajuste se hizo en la PC de
  entrenamiento y se copió a este repositorio el mismo día.
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

Lectura de los resultados:

- **Medias:** cada métrica se reporta como la media de cada modelo sobre las
  noticias de test. Las diferencias entre modelos se describen con las medias.
  Solo las siete comparaciones de METEOR de la sección 4.2 se someten a una
  prueba de significancia, como análisis exploratorio.
- **METEOR:** se calcula con la configuración por defecto de NLTK, cuyo stemmer y
  WordNet están en inglés, por lo que no reconoce sinónimos ni todas las
  flexiones del español. Se declara como limitación.
- **Por tema:** METEOR, ROUGE-L y BERTScore se desglosan por el `tema` anotado, con
  el número de noticias de cada grupo. Con grupos pequeños la media es poco
  estable.

## 4.1 Clasificación de temas

Paso secundario y sin fine-tuning, en el notebook 05. Asigna una de siete
categorías con seis combinaciones: TF-IDF,
`paraphrase-multilingual-MiniLM-L12-v2` y
`paraphrase-multilingual-mpnet-base-v2`, cada una con regresión logística y SVM
lineal, ambas con pesos por clase. La entrada es el título y los primeros 1000
caracteres del cuerpo.

La combinación se elige por el F1 macro medio de una validación cruzada de 5 folds
sobre las 1144 noticias de train y validation. Cada fold conserva la proporción de
temas y mantiene juntas las noticias casi duplicadas. TF-IDF se ajusta solo con la
parte de entrenamiento de cada fold. La combinación elegida se reentrena con train
y validation, y test se evalúa una sola vez con ella. Esta validación cruzada sigue
a los trabajos de clasificación de noticias #102, #103 y #104. El clasificador no
evalúa la veracidad de la noticia ni participa en la extracción de claims.

## 4.2 Comparación final de los modelos (fijada el 10/11/2026)

Estas reglas se fijan antes de crear `claims_1500`, para que no dependan de sus
resultados. Todo cambio posterior se anota en el registro del final de esta
sección; si ocurre después de ver test, se reporta como exploratorio.

**Comparación.** Se reportan los cuatro modelos, cada uno con la variante elegida
por pérdida de validación, evaluados sobre las mismas 127 noticias de test.
METEOR es la métrica principal; ROUGE-L y BERTScore se reportan como
complementarias. Las tablas y figuras se guardan en
`artifacts/05_resultados_finales/<conjunto>/`. Estos resultados comparan similitud
con la referencia; no prueban exactitud clínica ni veracidad.

**Semilla.** Todas las variantes y modelos usan una sola semilla, 20261103.
Repetir el entrenamiento con otras semillas mediría cuánto cambia el resultado al
reentrenar, pero dos semillas más sumarían 8 entrenamientos a los 16 de las
variantes, unas 32 horas estimadas en el equipo local. La semilla única se declara
como limitación. Antecedentes en Drive: #66 reporta media y desviación con 10
semillas, #43 y #51 repiten con varias semillas, y #69 usa una sola semilla y lo
declara como limitación.

**Líneas base y prueba de Wilcoxon (agregadas el 27/11/2026, exploratorias).**
Se decidieron después de ver test, así que se reportan como análisis
exploratorio. No cambian la elección de variantes ni la semilla.

- **Líneas base.** Ninguna se entrena:
  - el título limpio de la noticia;
  - la primera oración del cuerpo limpio que tenga al menos cinco tokens.

  Se evalúan con las mismas tres métricas y las mismas 127 noticias de test.
- **Prueba.** Wilcoxon de rangos con signo, bilateral, sobre el METEOR por
  noticia. Las diferencias iguales a cero se excluyen (`zero_method="wilcox"`).
  Se usa la variante elegida de cada modelo.
- **Comparaciones.** Son siete:
  - cada modelo frente a la primera oración, la línea base con mayor METEOR medio;
  - el modelo con mayor METEOR medio en test frente a los otros tres. Con las
    cuatro variantes entrenadas es mBART-50 (variante `lora`); sobre las 12
    combinaciones de ajuste completo era BARTO.

  Los valores p se ajustan con el método de Holm sobre las siete.
- **Qué se reporta.** La diferencia media, cuántas noticias gana, pierde o
  empata el primer sistema, la correlación biserial de rangos r y el p
  ajustado.
- **Dónde se calcula.** En `baseline.py`, que todavía no está versionado en
  este repositorio. Calcula también intervalos bootstrap, que no se reportan.

**Reglas.**

- Test no interviene en la elección de variante de `claims_1271`.
- La semilla y la regla de elección de variantes no cambian después de mirar test.
- Una corrida que falla por un error técnico se repite con la misma semilla. Una
  corrida no se repite por tener un mal resultado.
- El test de `claims_500` ya se vio. Sus análisis son un piloto y no se mezclan con
  los de 400 noticias ni con los de `claims_1271`.

**Registro de cambios.**

| Fecha | Cambio | Motivo | ¿Antes o después de ver test? |
| --- | --- | --- | --- |
| 10/11/2026 | Se adoptan esta sección, las semillas y la validación cruzada en temas | Medir la estabilidad del entrenamiento y comparar los modelos con pruebas pareadas | Antes de crear `claims_1500`. El test de `claims_500` ya se había visto |
| 10/11/2026 | Se usa una sola semilla en lugar de tres, y los resultados finales se guardan en `artifacts/05_resultados_finales/` | Las repeticiones costarían unas 32 horas y no cambiarían la lectura de las comparaciones | Antes de crear `claims_1500` |
| 25/11/2026 | Se retiran las hipótesis y pruebas estadísticas (Friedman, Wilcoxon, Holm), los intervalos bootstrap, las diferencias pareadas y METEOR con stemmer español | La tesis hasta Seminario 1 termina en las métricas de los cuatro modelos y en la clasificación de temas | Antes de los resultados finales de `claims_1500` |
| 26/11/2026 | El conjunto final es `claims_1271` (1017/127/127) en lugar de `claims_1500` (1200/150/150); las semillas de selección y split pasan a `sem1_reference_selection_1271_v1` y `sem1_fixed_split_1271_v1` | Las 3500 tareas anotadas dieron 1295 aptas y 1271 pares únicos; se usan todas con la proporción 80/10/10 prevista | Antes de entrenar. Ningún modelo ha visto este test |
| 27/11/2026 | Se agregan dos líneas base sin entrenamiento (título y primera oración) y una prueba de Wilcoxon con corrección de Holm sobre METEOR en siete comparaciones (sección 4.2); Friedman y los intervalos bootstrap siguen fuera | La rúbrica de la Nota 3 valora las comparaciones con métodos de referencia y los análisis estadísticos (ítem 1.5) | Después de ver test de `claims_1271` en las 12 combinaciones de ajuste completo. Se reportan como exploratorios |
| 27/11/2026 | Se completa la variante `lora` en los cuatro modelos. Por pérdida de validación, mBART-50 pasa a usar `lora` (0,658 frente a 0,716 con 20 épocas); los otros tres mantienen su variante. Las siete comparaciones se recalculan con mBART-50 como modelo de mayor METEOR medio | La regla de elección no cambió: faltaba la cuarta variante. Su primera corrida no terminó y se repitió con la misma semilla tras ajustar el gradient checkpointing (sección 3.1) | La configuración de LoRA estaba fijada desde el 08/11 y se eligió solo por validación. El test de las 12 combinaciones de ajuste completo ya se había visto; las comparaciones siguen siendo exploratorias |

## 5. Fuera del alcance de Sem1

La tesis hasta Seminario 1 termina en las métricas de los cuatro modelos y en la
clasificación de temas, más el análisis exploratorio de la sección 4.2. Quedan
fuera las hipótesis de investigación, las pruebas estadísticas distintas de las
siete comparaciones de la sección 4.2 (por ejemplo, Friedman), los intervalos de
confianza por bootstrap, los análisis de sensibilidad de métricas y de robustez
con otras semillas. También la revisión humana ciega, otros baselines
extractivos, las ablations de entrada, la extracción multi-claim, la traducción, generación de
consultas, recuperación PubMed y ranking. No se reportan como resultados de esta
entrega.

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

