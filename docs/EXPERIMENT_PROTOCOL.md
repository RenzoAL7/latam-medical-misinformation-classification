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
notebook 02 selecciona con semilla fija 1500 tareas aptas con afirmación completa,
tema y texto fuente no duplicado, y escribe un manifiesto final de `record_id` en
`artifacts/02_conjuntos/claims_1500/`. El manifiesto asigna los splits fijos,
estratificados por tema: 1200 para train, 150 para validation y 150 para test.
`claims_500` (400/50/50) se conserva sin cambios. Las tareas no seleccionadas no
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
con las mismas particiones en estas variantes. La de 5 épocas es la corrida
base y las demás todavía no se han ejecutado sobre el conjunto completo.

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
  En `claims_1500` todas las variantes se entrenan desde cero con early stopping.
- **Corridas:** cada corrida se nombra por conjunto, variante y semilla; la semilla
  base no añade sufijo. Su manifiesto registra una huella de los `record_id` de cada
  split, y los notebooks 03 y 04 se detienen si se intenta reanudar o evaluar una
  corrida hecha con otros datos o con otra semilla.
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

Lectura de los resultados:

- **Incertidumbre:** cada media de test se acompaña de un intervalo de confianza
  del 95 % por bootstrap sobre las noticias. Las diferencias entre modelos se
  calculan de forma pareada contra el de mayor media; si el intervalo incluye el
  cero, esos modelos no se distinguen con este test. En `claims_500`, BARTO supera
  a mBART-50 en 0,006 de METEOR con intervalo de −0,044 a 0,057. Las pruebas entre
  los cuatro modelos están en la sección 4.2.
- **Sensibilidad de METEOR:** METEOR se calcula con la configuración por defecto
  de NLTK, cuyo stemmer y WordNet están en inglés, por lo que no reconoce sinónimos
  ni todas las flexiones del español. `meteor_stem_es` repite el cálculo con
  stemmer español como análisis de sensibilidad y no reemplaza a la métrica
  principal.
- **Por tema:** METEOR, ROUGE-L y BERTScore se desglosan por el `tema` anotado, con
  el número de noticias de cada grupo. Con grupos pequeños los intervalos son
  amplios.

## 4.1 Clasificación de temas

Paso secundario y sin fine-tuning, en el notebook 05. Asigna una de siete
categorías con seis combinaciones: TF-IDF,
`paraphrase-multilingual-MiniLM-L12-v2` y
`paraphrase-multilingual-mpnet-base-v2`, cada una con regresión logística y SVM
lineal, ambas con pesos por clase. La entrada es el título y los primeros 1000
caracteres del cuerpo.

La combinación se elige por el F1 macro medio de una validación cruzada de 5 folds
sobre las 1350 noticias de train y validation. Cada fold conserva la proporción de
temas y mantiene juntas las noticias casi duplicadas. TF-IDF se ajusta solo con la
parte de entrenamiento de cada fold. La combinación elegida se reentrena con train
y validation, y test se evalúa una sola vez con ella. Esta validación cruzada sigue
a los trabajos de clasificación de noticias #102, #103 y #104. El clasificador no
evalúa la veracidad de la noticia ni participa en la extracción de claims.

## 4.2 Análisis estadístico (fijado el 10/11/2026)

Estas reglas se fijan antes de crear `claims_1500`, para que no dependan de sus
resultados. Todo cambio posterior se anota en el registro del final de esta
sección; si ocurre después de ver test, se reporta como exploratorio.

**Comparación.** Se comparan los cuatro modelos, cada uno con la variante elegida
por pérdida de validación con la semilla base. La unidad de análisis es la noticia:
los cuatro se evalúan sobre las mismas 150 noticias de test, así que cada comparación
es pareada. METEOR es la métrica principal. ROUGE-L, BERTScore y `meteor_stem_es` se
reportan como complementarias.

**Semillas.** Las repeticiones miden cuánto cambia el resultado al repetir el
entrenamiento con las mismas noticias, algo que el bootstrap sobre test no captura.

