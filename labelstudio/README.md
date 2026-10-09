# Revisión humana con Label Studio

Label Studio es la última revisión humana del pipeline. `silver/evidence/` no
es todavía Gold: contiene candidatos recuperados de PubMed y puntuaciones de
similitud. Una persona debe decidir la relación de la evidencia y la etiqueta
final antes de que una fila entre al conjunto de entrenamiento.

## 1. Crear un proyecto limpio y ejecutar Label Studio

Con Docker Desktop iniciado:

```bash
docker run -d --name mednews-labelstudio \
  -p 8080:8080 \
  -v "$PWD/labelstudio-data:/label-studio/data" \
  heartexlabs/label-studio:latest
```

Abre `http://localhost:8080`, crea una cuenta local y un proyecto. En la
configuración del proyecto copia el contenido de [`config.xml`](config.xml).
El proyecto usado durante la prueba fue `MedNews LATAM - Evidence Review`
(id 1); para un conjunto limpio se puede crear otro proyecto y pasar su id a
los scripts.

No reutilices un proyecto que tenga anotaciones creadas con la configuración
anterior (`claim_text`/`label`). La configuración actual usa
`claim`/`final_label`; mezclar ambas produce el error de incompatibilidad de
Label Studio. Para empezar la colaboración, crea un proyecto limpio con la
configuración actual o migra explícitamente las anotaciones antiguas antes de
importar más tareas.

## 2. Importar evidencia sin duplicar

Guarda uno o varios CSV de `silver/evidence/` en una carpeta local y ejecuta:

```bash
export LABEL_STUDIO_TOKEN='token-local-de-label-studio'
python3 labelstudio/import_evidence.py \
  --project-id 1 \
  --input /ruta/a/evidence_1.csv /ruta/a/evidence_2.csv \
  --manifest /ruta/privada/labelstudio-manifest.csv
```

El script conserva la fila más reciente por `record_id`, ignora estados que no
sean `OK`, consulta las tareas que ya existen en el proyecto y solo importa
registros nuevos. Se puede ejecutar después de cada corrida de evidence. No
guardes el token, las API keys ni el manifest con datos en Git.

Cada tarea conserva `evidence_text` completo y además crea `evidence_primary`,
`evidence_additional` y `evidence_count`. La interfaz muestra primero el
candidato mejor ordenado y deja los demás dentro de un panel desplegable. Si
ya tienes tareas importadas con la configuración anterior, ejecuta el mismo
comando con `--refresh-existing` para actualizar solo los datos de las tareas
correspondientes y conservar sus anotaciones. El importador seguirá
deduplicando por `record_id`.

```bash
python3 labelstudio/import_evidence.py \
  --project-id 1 \
  --input /ruta/a/evidence_1.csv \
  --refresh-existing
```

`--refresh-existing` usa la actualización de datos de la tarea; no borra ni
crea otra anotación.

## 3. Anotar

Para cada tarea compara el claim en español con la evidencia principal y abre
los candidatos adicionales solo cuando sea necesario. Marca `supports`,
`contradicts` o `unclear`, marca `RESPALDADA`, `REFUTADA`,
`NO_DETERMINABLE` o `EXCLUIDA`, y escribe una justificación verificable.
`EXCLUIDA` y `NO_DETERMINABLE` se conservan para auditoría, pero no entran al
Gold binario de entrenamiento. La decisión humana prevalece sobre la
puntuación de embedding.

## 4. Exportar las etiquetas

```bash
python3 labelstudio/export_annotations.py \
  --project-id 1 \
  --output /ruta/privada/gold_labels.csv \
  --raw-export /ruta/privada/labelstudio-export.json
```

El exportador toma la anotación completada más reciente por `record_id` y
produce una fila Gold por noticia. El control `final_label` de la interfaz se
exporta como la columna canónica `label`. El CSV resultante debe revisarse
antes de generar el split 70/15/15. `annotation_id` permite volver a la tarea
exacta.

## Cuándo importar y cuánto demora

La importación se puede ejecutar después de cada CSV de evidence o en lotes
diarios. No depende de los cron de OCI: Label Studio es la etapa humana y no
tiene el timeout síncrono de una Function. El manifest local y la consulta del
`record_id` existente hacen que volver a ejecutar el comando sea seguro. Una
fila que todavía no esté en estado `OK` se deja fuera hasta que evidence la
complete en una corrida posterior. Los tiempos de las Functions, el drenaje
horario y los límites de PubMed/Gemini/Embed están en
[`docs/limits_and_timing.md`](../docs/limits_and_timing.md).

### Sincronización automática en una VM OCI

