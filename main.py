from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
import pandas as pd
import os
from io import BytesIO
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

app = FastAPI()

# =========================
# CONFIG
# =========================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

EXCEL_FILE = "planilha_endo10.xlsx"
SHEET_NAME = "En"

# =========================
# CORS
# =========================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # depois você pode restringir em produção
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# STATIC
# =========================
if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

# =========================
# LOAD DATA
# =========================
try:
    df = pd.read_excel(EXCEL_FILE, sheet_name=SHEET_NAME)
except Exception as e:
    raise RuntimeError(f"Error loading spreadsheet '{EXCEL_FILE}' / sheet '{SHEET_NAME}': {e}")

# Limpeza básica
df.columns = [str(col).strip() for col in df.columns]
for col in df.columns:
    if df[col].dtype == "object":
        df[col] = df[col].astype(str).str.strip()

# =========================
# QUESTIONS
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
            {"value": "Diffuse", "description": "The lesion has poorly defined borders or a gradual transition to the adjacent tissues."},
            {"value": "Thickening", "description": "There is thickening or widening of the periodontal ligament space."},
            {"value": "Normal", "description": "The lamina dura is intact and the periodontal ligament space is uniform."},
            {"value": "Diffuse radiopaque", "description": "There is a diffuse increase in radiopacity with ill-defined borders and gradual transition to adjacent bone."},
        ],
    },
]

# =========================
# SESSION STORE
# =========================
sessions = {}


def create_session_if_needed(session_id: str):
    if session_id not in sessions:
        sessions[session_id] = {
            "stage": "greeting",         # greeting -> triage -> completed
            "current_question": 0,
            "answers": {},
            "diagnosis_result": {}
        }


def get_allowed_values(question_index: int):
    return [opt["value"] for opt in questions[question_index]["options"]]


def get_question_payload(index: int):
    if index < 0 or index >= len(questions):
        raise HTTPException(status_code=400, detail="Invalid question index.")

    q = questions[index]
    return {
        "index": index,
        "field": q["field"],
        "question": q["question"],
        "options": q["options"],
        "message": f"Question {index + 1} of {len(questions)}"
    }


# =========================
# ROOT
# =========================
@app.get("/", response_class=HTMLResponse)
async def root():
    if os.path.isfile("static/index.html"):
        with open("static/index.html", "r", encoding="utf-8") as f:
            return f.read()

    return """
    <html>
        <body>
            <h2>Endo10 API is running.</h2>
            <p>Use the API endpoints to start the screening flow.</p>
        </body>
    </html>
    """


# =========================
# START / GREETING
# =========================
@app.post("/start/")
async def start_chat(session_id: str = Form(...)):
    create_session_if_needed(session_id)

    sessions[session_id]["stage"] = "greeting"
    sessions[session_id]["current_question"] = 0
    sessions[session_id]["answers"] = {}
    sessions[session_id]["diagnosis_result"] = {}

    return {
        "message": (
            "Hello! I'm Endo10, an AI-powered assistant designed to support endodontic screening. "
            "I will guide you through a structured sequence of clinical questions. "
            "When you are ready, type or send 'start' to begin the screening."
        ),
        "stage": "greeting"
    }


@app.post("/begin/")
async def begin_screening(session_id: str = Form(...), user_input: str = Form(...)):
    create_session_if_needed(session_id)

    if user_input.strip().lower() not in ["start", "begin", "go", "ok", "okay"]:
        return {
            "message": "Please type 'start' when you are ready to begin the screening.",
            "stage": sessions[session_id]["stage"]
        }

    sessions[session_id]["stage"] = "triage"
    sessions[session_id]["current_question"] = 0

    return {
        "message": "Screening started.",
        "stage": "triage",
        "question_data": get_question_payload(0)
    }


