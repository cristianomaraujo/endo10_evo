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

# Mount static directory
app.mount("/static", StaticFiles(directory="static"), name="static")

# OpenAI API Key
openai.api_key = os.getenv("OPENAI_API_KEY")

# Load Spreadsheet
df = pd.read_excel("planilha_endo10.xlsx", sheet_name="Pt")

# Questions
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

async def detectar_idioma(mensagem_usuario: str):
    prompt = f"Detect the language of this message: {mensagem_usuario}. Reply with the language name (e.g., Portuguese, English, Spanish)."
    response = openai.ChatCompletion.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
        max_tokens=10
    )
    idioma = response.choices[0].message.content.strip()
    return idioma

async def traduzir_texto(texto: str, destino: str):
    prompt = f"Translate this to {destino}: {texto}"
    response = openai.ChatCompletion.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
        max_tokens=200
    )
    traducao = response.choices[0].message.content.strip()
    return traducao

@app.post("/iniciar/")
async def iniciar(session_id: str = Form(...), saudacao_usuario: str = Form(...)):
    idioma = await detectar_idioma(saudacao_usuario)
    sessions[session_id] = {"idioma": idioma, "respostas": {}, "estado": "perguntando", "indice": 0}
    pergunta_original = perguntas[0]["pergunta"]
    pergunta_traduzida = await traduzir_texto(pergunta_original, idioma)
    return {"pergunta": pergunta_traduzida}

@app.post("/responder/")
async def responder(session_id: str = Form(...), resposta_usuario: str = Form(...)):
    sessao = sessions.get(session_id)
    idioma = sessao["idioma"]
    indice = sessao["indice"]
    pergunta_info = perguntas[indice]

    prompt = f"Classify the user's response into one of these options: {', '.join(pergunta_info['opcoes'])}. User said: {resposta_usuario}. Answer with only one of the options."
    response = openai.ChatCompletion.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
        max_tokens=20
    )
    resposta_interpretada = response.choices[0].message.content.strip()

    sessao["resposta_interpretada"] = resposta_interpretada

    confirmacao = await traduzir_texto(f"Você quis dizer: {resposta_interpretada}? (sim/não)", idioma)

    return {"confirmacao": confirmacao}

@app.post("/confirmar/")
async def confirmar(session_id: str = Form(...), confirmacao_usuario: str = Form(...)):
    sessao = sessions.get(session_id)
    idioma = sessao["idioma"]

    if confirmacao_usuario.strip().lower() in ["sim", "yes"]:
        indice = sessao["indice"]
        campo = perguntas[indice]["campo"]
        sessao["respostas"][campo] = sessao["resposta_interpretada"]
        sessao["indice"] += 1

        if sessao["indice"] < len(perguntas):
            nova_pergunta = perguntas[sessao["indice"]]["pergunta"]
            pergunta_traduzida = await traduzir_texto(nova_pergunta, idioma)
            return {"proxima_pergunta": pergunta_traduzida}
        else:
            return {"mensagem": await traduzir_texto("Triagem finalizada. Vamos calcular seu diagnóstico.", idioma)}
    else:
        pergunta = perguntas[sessao["indice"]]["pergunta"]
        pergunta_traduzida = await traduzir_texto(pergunta, idioma)
        return {"pergunta": pergunta_traduzida}

@app.post("/diagnostico/")
async def diagnostico(session_id: str = Form(...)):
    sessao = sessions.get(session_id)
    respostas = sessao["respostas"]
    idioma = sessao["idioma"]

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
        diag_traduzido = await traduzir_texto(diagnostico, idioma)
        diag_comp_traduzido = await traduzir_texto(diagnostico_complementar, idioma)
        return {"diagnostico": diag_traduzido, "diagnostico_complementar": diag_comp_traduzido}
    else:
        return {"erro": await traduzir_texto("Diagnóstico não encontrado.", idioma)}

@app.get("/pdf/{session_id}")
async def gerar_pdf(session_id: str):
    sessao = sessions.get(session_id)
    respostas = sessao["respostas"]

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
