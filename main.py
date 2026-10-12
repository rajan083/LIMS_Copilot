import os, time
import numpy as np
from scipy.io import wavfile
import sounddevice as sd
from report_email import get_pending_email, confirm_and_send, cancel_pending_email
from agent import new_conversation, handle_turn, transcribe_audio
from rag_pipeline import ingest_documents, save_learned_answer

CLI_USER = "cli-user"
CLI_SESSION= "cli"

messages = new_conversation()
last_exchange = None

print("Hola, señor! Welcome to Qualitec group's AI portal, how should I assist you today.\n")

def record(path="voice.wav", rate=16000):
    chunks=[]
    stream= sd.InputStream(samplerate=rate, channels=1, dtype="int16",
                           callback= lambda d,f,t,s: chunks.append(d.copy()))
    input("Press Enter to start recording...")
    with stream:
        input("Recording...press Enter to stop.")
    if not chunks:
        return None
    audio = np.concatenate(chunks)
    if int(np.abs(audio).max()) < 300:
        print("[very quiet: check your microphone]")
        return None
    wavfile.write(path, rate, audio)
    return path

def stt(path, retries=3):
    with open(path, 'rb') as f:
        audio= f.read()
    for attempt in range(retries):
        try:
            return transcribe_audio(audio, "audio/wav")
        except RuntimeError:
            raise
        except Exception as e:
            if attempt== retries-1:
                raise
            wait= 2*(attempt+1)
            print(f'[retrying in {wait}s: {e}]')
            time.sleep(wait)
            

while True:
    content = input("You: (type, or /v for voice): ").strip()

    if content.lower() in ("exit", "quit"):
        print("Goodbye!")
        break

    if not content:
        continue

    if content.lower() == "ingest":
        result = ingest_documents()
        print(f"[ingest] {result}\n")
        continue

    if content.lower() == "learn":
        if last_exchange is None:
            print("[learn] No previous answer to save yet — ask something first.\n")
        else:
            question, answer = last_exchange
            result = save_learned_answer(question, answer)
            print(f"[learn] {result}\n")
        continue
    
    if content.lower()== "/v":
        wav= record()
        if not wav:
            print("[no audio captured]\n")
            continue
        try:
            heard= stt(wav).strip()
        except Exception as e:
            print(f'[transcription failed]: {e}\n')
            continue
        if not heard:
            print("\nBot: [Couldn't hear anything]\n")
            continue
        print(f"You (voice): {heard}")
        while True:
            ans = input("Send this? (y/n/edit): ").strip().lower()
            if ans in ("", "y", "yes"):
                content = heard
                break
            if ans in ("n", "no"):
                content = None
                break
            if ans == "edit":
                content = input("Edit: ").strip() or heard
                break
            print("Please type y, n or edit.")
        if content is None:
            continue
        content= (input("Edit: ").strip() or heard) if ans == "edit" else heard


    print("  [thinking...]")
    reply, messages, artifacts = handle_turn(messages, content, session_id= CLI_SESSION, user=CLI_USER)

    print(f"Bot: {reply}\n")
    if artifacts:
        for artifact in artifacts:
            print(f"  [generated {artifact['type']}] {artifact['path']}")
        print()
    
    pending = get_pending_email(CLI_SESSION, user=CLI_USER)
    if pending:
        c = pending["contact"]
        note = "   (address was not typed by you, check it)" if c.get("source") == "shown" else ""
        names = ", ".join(os.path.basename(f) for f in pending.get("files") or [pending["file"]])
        question = f"Send {names} to {c.get('name') or ''} <{c['email']}>?{note} (y/n): "
        while True:
            answer = input(question).strip().lower()
            if answer in ("y", "yes", "send"):
                ok, msg = confirm_and_send(CLI_SESSION, user=CLI_USER)
                print(f"[email] {msg}\n")
                break
            if answer in ("n", "no", "cancel"):
                cancel_pending_email(CLI_SESSION, user=CLI_USER)
                print("[email] cancelled\n")
                break
            print("Please type y or n.")

    last_exchange = (content, reply)