from agent import new_conversation, handle_turn
from rag_pipeline import ingest_documents, save_learned_answer

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
    reply, messages, artifacts = handle_turn(messages, content)

    print(f"Bot: {reply}\n")
    if artifacts:
        for artifact in artifacts:
            print(f"  [generated {artifact['type']}] {artifact['path']}")
        print()

    last_exchange = (content, reply)