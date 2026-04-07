from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
import pandas as pd
import os
from io import BytesIO
from pathlib import Path
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
import unicodedata
import textwrap

app = FastAPI()

# =========================
# CONFIG
# =========================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY was not found in environment variables.")

client = OpenAI(api_key=OPENAI_API_KEY)

BASE_DIR = Path(__file__).resolve().parent
EXCEL_FILE = BASE_DIR / "planilha_endo10.xlsx"
SHEET_NAME = "En"

if not EXCEL_FILE.exists():
    raise RuntimeError(
        f"Spreadsheet not found: {EXCEL_FILE}. "
        "Make sure dataset_Abr2026.xlsx is inside the project and included in the Railway deploy."
    )

# =========================
# CORS
# =========================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # depois você pode restringir ao domínio do frontend
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# STATIC
# =========================
STATIC_DIR = BASE_DIR / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# =========================
# LOAD DATA
# =========================
try:
    df = pd.read_excel(EXCEL_FILE, sheet_name=SHEET_NAME)
except Exception as e:
    raise RuntimeError(
        f"Error loading spreadsheet '{EXCEL_FILE}' / sheet '{SHEET_NAME}': {e}"
    )

df.columns = [str(col).strip() for col in df.columns]
for col in df.columns:
    if df[col].dtype == "object":
        df[col] = df[col].astype(str).fillna("").str.strip()

# Compatibilidade com nomes antigos/variáveis
if "PULPT VITALITY" in df.columns and "PULP VITALITY" not in df.columns:
    df = df.rename(columns={"PULPT VITALITY": "PULP VITALITY"})

# =========================
# QUESTIONS
# Valores canônicos internos em inglês
# =========================
questions = [
    {
        "field": "PAIN",
        "question": "Pain",
        "options": [
            {"value": "Absent", "description": "The patient does not report pain."},
            {"value": "Present", "description": "The patient reports pain or some type of discomfort."},
        ],
    },
    {
        "field": "ONSET",
        "question": "Onset of pain",
        "options": [
            {"value": "Not applicable", "description": "Use this when the patient does not report pain."},
            {"value": "Spontaneous", "description": "The pain starts spontaneously, without any provoking stimulus."},
            {"value": "Provoked", "description": "The pain starts after a stimulus, such as cold, heat, pressure, or sweets."},
        ],
    },
    {
        "field": "PULP VITALITY",
        "question": "Pulp vitality",
        "options": [
            {"value": "Altered", "description": "There is an exaggerated or persistent painful response to vitality testing."},
            {"value": "Negative", "description": "There is no response to vitality testing."},
            {"value": "Normal", "description": "There is a mild, transient response that disappears shortly after the stimulus is removed."},
        ],
    },
    {
        "field": "PERCUSSION",
        "question": "Percussion",
        "options": [
            {"value": "Not applicable", "description": "Use this when percussion testing is not applicable in the clinical situation."},
            {"value": "Normal", "description": "There is no pain or sensitivity on percussion."},
            {"value": "Sensitive", "description": "There is pain or sensitivity on percussion."},
        ],
    },
    {
        "field": "PALPATION",
        "question": "Palpation",
        "options": [
            {"value": "Edema", "description": "There is swelling of the adjacent tissues."},
            {"value": "Fistula", "description": "There is a sinus tract or a mucosal/cutaneous opening communicating with the root apex."},
            {"value": "Normal", "description": "There is no pain on palpation."},
            {"value": "Sensitive", "description": "There is pain or sensitivity on palpation."},
        ],
    },
    {
        "field": "RADIOGRAPHY",
        "question": "Radiographic finding",
        "options": [
            {"value": "Circumscribed", "description": "The lesion is well delimited, with relatively distinct borders."},
            {"value": "Diffuse", "description": "The lesion has poorly defined borders or a gradual transition to adjacent tissues."},
            {"value": "Thickening", "description": "There is widening or thickening of the periodontal ligament space."},
            {"value": "Normal", "description": "The lamina dura is intact and the periodontal ligament space is uniform."},
            {"value": "Diffuse radiopaque", "description": "There is a diffuse increase in radiopacity with ill-defined borders and gradual transition to adjacent bone."},
        ],
    },
]

# =========================
# SESSIONS
# =========================
sessions = {}


# =========================
# HELPERS
# =========================
def normalize_text(text: str) -> str:
    if text is None:
        return ""
    text = str(text).strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("\n", " ").replace("\r", " ")
    text = " ".join(text.split())
    return text


