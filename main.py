from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import pandas as pd
import openai
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

# Configurar chave da API OpenAI
openai.api_key = os.getenv("OPENAI_API_KEY")

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

sessions = {}

@app.get("/", response_class=HTMLResponse)
async def root():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.post("/detectar/")
async def detectar_idioma(texto: str = Form(...)):
    try:
        response = openai.ChatCompletion.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "Detect the language of the following text and reply only with the ISO 639-1 language code (e.g., en, pt, es, fr, de)."},
                {"role": "user", "content": texto}
            ],
            temperature=0,
            max_tokens=10
        )
        idioma = response["choices"][0]["message"]["content"].strip().lower()
    except Exception:
        idioma = "en"  # Default para inglês se falhar
    return {"idioma": idioma}

@app.post("/perguntar/")
async def perguntar(indice: int = Form(...)):
    if indice < len(perguntas):
        return perguntas[indice]
    else:
        return {"mensagem": "Triagem finalizada. Vamos calcular seu diagnóstico."}

@app.post("/responder/")
async def responder(indice: int = Form(...), resposta_usuario: str = Form(...), session_id: str = Form(...), idioma: str = Form(...)):
    pergunta_info = perguntas[indice]
    prompt = f"""
You are a dental assistant.

Interpret the user response and match it to one of the possible options below.

Question: {pergunta_info['pergunta']}
Options: {', '.join(pergunta_info['opcoes'])}
User response: {resposta_usuario}

Respond only with the most appropriate option from the list.
"""
    response = openai.ChatCompletion.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "You are an expert in endodontic diagnosis."},
            {"role": "user", "content": prompt}
        ],
        temperature=0,
        max_tokens=50
    )
    resposta_interpretada = response["choices"][0]["message"]["content"].strip()

    if session_id not in sessions:
        sessions[session_id] = {"respostas": {}, "idioma": idioma}

    sessions[session_id]["respostas"][pergunta_info["campo"]] = resposta_interpretada

    return {
        "campo": pergunta_info["campo"],
        "resposta_interpretada": resposta_interpretada
    }

@app.post("/diagnostico/")
async def diagnostico(session_id: str = Form(...)):
    respostas = sessions.get(session_id, {}).get("respostas", {})
    idioma = sessions.get(session_id, {}).get("idioma", "en")

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
Explain in a clear, simple way the following endodontic diagnosis, considering the user speaks {idioma}:

Main diagnosis: {diagnostico}
Complementary diagnosis: {diagnostico_complementar}
"""
        response = openai.ChatCompletion.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are an expert in endodontics."},
                {"role": "user", "content": explicacao_prompt}
            ],
            temperature=0.5,
            max_tokens=300
        )
        explicacao = response["choices"][0]["message"]["content"].strip()

        return {
            "diagnostico": diagnostico,
            "diagnostico_complementar": diagnostico_complementar,
            "explicacao": explicacao
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
