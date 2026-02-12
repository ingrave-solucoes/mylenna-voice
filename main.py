import json
import logging
import base64
import asyncio
import os
import subprocess
import uuid
import requests
from fastapi import FastAPI, WebSocket, Request, Response
from elevenlabs.client import ElevenLabs
import xml.etree.ElementTree as ET
from groq import Groq

# ==========================================
# CONFIGURAÇÕES DE INTEGRAÇÃO (INGRAVE)
# ==========================================
BETTERSTACK_HEARTBEAT_URL = "https://uptime.betterstack.com/api/v1/heartbeat/GRGuqPNRz7Jn67KrfwFvnBhz"
CHATWOOT_BASE_URL = "https://app.ingrave.com.br"
CHATWOOT_ACCESS_TOKEN = "tGRLZZZpPCjUauSBDVUUKjzq"

# CONFIGURAÇÕES DE IA E VOZ
ELEVENLABS_API_KEY = "sk_786a3d1611d8cb1351547486eb46750f5d4b79161b17fb87"
VOICE_ID = "21m00Tcm4TlvDq8ikWAM"
MODEL_ID = "eleven_multilingual_v2"
GROQ_API_KEY = "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"

client_eleven = ElevenLabs(api_key=ELEVENLABS_API_KEY)
client_groq = Groq(api_key=GROQ_API_KEY)

app = FastAPI()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("uvicorn")

audio_buffers = {}
processing_locks = {}
session_metadata = {} # Armazena account_id e conversation_id por stream_sid

HALUCINATIONS = ["e aí, tudo bem?", "obrigado.", "legendas por", "tchau.", "e ai tudo bem por ai", "você", "oi", "olá"]

# ==========================================
# FUNÇÕES DE SINCRONIZAÇÃO COM INGRAVE
# ==========================================

async def post_to_chatwoot(stream_sid: str, text: str, message_type: str = "outgoing"):
    """Envia a transcrição ou resposta para o dashboard do Ingrave/Chatwoot."""
    meta = session_metadata.get(stream_sid)
    
    # Se não tiver IDs, tenta buscar pelo número de telefone (fallback inteligente)
    if not meta or not meta.get("account_id") or not meta.get("conversation_id"):
        logger.info(f"[Ingrave] IDs ausentes. Tentando fallback para postar mensagem...")
        # Por enquanto vamos logar, mas podemos implementar a busca por telefone aqui
        return

    url = f"{CHATWOOT_BASE_URL}/api/v1/accounts/{meta['account_id']}/conversations/{meta['conversation_id']}/messages"
    headers = {
        "api_access_token": CHATWOOT_ACCESS_TOKEN,
        "Content-Type": "application/json"
    }
    payload = {
        "content": text,
        "message_type": message_type,
        "private": False
    }

    try:
        # Rodar request bloqueante em thread para não travar o loop async
        loop = asyncio.get_event_loop()
        res = await loop.run_in_executor(None, lambda: requests.post(url, json=payload, headers=headers))
        if res.status_code == 200:
            logger.info(f"[Ingrave] Mensagem sincronizada no Dashboard: {text[:30]}...")
        else:
            logger.error(f"[Ingrave] Erro na API (Status {res.status_code}): {res.text}")
    except Exception as e:
        logger.error(f"[Ingrave] Erro de conexão com a API: {e}")

# ==========================================
# MOTOR CEREBRAL (IA E VOZ)
# ==========================================

async def get_ai_response(text: str, session_id: str):
    logger.info(f"[OpenClaw] Processando: {text}")
    try:
        cmd = [
            "/home/openclaw/.npm-global/bin/openclaw", "agent", "--agent", "mylenna", 
            "--session-id", session_id,
            "--message", f"(VOICE): {text}", "--json"
        ]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await proc.communicate()
        
        if proc.returncode != 0:
            err_msg = stderr.decode()
            logger.error(f"[AI] Erro no comando OpenClaw: {err_msg}")
            return "Tive um problema no motor central."

        output_str = stdout.decode()
        start = output_str.find('{')
        end = output_str.rfind('}')
        if start != -1 and end != -1:
            try:
                data = json.loads(output_str[start:end+1])
                result = data.get("result", data)
                payloads = result.get("payloads", [])
                if payloads: return payloads[0].get("text", "")
                return result.get("output", {}).get("text", "") or result.get("message", "Entendido.")
            except: pass
        return "Desculpe, me perdi nos meus pensamentos."
    except Exception as e:
        logger.error(f"[AI] Erro: {e}")
        return "Erro de conexão cerebral."