def create_session_if_needed(session_id: str):
    if session_id not in sessions:
        sessions[session_id] = {
            "language": None,
            "stage": "greeting",  # greeting -> triage -> completed
            "current_question": 0,
            "answers": {},
            "pending_answer": None,
            "diagnosis_result": {}
        }


def detect_language(text: str) -> str:
    try:
        prompt = (
            "Detect the language of the following text. "
            "Respond only with the language name in English, such as: "
            "English, Portuguese, Spanish, French, Italian, German, Chinese, Arabic.\n\n"
            f"Text: {text}"
        )
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0
        )
        language = response.choices[0].message.content.strip()
        return language if language else "English"
    except Exception:
        return "English"


def translate_text(text: str, target_language: str) -> str:
    if not text:
        return text
    if normalize_text(target_language) == "english":
        return text
    try:
        prompt = (
            f"Translate the following text into {target_language}. "
            "Keep the meaning clear and natural. "
            "Preserve line breaks and list structure.\n\n"
            f"{text}"
        )
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0
        )
        translated = response.choices[0].message.content.strip()
        return translated if translated else text
    except Exception:
        return text


def is_yes(text: str) -> bool:
    value = normalize_text(text)
    yes_values = {
        "yes", "y", "yeah", "yep", "ok", "okay", "confirm", "confirmed",
        "sim", "s", "claro", "confirmo",
        "si", "sí",
        "oui",
        "ja",
        "hai"
    }
    return value in yes_values


def is_no(text: str) -> bool:
    value = normalize_text(text)
    no_values = {
        "no", "n", "nope", "negative",
        "nao", "não", "nunca",
        "non",
        "nein"
    }
    return value in no_values


def get_current_question():
    return questions[sessions_current()["current_question"]]


def sessions_current():
    raise RuntimeError("This helper should not be called directly without session context.")


def get_question_by_index(index: int):
    if index < 0 or index >= len(questions):
        raise HTTPException(status_code=400, detail="Invalid question index.")
    return questions[index]


def build_question_text(index: int, language: str) -> str:
    q = get_question_by_index(index)
    base_text = f"{q['question']}\n\n"
    for opt in q["options"]:
        base_text += f"{opt['value']} - {opt['description']}\n"
    return translate_text(base_text.strip(), language)


def build_confirmation_text(value: str, language: str) -> str:
    text = f"Based on your answer, may I consider **{value}**? (Type: Yes or No)"
    return translate_text(text, language)


def build_final_message(language: str) -> str:
    text = "Screening completed. We can now calculate the diagnosis."
    return translate_text(text, language)


def build_inconsistent_message(language: str) -> str:
    text = "I could not find a diagnosis for this exact combination of answers. Please review the selected options."
    return translate_text(text, language)


def build_incomplete_message(language: str) -> str:
    text = "The screening is incomplete. Please answer all questions before requesting the diagnosis."
    return translate_text(text, language)


def build_greeting_message(language: str) -> str:
    text = (
        "Hello! I am Endo10 EVO, your assistant for endodontic diagnosis. "
        "I will guide you through a structured clinical screening."
    )
    return translate_text(text, language)