Para una instancia privada de Label Studio,
[`sync_evidence_from_object_storage.py`](sync_evidence_from_object_storage.py)
lee con el *Instance Principal* de la VM únicamente los objetos
`silver/evidence/evidence_run_*.csv`. Ignora expresamente los CSV históricos
`evidence_translated_*` y los objetos rechazados. Antes de importar, aplica
esta misma `config.xml`; después ejecuta `import_evidence.py` con
`--refresh-existing`, por lo que corrige una importación directa anterior sin
borrar anotaciones.

Instala `oci>=2.0.0` en un entorno virtual bajo
`/opt/mednews-labelstudio-sync/` (no bajo `/home`: una VM con SELinux puede
impedir que systemd ejecute código desde allí), copia los tres archivos de
`labelstudio/systemd/` a `/etc/systemd/system/` y crea
`/etc/mednews-labelstudio-sync.env` desde el ejemplo con un token de API de
Label Studio. El token nunca debe estar en Git. Finalmente habilita el timer:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mednews-labelstudio-sync.timer
sudo systemctl start mednews-labelstudio-sync.service
sudo journalctl -u mednews-labelstudio-sync.service -n 50 --no-pager
```

El timer revisa Object Storage cada cinco minutos. No requiere abrir Label
Studio al exterior ni añadir otro schedule al pipeline OCI.

## 5. Trabajo entre dos personas

Para colaborar en la misma instancia, el dueño del proyecto invita al otro
usuario desde **Members** y ambos usan el mismo `project_id`. La instancia debe
ser accesible para ambos: `localhost` solo sirve en la computadora que ejecuta
el contenedor. Cada importación se deduplica por `record_id`; las
reimportaciones no crean una segunda tarea.

Una división simple es que tu amigo revise las tareas cuyo `evidence_run_id`
proviene de la corrida de las 08:00 Lima y tú las de las 20:00 Lima. Antes de
empezar, registren esa asignación en una lista compartida de `record_id`; el
manifest controla importaciones, no asignaciones de revisor. Si se necesita
doble anotación, ambos revisan la misma tarea y resuelven el desacuerdo antes
de exportar.

## 6. Validar antes de publicar Gold

No copies ni edites a mano un CSV de `silver/evidence/` para crear Gold. Primero
exporta las anotaciones humanas y luego valida la exportación:

```bash
python3 labelstudio/export_annotations.py \
  --project-id 1 \
  --output labelstudio/artifacts/gold_labels_2026-10-07.csv \
  --raw-export labelstudio/artifacts/labelstudio_export_2026-10-07.json

python3 labelstudio/validate_gold.py \
  --input labelstudio/artifacts/gold_labels_2026-10-07.csv \
  --reviewed-output labelstudio/artifacts/gold_reviewed_2026-10-07.csv \
  --training-output labelstudio/artifacts/gold_training_2026-10-07.csv \
  --rejected-output labelstudio/artifacts/gold_rejected_2026-10-07.csv \
  --fail-on-reject
```

El validador exige trazabilidad de la anotación humana y una combinación
coherente entre relación y etiqueta: `RESPALDADA/supports`,
`REFUTADA/contradicts` o `NO_DETERMINABLE/unclear`. Mantiene todas las filas
válidas en el CSV de auditoría (`gold/reviewed/`), pero solo deja
`RESPALDADA` y `REFUTADA` en el CSV de entrenamiento (`gold/training/`). Las
filas rechazadas requieren corrección en Label Studio; con `--fail-on-reject`
no debes publicarlas.

Después de una validación sin rechazados, publica explícitamente los dos CSV
generados —no el CSV Silver ni el export JSON crudo— en Object Storage:

```bash
oci os object put --namespace "$OBJECT_STORAGE_NAMESPACE" \
  --bucket-name mednews-data \
  --name "gold/reviewed/gold_reviewed_2026-10-07.csv" \
  --file labelstudio/artifacts/gold_reviewed_2026-10-07.csv

oci os object put --namespace "$OBJECT_STORAGE_NAMESPACE" \
  --bucket-name mednews-data \
  --name "gold/training/gold_training_2026-10-07.csv" \
  --file labelstudio/artifacts/gold_training_2026-10-07.csv
```

Guarda el export JSON y el CSV de rechazados como evidencia privada de auditoría;
no forman parte del conjunto binario de entrenamiento.

```text
silver/evidence/*.csv
        |
        | import_evidence.py (dedupe record_id)
        v
Label Studio: relación + etiqueta + nota humana
        |
        | export_annotations.py + validate_gold.py
        v
gold/reviewed (auditoría) + gold/training (binario)
```