async def send_audio(websocket: WebSocket, stream_sid: str, text: str):
    if not text: return
    logger.info(f"[TTS] Falando: {text[:40]}...")
    try:
        audio_stream = client_eleven.text_to_speech.convert(
            voice_id=VOICE_ID, model_id=MODEL_ID, text=text,
            output_format="ulaw_8000", voice_settings={"stability": 0.8, "similarity_boost": 0.8, "style": 0.0, "use_speaker_boost": True}
        )
        for chunk in audio_stream:
            if chunk:
                # Garantir que o websocket ainda está aberto antes de enviar
                try:
                    payload = base64.b64encode(chunk).decode("utf-8")
                    await websocket.send_text(json.dumps({"event": "media", "streamSid": stream_sid, "media": {"payload": payload}}))
                    await asyncio.sleep(0.01)
                except Exception as send_err:
                    logger.warning(f"[TTS] Falha ao enviar chunk de áudio (conexão fechada?): {send_err}")
                    break
    except Exception as e: logger.error(f"[TTS] Erro: {e}")

async def transcribe_audio(stream_sid: str):
    buffer = audio_buffers.get(stream_sid, b"")
    if len(buffer) < 16000: return None 
    audio_buffers[stream_sid] = b""
    filename = f"/tmp/{stream_sid}.ulaw"
    wav_filename = f"/tmp/{stream_sid}.wav"
    with open(filename, "wb") as f: f.write(buffer)
    subprocess.run(["ffmpeg", "-y", "-f", "mulaw", "-ar", "8000", "-i", filename, "-ar", "16000", wav_filename], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with open(wav_filename, "rb") as file:
            transcription = client_groq.audio.transcriptions.create(file=(wav_filename, file.read()), model="whisper-large-v3", response_format="text", language="pt")
        text = transcription.strip().lower()
        if text in HALUCINATIONS or len(text) < 5: return None
        return text
    except Exception: return None
    finally:
        for f in [filename, wav_filename]:
            if os.path.exists(f): os.remove(f)

async def handle_ai_turn(websocket: WebSocket, stream_sid: str, text: str, session_id: str):
    if processing_locks.get(stream_sid): return 
    
    processing_locks[stream_sid] = True
    try:
        # Sincroniza o que o cliente falou no Dashboard (Incoming)
        await post_to_chatwoot(stream_sid, text, "incoming")

        # Busca resposta da IA
        response = await get_ai_response(text, session_id)
        
        # Sincroniza resposta da IA no Dashboard (Outgoing)
        await post_to_chatwoot(stream_sid, response, "outgoing")

        # Envia áudio da IA
        await send_audio(websocket, stream_sid, response)
        
        await asyncio.sleep(0.2)
        audio_buffers[stream_sid] = b""
    except Exception as e:
        logger.error(f"[AI-Turn] Erro: {e}")
    finally:
        processing_locks[stream_sid] = False

# ==========================================
# HANDLERS WEBSOCKET E HTTP
# ==========================================

@app.websocket("/voice/stream")
async def stream(websocket: WebSocket):
    await websocket.accept()
    logger.info("[WS] Conectado.")
    stream_sid = None
    session_id = f"vcall-{uuid.uuid4().hex[:6]}"
    try:
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive_text(), timeout=0.5)
                data = json.loads(message)
                event = data.get('event')
                
                if event == 'start':
                    start_data = data.get('start', {})
                    stream_sid = start_data.get('streamSid')
                    call_sid = start_data.get('callSid')
                    logger.info(f"[WS] Chamada iniciada. CallSid: {call_sid}")
                    
                    params = start_data.get('customParameters', {})
                    # Se vier via Ingrave, os params podem estar no topo ou aninhados
                    session_metadata[stream_sid] = {
                        "account_id": params.get("account_id") or 1, # Default para conta 1
                        "conversation_id": params.get("conversation_id"),
                        "call_sid": call_sid
                    }
                    audio_buffers[stream_sid] = b""
                    processing_locks[stream_sid] = False
                    logger.info(f"[WS] Start recebido. IDs: {session_metadata[stream_sid]}")
                    
                    await send_audio(websocket, stream_sid, "Olá, Allington! Aqui é a Mylenna... Já estou online e pronta para te ajudar. Pode falar!")
                
                elif event == 'media':
                    if stream_sid and not processing_locks.get(stream_sid):
                        audio_buffers[stream_sid] += base64.b64decode(data['media']['payload'])
                        # Aumentado para 4 segundos de buffer para estabilidade do Groq
                        if len(audio_buffers[stream_sid]) > 32000: 
                            text = await transcribe_audio(stream_sid)
                            if text:
                                logger.info(f"[STT] Você: {text}")
                                asyncio.create_task(handle_ai_turn(websocket, stream_sid, text, session_id))
                
                elif event == 'stop': break
            except asyncio.TimeoutError: continue
    except Exception as e: logger.error(f"[WS] Erro: {e}")
    finally:
        if stream_sid in audio_buffers: del audio_buffers[stream_sid]
        if stream_sid in processing_locks: del processing_locks[stream_sid]
        if stream_sid in session_metadata: del session_metadata[stream_sid]