def interpret_answer_with_ai(question_index: int, user_answer: str, language: str) -> str:
    q = get_question_by_index(question_index)
    options_block = "\n".join(
        [f"- {opt['value']}: {opt['description']}" for opt in q["options"]]
    )

    prompt = f"""
You are an assistant for structured endodontic screening.

Your task is to map the user's answer to exactly one of the allowed options below.

Question:
{q['question']}

Allowed options:
{options_block}

User language: {language}
User answer: {user_answer}

Rules:
- Return only one option value exactly as written.
- Do not explain.
- Do not translate the option.
- If the user answer is a greeting or unrelated to the clinical question, return exactly: INVALID
"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a specialist in endodontic screening."},
                {"role": "user", "content": prompt}
            ],
            temperature=0
        )
        mapped = response.choices[0].message.content.strip()

        allowed = {opt["value"] for opt in q["options"]}
        if mapped in allowed:
            return mapped
        return "INVALID"
    except Exception:
        return "INVALID"


def find_diagnosis_row(answers: dict):
    temp_df = df.copy()

    required_cols = ["PAIN", "ONSET", "PULP VITALITY", "PERCUSSION", "PALPATION", "RADIOGRAPHY"]
    for col in required_cols:
        if col not in temp_df.columns:
            raise RuntimeError(f"Required column '{col}' not found in spreadsheet.")

    for col in required_cols:
        temp_df[f"__norm_{col}"] = temp_df[col].apply(normalize_text)

    conditions = (
        (temp_df["__norm_PAIN"] == normalize_text(answers.get("PAIN", ""))) &
        (temp_df["__norm_ONSET"] == normalize_text(answers.get("ONSET", ""))) &
        (temp_df["__norm_PULP VITALITY"] == normalize_text(answers.get("PULP VITALITY", ""))) &
        (temp_df["__norm_PERCUSSION"] == normalize_text(answers.get("PERCUSSION", ""))) &
        (temp_df["__norm_PALPATION"] == normalize_text(answers.get("PALPATION", ""))) &
        (temp_df["__norm_RADIOGRAPHY"] == normalize_text(answers.get("RADIOGRAPHY", "")))
    )

    result = temp_df[conditions]
    if result.empty:
        return None
    return result.iloc[0]


def wrap_pdf_lines(text: str, width: int = 90):
    if not text:
        return [""]
    lines = []
    for paragraph in str(text).split("\n"):
        wrapped = textwrap.wrap(paragraph, width=width) or [""]
        lines.extend(wrapped)
    return lines


# =========================
# ROOT
# =========================
@app.get("/", response_class=HTMLResponse)
async def root():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return index_file.read_text(encoding="utf-8")

    return """
    <html>
        <body>
            <h2>Endo10 EVO API is running.</h2>
        </body>
    </html>
    """


# =========================
# HEALTH
# =========================
@app.get("/health")
async def health():
    return {"status": "ok"}


# =========================
# PERGUNTAR
# Compatível com frontend antigo
# =========================
@app.post("/perguntar/")
async def perguntar(indice: int = Form(...), session_id: str = Form(...)):
    create_session_if_needed(session_id)
    session = sessions[session_id]
    language = session["language"] or "English"

    if session["stage"] == "greeting":
        texto = build_greeting_message(language)
        return {"pergunta": texto, "mensagem": texto}

    current_index = session["current_question"]

    if current_index < len(questions):
        pergunta_texto = build_question_text(current_index, language)
        return {"pergunta": pergunta_texto}
    else:
        texto = build_final_message(language)
        return {"mensagem": texto}


# =========================
# RESPONDER
# Compatível com frontend antigo
# =========================
@app.post("/responder/")
async def responder(indice: int = Form(...), resposta_usuario: str = Form(...), session_id: str = Form(...)):
    create_session_if_needed(session_id)
    session = sessions[session_id]

    user_text = (resposta_usuario or "").strip()

    if not session["language"]:
        session["language"] = detect_language(user_text)

    language = session["language"]

    # Se ainda está na fase de saudação, não interpreta como dado clínico
    if session["stage"] == "greeting":
        session["stage"] = "triage"
        session["current_question"] = 0
        pergunta_texto = build_question_text(0, language)

        session["pending_answer"] = {
            "type": "FLOW",
            "action": "START_SCREENING"
        }

        return {
            "campo": "__FLOW__",
            "resposta_interpretada": "START_SCREENING",
            "mensagem": pergunta_texto,
            "pergunta": pergunta_texto
        }

    # Se há resposta pendente e o usuário respondeu yes/no
    if session["pending_answer"] is not None:
        if is_yes(user_text):
            pending = session["pending_answer"]

            if pending.get("type") == "ANSWER":
                field = pending["field"]
                value = pending["value"]

                session["answers"][field] = value
                session["pending_answer"] = None
                session["current_question"] += 1

                if session["current_question"] < len(questions):
                    next_question = build_question_text(session["current_question"], language)
                    return {
                        "campo": "__FLOW__",
                        "resposta_interpretada": "ASK_NEXT",
                        "mensagem": next_question,
                        "pergunta": next_question
                    }
                else:
                    session["stage"] = "completed"
                    final_message = build_final_message(language)
                    return {
                        "campo": "__FLOW__",
                        "resposta_interpretada": "READY_FOR_DIAGNOSIS",
                        "mensagem": final_message
                    }

            elif pending.get("type") == "FLOW":
                session["pending_answer"] = None
                if session["current_question"] < len(questions):
                    next_question = build_question_text(session["current_question"], language)
                    return {
                        "campo": "__FLOW__",
                        "resposta_interpretada": "ASK_NEXT",
                        "mensagem": next_question,
                        "pergunta": next_question
                    }

        if is_no(user_text):
            session["pending_answer"] = None
            current_question_text = build_question_text(session["current_question"], language)
            return {
                "campo": "__FLOW__",
                "resposta_interpretada": "REASK_CURRENT",
                "mensagem": current_question_text,
                "pergunta": current_question_text
            }

    # Se já completou e o usuário continua digitando
    if session["stage"] == "completed":
        final_message = build_final_message(language)
        return {
            "campo": "__FLOW__",
            "resposta_interpretada": "READY_FOR_DIAGNOSIS",
            "mensagem": final_message
        }

    # Interpretação normal da resposta clínica
    current_index = session["current_question"]
    if current_index >= len(questions):
        session["stage"] = "completed"
        final_message = build_final_message(language)
        return {
            "campo": "__FLOW__",
            "resposta_interpretada": "READY_FOR_DIAGNOSIS",
            "mensagem": final_message
        }

    mapped_answer = interpret_answer_with_ai(current_index, user_text, language)

    if mapped_answer == "INVALID":
        current_question_text = build_question_text(current_index, language)
        invalid_text = translate_text(
            "I could not understand your clinical answer for this item. Please choose one of the listed options.",
            language
        )
        mensagem = f"{invalid_text}\n\n{current_question_text}"
        return {
            "campo": "__FLOW__",
            "resposta_interpretada": "REASK_CURRENT",
            "mensagem": mensagem,
            "pergunta": mensagem
        }

    field = questions[current_index]["field"]
    session["pending_answer"] = {
        "type": "ANSWER",
        "field": field,
        "value": mapped_answer
    }

    return {
        "campo": field,
        "resposta_interpretada": mapped_answer
    }


# =========================
# CONFIRMAR
# Compatível com frontend antigo
# =========================
@app.post("/confirmar/")
async def confirmar(indice: int = Form(...), resposta_interpretada: str = Form(...), session_id: str = Form(...)):
    create_session_if_needed(session_id)
    session = sessions[session_id]
    language = session["language"] or "English"

    interpreted = (resposta_interpretada or "").strip()

    # Fluxos especiais para manter o frontend antigo funcionando
    if interpreted in {"START_SCREENING", "ASK_NEXT", "REASK_CURRENT"}:
        texto = build_question_text(session["current_question"], language)
        return {"mensagem": texto, "pergunta": texto}

    if interpreted == "READY_FOR_DIAGNOSIS":
        texto = build_final_message(language)
        return {"mensagem": texto}

    # Confirmação normal
    texto_confirmacao = build_confirmation_text(interpreted, language)
    return {"mensagem": texto_confirmacao}


# =========================
# DIAGNOSTICO
# =========================
@app.post("/diagnostico/")
async def diagnostico(session_id: str = Form(...)):
    create_session_if_needed(session_id)
    session = sessions[session_id]
    language = session["language"] or "English"

    required_fields = [q["field"] for q in questions]
    missing_fields = [field for field in required_fields if field not in session["answers"]]

    if missing_fields:
        return JSONResponse(
            status_code=400,
            content={
                "mensagem": build_incomplete_message(language),
                "missing_fields": missing_fields
            }
        )

    row = find_diagnosis_row(session["answers"])

    if row is None:
        return {
            "mensagem": build_inconsistent_message(language)
        }

    col_2009 = "DIAGNOSIS (AAE NOMENCLATURE 2009/2013)"
    col_2025 = "DIAGNOSIS (AAE/ESE NOMENCLATURE 2025)"
    col_comp = "COMPLEMENTARY DIAGNOSIS"

    diagnosis_aae_2009_2013 = str(row[col_2009]).strip() if col_2009 in row.index else ""
    diagnosis_aae_ese_2025 = str(row[col_2025]).strip() if col_2025 in row.index else ""
    complementary_diagnosis = str(row[col_comp]).strip() if col_comp in row.index else ""

    session["diagnosis_result"] = {
        "DIAGNOSIS (AAE NOMENCLATURE 2009/2013)": diagnosis_aae_2009_2013,
        "DIAGNOSIS (AAE/ESE NOMENCLATURE 2025)": diagnosis_aae_ese_2025,
        "COMPLEMENTARY DIAGNOSIS": complementary_diagnosis
    }

    # Mantém chaves novas e antigas por compatibilidade
    return {
        "diagnosis_aae_2009_2013": diagnosis_aae_2009_2013,
        "diagnosis_aae_ese_2025": diagnosis_aae_ese_2025,
        "complementary_diagnosis": complementary_diagnosis,
        "diagnostico": diagnosis_aae_2009_2013,
        "diagnostico_complementar": complementary_diagnosis
    }


# =========================
# EXPLICACAO
# Aceita tanto os nomes novos quanto os antigos
# =========================
@app.post("/explicacao/")
async def explicacao(
    session_id: str = Form(...),
    diagnosis_aae_2009_2013: str = Form(None),
    diagnosis_aae_ese_2025: str = Form(None),
    complementary_diagnosis: str = Form(None),
    diagnostico: str = Form(None),
    diagnostico_complementar: str = Form(None)
):
    create_session_if_needed(session_id)
    session = sessions[session_id]
    language = session["language"] or "English"

    stored = session.get("diagnosis_result", {})

    diag_2009 = diagnosis_aae_2009_2013 or stored.get("DIAGNOSIS (AAE NOMENCLATURE 2009/2013)") or diagnostico or ""
    diag_2025 = diagnosis_aae_ese_2025 or stored.get("DIAGNOSIS (AAE/ESE NOMENCLATURE 2025)") or ""
    comp_diag = complementary_diagnosis or stored.get("COMPLEMENTARY DIAGNOSIS") or diagnostico_complementar or ""

    if not diag_2009 and not diag_2025 and not comp_diag:
        return JSONResponse(
            status_code=400,
            content={"mensagem": "No diagnosis is available yet. Run /diagnostico/ first."}
        )

    prompt = f"""
