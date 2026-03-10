#!/usr/bin/env python3
"""
Interactive dev client for testing Nonna Notes over the real WebSocket/Live API.
No mic or speakers needed — text in, text transcripts out.

Usage:
  python dev_client.py
  python dev_client.py <recipe_url>
  python dev_client.py <recipe_url> <image_url>   # attach one image at session start

During the session, special commands:
  !image <url>    — fetch a URL and send it as a video frame, then wait for response
  !img   <url>    — alias for !image
  quit / exit     — end session
"""

import asyncio
import base64
import json
import sys

import httpx
import websockets

WS_URL = "ws://localhost:8000/ws"


async def fetch_img_b64(url: str) -> str:
    async with httpx.AsyncClient(follow_redirects=True, timeout=10) as c:
        r = await c.get(url)
        r.raise_for_status()
    return base64.b64encode(r.content).decode()


async def collect(ws, first_timeout=25, idle_timeout=15):
    """Stream transcript chunks to stdout until turn_complete or timeout.

    Tool calls arrive in their own turn (turn_complete with no text), followed
    by the assistant's spoken response in the next turn.  We only stop on turn_complete
    once we've received at least one transcript chunk so we don't bail out early
    after a tool-call-only turn.
    """
    timeout = first_timeout
    got_text = False
    for _ in range(500):
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            data = json.loads(raw)
            if data["type"] == "transcript":
                print(data["text"], end="", flush=True)
                timeout = idle_timeout
                got_text = True
            elif data["type"] == "tool_call":
                print(f"\n  [tool] {data['name']}({json.dumps(data['args'])})", flush=True)
                timeout = idle_timeout
            elif data["type"] == "turn_complete":
                if got_text:
                    break  # Assistant finished a spoken turn — we're done
                # Otherwise this was a tool-call-only turn; keep waiting for
                # the follow-up text turn.
                timeout = idle_timeout
            elif data["type"] == "audio":
                timeout = idle_timeout
        except asyncio.TimeoutError:
            break
    print()


async def main():
    recipe    = sys.argv[1] if len(sys.argv) > 1 else ""
    image_url = sys.argv[2] if len(sys.argv) > 2 else ""

    print(f"Connecting to {WS_URL}…")
    async with websockets.connect(WS_URL, open_timeout=30) as ws:
        await ws.send(json.dumps({"type": "config", "recipe": recipe}))
        if recipe:
            print(f"Recipe: {recipe}")

        if image_url:
            print(f"Fetching image: {image_url}")
            enc = await fetch_img_b64(image_url)
            await ws.send(json.dumps({"type": "video", "data": enc}))

        print("\nNonna: ", end="", flush=True)
        await collect(ws, first_timeout=25)

        loop = asyncio.get_event_loop()
        print('\nType a message, "!image <url>" to send a photo, or "quit" to exit.\n')

        while True:
            try:
                user_input = await loop.run_in_executor(None, input, "")
            except EOFError:
                break

            user_input = user_input.strip()
            if not user_input:
                continue
            if user_input.lower() in ("quit", "exit", "q"):
                break

            print(f"You: {user_input}")

            # Image command: !image <url> or !img <url>
            if user_input.lower().startswith(("!image ", "!img ")):
                url = user_input.split(" ", 1)[1].strip()
                print(f"  [fetching {url}…]")
                try:
                    enc = await fetch_img_b64(url)
                    await ws.send(json.dumps({"type": "video", "data": enc}))
                    print("  [image sent]")
                    await ws.send(json.dumps({"type": "text", "text": "What do you see?"}))
                except Exception as e:
                    print(f"  [error fetching image: {e}]")
                    continue
            else:
                await ws.send(json.dumps({"type": "text", "text": user_input}))

            print("Nonna: ", end="", flush=True)
            await collect(ws, first_timeout=12, idle_timeout=8)

    print("\n[session ended]")


if __name__ == "__main__":
    asyncio.run(main())
