# Metodología implementada en Seminario 1

## 1. Descubrimiento de candidatas

El Colab obtiene los `record_id` ya registrados en `Bronze` y ejecuta el recolector del repositorio. La configuración busca una meta global de 12 candidatas en El Comercio, La República, Diario Correo, Diario Ojo, MINSA, Gestión, El Popular y Canal N. Cada fuente puede aportar como máximo dos candidatas.

Las fuentes se descubren desde páginas públicas permitidas por `robots.txt`, por lo que la corrida vigente no consume créditos de NewsData. Antes de conservar una URL se valida que pertenezca al medio autorizado y que el título o bajada incluya un término médico y una señal de afirmación contrastable. La selección final rota el primer medio según la cantidad de registros existentes y toma una noticia por fuente en cada ronda. La integración opcional con NewsData permanece limitada a cuatro solicitudes por corrida, excluye resultados de video y se detiene al alcanzar la meta global.

Cada URL se normaliza y se transforma en un `record_id`. Las coincidencias con `Bronze` se descartan durante la búsqueda. Antes de anexar, el Colab vuelve a leer la hoja y aplica una segunda deduplicación. La ampliación no significa aceptar cualquier nota: los medios o formatos que no permiten automatización, devuelven videos o producen cuerpos insuficientes permanecen fuera hasta que una nueva prueba documentada justifique incorporarlos.

La frecuencia operativa recomendada es una corrida cada 24 horas. Si no hay suficientes URLs nuevas, se espera 48 horas. La pausa de un segundo configurada en `request_delay_seconds` regula solicitudes dentro de la misma corrida y no representa el intervalo entre corridas. Para ampliar el corpus histórico se incorporará paginación o búsqueda por fechas; no se resolverá repitiendo de inmediato la misma portada.

## 2. Revisión humana en Bronze

Las candidatas ingresan con `selection_status=PENDIENTE`. El investigador toma una sola decisión: `INCLUIR` si la noticia es médica, tiene texto periodístico y contiene una afirmación concreta y contrastable; en caso contrario elige `EXCLUIR_NO_MEDICA`, `EXCLUIR_SIN_AFIRMACION` o `EXCLUIR_SIN_TEXTO`. Así se evita completar cuatro columnas distintas para expresar una misma decisión operativa.

## 3. Extracción del cuerpo

El Colab selecciona todas las filas con `selection_status=INCLUIR` que todavía no aparecen en `Silver`. Descarga la página con HTTPX, intenta extraer el texto con Trafilatura y usa BeautifulSoup como respaldo. Solo agrega cuerpos de al menos 150 palabras y 800 caracteres; cuando no lo consigue actualiza Bronze a `EXCLUIR_SIN_TEXTO`.

## 4. Validación con evidencia

En `Silver`, el investigador delimita la afirmación principal y la contrasta con fuentes especializadas como MINSA, INS, EsSalud, OMS/OPS, PubMed u otra autoridad pertinente. Registra una URL de evidencia, una nota breve de verificación, la etiqueta, el revisor y el estado de validación. Ni NewsData ni el medio periodístico determinan la etiqueta. Cuando la candidata proviene de MINSA, la evidencia confirmatoria debe registrarse con una fuente independiente y no asumirse a partir del dominio.

## 5. Construcción de Gold

El Colab reconstruye `Gold` a partir de `Silver`. Solo copia registros con cuerpo, afirmación médica, URL de evidencia, etiqueta binaria válida y validación completada. Deduplica por `record_id` y deja `dataset_split` vacío hasta aplicar la partición estratificada 70/15/15. Los modelos usarán `title + body` como entrada y `label` como objetivo; fuente y tema se reservan para auditoría.

## 6. Alcance actual

El repositorio cubre adquisición, trazabilidad y deduplicación. El Colab cubre integración con Google Sheets, extracción del cuerpo y generación del corpus Gold. El primer entrenamiento local se ejecuta con `scripts/03_train_baseline.py` una vez cerradas al menos 100 filas binarias validadas; usa TF-IDF más regresión logística como baseline reproducible y mantiene una partición estratificada 70/15/15. Este resultado es un piloto metodológico, no una métrica final del corpus.