Explain clearly to a newly graduated dentist the following endodontic diagnostic result.

Write the entire answer in {language}.
Do not switch languages.
Be objective, clinically coherent, and easy to understand.

If there are two nomenclatures, explain that they refer to different diagnostic naming systems.
If there is a complementary diagnosis, explain what it means in practical clinical terms.

Diagnostic result:
- Diagnosis according to AAE nomenclature 2009/2013: {diag_2009}
- Diagnosis according to AAE/ESE nomenclature 2025: {diag_2025}
- Complementary diagnosis: {comp_diag}
"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are an endodontics professor."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3
        )
        explanation_text = response.choices[0].message.content.strip()
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"mensagem": f"Error generating explanation: {str(e)}"}
        )

    return JSONResponse(content={"explicacao": explanation_text})


# =========================
# RESET
# =========================
@app.post("/reset/")
async def reset_session(session_id: str = Form(...)):
    sessions[session_id] = {
        "language": None,
        "stage": "greeting",
        "current_question": 0,
        "answers": {},
        "pending_answer": None,
        "diagnosis_result": {}
    }
    return {"mensagem": "Session reset successfully."}


# =========================
# PDF
# =========================
@app.get("/pdf/{session_id}")
async def gerar_pdf(session_id: str):
    create_session_if_needed(session_id)
    session = sessions[session_id]

    answers = session.get("answers", {})
    diagnosis_result = session.get("diagnosis_result", {})

    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter
    y = height - 50
    left = 50

    def write_lines(lines, step=16):
        nonlocal y
        for line in lines:
            if y < 50:
                p.showPage()
                p.setFont("Helvetica", 11)
                y = height - 50
            p.drawString(left, y, line)
            y -= step

    p.setFont("Helvetica-Bold", 13)
    write_lines(["Endo10 EVO - Endodontic Screening Report"], step=20)

    p.setFont("Helvetica", 11)
    write_lines([f"Session ID: {session_id}"], step=18)
    write_lines([""], step=10)

    write_lines(["Answers:"], step=18)
    for q in questions:
        field = q["field"]
        value = answers.get(field, "Not answered")
        for line in wrap_pdf_lines(f"- {field}: {value}", width=95):
            write_lines([line])

    write_lines([""], step=10)
    write_lines(["Diagnostic result:"], step=18)

    if diagnosis_result:
        lines = [
            f"- Diagnosis (AAE Nomenclature 2009/2013): {diagnosis_result.get('DIAGNOSIS (AAE NOMENCLATURE 2009/2013)', '')}",
            f"- Diagnosis (AAE/ESE Nomenclature 2025): {diagnosis_result.get('DIAGNOSIS (AAE/ESE NOMENCLATURE 2025)', '')}",
            f"- Complementary diagnosis: {diagnosis_result.get('COMPLEMENTARY DIAGNOSIS', '')}",
        ]
        for item in lines:
            for line in wrap_pdf_lines(item, width=95):
                write_lines([line])
    else:
        write_lines(["No diagnosis has been generated yet."])

    p.save()
    buffer.seek(0)

    return StreamingResponse(
        buffer,
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=endodontic_screening_report.pdf"}
    )
