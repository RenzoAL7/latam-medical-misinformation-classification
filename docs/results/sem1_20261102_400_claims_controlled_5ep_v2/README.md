# Resultados Sem1 — extracción de afirmaciones médicas centrales

Corrida controlada: `sem1_20261102_400_claims_controlled_5ep_v2`. Los cuatro modelos usan el mismo conjunto de 400 noticias: 320 de entrenamiento, 40 de validación y 40 de prueba.

La configuración común fija 5 épocas como máximo, batch efectivo 8, learning rate 3e-05, weight decay 0, warmup 0 y cuatro beams. El checkpoint se elige por menor pérdida de validación dentro de ese presupuesto y se evalúa una sola vez sobre el test.

## Métricas principales

Media por noticia en escala de 0 a 1. METEOR es la métrica principal; ROUGE-L F1 y BERTScore F1 son complementarias.

| Modelo | Noticias de prueba | METEOR | ROUGE-L F1 | BERTScore F1 |
| --- | ---: | ---: | ---: | ---: |
| T5S | 40 | 0.1735 | 0.2209 | 0.8246 |
| BARTO | 40 | 0.3150 | 0.3258 | 0.8997 |
| mBART-50 | 40 | 0.2786 | 0.2941 | 0.8974 |
| uMT5-Base | 40 | 0.2160 | 0.1981 | 0.7427 |

## Tablas exportadas

Los CSV y HTML de las siete tablas se encuentran en `tables/`. Las cuatro figuras se encuentran en `figures/` en PNG y PDF.

## Alcance

La entrega mide similitud entre el claim central generado y la referencia anotada. No demuestra veracidad médica ni incluye extracción multiclaim, traducción, generación de consultas, recuperación PubMed o ranking. El corpus, las anotaciones, las predicciones individuales y los pesos permanecen locales.
