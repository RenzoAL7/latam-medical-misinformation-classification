# Guía de anotación

Versión 1, 24/11/2026. Rige para las noticias que se anoten desde esta fecha en el
proyecto 9 de Label Studio. La configuración de la interfaz está en
[`configs/labelstudio_proyecto9.xml`](../configs/labelstudio_proyecto9.xml).

Esta guía responde tres preguntas de la asesora: cómo se decide que una noticia es
médica, cómo se encuentra el claim central (en una oración o en varias) y cómo se
asigna el tema. Las reglas siguen trabajos de la carpeta de papers de Drive; el número
entre paréntesis es el de esa carpeta.

## 1. Qué produce cada anotación

Por cada noticia:

1. `news_eligibility`, con los tres valores que el proyecto ya usaba:
   - `APTA_CON_CLAIM`: pasa la prueba de la sección 2.
   - `SIN_CLAIM_MEDICO`: el texto se lee bien, pero no pasa la prueba.
   - `NO_UTILIZABLE_POR_RUIDO`: el cuerpo no es una noticia legible (menú, lista de
     titulares de otras notas, texto cortado, aviso de cookies). No se aplica la
     prueba.
2. `motivo_sin_claim`: qué pregunta de la prueba falló. Solo se pide con
   `SIN_CLAIM_MEDICO`.
3. Si es apta:
   - **Oración central**, marcada en el título o en el cuerpo con la etiqueta `CENTRAL`.
   - **Oraciones de contexto**, marcadas con `CONTEXTO`: cero, una o varias.
   - `tipo_claim`: `RELACION` o `CIFRA_O_HECHO`.
   - `tema`: una de las siete categorías.
   - `reference_claim`: la afirmación central reescrita.
4. `revisar_en_pareja`: casilla opcional para los casos dudosos.

La `reference_claim` es la salida esperada de los modelos. No dice si la afirmación
es verdadera: no es una verificación médica.

## 2. ¿Es médica? Prueba de tres preguntas

