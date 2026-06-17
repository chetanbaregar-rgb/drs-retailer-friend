"""
agent.py — DRS Retailer Friend: a conversational chatbot for UK retailers.

Run:
    python agent.py

What it does:
  - Answers questions about the UK Deposit Return Scheme (DRS)
  - Sticks strictly to DRS topics — politely declines anything else
  - Never guesses — directs retailers to official sources when uncertain
  - Remembers the conversation within a session
  - Streams responses so you see the answer as it's generated
"""

import json
import os
import string
import sys
from pathlib import Path

import anthropic

from persona import SYSTEM_PROMPT

# ── Config ────────────────────────────────────────────────────────────────────

MODEL          = "claude-sonnet-4-6"
HISTORY_FILE   = Path(__file__).parent / "data" / "history.json"
HISTORY_WINDOW = 20   # messages kept per session (older ones dropped)


# ── Conversation history ──────────────────────────────────────────────────────

def load_history() -> list:
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return []


def save_history(messages: list) -> None:
    HISTORY_FILE.parent.mkdir(exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(messages, indent=2))


# ── Streaming response ────────────────────────────────────────────────────────

def stream_response(client: anthropic.Anthropic, messages: list) -> str:
    """Send messages to the model and stream the reply to stdout."""
    full_text = ""
    print("\nRetEarn Ready: ", end="", flush=True)

    with client.messages.stream(
        model=MODEL,
        max_tokens=1024,
        system=[{"type": "text", "text": SYSTEM_PROMPT,
                 "cache_control": {"type": "ephemeral"}}],
        messages=messages[-HISTORY_WINDOW:],
    ) as stream:
        for text in stream.text_stream:
            print(text, end="", flush=True)
            full_text += text

    print("\n")
    return full_text


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    # Load .env
    env = Path(__file__).parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY not set. Add it to .env and try again.")
        sys.exit(1)

    client   = anthropic.Anthropic()
    messages = load_history()

    print()
    print("┌─────────────────────────────────────────────────────────────┐")
    print("│  RetEarn Ready — Learn about DRS & how you can make money  │")
    print("│          Type your question. Type 'exit' to quit.           │")
    print("└─────────────────────────────────────────────────────────────┘")

    # Greet the retailer on first visit
    if not messages:
        opener = [{"role": "user", "content": "Hello"}]
        greeting = stream_response(client, opener)
        messages.append({"role": "user",      "content": "Hello"})
        messages.append({"role": "assistant", "content": greeting})
        save_history(messages)
    else:
        print(f"\n(Welcome back — resuming your previous conversation.)\n")

    exit_words = {"exit", "quit", "bye", "goodbye", "stop"}

    while True:
        try:
            user_input = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            save_history(messages)
            print("\n\nDRS Friend: Take care — good luck with your DRS preparations!")
            return

        if not user_input:
            continue

        if user_input.lower().strip(string.punctuation + " ") in exit_words:
            save_history(messages)
            print("RetEarn Ready: Thanks for chatting. Best of luck with your DRS journey — goodbye!\n")
            return

        messages.append({"role": "user", "content": user_input})
        reply = stream_response(client, messages)
        messages.append({"role": "assistant", "content": reply})
        save_history(messages)


if __name__ == "__main__":
    main()
