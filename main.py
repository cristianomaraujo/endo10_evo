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

# Montar a pasta static
app.mount("/static", StaticFiles(directory="static"), name="static")

# Instancia o cliente OpenAI com a API KEY
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Carrega planilha
df = pd.read_excel("planilha_endo10.xlsx", sheet_name="Pt")

# Perguntas da triagem
perguntas = [
    {"campo": "DOR", "pergunta": "O paciente apresenta dor?", "opcoes": ["Ausente", "Presente"]},
    {"campo": "APARECIMENTO", "pergunta": "Como a dor aparece?", "opcoes": ["Não se aplica", "Espontânea", "Provocada"]},
    {"campo": "VITALIDADE PULPAR", "pergunta": "Qual é a condição da vitalidade pulpar do dente?", "opcoes": ["Normal", "Alterado", "Negativo"]},
    {"campo": "PERCUSSÃO", "pergunta": "O dente é sensível à percussão?", "opcoes": ["Não se aplica", "Sensível", "Normal"]},
    {"campo": "PALPAÇÃO", "pergunta": "Durante a palpação, o que foi observado no dente?", "opcoes": ["Sensível", "Edema", "Fístula", "Normal"]},
    {"campo": "RADIOGRAFIA", "pergunta": "O que a radiografia mostra?", "opcoes": ["Normal", "Espessamento", "Difusa", "Circunscrita", "Radiopaca difusa"]}
]

# Sessões para armazenar respostas
sessions = {}

# Mensagem de boas-vindas multilíngue
saudacao_inicial = """
Olá! Hello! Ciao! Hola! Bonjour! こんにちは! مرحبا! 👋\nDiga olá no seu idioma para começarmos.
"""

@app.get("/", response_class=HTMLResponse)
async def root():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.post("/perguntar/")
async def perguntar(indice: int = Form(...)):
    if indice == 0:
        return {"pergunta": saudacao_inicial}
    else:
        return {"mensagem": "Triagem iniciada."}

@app.post("/responder/")
async def responder(indice: int = Form(...), resposta_usuario: str = Form(...), session_id: str = Form(...)):
    if session_id not in sessions:
        sessions[session_id] = {"respostas": {}, "idioma": resposta_usuario}
        return {"pergunta": "Detected language! Let's begin the triage.", "continuar": True}

    # Sessão já iniciada
    idioma_usuario = sessions[session_id]["idioma"]
    pergunta_info = perguntas[indice - 1]

    prompt = f"""
You are a virtual assistant specialized in endodontic triage.

Always respond in the user's language: {idioma_usuario}

Map the user response to one of the possible options.

Question: {pergunta_info['pergunta']}
Options: {', '.join(pergunta_info['opcoes'])}
User Response: {resposta_usuario}

Respond with ONLY the best matching option.
"""

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "You are an endodontic diagnosis assistant."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.0,
        max_tokens=50
    )

    resposta_interpretada = response.choices[0].message.content.strip()

    sessions[session_id]["respostas"][pergunta_info["campo"]] = resposta_interpretada

    if indice < len(perguntas):
        proxima_pergunta = perguntas[indice]["pergunta"]
        return {"pergunta": proxima_pergunta, "continuar": True}
    else:
        return {"mensagem": "Triagem finalizada. Calculando o diagnóstico...", "continuar": False}

@app.post("/diagnostico/")
async def diagnostico(session_id: str = Form(...)):
    respostas = sessions.get(session_id, {}).get("respostas", {})
    idioma_usuario = sessions.get(session_id, {}).get("idioma", "en")

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

        prompt = f"""
Summarize the following endodontic diagnosis and complementary diagnosis to the patient:

Diagnosis: {diagnostico}
Complementary Diagnosis: {diagnostico_complementar}

Respond in {idioma_usuario}.
"""
        explanation = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "user", "content": prompt}
            ],
            temperature=0.5,
            max_tokens=300
        ).choices[0].message.content.strip()

        return {
            "diagnostico": diagnostico,
            "diagnostico_complementar": diagnostico_complementar,
            "explicacao": explanation
        }
    else:
        return {"erro": "Diagnóstico não encontrado."}

@app.get("/pdf/{session_id}")
async def gerar_pdf(session_id: str):
    respostas = sessions.get(session_id, {}).get("respostas", {})
    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=letter)
    p.setFont("Helvetica", 12)

    y = 750
    p.drawString(100, y, "Relatório da Triagem Endodôntica - Endo10 EVO")
    y -= 40

    for campo, resposta in respostas.items():
        p.drawString(100, y, f"{campo}: {resposta}")
        y -= 20

    p.save()
    buffer.seek(0)

    return StreamingResponse(buffer, media_type="application/pdf", headers={"Content-Disposition": "attachment;filename=relatorio_triagem_endo10evo.pdf"})
