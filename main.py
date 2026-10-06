import os
from report_email import get_pending_email, confirm_and_send, cancel_pending_email
from agent import new_conversation, handle_turn
from rag_pipeline import ingest_documents, save_learned_answer

CLI_USER = "cli-user"
CLI_SESSION= "cli"

messages = new_conversation()
last_exchange = None

print("Hola, señor! Welcome to Qualitec group's AI portal, how should I assist you today.\n")

while True:
    content = input("You: ").strip()

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
        answer = input(f"Send {os.path.basename(pending['file'])} to {c.get('name') or ''} <{c['email']}>?{note} (y/n): ").strip().lower()
        if answer == "y":
            ok, msg = confirm_and_send(CLI_SESSION, user=CLI_USER)
            print(f"[email] {msg}\n")
        else:
            cancel_pending_email(CLI_SESSION, user=CLI_USER)
            print("[email] cancelled\n")

    last_exchange = (content, reply)