from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import pandas as pd
from openai import OpenAI
import os
from io import BytesIO
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

app = FastAPI()

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# OpenAI Client
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Load Excel sheet
df = pd.read_excel("planilha_endo10.xlsx", sheet_name="Pt")

# Questions
perguntas = [
    {"campo": "DOR", "pergunta": "Does the patient have pain?", "opcoes": ["Ausente", "Presente"]},
    {"campo": "APARECIMENTO", "pergunta": "How does the pain appear?", "opcoes": ["Não se aplica", "Espontânea", "Provocada"]},
    {"campo": "VITALIDADE PULPAR", "pergunta": "What is the condition of the pulp vitality?", "opcoes": ["Normal", "Alterado", "Negativo"]},
    {"campo": "PERCUSSÃO", "pergunta": "Is the tooth sensitive to percussion?", "opcoes": ["Não se aplica", "Sensível", "Normal"]},
    {"campo": "PALPAÇÃO", "pergunta": "What was observed during palpation?", "opcoes": ["Sensível", "Edema", "Fístula", "Normal"]},
    {"campo": "RADIOGRAFIA", "pergunta": "What does the radiograph show?", "opcoes": ["Normal", "Espessamento", "Difusa", "Circunscrita", "Radiopaca difusa"]}
]

sessions = {}

@app.get("/", response_class=HTMLResponse)
async def root():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.post("/perguntar/")
async def perguntar(indice: int = Form(...)):
    if indice < len(perguntas):
        return perguntas[indice]
    else:
        return {"mensagem": "Triage finished. Let's calculate your diagnosis."}

@app.post("/responder/")
async def responder(indice: int = Form(...), resposta_usuario: str = Form(...), session_id: str = Form(...)):
    pergunta_info = perguntas[indice]
    prompt = f"""
You are an endodontic diagnosis assistant.

Your task is to interpret the user's response and map it to one of the possible options.

Question: {pergunta_info['pergunta']}
Possible options: {', '.join(pergunta_info['opcoes'])}
User's response: {resposta_usuario}

Respond only with the most appropriate option from the list.
"""
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "You are a specialist in endodontic diagnosis. Always reply in the same language used by the user."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.0,
        max_tokens=50
    )
    resposta_interpretada = response.choices[0].message.content.strip()

    if session_id not in sessions:
        sessions[session_id] = {}
    sessions[session_id][pergunta_info["campo"]] = resposta_interpretada

    return {
        "campo": pergunta_info["campo"],
        "resposta_interpretada": resposta_interpretada
    }

@app.post("/diagnostico/")
async def diagnostico(session_id: str = Form(...)):
    respostas = sessions.get(session_id, {})
    filtro = (
        (df["DOR"] == respostas.get("DOR")) &
        (df["APARECIMENTO"] == respostas.get("APARECIMENTO")) &
        (df["VITALIDADE PULPAR"] == respostas.get("VITALIDADE PULPAR")) &
        (df["PERCUSSÃO"] == respostas.get("PERCUSSÃO")) &
        (df["PALPAÇÃO"] == respostas.get("PALPAÇÃO")) &
        (df["RADIOGRAFIA"] == respostas.get("RADIOGRAFIA"))
    )
    resultado = df[filtro]
    if not resultado.empty:
        diagnostico = resultado.iloc[0]["DIAGNÓSTICO"]
        diagnostico_complementar = resultado.iloc[0]["DIAGNÓSTICO COMPLEMENTAR"]

        explicacao_prompt = f"""
Explain the following endodontic diagnosis clearly for a recent dental graduate:
- Main Diagnosis: {diagnostico}
- Complementary Diagnosis: {diagnostico_complementar}
"""

        explicacao_response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are a specialist in endodontic diagnosis. Always reply in the same language used by the user."},
                {"role": "user", "content": explicacao_prompt}
            ],
            temperature=0.5,
            max_tokens=500
        )
        explicacao = explicacao_response.choices[0].message.content.strip()

        return {
            "diagnostico": diagnostico,
            "diagnostico_complementar": diagnostico_complementar,
            "explicacao": explicacao
        }
    else:
        return {"erro": "Diagnosis not found."}

@app.get("/pdf/{session_id}")
async def gerar_pdf(session_id: str):
    respostas = sessions.get(session_id, {})
    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=letter)
    p.setFont("Helvetica", 12)

    y = 750
    p.drawString(100, y, "Endodontic Diagnosis Report - Endo10 EVO")
    y -= 40

    for campo, resposta in respostas.items():
        p.drawString(100, y, f"{campo}: {resposta}")
        y -= 20

    p.save()
    buffer.seek(0)

    return StreamingResponse(buffer, media_type="application/pdf", headers={"Content-Disposition": "attachment;filename=endo10evo_report.pdf"})