@app.api_route("/voice/webhook", methods=["GET", "POST"])
async def webhook(request: Request):
    host = request.headers.get("host")
    protocol = "wss" if "ingrave.com.br" in host or "ngrok" in host else "ws"
    response = ET.Element('Response')
    connect = ET.SubElement(response, 'Connect')
    ET.SubElement(connect, 'Stream', url=f'{protocol}://{host}/voice/stream')
    logger.info(f"[HTTP] Webhook chamado.")
    return Response(content='<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(response, encoding='unicode'), media_type="text/xml")

@app.api_route("/voice/status", methods=["GET", "POST"])
async def status(request: Request):
    return Response(content="OK", media_type="text/plain")

@app.api_route("/voice/status-callback", methods=["GET", "POST"])
async def status_callback(request: Request):
    """Recebe status callbacks do Twilio para monitorar chamadas."""
    try:
        form_data = await request.form()
        call_status = form_data.get("CallStatus")
        call_sid = form_data.get("CallSid")
        call_duration = form_data.get("CallDuration", "0")
        
        logger.info(f"[Twilio Callback] CallSid: {call_sid} | Status: {call_status} | Duração: {call_duration}s")
        
        # Aqui podemos adicionar lógica futura:
        # - Atualizar Chatwoot quando chamada terminar
        # - Salvar métricas em banco de dados
        # - Enviar notificações
        
        return Response(content="OK", media_type="text/plain")
    except Exception as e:
        logger.error(f"[Status Callback] Erro: {e}")
        return Response(content="ERROR", media_type="text/plain", status_code=500)

async def send_heartbeat():
    """Envia sinal de vida para o BetterStack periodicamente."""
    while True:
        try:
            # Usar loop.run_in_executor para requisição bloqueante
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: requests.get(BETTERSTACK_HEARTBEAT_URL, timeout=10))
            logger.info("[Monitor] Heartbeat enviado para o BetterStack.")
        except Exception as e:
            logger.error(f"[Monitor] Falha no heartbeat: {e}")
        await asyncio.sleep(60) # Avisar a cada 1 minuto

if __name__ == "__main__":
    import uvicorn
    # Inicia o heartbeat em background
    @app.on_event("startup")
    async def startup_event():
        asyncio.create_task(send_heartbeat())
    
    uvicorn.run(app, host="0.0.0.0", port=4567)
