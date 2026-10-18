# Entrenadores de los modelos de tesis

Cada carpeta tiene un `train.py` ejecutable. Los motores compartidos de abajo
evitan duplicar el contrato de datos; los pesos y resultados se escriben en
`artifacts/training_runs/`, que está ignorado por Git.

```text
experiments/models/
├── common.py                 validación Gold, split común, métricas y artefactos
├── sparse_train.py           TF-IDF + Naive Bayes / SVM lineal
├── sequence_train.py         Word2Vec + LSTM / GloVe + BiLSTM
├── transformer_train.py      fine-tuning de BERT, BETO y RoBERTa-BNE
├── sbert_train.py            SBERT congelado + regresión logística
├── 01_tfidf_naive_bayes/train.py
├── 02_tfidf_logistic_regression/train.py
├── 03_tfidf_linear_svm/train.py
├── 04_word2vec_lstm/train.py
├── 05_glove_bilstm/train.py
├── 06_bert_beto/bert/train.py
├── 06_bert_beto/beto/train.py
├── 07_roberta_bne/train.py
└── 08_sbert/train.py
```

El esquema tiene ocho carpetas numeradas y nueve corridas si se ejecutan BERT
y BETO por separado. El paper de Edwin presenta otra lista de ocho
combinaciones; estos scripts siguen el esquema de modelos de la tesis, no
replican sus modelos ni sus resultados.

## Contrato experimental

- Una fila de `training.csv` representa una noticia (`record_id`). Las únicas
  entradas textuales son `title` y `body`, concatenados por el baseline común.
- Solo se entrenan etiquetas humanas binarias `REFUTADA` y `RESPALDADA`. Claims,
  evidencia, fuente y tema no se agregan al texto de entrada.
- El primer comando genera `split_manifest.csv`. Los otros modelos reciben ese
  manifiesto con `--split-manifest`, por lo que comparan los mismos artículos y
  no solo usan la misma semilla.
- El split es 70/15/15 y se conserva en cada carpeta de corrida. Para modelos
  neuronales, validation elige el checkpoint por pérdida; test se evalúa al
  final y no se usa para ajustar parámetros.
- Cada corrida guarda `metrics.json`, `predictions.csv` y su manifiesto. Los
  modelos clásicos guardan `.joblib`; las redes guardan un checkpoint. No
  versionar CSV, pesos ni resultados.

## Preparar dependencias

Desde la raíz del repositorio, instala solo la familia que vayas a ejecutar:

```bash
python3 -m pip install -e '.[training]'
python3 -m pip install -e '.[training,sequence]'
python3 -m pip install -e '.[training,transformers]'
```

`sequence` habilita Word2Vec y LSTM. `transformers` instala PyTorch, Hugging
Face y sentence-transformers. Los pesos de modelos grandes no se instalan con
pip: Hugging Face los obtiene al ejecutar el primer fine-tuning, así que se
necesita conectividad y espacio local. `--help` funciona sin instalar esas
dependencias.

## Corridas comparables

Reemplaza `/ruta/privada/training.csv` por el CSV binario Gold generado por
[`scripts/04_build_training_dataset.py`](../../../scripts/04_build_training_dataset.py).
Primero ejecuta el baseline logístico, que crea el manifiesto compartido:

```bash
GOLD=/ruta/privada/training.csv
python experiments/models/02_tfidf_logistic_regression/train.py \
  --input "$GOLD" --run-name pilot100_seed42 --limit 100 --min-rows 100 --seed 42
SPLIT=artifacts/training_runs/pilot100_seed42/split_manifest.csv
```

Luego reutiliza ese archivo en cada variante; usa un nombre de corrida distinto
para que los resultados no se sobrescriban:

```bash
python experiments/models/01_tfidf_naive_bayes/train.py --input "$GOLD" --split-manifest "$SPLIT" --run-name nb_seed42
python experiments/models/03_tfidf_linear_svm/train.py --input "$GOLD" --split-manifest "$SPLIT" --run-name svm_seed42
python experiments/models/04_word2vec_lstm/train.py --input "$GOLD" --split-manifest "$SPLIT" --run-name word2vec_lstm_seed42
python experiments/models/05_glove_bilstm/train.py --input "$GOLD" --split-manifest "$SPLIT" --run-name glove_bilstm_seed42 --glove-file /ruta/local/glove.6B.100d.txt
python experiments/models/06_bert_beto/bert/train.py --input "$GOLD" --split-manifest "$SPLIT" --run-name bert_seed42
python experiments/models/06_bert_beto/beto/train.py --input "$GOLD" --split-manifest "$SPLIT" --run-name beto_seed42
python experiments/models/07_roberta_bne/train.py --input "$GOLD" --split-manifest "$SPLIT" --run-name roberta_bne_seed42
python experiments/models/08_sbert/train.py --input "$GOLD" --split-manifest "$SPLIT" --run-name sbert_seed42
```

Si se usa un corpus mayor, fija `--limit` en la primera corrida y eleva
`--min-rows` según el protocolo aprobado; el manifiesto hace que los demás
modelos hereden exactamente esa selección. El piloto de 100 solo deja 15
artículos de test: reportarlo como exploratorio, no como una comparación
definitiva.

## Qué hace cada entrenador

| Carpeta | Modelo y decisión de implementación |
|---|---|
| `01_tfidf_naive_bayes` | TF-IDF de unigramas/bigramas + MultinomialNB. |
| `02_tfidf_logistic_regression` | Reutiliza `scripts/03_train_baseline.py`, con su baseline de mayoría. |
| `03_tfidf_linear_svm` | TF-IDF de unigramas/bigramas + LinearSVC balanceado. |
| `04_word2vec_lstm` | Word2Vec skip-gram se aprende solo con noticias del split train; después entrena el LSTM. |
| `05_glove_bilstm` | Usa un archivo GloVe local provisto con `--glove-file`; no descarga ni redistribuye vectores. |
| `06_bert_beto/bert` | Fine-tuning de `google-bert/bert-base-multilingual-cased`. |
| `06_bert_beto/beto` | Fine-tuning de `dccuchile/bert-base-spanish-wwm-cased` (BETO). |
| `07_roberta_bne` | Fine-tuning de `PlanTL-GOB-ES/roberta-base-bne`; su ficha de modelo advierte que está obsoleto/no recomendado, así que mantenlo como comparación exploratoria o reemplázalo tras acordar el protocolo. |
| `08_sbert` | Embeddings multilingües SBERT congelados + LogisticRegression entrenada solo con train; no es fine-tuning del encoder. |

Fichas de los checkpoints: [BERT multilingüe](https://huggingface.co/google-bert/bert-base-multilingual-cased), [BETO](https://huggingface.co/dccuchile/bert-base-spanish-wwm-cased), [RoBERTa-BNE](https://huggingface.co/PlanTL-GOB-ES/roberta-base-bne) y [SBERT multilingüe](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2).

Los scripts implementan el protocolo y reportan métricas; no implican que los
modelos ya hayan sido entrenados ni que el tamaño actual del Gold sea
suficiente. Antes de interpretar resultados, inspecciona distribución de
clases, artículos por fuente/país, duplicados y el número efectivo de noticias.
