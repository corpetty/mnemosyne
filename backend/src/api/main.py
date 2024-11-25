from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import asyncio
from typing import List, Dict, Optional
import json
import os
from datetime import datetime
from pydantic import BaseModel

from ..audio.capture import AudioCapture
from ..audio.transcriber import AudioTranscriber
from ..llm.summarizer import Summarizer

app = FastAPI()

# CORS middleware configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class StartRecordingRequest(BaseModel):
    device_ids: List[str]

class TranscriptionManager:
    def __init__(self):
        self.audio_capture = AudioCapture()
        self.transcriber = AudioTranscriber()
        self.summarizer = Summarizer()
        self.active_connections: List[WebSocket] = []
        self.is_recording = False
        self.current_recording_file = None
        self._broadcast_task = None

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: Dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception as e:
                print(f"Error broadcasting to connection: {e}")

    async def _check_transcriptions(self):
        """Background task to check for and broadcast new transcriptions"""
        while True:
            try:
                latest = self.transcriber.get_latest_transcription()
                if latest:
                    await self.broadcast({
                        "type": "transcription",
                        "data": latest
                    })
            except Exception as e:
                print(f"Error checking transcriptions: {e}")
            await asyncio.sleep(0.1)

    def start_recording(self, device_ids: List[str]):
        if not self.is_recording:
            self.is_recording = True
            self.transcriber.clear_transcript()
            self.audio_capture.start_recording(device_ids)
            # Start the broadcast task
            if self._broadcast_task is None:
                self._broadcast_task = asyncio.create_task(self._check_transcriptions())

    async def stop_recording(self):
        """Stop recording and process the file"""
        if self.is_recording:
            self.is_recording = False
            # Stop recording and get the recorded file path
            self.current_recording_file = self.audio_capture.stop_recording()
            
            if self.current_recording_file:
                print(f"Processing recording: {self.current_recording_file}")
                # Process the recorded file
                success = self.transcriber.process_audio_file(self.current_recording_file)
                if not success:
                    print("Failed to process recording")
                    return False
                
                # Wait a bit to ensure all transcriptions are processed
                await asyncio.sleep(1)
                
                # Cancel the broadcast task
                if self._broadcast_task:
                    self._broadcast_task.cancel()
                    self._broadcast_task = None
                
                return True
            return False

    def generate_summary(self) -> str:
        transcript = self.transcriber.get_full_transcript()
        if not transcript:
            return "No transcript available to summarize."
        return self.summarizer.summarize_transcript(transcript)

    def save_transcript(self) -> str:
        """Save transcript to markdown file"""
        transcript = self.transcriber.get_full_transcript()
        summary = self.generate_summary()
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"transcripts/transcript_{timestamp}.md"
        
        os.makedirs("transcripts", exist_ok=True)
        
        with open(filename, "w") as f:
            f.write("# Audio Transcription\n\n")
            f.write(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            if self.current_recording_file:
                f.write(f"Recording: {self.current_recording_file}\n\n")
            f.write("## Transcript\n\n")
            for segment in transcript:
                timestamp = datetime.fromtimestamp(segment['timestamp']).strftime('%H:%M:%S')
                if 'start_time' in segment and 'end_time' in segment:
                    timestamp = f"{segment['start_time']/1000:.1f}s - {segment['end_time']/1000:.1f}s"
                f.write(f"**{segment['speaker']}** ({timestamp}):\n")
                f.write(f"{segment['text']}\n\n")
            f.write("## Summary\n\n")
            f.write(summary)
        
        return filename

manager = TranscriptionManager()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            # Keep connection alive
            await websocket.send_json({"type": "ping"})
    except WebSocketDisconnect:
        manager.disconnect(websocket)

@app.get("/devices")
async def get_devices():
    """Get list of available audio input devices"""
    devices = AudioCapture.list_devices()
    return {"devices": devices}

@app.post("/start")
async def start_recording(request: StartRecordingRequest):
    manager.start_recording(request.device_ids)
    return {"status": "Recording started"}

@app.post("/stop")
async def stop_recording():
    success = await manager.stop_recording()
    if success:
        filename = manager.save_transcript()
        summary = manager.generate_summary()
        # Broadcast the final summary
        await manager.broadcast({
            "type": "summary",
            "data": summary
        })
        return {
            "status": "Recording stopped",
            "transcript_file": filename,
            "summary": summary
        }
    return {"status": "Recording stopped with errors"}

@app.get("/status")
async def get_status():
    return {"is_recording": manager.is_recording}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
