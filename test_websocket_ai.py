import asyncio
import websockets
import json

async def test_websocket_ai():
    # WebSocket URL (adjust conversation_id if needed)
    uri = "ws://localhost:8000/ws/chat/1/"
    
    try:
        async with websockets.connect(uri) as websocket:
            print(f"Connected to {uri}")
            
            # Send a user message
            payload = {
                "message": "Hey there! Can you tell me something funny about this shop but also let me know how many products you have?",
                "sender_id": 1
            }
            await websocket.send(json.dumps(payload))
            print(f"Sent: {payload['message']}")
            
            # Wait for user message broadcast
            response = await websocket.recv()
            print(f"Received (User): {response}")
            
            # Wait for AI response broadcast
            print("Waiting for AI response...")
            try:
                ai_response = await asyncio.wait_for(websocket.recv(), timeout=15)
                print(f"Received (AI): {ai_response}")
            except asyncio.TimeoutError:
                print("Timed out waiting for AI response. Is n8n running?")
                
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(test_websocket_ai())