# =========================
# GET CURRENT QUESTION
# =========================
@app.post("/current-question/")
async def current_question(session_id: str = Form(...)):
    create_session_if_needed(session_id)

    stage = sessions[session_id]["stage"]
    idx = sessions[session_id]["current_question"]

    if stage != "triage":
        return {
            "message": "The screening is not currently active.",
            "stage": stage
        }

    if idx >= len(questions):
        return {
            "message": "All questions have already been answered.",
            "stage": "completed"
        }

    return {
        "stage": stage,
        "question_data": get_question_payload(idx)
    }


# =========================
# ANSWER QUESTION
# =========================
@app.post("/answer/")
async def answer_question(
    session_id: str = Form(...),
    selected_value: str = Form(...)
):
    create_session_if_needed(session_id)

    if sessions[session_id]["stage"] != "triage":
        return {
            "message": "The screening has not started yet. Use /begin/ first.",
            "stage": sessions[session_id]["stage"]
        }

    idx = sessions[session_id]["current_question"]

    if idx >= len(questions):
        return {
            "message": "All questions have already been answered.",
            "stage": "completed"
        }

    allowed_values = get_allowed_values(idx)
    selected_value = selected_value.strip()

    if selected_value not in allowed_values:
        return JSONResponse(
            status_code=400,
            content={
                "message": "Invalid option selected.",
                "allowed_values": allowed_values
            }
        )

    field = questions[idx]["field"]
    sessions[session_id]["answers"][field] = selected_value
    sessions[session_id]["current_question"] += 1

    next_idx = sessions[session_id]["current_question"]

    if next_idx < len(questions):
        return {
            "message": f"Answer recorded for {field}.",
            "stage": "triage",
            "recorded_answer": {
                "field": field,
                "value": selected_value
            },
            "next_question": get_question_payload(next_idx)
        }

    sessions[session_id]["stage"] = "completed"
    return {
        "message": "All questions were answered. You can now request the diagnosis.",
        "stage": "completed",
        "recorded_answer": {
            "field": field,
            "value": selected_value
        }
    }


# =========================
# DIAGNOSIS
# =========================
@app.post("/diagnosis/")
async def diagnosis(session_id: str = Form(...)):
    create_session_if_needed(session_id)

    answers = sessions[session_id]["answers"]

    required_fields = [q["field"] for q in questions]
    missing_fields = [field for field in required_fields if field not in answers]

    if missing_fields:
        return JSONResponse(
            status_code=400,
            content={
                "message": "The screening is incomplete.",
                "missing_fields": missing_fields
            }
        )

    result = df[
        (df["PAIN"] == str(answers.get("PAIN", "")).strip()) &
        (df["ONSET"] == str(answers.get("ONSET", "")).strip()) &
        (df["PULP VITALITY"] == str(answers.get("PULP VITALITY", "")).strip()) &
        (df["PERCUSSION"] == str(answers.get("PERCUSSION", "")).strip()) &
        (df["PALPATION"] == str(answers.get("PALPATION", "")).strip()) &
        (df["RADIOGRAPHY"] == str(answers.get("RADIOGRAPHY", "")).strip())
    ]

    if result.empty:
        return {
            "message": (
                "No diagnosis was found for this exact combination of answers. "
                "Please review the selected options and try again."
            )
        }

    diagnosis_aae_2009_2013 = str(result.iloc[0]["DIAGNOSIS (AAE NOMENCLATURE 2009/2013)"]).strip()
    diagnosis_aae_ese_2025 = str(result.iloc[0]["DIAGNOSIS (AAE/ESE NOMENCLATURE 2025)"]).strip()

    complementary_diagnosis = ""
    if "COMPLEMENTARY DIAGNOSIS" in result.columns:
        complementary_diagnosis = str(result.iloc[0]["COMPLEMENTARY DIAGNOSIS"]).strip()

    sessions[session_id]["diagnosis_result"] = {
        "DIAGNOSIS (AAE NOMENCLATURE 2009/2013)": diagnosis_aae_2009_2013,
        "DIAGNOSIS (AAE/ESE NOMENCLATURE 2025)": diagnosis_aae_ese_2025,
        "COMPLEMENTARY DIAGNOSIS": complementary_diagnosis,
    }

    return {
        "diagnosis_aae_2009_2013": diagnosis_aae_2009_2013,
        "diagnosis_aae_ese_2025": diagnosis_aae_ese_2025,
        "complementary_diagnosis": complementary_diagnosis
    }


