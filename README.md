# Extracción de claims médicos en español

Este repositorio contiene el experimento local para extraer y normalizar un
claim central, verificable y autocontenido desde noticias médicas en español de
Latinoamérica. No clasifica una noticia como verdadera o falsa y todavía no
recupera ni ordena evidencia.

La primera entrega compara cuatro familias generativas en español o
multilingües. La traducción,
generación de consultas y ranking de artículos son fases posteriores y quedan
fuera de esta rama.

## Modelos incluidos

| Id | Modelo base local | Papel metodológico | Referencia |
| --- | --- | --- | --- |
| t5s_spanish | vgaraujov/t5-base-spanish | T5 preentrenado en español | Spanish Pre-trained Language Models; AKCIT-FN 2025 |
| barto_spanish | vgaraujov/bart-base-spanish | BART preentrenado en español | Spanish Pre-trained Language Models |
| mbart50_es | facebook/mbart-large-50 | Seq2seq multilingüe con entrada y salida `es_XX` | AKCIT-FN at CheckThat! 2025 |
| umt5_base_es | google/umt5-base | Normalización multilingüe con configuración española | MMA at CheckThat! 2025 |

Los cuatro se ejecutan localmente con PyTorch y Transformers. mBART-50 se
configura explícitamente con `src_lang=es_XX`, `tgt_lang=es_XX` y el token
forzado de inicio de generación en español. No se usa una API
de generación ni LM Studio. LM Studio no aporta una ventaja para estos modelos
encoder-decoder y no reproduce el flujo de fine-tuning de los artículos.

T5S y BARTO son checkpoints encoder-decoder preentrenados en español. mBART-50
y uMT5-Base son checkpoints multilingües; en ambos casos el entrenamiento y la
generación de esta rama se orientan explícitamente a español.

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
generados por una API, Label Studio y todo el código OCI. La rama queda
deliberadamente limitada al entrenamiento, inferencia y evaluación local de
los cuatro modelos; traducción, generación de consultas, PubMed y ranking son
fases posteriores fuera de este checkout.

## Flujo mínimo

Primero prepara candidatos desde el CSV entregado. El archivo original no se
copia ni se versiona.

    make prepare-corpus INPUT=/ruta/noticias_mednews_titulos_bodies_20261028.csv OUT=artifacts/candidates.jsonl

Para construir el conjunto de referencia:

    make reference-template INPUT=artifacts/candidates.jsonl OUT=artifacts/references_to_annotate.csv SAMPLE_SIZE=100

Completa reference_claim manualmente y divide esa tabla en train/validation
antes de entrenar. El script falla si quedan claims vacíos.

    make split-reference INPUT=artifacts/references_to_annotate.csv TRAIN=artifacts/train.csv VALIDATION=artifacts/validation.csv VALIDATION_FRACTION=0.2
    make train MODEL=t5s_spanish TRAIN=/ruta/train.csv VALIDATION=/ruta/validation.csv OUT=artifacts/checkpoints/t5s_spanish
    make train MODEL=barto_spanish TRAIN=/ruta/train.csv VALIDATION=/ruta/validation.csv OUT=artifacts/checkpoints/barto_spanish
    make train MODEL=mbart50_es TRAIN=/ruta/train.csv VALIDATION=/ruta/validation.csv OUT=artifacts/checkpoints/mbart50_es
    make train MODEL=umt5_base_es TRAIN=/ruta/train.csv VALIDATION=/ruta/validation.csv OUT=artifacts/checkpoints/umt5_base_es
    make infer MODEL=t5s_spanish CHECKPOINT=artifacts/checkpoints/t5s_spanish/best INPUT=artifacts/candidates.jsonl OUT=artifacts/predictions/t5s_spanish.jsonl
    make evaluate PREDICTIONS=artifacts/predictions/t5s_spanish.jsonl REFERENCES=/ruta/validation.csv OUT=artifacts/evaluation/t5s_spanish

La revisión humana ciega es opcional y solo se ejecuta cuando ya hay
predicciones de los cuatro modelos:

    make review-sheet INPUT=artifacts/candidates.jsonl PREDICTIONS=artifacts/predictions/all_models.jsonl OUT=artifacts/review SAMPLE_SIZE=100

La guía metodológica detallada está en docs/EXPERIMENT_PROTOCOL.md.

