# Extracción de claims médicos en español

Este repositorio contiene el experimento local para extraer y normalizar un
claim central, verificable y autocontenido desde noticias médicas en español de
Latinoamérica. No clasifica una noticia como verdadera o falsa y todavía no
recupera ni ordena evidencia.

La primera entrega compara cuatro familias generativas. La traducción,
generación de consultas y ranking de artículos son fases posteriores y quedan
fuera de esta rama.

## Modelos incluidos

| Id | Modelo base local | Papel metodológico | Referencia |
| --- | --- | --- | --- |
| umt5_base_es | google/umt5-base | Normalización multilingüe post a claim; configuración española | MMA at CheckThat! 2025 |
| t5_small | google-t5/t5-small | Baseline seq2seq de extracción y normalización | JU_NLP@M&S at CheckThat! 2025 |
| pegasus_large | google/pegasus-large | Baseline seq2seq de extracción y normalización | JU_NLP@M&S at CheckThat! 2025 |
| claimgen_bart_transfer | facebook/bart-large | Adaptación explícita de CLAIMGEN-BART a noticia y entradilla | Wright et al., ACL 2022 |

Los cuatro se ejecutan localmente con PyTorch y Transformers. No se usa una API
de generación ni LM Studio. LM Studio no aporta una ventaja para estos modelos
encoder-decoder y no reproduce el flujo de fine-tuning de los artículos.

CLAIMGEN-BART no es un checkpoint público independiente para descargar: el
repositorio original entrena BART con pares de citancia y claim de SciFact. Por
eso este proyecto descarga facebook/bart-large como peso base y etiqueta su uso
sobre noticias como transferencia adaptada, nunca como réplica literal del
experimento de citancias.

## Diseño experimental de esta fase

El CSV recibido contiene noticias y cuerpos, pero no claims de referencia. Por
eso la primera fase tiene un requisito explícito: anotar un subconjunto con un
claim central, declarativo, verificable y autocontenido antes de entrenar o
calcular métricas de referencia. El script create_reference_template.py crea
la plantilla; no inventa las etiquetas.

Con reference_claim anotado, se reportan METEOR, ROUGE-L F1 y exact match
normalizado. Sin referencia solo se reportan diagnósticos operativos (salidas
vacías, claims muy cortos, longitud y duplicados); no se presentan como calidad
del modelo. Esta separación reproduce la lógica evaluable de los trabajos de
CheckThat! y evita calcular métricas sin ground truth.

La hoja ciega de atomicidad, fluidez, descontextualización y fidelidad es
opcional. Se incluye porque CLAIMGEN-BART evaluó propiedades de calidad humana,
pero no se necesita para la comparación automática principal. Si se usa, la
clave de modelos queda separada de la hoja entregada al revisor.

## Preparación local

En un Mac con Apple Silicon el código detecta MPS automáticamente. La
configuración conserva los hiperparámetros de cada artículo como punto de
partida y ajusta batch/gradient accumulation al límite de memoria local.

    make setup
    make test
    make download-models

Los pesos se guardan en la caché local de Hugging Face, no dentro del
repositorio. La descarga incluye pesos base, no checkpoints finetuneados
inexistentes o no publicados. Se eliminaron del checkout los claims antiguos
generados por una API, Label Studio y la función OCI que los producía. El
componente PubMed que queda es opcional y no llama a ninguna API de traducción;
su ranking se reservará para una fase posterior.

## Flujo mínimo

Primero prepara candidatos desde el CSV entregado. El archivo original no se
copia ni se versiona.

    make prepare-corpus INPUT=/ruta/noticias_mednews_titulos_bodies_20261028.csv OUT=artifacts/candidates.jsonl

Para construir el conjunto de referencia:

    make reference-template INPUT=artifacts/candidates.jsonl OUT=artifacts/references_to_annotate.csv SAMPLE_SIZE=100

Completa reference_claim manualmente y divide esa tabla en train/validation
antes de entrenar. El script falla si quedan claims vacíos.

    make split-reference INPUT=artifacts/references_to_annotate.csv TRAIN=artifacts/train.csv VALIDATION=artifacts/validation.csv VALIDATION_FRACTION=0.2
    make train MODEL=umt5_base_es TRAIN=/ruta/train.csv VALIDATION=/ruta/validation.csv OUT=artifacts/checkpoints/umt5
    make infer MODEL=umt5_base_es CHECKPOINT=artifacts/checkpoints/umt5 INPUT=artifacts/candidates.jsonl OUT=artifacts/predictions/umt5.jsonl
    make evaluate PREDICTIONS=artifacts/predictions/umt5.jsonl REFERENCES=/ruta/validation.csv OUT=artifacts/evaluation/umt5

La revisión humana ciega es opcional y solo se ejecuta cuando ya hay
predicciones de los cuatro modelos:

    make review-sheet INPUT=artifacts/candidates.jsonl PREDICTIONS=artifacts/predictions/all_models.jsonl OUT=artifacts/review SAMPLE_SIZE=100

La guía metodológica detallada está en docs/EXPERIMENT_PROTOCOL.md.