# =========================
# EXPLANATION
# =========================
@app.post("/explanation/")
async def explanation(session_id: str = Form(...)):
    create_session_if_needed(session_id)

    diagnosis_result = sessions[session_id].get("diagnosis_result", {})

    if not diagnosis_result:
        return JSONResponse(
            status_code=400,
            content={"message": "No diagnosis is available yet. Run /diagnosis/ first."}
        )

    diagnosis_aae_2009_2013 = diagnosis_result.get("DIAGNOSIS (AAE NOMENCLATURE 2009/2013)", "")
    diagnosis_aae_ese_2025 = diagnosis_result.get("DIAGNOSIS (AAE/ESE NOMENCLATURE 2025)", "")
    complementary_diagnosis = diagnosis_result.get("COMPLEMENTARY DIAGNOSIS", "")

    prompt = f"""
Explain clearly to a newly graduated dentist the following endodontic diagnostic result.

Write the entire answer in English.
Be objective, clinically coherent, and easy to understand.
Do not switch to Portuguese.
If the complementary diagnosis contains recommendations, explain them in plain English.

Diagnostic result:
- Diagnosis according to AAE nomenclature 2009/2013: {diagnosis_aae_2009_2013}
- Diagnosis according to AAE/ESE nomenclature 2025: {diagnosis_aae_ese_2025}
- Complementary diagnosis: {complementary_diagnosis}
"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are an endodontics professor."},
                {"role": "user", "content": prompt}
            ]
        )
        explanation_text = response.choices[0].message.content.strip()
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"message": f"Error generating explanation: {str(e)}"}
        )

    return JSONResponse(content={"explanation": explanation_text})


# =========================
# RESET SESSION
# =========================
@app.post("/reset/")
async def reset_session(session_id: str = Form(...)):
    sessions[session_id] = {
        "stage": "greeting",
        "current_question": 0,
        "answers": {},
        "diagnosis_result": {}
    }

    return {"message": "Session reset successfully."}


# =========================
# PDF REPORT
# =========================
@app.get("/pdf/{session_id}")
async def generate_pdf(session_id: str):
    create_session_if_needed(session_id)

    answers = sessions[session_id].get("answers", {})
    diagnosis_result = sessions[session_id].get("diagnosis_result", {})

    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=letter)
    p.setFont("Helvetica", 11)

    y = 760
    left = 50

    def write_line(text, step=18):
        nonlocal y
        p.drawString(left, y, text[:110])
        y -= step
        if y < 60:
            p.showPage()
            p.setFont("Helvetica", 11)
            y = 760

    write_line("Endo10 Screening Report", 24)
    write_line(f"Session ID: {session_id}", 20)

    write_line("Answers:", 20)
    for q in questions:
        field = q["field"]
        value = answers.get(field, "Not answered")
        write_line(f"- {field}: {value}")

    write_line("", 10)
    write_line("Diagnostic result:", 20)

    if diagnosis_result:
        write_line(f"- Diagnosis (AAE Nomenclature 2009/2013): {diagnosis_result.get('DIAGNOSIS (AAE NOMENCLATURE 2009/2013)', '')}")
        write_line(f"- Diagnosis (AAE/ESE Nomenclature 2025): {diagnosis_result.get('DIAGNOSIS (AAE/ESE NOMENCLATURE 2025)', '')}")
        write_line(f"- Complementary diagnosis: {diagnosis_result.get('COMPLEMENTARY DIAGNOSIS', '')}")
    else:
        write_line("No diagnosis has been generated yet.")

    p.save()
    buffer.seek(0)

    return StreamingResponse(
        buffer,
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=endodontic_screening_report.pdf"}
    )
