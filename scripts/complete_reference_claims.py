#!/usr/bin/env python3
"""Completa un lote piloto de reference_claims a partir del texto de cada noticia.

Las referencias son borradores de anotación y deben revisarse antes de usarlas
como gold labels definitivos.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


CLAIMS = {
    "a2dc567907dbb7c3": "Omar Fernández destinó recursos del Fondo Gadiel a respaldar a pacientes con esclerosis lateral amiotrófica en República Dominicana.",
    "ae548e20ca9428de": "La nota presenta predicciones astrológicas para Sagitario sobre salud, amor y dinero para el 25 de octubre de 2026.",
    "1130722f4dbe0f0a": "Seis médicos advirtieron que el último concierto de Ozzy Osbourne podía ser mortal debido a sus problemas de salud.",
    "beaa7a6d51d1d94a": "El Día Internacional de la Lucha contra el Cáncer de Mama promueve la detección temprana y los controles periódicos.",
    "45967c4ffcd0a091": "El diagnóstico y tratamiento del cáncer de mama pueden producir miedo, ansiedad y depresión, por lo que el apoyo psicológico puede ayudar a las pacientes.",
    "65c077cceebd1d76": "La nota repasa las enfermedades y otras causas de muerte atribuidas a los últimos quince pontífices.",
    "60b12c0c1156d409": "El Día Mundial contra el Cáncer de Mama busca concienciar sobre la prevención, la mamografía y el diagnóstico temprano.",
    "8108212657b1c5d7": "La nota presenta predicciones astrológicas para Géminis sobre salud, amor y dinero para el 10 de octubre de 2026.",
    "129e3b5e8d6e5fc1": "El Festival Culinario Infantil Lonchera Saludable promueve hábitos de nutrición y preparación de alimentos entre los niños.",
    "04c39a43797d2cf2": "La nota presenta predicciones astrológicas para Virgo sobre salud, amor y dinero para el 16 de octubre de 2026.",
    "6fccb26f00219a89": "Un estudio de Duke-NUS identificó dos periodos de aceleración del envejecimiento del sistema inmunitario, cerca de los 40 y después de los 60 años.",
    "13be427eb71087c5": "El papa León XIV anunció que será operado para retirar un nódulo pulmonar detectado en una fase temprana.",
    "59de232033488af5": "Dermatólogos recomiendan usar jabones suaves y mantener la hidratación para cuidar los talones agrietados.",
    "d1288c8c7b0cd39c": "El Foro SUMA + Inspira 2026 abordará salud mental, alcohol, fiesta y vida digital con estudiantes de Culiacán.",
    "50b5a3e9fc8f7148": "Una radiografía mostró una buena recuperación del pulmón operado del papa León XIV y controles posoperatorios normales.",
    "eb06b33bec27194d": "La nota recomienda reducir el uso de pantallas, cenar ligero y realizar actividad física para mejorar el sueño.",
    "cc9468ebfe2d88c8": "La Resolución 00270-26 de Sisalril amplió el cuadro dominicano de enfermedades profesionales para incluir afecciones psicosociales y varios tipos de cáncer ocupacional.",
    "b9da78efaac7a478": "Venezuelan Press y el CNP Carabobo donaron seis nebulizadores y diez tensiómetros digitales a periodistas adultos mayores y sus familiares.",
    "321db4701a038d10": "El presidente del Colegio Médico Dominicano advirtió que la falta de sangre e insumos puede comprometer la atención hospitalaria en República Dominicana.",
    "fc686fd1bd68553a": "La escasez mundial de tratamientos hormonales ha coincidido con aumentos de precios y problemas de abastecimiento en farmacias españolas.",
    "06fba92d951b2015": "La campaña Sonríe Guayaquil ofrece educación de higiene bucal y atención odontológica preventiva gratuita a estudiantes.",
    "e60bb47f53d04ed0": "La nota presenta predicciones astrológicas para Escorpio sobre salud, amor y dinero para el 14 de octubre de 2026.",
    "f65d53fa4b35b542": "La enfermedad tromboembólica venosa se produce cuando un coágulo obstruye una vena y puede causar trombosis profunda o embolia pulmonar.",
    "1ddab8b36046c6ec": "La cirugía bariátrica se presenta como una opción terapéutica para pacientes con obesidad que cumplen criterios clínicos y requieren seguimiento profesional.",
    "9e2b91d6124bad5c": "Metrocentro organizó una actividad de Octubre Rosa para promover la prevención y la detección temprana del cáncer de mama.",
    "b68e2a0012c4ce6e": "CMS anunció que la prima mensual promedio de Medicare Advantage disminuiría 16,5 % en 2027.",
    "fdebc6b37a2bca48": "Una jornada veterinaria en Maracaibo ofrecerá vacunación, desparasitación y esterilización para perros y gatos.",
    "f710c1e14b6c3bd7": "La nota sostiene que los huevos de tortuga carey y de aves bubíes no son potenciadores sexuales y que su consumo constituye un delito ambiental.",
    "e22146b2b23d1deb": "Una presentación de Philip Morris sostuvo que la innovación en productos libres de humo podría reducir daños del tabaquismo si existe evidencia y regulación responsable.",
    "d58f9f432007a7d9": "El hospital Torrecárdenas impulsa mejoras de asistencia humanizada orientadas al bienestar de pacientes ingresados y al confort de áreas infantiles y de salud mental.",
    "e45fe540df8f42b5": "La farmacéutica rusa PSK Pharma presentó un inhalador triple que combina glicopirronio, indacaterol y ciclesonida para tratar el asma.",
    "13392196b9faddf2": "Francia detectó por primera vez en humanos el virus shamonda en un paciente gravemente inmunodeprimido que falleció.",
    "aad2ea89170c9197": "El consumo regular de peras puede favorecer la digestión y contribuir al control del azúcar en sangre por su contenido de fibra.",
    "cfaa102ab02986d1": "Un juez ordenó entregar y administrar 152 ampollas de asparaginasa Erwinia a un adolescente con leucemia.",
    "2ead25da2c53f78d": "Osvaldo Cardoso quedó con secuelas neurológicas graves después de una operación de vesícula, según el caso relatado por su familia.",
    "190a236d54391418": "Un informe de salud mental en Chile señaló que alrededor del 18,5 % de la población presenta problemas de salud mental.",
    "6e0b8f73a95c6bf1": "LA GACETA lanzó el newsletter Bienestar +60 para promover salud, autonomía y proyectos en personas mayores de 60 años.",
    "e1f1a8a90e45070d": "El bienestar emocional y mental de la gestante forma parte de un embarazo saludable junto con el cuidado físico y médico.",
    "acd3b126aae0453a": "La provincia de Río Negro brinda atención pública de salud mental mediante una red comunitaria presente en sus 36 hospitales.",
    "57339ba6e3ec0667": "La Feria Informativa de Cáncer de Magallanes promovió la prevención, la detección temprana y el acceso a información oncológica.",
    "9e0986483db52b64": "La DNCD y Digemaps pusieron en marcha un sistema de vigilancia para controlar medicamentos que contienen sustancias controladas.",
    "602027c6ebbc1510": "Bañarse todos los días no es necesariamente indispensable y el agua caliente o los jabones fuertes pueden irritar y resecar la piel.",
    "a95d49e968365246": "El INCOR realizó con éxito una cirugía cardíaca híbrida de alta complejidad a un bebé prematuro de 1.600 gramos con coartación de la aorta.",
    "5b68dde8e72a25e1": "La Municipalidad de Lima organizó una campaña veterinaria gratuita con consultas, desparasitación y orientación sobre tenencia responsable.",
    "6b923afceecbed7b": "El gobierno de José Antonio Kast registró la salida, cambio o nombramiento fallido de 45 seremis durante sus primeros siete meses.",
    "4233dd36d0056b55": "La medicina está priorizando la prevención, el diagnóstico temprano y la preservación de la calidad de vida antes de que aparezcan las enfermedades.",
    "61319b8f4a99625a": "La nota presenta predicciones astrológicas para Acuario sobre salud, amor y dinero para el 14 de octubre de 2026.",
    "66ec297d5b705108": "EsSalud realizó 9.255 atenciones y procedimientos médicos especializados en Loreto durante una campaña de ocho días.",
    "253f41f2bf10a0c3": "Francia registró la primera detección documentada del virus shamonda en una persona y no identificó riesgo para la población general.",
    "f8aa053c8c17b4b2": "Una cámara civil condenó a una médica por mala praxis después de que una paciente recibiera silicona en lugar de ácido hialurónico.",
    "be481a81286c4e21": "En Japón, la micropigmentación capilar se considera un procedimiento médico que requiere licencia sanitaria.",
    "5e0b47742b78f6e4": "Un estudio asoció el consumo elevado de alimentos ultraprocesados con una mayor frecuencia de lesiones cerebrales silenciosas.",
    "c4949dfad1f229bb": "El médico mexicano Marco Antonio Zenteno Castellanos impulsó el desarrollo del neurointervencionismo y de la terapia endovascular neurológica en México.",
    "4fb46e516e6aa3e4": "Un informe indicó que la depresión y la ansiedad en Chile alcanzaron niveles especialmente altos desde la pandemia.",
    "14efbd7a5300f0c5": "La República Democrática del Congo inició un ensayo clínico de la vacuna Ervebo para evaluar su protección frente a la cepa Bundibugyo del ébola.",
    "75b305e1249199d8": "El Servicio Nacional de Salud informó que designó 20.730 médicos generales, especialistas y residentes entre 2020 y 2026.",
    "9c4f021c10d44a11": "El Hospital Urquiza realizó el primer implante de un cardiodesfibrilador subcutáneo en la costa del Uruguay a un paciente con síndrome de Brugada.",
    "1d9f81b4794f3180": "La carrera Botas Rosas promovió la prevención y la detección oportuna del cáncer de mama en Hidalgo del Parral.",
    "4c2963bc295c1583": "Jenna Ortega relató que presenta síntomas y rituales asociados al trastorno obsesivo-compulsivo desde los siete años.",
    "dd057863fed15401": "Aldeas Infantiles SOS destacó que la salud mental infantil requiere vínculos seguros, prevención, detección temprana y acceso a atención especializada.",
    "44bd1650eb748df5": "Gremios dominicanos de psicología pidieron mayor inversión en salud mental y prevención del suicidio.",
    "5f360b7abe171494": "La nota presenta predicciones astrológicas para Capricornio sobre salud, amor y dinero para el 16 de octubre de 2026.",
    "27da63fd7db3eb94": "La OMS impulsa integrar la salud bucal en la agenda de enfermedades no transmisibles y en los servicios esenciales de salud.",
    "52895f0535a64ae7": "Una oftalmopediatra advirtió que usar lentes no garantiza por sí solo una buena salud visual y recomendó controles preventivos.",
    "104b47ee141d75f6": "Harvard Health destacó la natación, el tai chi, la fuerza, la caminata y los ejercicios de Kegel como actividades útiles para la salud.",
    "1a16f110fe05435a": "Xalapa iluminó edificios públicos de color rosa para concienciar sobre la prevención y la detección temprana del cáncer de mama.",
    "c4416132e1ef218a": "El SNS inauguró una Unidad de Hemodinamia en el Hospital Gautier para ampliar el acceso a procedimientos cardiovasculares y neurovasculares.",
    "a961e5a074b7c0a9": "El cáncer de mama mantiene una alta carga mundial y su detección temprana se relaciona con mejores posibilidades de supervivencia.",
    "74112a5f662fe6fb": "Magaly Medina informó que padece una infección intestinal y que sigue una dieta indicada por su médico.",
    "54fd6aa2820f4aac": "Caminar durante unos diez minutos después de comer puede ayudar a reducir los picos de azúcar en sangre.",
    "2a33da0250d851fd": "Una adolescente de Durazno fue diagnosticada con tuberculosis sin presentar síntomas y las autoridades estudiaron a sus contactos.",
    "b77b73b1529be21d": "La epidermólisis bullosa es una enfermedad rara que vuelve la piel extremadamente frágil y se conoce como piel de mariposa.",
    "64690d32121fa7b9": "La cirugía del papa León XIV confirmó un adenocarcinoma pulmonar y permitió extirpar el lóbulo inferior derecho.",
    "5ca0a56707e8d3a3": "Guatemala validó una estrategia para prevenir y atender diabetes, hipertensión, enfermedad renal crónica y obesidad entre 2027 y 2037.",
    "824d61d569677f96": "Varias celebridades que superaron el cáncer de mama han utilizado sus experiencias para promover la detección temprana y el apoyo a pacientes.",
    "2d235770f3ceb9be": "La pérdida gestacional o perinatal puede generar depresión y estrés postraumático, por lo que especialistas recomiendan visibilizar y acompañar el duelo.",
    "d53aa7d4325beca3": "La artritis reumatoide es una enfermedad autoinmune inflamatoria que requiere diagnóstico temprano, tratamiento y actividad física guiada.",
    "9fd14cf11e775e54": "En Magallanes se reportó que 25 mujeres habrían fallecido por cáncer de mama durante 2025 y se destacó la importancia de la mamografía.",
    "233bfd66fa9c09f1": "La Sisalril incorporó el burnout, los trastornos mentales y otras afecciones al cuadro dominicano de enfermedades profesionales.",
    "b6a8bfefc27b7bea": "Más de 7.000 personas participaron en Zamora en una marcha solidaria de la Asociación Española Contra el Cáncer de Mama.",
    "c9bd67c357fc88d6": "La Ucab Guayana organizó una jornada para concienciar sobre el diagnóstico precoz y la mamografía frente al cáncer de mama.",
    "06154350c5f22341": "La OMS advirtió que la falta de innovación y acceso a tratamientos para la hipertensión durante el embarazo causa una importante carga de muertes maternas y neonatales.",
    "e2d56d22abe92ed3": "Una jornada comunitaria en Los Peralejos ofreció atención médica gratuita en seis especialidades.",
    "6a233b6d1ccfa180": "Los médicos informaron que el papa León XIV evolucionaba con normalidad después de una cirugía por cáncer de pulmón.",
    "c9a10389447d3b50": "El adenocarcinoma pulmonar diagnosticado al papa León XIV puede presentarse incluso en personas que nunca han fumado.",
    "d045315457860831": "El programa EPIC de Nueva York ayuda a mayores de 65 años a reducir el costo de sus medicamentos y complementa Medicare Parte D.",
    "babb9579c83541b3": "La nota presenta predicciones astrológicas para Libra sobre salud, amor y dinero para el 25 de octubre de 2026.",
    "895f473a93fe8719": "La nota propone utilizar agua de arroz como tónico facial y advierte que puede causar tirantez en piel seca.",
    "df06d8bb9100254b": "Un consorcio europeo inició el desarrollo de una terapia celular estandarizada contra el mieloma múltiple.",
    "fd19d4c8bd3f1dd8": "El Consejo de la Judicatura de Ecuador aprobó un examen de confianza que evaluará patrimonio, conducta y credibilidad de aspirantes judiciales.",
    "46355ee5ff0f6c0b": "El papa León XIV sería hospitalizado para someterse a una operación destinada a extirpar un nódulo pulmonar.",
    "a0344ba0f5ea5d2a": "La nota presenta predicciones astrológicas para Tauro sobre salud, amor y dinero para el 18 de octubre de 2026.",
    "1a1f8382dabd365a": "La Embajada de Estados Unidos alertó sobre un brote de salmonela en el Hospital CIMA con casos sospechosos, casos confirmados y pacientes hospitalizados.",
    "9cc1cdddfe9bce75": "Victoria Tolosa Paz afirmó que la reducción del programa Remediar limitaría el listado nacional de medicamentos esenciales y trasladaría presión a las provincias.",
    "15d0229e445b11d3": "El papa León XIV sería hospitalizado para extirpar un nódulo pulmonar detectado durante revisiones médicas.",
    "9e168a7e5bf3df23": "Ruth Gamarra se encuentra hospitalizada y su entorno informó que atraviesa un complejo estado de salud que podría requerir una operación.",
    "7d2999999bb607e4": "Un especialista recomendó priorizar la mamografía y la detección temprana porque el cáncer de mama puede avanzar sin dolor en sus primeras etapas.",
    "d58f5e27f6e96614": "La nota propone usar arroz como exfoliante casero para ayudar a tratar los granitos de la espalda.",
    "518f51aa7c2842d5": "La nota presenta predicciones astrológicas para Escorpio sobre salud, amor y dinero para el 12 de octubre de 2026.",
    "34f242f193b7abf0": "La nota presenta predicciones astrológicas para Acuario sobre salud, amor y dinero para el 23 de octubre de 2026.",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    with Path(args.input).open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    missing = sorted({row["record_id"] for row in rows} - CLAIMS.keys())
    if missing:
        raise ValueError(f"Faltan claims para record_id: {', '.join(missing)}")
    for row in rows:
        row["reference_claim"] = CLAIMS[row["record_id"]]
        row["annotation_status"] = "AI_DRAFT"
        row["annotator_id"] = "codex"
        row["annotation_notes"] = "Borrador generado a partir de la noticia; revisar antes de considerarlo gold label."

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"completed={len(rows)} output={output}")


if __name__ == "__main__":
    main()

