from fastapi import FastAPI, WebSocket, WebSocketDisconnect, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import asyncio
from typing import List, Dict, Optional
import json
import os
import tempfile
from datetime import datetime
from pydantic import BaseModel
import logging

from ..audio.transcriber import AudioTranscriber
from ..llm.summarizer import Summarizer
from ..audio.capture import AudioCapture
from .devices import router as devices_router

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# CORS middleware configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class TranscriptionManager:
    def __init__(self):
        self.transcriber = AudioTranscriber()
        self.summarizer = Summarizer()
        self.audio_capture = AudioCapture()
        self.active_connections: List[WebSocket] = []
        self.is_recording = False
        self.current_recording_file = None

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
                logger.error(f"Error broadcasting to connection: {e}")

    async def broadcast_status(self, message: str):
        await self.broadcast({
            "type": "status",
            "message": message
        })

    async def start_recording(self, device_ids: List[str]):
        if self.is_recording:
            raise HTTPException(status_code=400, detail="Recording is already in progress")
        
        try:
            self.audio_capture.start_recording(device_ids)
            self.is_recording = True
            self.current_recording_file = self.audio_capture.output_file
            await self.broadcast_status("Recording started")
            logger.info(f"Recording started with devices: {device_ids}")
        except Exception as e:
            logger.error(f"Error starting recording: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    async def stop_recording(self):
        if not self.is_recording:
            raise HTTPException(status_code=400, detail="No recording in progress")
        
        try:
            recorded_file = self.audio_capture.stop_recording()
            self.is_recording = False
            await self.broadcast_status("Recording stopped")
            logger.info("Recording stopped")
            
            # Process the recorded file
            await self.process_audio_file(recorded_file)
        except Exception as e:
            logger.error(f"Error stopping recording: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    async def process_audio_file(self, file_path: str):
        """Process an audio file"""
        try:
            await self.broadcast_status("Processing audio file...")
            logger.info(f"Processing audio file: {file_path}")

            # Process the file
            success = self.transcriber.process_audio_file(file_path)
            if not success:
                logger.error("Failed to process audio file")
                await self.broadcast_status("Failed to process audio file")
                return False

            # Send all transcript segments through WebSocket
            transcript = self.transcriber.get_full_transcript()
            logger.info(f"Transcript segments: {len(transcript)}")
            for segment in transcript:
                await self.broadcast({
                    "type": "transcription",
                    "data": segment
                })
                # Small delay to prevent overwhelming the WebSocket
                await asyncio.sleep(0.01)

            await self.broadcast_status("Generating summary...")
            summary = self.generate_summary()
            await self.broadcast({
                "type": "summary",
                "data": summary
            })

            logger.info("Audio file processing completed successfully")
            return True

        except Exception as e:
            logger.error(f"Error processing audio file: {e}")
            import traceback
            logger.error(f"Processing error traceback: {traceback.format_exc()}")
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
                if 'start' in segment and 'end' in segment:
                    timestamp = f"{segment['start']:.1f}s - {segment['end']:.1f}s"
                f.write(f"**{segment['speaker']}** ({timestamp}):\n")
                f.write(f"{segment['text']}\n\n")
            f.write("## Summary\n\n")
            f.write(summary)
        
        logger.info(f"Transcript saved to {filename}")
        return filename

manager = TranscriptionManager()

app.include_router(devices_router)

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

@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """Upload an audio file for transcription and summarization"""
    logger.info(f"Received file upload: {file.filename}")
    
    # Save the uploaded file to a temporary location
    with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(file.filename)[1]) as temp_file:
        content = await file.read()
        temp_file.write(content)
        temp_file_path = temp_file.name

    try:
        success = await manager.process_audio_file(temp_file_path)
        if success:
            filename = manager.save_transcript()
            logger.info(f"File processed successfully, transcript saved to {filename}")
            return {
                "status": "File processed successfully",
                "transcript_file": filename
            }
        logger.error("Failed to process file")
        return {"status": "Failed to process file"}
    finally:
        # Clean up the temporary file
        os.unlink(temp_file_path)

@app.post("/start")
async def start_recording(device_ids: List[str]):
    """Start recording audio"""
    await manager.start_recording(device_ids)
    return {"status": "Recording started"}

@app.post("/stop")
async def stop_recording():
    """Stop recording audio"""
    await manager.stop_recording()
    return {"status": "Recording stopped"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
