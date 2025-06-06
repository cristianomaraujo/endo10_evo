from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
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

sessions = {}

@app.get("/", response_class=HTMLResponse)
async def root():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.post("/perguntar/")
async def perguntar(indice: int = Form(...), session_id: str = Form(...)):
    if indice < len(perguntas):
        pergunta_info = perguntas[indice]
        # Traduzir a pergunta antes de enviar
        idioma_usuario = sessions.get(session_id, {}).get("language", "pt")

        if idioma_usuario == "pt":
            pergunta_traduzida = pergunta_info["pergunta"]
        else:
            prompt = f"Translate the following text into {idioma_usuario}: {pergunta_info['pergunta']}"
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "user", "content": prompt}
                ],
                temperature=0.0,
                max_tokens=100
            )
            pergunta_traduzida = response.choices[0].message.content.strip()

        return {"pergunta": pergunta_traduzida}
    else:
        return {"mensagem": "Triagem finalizada. Vamos calcular seu diagnóstico."}

@app.post("/responder/")
async def responder(indice: int = Form(...), resposta_usuario: str = Form(...), session_id: str = Form(...)):
    if session_id not in sessions:
        # Detectar idioma na primeira interação
        prompt_detect = f"Detect the language of this text: {resposta_usuario}. Only output the language name in English, like: English, Spanish, Portuguese, Italian."
        response_detect = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "user", "content": prompt_detect}
            ],
            temperature=0.0,
            max_tokens=10
        )
        detected_language = response_detect.choices[0].message.content.strip()
        sessions[session_id] = {"language": detected_language}

    pergunta_info = perguntas[indice]
    prompt = f"""
You are an endodontic assistant.

Interpret the user's answer and map it to one of the possible options.

Question: {pergunta_info['pergunta']}
Possible options: {', '.join(pergunta_info['opcoes'])}
User's answer: {resposta_usuario}

Respond only with the most appropriate option from the list.
"""
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "You are a specialist in endodontic diagnosis."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.0,
        max_tokens=50
    )
    resposta_interpretada = response.choices[0].message.content.strip()

    return {
        "campo": pergunta_info["campo"],
        "resposta_interpretada": resposta_interpretada
    }

@app.post("/confirmar/")
async def confirmar(indice: int = Form(...), resposta_interpretada: str = Form(...), session_id: str = Form(...)):
    pergunta_info = perguntas[indice]

    if session_id not in sessions:
        sessions[session_id] = {}

    sessions[session_id][pergunta_info["campo"]] = resposta_interpretada
    return {"mensagem": "Resposta confirmada."}

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
        return {
            "diagnostico": resultado.iloc[0]["DIAGNÓSTICO"],
            "diagnostico_complementar": resultado.iloc[0]["DIAGNÓSTICO COMPLEMENTAR"]
        }
    else:
        return {"erro": "Diagnóstico não encontrado."}

@app.post("/explicacao/")
async def explicacao(diagnostico: str = Form(...), diagnostico_complementar: str = Form(...)):
    prompt = f"""
Explain clearly to a newly graduated dentist the following diagnosis:
- Main Diagnosis: {diagnostico}
- Complementary Diagnosis: {diagnostico_complementar}

The explanation should be clear, without overly technical terms, and present the clinical reasoning behind the diagnosis.
"""
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "You are an endodontics professor."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.5,
        max_tokens=300
    )
    explicacao = response.choices[0].message.content.strip()
    return JSONResponse(content={"explicacao": explicacao})

@app.get("/pdf/{session_id}")
async def gerar_pdf(session_id: str):
    respostas = sessions.get(session_id, {})
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
