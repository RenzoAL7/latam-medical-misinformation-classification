# Guía de anotación

Versión 1.1, 26/11/2026. Rige para las noticias que se anoten desde esta fecha en el
proyecto 9 de Label Studio. La configuración de la interfaz está en
[`configs/labelstudio_proyecto9.xml`](../configs/labelstudio_proyecto9.xml).

Esta guía responde tres preguntas de la asesora: cómo se decide que una noticia es
médica, cómo se encuentra el claim central (en una oración o en varias) y cómo se
asigna el tema. Cada regla sigue un trabajo de la carpeta de papers de Drive; el número
entre paréntesis es el de esa carpeta. Lo que no sale de un paper se declara como
adaptación propia.

### Cambios frente a la versión 1 (24/11/2026)

La v1.1 quita las reglas que no tenían respaldo en los papers de Drive:

| v1 | v1.1 | Motivo |
| --- | --- | --- |
| Motivo `SOLO_ADMINISTRATIVA` | Eliminado: esos casos van a `NO_ES_SALUD` | No tiene paper; es un caso de la pregunta 1 (#90) |
| La bajada se mostraba, con la regla «no es fuente del claim» | Se quitó la bajada de la interfaz | La regla no tiene paper, y el modelo solo recibe título y cuerpo |
| Tema «según el resultado de salud del claim central», con desempate | Se quitó la regla; quedan las siete categorías | Ningún paper la respalda. El campo se conserva porque lo pidió la asesora (Acta N.° 1) y lo exige el notebook 01 |
| La pregunta 1 incluía salud mental, nutrición y acceso a la atención | Solo lo que se mide: enfermedad, síntoma, riesgo o muerte | Es la variable medida de un health claim en #90 |
| `CIFRA_O_HECHO` como «dato comprobable» | «Oración factual y relevante» | Es el criterio textual de #66 |
| Umbral de kappa de 0,61 | Comparación con el kappa de #90 y de #66 | El 0,61 viene de Landis y Koch, que no está en Drive |
| Contexto: «las cinco oraciones anteriores» (#82) | Las oraciones anteriores y siguientes | #82 no dice «cinco» |
| Se marcaban palabras | Se marcan oraciones completas (`granularity="sentence"`) | Los claims salen de oraciones centrales (#84) |

El 26/11/2026, junto con el paso a la v1.1, se borraron las anotaciones previas del
proyecto 9: todas las noticias se anotan desde cero con esta versión. El respaldo de
las anotaciones borradas está en
`artifacts/01_labelstudio/exports/project-9-respaldo-antes-de-reinicio-2026-11-26.json`,
que Git ignora.

## 1. Qué produce cada anotación

Por cada noticia:

1. `news_eligibility`, con los tres valores que el proyecto ya usaba:
   - `APTA_CON_CLAIM`: pasa la prueba de la sección 2.
   - `SIN_CLAIM_MEDICO`: el texto se lee bien, pero no pasa la prueba.
   - `NO_UTILIZABLE_POR_RUIDO`: el cuerpo no deja leer una noticia (ver sección 2).
     No se aplica la prueba.
2. `motivo_sin_claim`: qué pregunta de la prueba falló, `NO_ES_SALUD` o
   `SIN_AFIRMACION_VERIFICABLE`. Solo se pide con `SIN_CLAIM_MEDICO`.
3. Si es apta:
   - **Oración central**, marcada en el título o en el cuerpo con la etiqueta `CENTRAL`.
   - **Oraciones de contexto**, marcadas con `CONTEXTO`: cero, una o varias.
   - `tipo_claim`: `RELACION` o `CIFRA_O_HECHO`.
   - `tema`: una de las siete categorías.
   - `reference_claim`: la afirmación central reescrita.
4. `revisar_en_pareja`: casilla `DUDOSA`, opcional, para los casos dudosos.

La `reference_claim` es la salida esperada de los modelos. No dice si la afirmación
es verdadera: no es una verificación médica.

**Detalles de la interfaz.** En Label Studio el cuerpo se llama `article_body`, pero
lee la columna `body` de la tarea; el export sigue trayendo `body`, que es la que usa
el notebook 01. Arriba se muestran el ID, la fuente, el país y la fecha. El nombre
`article_body` y los campos ocultos `claim_evidence` y `news_utility` se mantuvieron
porque Label Studio no aceptaba la configuración mientras existían anotaciones
antiguas que los usaban. Esas anotaciones se borraron el 26/11/2026, así que ya se
podrían simplificar.

## 2. ¿Es médica? Prueba de tres preguntas

No hace falta formación médica. Wührl et al. (2024, #28) compararon anotadores con y
sin formación médica y no encontraron un efecto sustancial de la experiencia en la
confiabilidad. En los trabajos revisados anotaron investigadores sin formación médica,
siguiendo una guía: dos investigadores de ciencias de la información en Yuan y Yu
(2019, #90 y #91), los tres autores, informáticos, en Martinez-Rico et al. (2024, #66),
y tres autores de PLN biomédico en Sarrouti et al. (2021, #77).

Busca en el título y el primer párrafo una oración que cumpla las tres condiciones:

**1. ¿Habla de un resultado de salud?**
Es decir, de lo que se mide: enfermedad, síntoma, riesgo o muerte. En Yuan y Yu (#90),
un health claim tiene tres partes: lo que se manipula, lo que se mide y la relación
entre ambos. Esta pregunta busca lo que se mide.
Si no hay ninguno: `SIN_CLAIM_MEDICO` con `NO_ES_SALUD`.

**2. ¿Afirma algo concreto sobre ese resultado?** Puede ser de dos tipos:

| `tipo_claim` | Qué es | Ejemplo inventado |
| --- | --- | --- |
| `RELACION` | Algo (fármaco, vacuna, alimento, conducta, exposición, política) se relaciona con el resultado. Es la definición de afirmación de salud de Yuan y Yu (#90): lo que se manipula, la relación y lo que se mide | «Dormir menos de seis horas se asocia con mayor riesgo de hipertensión en adultos.» |
| `CIFRA_O_HECHO` | Una oración factual y relevante sobre el resultado, como las que selecciona KEANE (#66) | «Bolivia registró 1200 casos de dengue en enero de 2026.» |

Si solo hay un llamado general o un consejo sin dato («hay que cuidarse del
calor»): `SIN_CLAIM_MEDICO` con `SIN_AFIRMACION_VERIFICABLE`.

**3. ¿Se puede comprobar con evidencia?**
No es una opinión, un consejo, una pregunta ni una experiencia personal. Claimify (#82)
toma como claims los enunciados que se pueden comprobar como verdaderos o falsos con
evidencia, y Togedemaru (#92) separa los claims de las experiencias y las preguntas.
Si falla: `SIN_CLAIM_MEDICO` con `SIN_AFIRMACION_VERIFICABLE`.

### Noticias de sistema de salud y políticas

Una noticia institucional es apta solo si su oración central pasa las tres preguntas.
Las piezas solo administrativas, como presupuestos, inauguraciones, nombramientos o
convenios que no hablan de un resultado de salud, fallan la pregunta 1:
`SIN_CLAIM_MEDICO` con `NO_ES_SALUD`.

### Ruido y duplicados

- **`NO_UTILIZABLE_POR_RUIDO`:** el cuerpo no deja leer una noticia ni identificar
  sus afirmaciones; por ejemplo, es solo un menú, una lista de titulares de otras notas
  o un aviso de cookies, o está cortado. #87 excluyó los artículos demasiado ambiguos
  para tener claims claros, y #66 tiene la clase *Not Applicable*. Esto se decide antes
  de la prueba de tres preguntas. Si hay ruido mezclado (titulares de otras notas,
  pies de foto, «Publicidad») pero la noticia se lee completa, se sigue con la prueba.
- **Duplicados:** no se marcan a mano. El pipeline de OCI descarta los repetidos por
  `record_id`, título y cuerpo, y el notebook 02 agrupa los casi duplicados. Si ves uno
  evidente, márcalo con `DUDOSA`.

## 3. ¿Dónde está el claim central?

Se sigue a Deng et al. (2024, #84): el claim central parte de **una oración central**,
la más relacionada con la idea principal de la noticia, y se **descontextualiza** con
datos de otras oraciones para que se entienda sola (#84, #82). Así, «¿una o varias
oraciones?» tiene respuesta fija: siempre una oración central, más las de contexto que
hagan falta. Que haya un solo claim central por noticia sigue al claim normalizado de
CheckThat! 2025 (#95).

### Paso a paso

1. **Empieza a buscar en el título y el primer párrafo.** En Deng et al. la primera
   oración fue la central en el 42,3 % de los casos (P@1 de la línea base *Lead
   Sentence*); TextRank (42,7) y BertSum (43,4) quedan casi igual. El dato sirve para
   saber dónde empezar, no para suponer que la central es siempre la primera.
2. **Elige la oración central.** Es la que responde «¿qué dice esta noticia sobre
   salud?». Si hay varias afirmaciones médicas:
   - Prefiere la que anuncia el título.
   - Si el título no afirma nada, elige la que el texto más desarrolla.
   - Prefiere el hallazgo o el hecho nuevo antes que el antecedente.
3. **Márcala con `CENTRAL`.** Marca exactamente una, en el título o en el cuerpo. Si
   título y cuerpo dicen lo mismo, marca la versión más completa. Para marcarla, elige
   la etiqueta y arrastra el mouse sobre unas palabras: Label Studio extiende la marca a
   la oración completa. Label Studio no obliga a hacer esta marca: deja enviar una tarea
   apta sin ella. Revisa el panel *Regions* antes de pulsar *Submit*.
4. **Marca con `CONTEXTO` cada oración de la que tomes un dato** que no está en la
   central:
   - quién lo dice o hizo el estudio;
   - a qué población se aplica;
   - dónde y cuándo;
   - una cantidad;
   - a qué se refiere un pronombre («el fármaco», «esta enfermedad»).

   Claimify (#82) considera como contexto las oraciones anteriores y las siguientes.
   Busca primero en las cercanas y luego en el resto del texto.
5. **No marques contexto que no usaste.** Si la central se entiende sola, no hay
   ninguna oración de contexto.

Con estas marcas se puede reportar qué proporción de claims salió de una sola oración
y cuál necesitó dos o más. La posición de la oración central también muestra si cae
dentro de los 512 tokens que recibe el modelo.

**Limitación conocida.** Cuando el texto extraído no trae punto entre un pie de foto y
la oración siguiente, Label Studio los toma como una sola oración, y la marca arrastra
ese texto (por ejemplo, «(REUTERS/…) 07 Nov… Guardar» o «PUBLICIDAD»). La oración
correcta queda dentro de la marca, así que sirve, pero el texto del span no es limpio.

## 4. Cómo escribir `reference_claim`

Todo lo que se escriba debe estar en la noticia: no se agregan ni se cambian datos.
Claimify (#82) exige que el claim esté respaldado por la fuente (*entailment*). Las
reglas de la tabla son formas de aplicar ese criterio, junto con la autonomía del
enunciado (#84, #82).

| Regla | Mal | Bien |
| --- | --- | --- |
| Una sola oración declarativa | «¿El café protege el hígado? Un estudio dice que sí.» | «El consumo diario de café se asoció con menor riesgo de cirrosis en adultos.» |
| Se entiende sin leer la noticia: sin pronombres sueltos ni «el estudio» sin decir cuál | «Esta vacuna redujo las hospitalizaciones.» | «La vacuna contra el VSR redujo las hospitalizaciones de adultos mayores.» |
| Conserva cifras, población, lugar y condiciones | «El ayuno afecta a los pacientes.» | «En pacientes críticos que esperan una traqueotomía, cada hora adicional de ayuno se asoció con un déficit de cuatro gramos de proteína.» |
| Conserva negaciones | «El suplemento mejora la memoria.» (la noticia decía que no) | «El suplemento de omega-3 no mejoró la memoria en adultos mayores.» |
| No cambia la fuerza de la afirmación: «se asocia» no pasa a «causa», ni «podría» a «logra» | «El celular causa insomnio.» | «El uso del celular antes de dormir se asoció con peor calidad de sueño en adolescentes.» |
| Solo lo que está en el título y el cuerpo, que es lo que recibe el modelo | Datos que no aparecen en la noticia | Datos del título o del cuerpo |
| Sin opinión, veredicto ni muletillas | «Según la noticia, sorprendentemente…» | Afirmación directa |
| Entre 5 y 80 palabras: el notebook 02 descarta las demás | — | — |

Si una ambigüedad no se puede resolver con el texto, no se inventa: se marca `DUDOSA`.
Claimify (#82) etiqueta esos casos como *Cannot be disambiguated*.

Escribe tú la claim: no la generes ni la copies de un asistente o un modelo. Una
anotación hecha por un asistente no es una anotación humana y sesgaría la evaluación
hacia el estilo de ese modelo.

## 5. Tema

Elige uno solo de las siete categorías.

| `tema` | Incluye |
| --- | --- |
| Cáncer | Cualquier tumor; tamizaje y tratamiento oncológico |
| Cardiometabólicas | Corazón, presión arterial, ACV, colesterol, diabetes, obesidad, riñón |
| Nutrición y suplementos | Efecto de alimentos, dietas o suplementos sobre la salud |
| Salud mental y neurología | Depresión, ansiedad, suicidio, adicciones, sueño, demencia, epilepsia, desarrollo infantil |
| Infecciosas e inmunidad | Virus, bacterias, parásitos, vacunas, resistencia antimicrobiana, alergias y enfermedades autoinmunes |
| Sistema de salud y políticas | Acceso, cobertura, programas, normas y servicios de salud |
| Otra | Lo que no encaja arriba: salud materna, piel, vista, huesos, accidentes, salud ambiental sin enfermedad específica |

Los nombres deben escribirse exactamente así: el notebook 01 y
`src/claim_extraction/topics.py` rechazan otros valores.

Usar una taxonomía fija sigue a MN-DS, que anota con IPTC Media Topics (#101). Estas
siete categorías no salen de un paper: son una **adaptación propia** y así se declaran
en la tesis. Queda pendiente compararlas con la rama de salud de IPTC; derivarlas de
IPTC cambiaría el notebook 05.

## 6. Acuerdo entre anotadores

Antes de seguir con la anotación masiva se mide si la guía se aplica igual entre los
dos anotadores. El procedimiento sigue a Yuan y Yu (#90), con 100 titulares y dos
anotadores, y a KEANE (#66), que anotó 100 oraciones en común y después repartió el
resto.

1. Se crea un **proyecto aparte en Label Studio** con 100 noticias nuevas tomadas al
   azar, con la misma configuración. Así el export del proyecto 9 sigue teniendo una
   fila por noticia.
2. Cada uno anota las 100 **sin consultar al otro**.
3. Se calculan:
   - kappa de Cohen para `news_eligibility`;
   - kappa de Cohen para `tema`, en las noticias que ambos marcaron como aptas;
   - porcentaje de noticias en que ambos eligieron la misma oración central;
   - METEOR, ROUGE-L y BERTScore entre las dos `reference_claim`. Sirven como techo
     humano: si dos personas coinciden 0,45 en METEOR, no se puede esperar mucho más
     de un modelo.
4. Los dos kappa se comparan con los de los trabajos de referencia: 0,71 en #90 y
   0,635 (Fleiss) en #66, que ambos describen como acuerdo sustancial. Si quedan por
   debajo:
   - se revisan los desacuerdos;
   - se aclara la regla que falló en esta guía, subiendo la versión;
   - se repite con 50 noticias nuevas.
5. Los desacuerdos se resuelven conversando, como en #82 y #87, y la versión acordada
   se carga en el proyecto 9.

El script para calcular estas medidas todavía no existe.

## 7. Casos frecuentes

| Caso | Decisión |
| --- | --- |
| La noticia trae varios estudios | Central: el que anuncia el título |
| El titular exagera respecto del cuerpo | La claim sigue al cuerpo y conserva su fuerza real |
| Nota de servicio («cómo prevenir el dengue») sin datos | `SIN_CLAIM_MEDICO`, `SIN_AFIRMACION_VERIFICABLE` |
| Anuncio de un tratamiento aprobado | Apta, `CIFRA_O_HECHO` |
| Afirmación sobre animales sin relación con personas | `SIN_CLAIM_MEDICO`, `NO_ES_SALUD` |
| Perfil o entrevista con frases médicas sueltas, cuya oración central no habla de un resultado de salud | `SIN_CLAIM_MEDICO`, `NO_ES_SALUD`. Las experiencias personales no son claims (#92) |
| El cuerpo es solo una lista de titulares de otras noticias | `NO_UTILIZABLE_POR_RUIDO` |
| La afirmación es de un vocero y no tiene respaldo en el texto | Apta si cumple las tres preguntas; escribe quién la sostiene si eso cambia el sentido («Según la OMS, …») |
| Dudas | Marca `DUDOSA` y decide en la siguiente revisión conjunta |