- Se usan tres semillas: 20261103 (base), 20261104 y 20261105.
- Solo se repite la variante elegida de cada modelo, con los mismos splits. La
  semilla cambia el orden de las noticias, el dropout y, en LoRA, los valores
  iniciales de los adaptadores.
- Antes de generar predicciones de test de `claims_1500` se puede hacer un piloto con
  `claims_500` y `epocas_5`. Si la desviación entre semillas del METEOR medio de algún
  modelo supera 0,02, se usan cinco semillas, agregando 20261106 y 20261107. Con tres
  semillas, esa desviación aportaría a la diferencia entre dos modelos un ruido de
  0,016, parecido al de muestrear 150 noticias (0,017, estimado con las diferencias
  por noticia de `claims_500_v1`).
- Ninguna semilla se elige por su resultado. Todas las previstas se reportan.

Antecedentes en Drive: #66 reporta media y desviación con 10 semillas, #43 y #51
repiten con varias semillas, y #69 declara la semilla única como limitación.

**Pruebas.** Se aplican con un nivel de significancia de 0,05:

1. El puntaje de cada modelo en cada noticia se promedia entre semillas.
2. Friedman compara los cuatro modelos a la vez.
3. Wilcoxon de rangos con signo, bilateral, compara cada uno de los seis pares.
4. Holm corrige los seis p-valores, por separado en cada métrica.
5. Cada par reporta su diferencia media, un intervalo del 95 % por bootstrap pareado
   sobre las noticias (10 000 réplicas), el efecto rango-biserial y cuántas noticias
   gana cada modelo.

No se usa una prueba t porque no se asume normalidad de las diferencias, ni McNemar
porque METEOR es continuo. Antecedentes en Drive: #87 usa bootstrap pareado por
artículo en extracción de claims de noticias, #51 usa Wilcoxon por ítem, Friedman y
bootstrap de 10 000 réplicas, #66 compara modelos con Wilcoxon y #82 corrige con
Holm-Bonferroni. Una búsqueda por texto no encontró estas pruebas en los trabajos de
normalización o extracción generativa de claims #49, #80, #81, #84, #88, #89, #93 y
#95 a #100, así que no hay un antecedente directo para modelos seq2seq.

**Qué se puede afirmar.** Estos resultados comparan similitud con la referencia; no
prueban exactitud clínica ni veracidad.

| Resultado | Frase permitida |
| --- | --- |
| Friedman con p ≥ 0,05 | «Con este test no se detectan diferencias entre los modelos.» |
| Friedman con p < 0,05 y par con p de Holm < 0,05 | «A supera a B», con diferencia, intervalo, p ajustado y efecto. |
| Par con p de Holm ≥ 0,05 | «A y B no se distinguen con este test.» No se escribe que sean iguales o equivalentes. |
| La media y los rangos apuntan en sentidos opuestos | No se concluye y se explica la discrepancia. |
| El primer lugar cambia entre semillas | Se reporta cuántas veces quedó primero cada modelo y se escribe que ese lugar no es estable. |

**Reglas.**

- Test no interviene en la elección de variante ni en el número de semillas de
  `claims_1500`.
- El nivel de significancia, las semillas y las comparaciones no cambian después de
  mirar test.
- Una corrida que falla por un error técnico se repite con la misma semilla. Una
  corrida no se repite por tener un mal resultado.
- El test de `claims_500` ya se vio. Sus análisis son un piloto y no se mezclan con
  los de 400 noticias ni con los de `claims_1500`.

**Registro de cambios.**

| Fecha | Cambio | Motivo | ¿Antes o después de ver test? |
| --- | --- | --- | --- |
| 10/11/2026 | Se adoptan esta sección, las semillas y la validación cruzada en temas | Medir la estabilidad del entrenamiento y comparar los modelos con pruebas pareadas | Antes de crear `claims_1500`. El test de `claims_500` ya se había visto |

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

