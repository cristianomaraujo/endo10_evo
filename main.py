from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import pandas as pd
import openai

app = FastAPI()

# Adiciona Middleware CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Permite acesso de qualquer origem — pode restringir depois se quiser
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Carrega sua planilha Excel
df = pd.read_excel("planilha_endo10.xlsx", sheet_name="Pt")

# Página inicial personalizada
@app.get("/", response_class=HTMLResponse)
async def root():
    return """
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <title>Endo10 EVO - Diagnóstico Endodôntico</title>
        <style>
            body {
                background-color: #0b0b0b;
                color: #f0f0f0;
                font-family: 'Arial', sans-serif;
                text-align: center;
                padding: 40px;
            }
            img {
                width: 250px;
                margin-bottom: 20px;
            }
            h1 {
                font-size: 2.8em;
                margin-bottom: 20px;
                color: #f5a623;
            }
            p {
                font-size: 1.3em;
                margin-bottom: 40px;
                color: #ccc;
            }
            a {
                text-decoration: none;
                font-size: 1.2em;
                background-color: #f5a623;
                color: white;
                padding: 15px 30px;
                border-radius: 30px;
                transition: background-color 0.3s;
            }
            a:hover {
                background-color: #e09117;
            }
        </style>
    </head>
    <body>
        <img src="https://www.narsm.com.br/wp-content/uploads/2024/06/Eng.jpg" alt="Logo Endo10 EVO">
        <h1>Bem-vindo ao Endo10 EVO</h1>
        <p>Seu assistente inteligente para triagem e diagnóstico endodôntico.</p>
        <a href="/docs">Iniciar Diagnóstico</a>
    </body>
    </html>
    """

perguntas = [
    {"campo": "DOR", "pergunta": "O paciente apresenta dor?", "opcoes": ["Ausente", "Presente"]},
    {"campo": "APARECIMENTO", "pergunta": "Como a dor aparece?", "opcoes": ["Não se aplica", "Espontânea", "Provocada"]},
    {"campo": "VITALIDADE PULPAR", "pergunta": "Qual é a condição da vitalidade pulpar do dente?", "opcoes": ["Normal", "Alterado", "Negativo"]},
    {"campo": "PERCUSSÃO", "pergunta": "O dente é sensível à percussão?", "opcoes": ["Não se aplica", "Sensível", "Normal"]},
    {"campo": "PALPAÇÃO", "pergunta": "Durante a palpação, o que foi observado no dente?", "opcoes": ["Sensível", "Edema", "Fístula", "Normal"]},
    {"campo": "RADIOGRAFIA", "pergunta": "O que a radiografia mostra?", "opcoes": ["Normal", "Espessamento", "Difusa", "Circunscrita", "Radiopaca difusa"]}
]

def interpretar_resposta(pergunta, resposta_usuario, opcoes):
    prompt = f"""
Você é um assistente de diagnóstico endodôntico. Mapeie a resposta do usuário para uma das opções:

Pergunta: {pergunta}
Opções: {', '.join(opcoes)}
Resposta do usuário: {resposta_usuario}

Responda apenas com a melhor opção.
"""
    response = openai.ChatCompletion.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        temperature=0
    )
    return response["choices"][0]["message"]["content"].strip()

def buscar_diagnostico(respostas):
    filtro = (
        (df["DOR"] == respostas["DOR"]) &
        (df["APARECIMENTO"] == respostas["APARECIMENTO"]) &
        (df["VITALIDADE PULPAR"] == respostas["VITALIDADE PULPAR"]) &
        (df["PERCUSSÃO"] == respostas["PERCUSSÃO"]) &
        (df["PALPAÇÃO"] == respostas["PALPAÇÃO"]) &
        (df["RADIOGRAFIA"] == respostas["RADIOGRAFIA"])
    )
    resultado = df[filtro]
    if not resultado.empty:
        return {
            "diagnostico": resultado.iloc[0]["DIAGNÓSTICO"],
            "diagnostico_complementar": resultado.iloc[0]["DIAGNÓSTICO COMPLEMENTAR"]
        }
    else:
        return {"erro": "Diagnóstico não encontrado."}

@app.post("/perguntar/")
async def perguntar(indice: str = Form(...)):
    return {"response": f"Você disse: {indice}"}

@app.post("/responder/")
async def responder(indice: str = Form(...), resposta_usuario: str = Form(...)):
    # Aqui você poderia adaptar para fluxo de perguntas se quiser
    return {"campo": "Campo exemplo", "resposta_interpretada": resposta_usuario}

@app.post("/diagnostico/")
async def diagnostico(respostas: dict):
    return buscar_diagnostico(respostas)