No hace falta formación médica. Wührl et al. (2024, #28) compararon anotadores con y
sin formación médica y no encontraron un efecto sustancial de la experiencia en la
confiabilidad. En los trabajos revisados anotaron investigadores sin formación médica,
siguiendo una guía: dos investigadores de ciencias de la información en Yuan y Yu
(2019, #90 y #91), los tres autores, informáticos, en Martinez-Rico et al. (2024, #66),
y tres autores de PLN biomédico en Sarrouti et al. (2021, #77).

Busca en el título, la bajada y el primer párrafo una oración que cumpla las tres
condiciones:

**1. ¿Habla de un resultado de salud?**
Enfermedad, síntoma, lesión, riesgo, muerte, embarazo, salud mental, estado
nutricional, o acceso y cobertura de atención de salud.
Si no hay ninguno: `SIN_CLAIM_MEDICO` con `NO_ES_SALUD`.

**2. ¿Afirma algo concreto sobre ese resultado?** Puede ser de dos tipos:

| `tipo_claim` | Qué es | Ejemplo inventado |
| --- | --- | --- |
| `RELACION` | Algo (fármaco, vacuna, alimento, conducta, exposición, política) se relaciona con el resultado. Es la definición de afirmación de salud de Yuan y Yu (#90): lo que se manipula, la relación y lo que se mide | «Dormir menos de seis horas se asocia con mayor riesgo de hipertensión en adultos.» |
| `CIFRA_O_HECHO` | Un dato comprobable sobre el resultado: casos, prevalencia, cobertura, aprobación de un tratamiento. Corresponde a las oraciones «factuales y relevantes» de KEANE (#66) | «Bolivia registró 1200 casos de dengue en enero de 2026.» |

Si solo hay un llamado general o un consejo sin dato («hay que cuidarse del
calor»): `SIN_CLAIM_MEDICO` con `SIN_AFIRMACION_VERIFICABLE`.

**3. ¿Se puede comprobar con evidencia?**
No es una opinión, una pregunta, un testimonio personal ni una promesa. Togedemaru
(#92) separa afirmaciones de experiencias y preguntas, y KEANE (#66) separa lo
factual de lo no factual.
Si falla: `SIN_CLAIM_MEDICO` con `SIN_AFIRMACION_VERIFICABLE`.

### Noticias de sistema de salud y políticas

Una noticia institucional es apta solo si afirma algo sobre la salud de las personas
o sobre su acceso a la atención: cobertura, vacunados, tiempos de espera o atenciones
realizadas.

- **Apta:** «El programa amplió la vacunación contra el VPH a niños de 9 años en
  2026.»
- **No apta** (`SIN_CLAIM_MEDICO` con `SOLO_ADMINISTRATIVA`): presupuestos,
  inauguraciones, nombramientos o convenios que no dicen nada de la salud ni del
  acceso.

Esta regla mantiene el criterio de exclusión de la metodología: se descartan las
piezas exclusivamente administrativas sin afirmación médica concreta.

### Ruido y duplicados

- **`NO_UTILIZABLE_POR_RUIDO`:** el cuerpo es un menú, una lista de titulares de otras
  notas, un aviso de cookies, está cortado o tiene menos de un párrafo de noticia.
  Esto se decide antes de la prueba de tres preguntas.
- **Duplicados:** no se marcan a mano. El pipeline de OCI descarta los repetidos por
  `record_id`, título y cuerpo, y el notebook 02 agrupa los casi duplicados. Si ves uno
  evidente, márcalo con `revisar_en_pareja`.

## 3. ¿Dónde está el claim central?

Se sigue a Deng et al. (2024, #84): el claim central parte de **una oración central**,
la más relacionada con la idea principal de la noticia, y se **descontextualiza** con
datos de otras oraciones para que se entienda sola. Así, «¿una o varias oraciones?»
tiene respuesta fija: siempre una oración central, más las de contexto que hagan falta.

### Paso a paso

1. **Lee primero el título, la bajada y el primer párrafo.** En Deng et al. la primera
   oración ya era la central en el 42,3 % de los casos (P@1 de la línea base *Lead
   Sentence*).
2. **Elige la oración central.** Es la que responde «¿qué dice esta noticia sobre
   salud?». Si hay varias afirmaciones médicas:
   - Prefiere la que anuncia el título.
   - Si el título no afirma nada, elige la que el texto más desarrolla.
   - Prefiere el hallazgo o el hecho nuevo antes que el antecedente.
3. **Márcala con `CENTRAL`.** Marca exactamente una, en el título o en el cuerpo. Si
   título y cuerpo dicen lo mismo, marca la versión más completa. Label Studio no
   obliga a hacer esta marca: deja enviar una tarea apta sin ella. Revisa el panel
   *Regions* antes de pulsar *Submit*.
4. **Marca con `CONTEXTO` cada oración de la que tomes un dato** que no está en la
   central:
   - quién lo dice o hizo el estudio;
   - a qué población se aplica;
   - dónde y cuándo;
   - una cantidad;
   - a qué se refiere un pronombre («el fármaco», «esta enfermedad»).

   Claimify (#82) dio a sus anotadores las cinco oraciones anteriores como contexto.
   Busca primero ahí y luego en el resto del texto.
5. **No marques contexto que no usaste.** Si la central se entiende sola, no hay
   ninguna oración de contexto.

Con estas marcas se puede reportar qué proporción de claims salió de una sola oración
y cuál necesitó dos o más. La posición de la oración central también muestra si cae
dentro de los 512 tokens que recibe el modelo.

Las 549 claims anotadas antes de esta guía no tienen oración marcada. Para ellas se
estimará automáticamente como la oración con mayor chrF frente a la claim, como hicieron
Deng et al. cuando el dato no estaba anotado. No se reanotan.

## 4. Cómo escribir `reference_claim`

| Regla | Mal | Bien |
| --- | --- | --- |
| Una sola oración declarativa | «¿El café protege el hígado? Un estudio dice que sí.» | «El consumo diario de café se asoció con menor riesgo de cirrosis en adultos.» |
| Se entiende sin leer la noticia: sin pronombres sueltos ni «el estudio» sin decir cuál | «Esta vacuna redujo las hospitalizaciones.» | «La vacuna contra el VSR redujo las hospitalizaciones de adultos mayores.» |
| Conserva cifras, población, lugar y condiciones | «El ayuno afecta a los pacientes.» | «En pacientes críticos que esperan una traqueotomía, cada hora adicional de ayuno se asoció con un déficit de cuatro gramos de proteína.» |
| Conserva negaciones | «El suplemento mejora la memoria.» (la noticia decía que no) | «El suplemento de omega-3 no mejoró la memoria en adultos mayores.» |
| No cambia la fuerza de la afirmación: «se asocia» no pasa a «causa», ni «podría» a «logra» | «El celular causa insomnio.» | «El uso del celular antes de dormir se asoció con peor calidad de sueño en adolescentes.» |
| Solo lo que está en el título y el cuerpo, que es lo que recibe el modelo. La bajada sirve para entender, no como fuente | Datos que solo aparecen en la bajada | Datos del título o del cuerpo |
| Sin opinión, veredicto ni muletillas | «Según la noticia, sorprendentemente…» | Afirmación directa |
| Entre 5 y 80 palabras; lo normal es entre 12 y 35 | — | — |

Cada regla mantiene el significado de la fuente y la autonomía del enunciado (Deng
et al., #84; Metropolitansky y Larson, #82). Escribe tú la claim: no la generes ni la
copies de un asistente o un modelo. Una anotación hecha por un asistente no es una
anotación humana y sesgaría la evaluación hacia el estilo de ese modelo.

## 5. Tema

El tema se asigna según el **resultado de salud del claim central**, no según la
noticia completa ni la palabra más repetida. Elige uno solo.

| `tema` | Incluye |
| --- | --- |
| Cáncer | Cualquier tumor; tamizaje y tratamiento oncológico |
| Cardiometabólicas | Corazón, presión arterial, ACV, colesterol, diabetes, obesidad, riñón |
| Nutrición y suplementos | Efecto de alimentos, dietas o suplementos sobre la salud general, cuando el resultado no es una enfermedad de otra categoría |
| Salud mental y neurología | Depresión, ansiedad, suicidio, adicciones, sueño, demencia, epilepsia, desarrollo infantil |
| Infecciosas e inmunidad | Virus, bacterias, parásitos, vacunas, resistencia antimicrobiana, alergias y enfermedades autoinmunes |
| Sistema de salud y políticas | Acceso, cobertura, programas, normas y servicios de salud (ver sección 2) |
| Otra | Lo que no encaja arriba: salud materna, piel, vista, huesos, accidentes, salud ambiental sin enfermedad específica |

**Desempate:** si la exposición es de un tema y el resultado de otro, gana el
resultado. Por ejemplo, «comer fibra reduce el riesgo de cáncer de colon» va en Cáncer.

Los nombres deben escribirse exactamente así: el notebook 01 y
`src/claim_extraction/topics.py` rechazan otros valores.

La forma de etiquetar sigue la de los trabajos de clasificación de noticias revisados:
anotación manual con una taxonomía fija, como MN-DS con IPTC Media Topics (#101), o
categorías tomadas de la fuente, como #103 y #104. Queda pendiente comparar estas
siete categorías con la rama de salud de IPTC.

## 6. Acuerdo entre anotadores

Antes de seguir con la anotación masiva se mide si la guía se aplica igual entre los
dos anotadores. El procedimiento es el de Yuan y Yu (#90) y KEANE (#66): ambos
equipos anotaron por separado una muestra de 100 y luego dividieron el resto.

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
4. Si los dos kappa llegan a 0,61 (acuerdo sustancial; #90 obtuvo 0,71 y #66, 0,635),
   se sigue con la anotación individual. Si no:
   - se revisan los desacuerdos;
   - se aclara la regla que falló en esta guía, subiendo la versión;
   - se repite con 50 noticias nuevas.
5. Los desacuerdos se resuelven conversando, como en #90, y la versión acordada se
   carga en el proyecto 9.

El script para calcular estas medidas todavía no existe.

## 7. Casos frecuentes

| Caso | Decisión |
| --- | --- |
| La noticia trae varios estudios | Central: el que anuncia el título |
| El titular exagera respecto del cuerpo | La claim sigue al cuerpo y conserva su fuerza real |
| Nota de servicio («cómo prevenir el dengue») sin datos | `SIN_CLAIM_MEDICO`, `SIN_AFIRMACION_VERIFICABLE` |
| Anuncio de un tratamiento aprobado | Apta, `CIFRA_O_HECHO` |
| Afirmación sobre animales sin relación con personas | `SIN_CLAIM_MEDICO`, `NO_ES_SALUD` |
| El cuerpo junta titulares de varias noticias distintas | `NO_UTILIZABLE_POR_RUIDO` |
| La afirmación es de un vocero y no tiene respaldo en el texto | Apta si cumple las tres preguntas; escribe quién la sostiene si eso cambia el sentido («Según la OMS, …») |
| Dudas | Marca `revisar_en_pareja` y decide en la siguiente revisión conjunta |
