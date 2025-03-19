from fastapi import FastAPI, WebSocket, WebSocketDisconnect, File, UploadFile, HTTPException, Query, Path, Depends
from fastapi.middleware.cors import CORSMiddleware
import asyncio
from typing import List, Dict, Optional, Any
import json
import os
import tempfile
import glob
from datetime import datetime
from pydantic import BaseModel
import logging

from ..audio.transcriber import AudioTranscriber
from ..llm.summarizer import Summarizer
from ..audio.capture import AudioCapture
from .devices import router as devices_router
from .session import Session

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

# Startup and shutdown event handlers
@app.on_event("startup")
async def startup_event():
    """Initialize application resources"""
    logger.info("Starting application - initializing resources")
    # Ensure the model service is initialized but models aren't loaded yet
    ModelService.get_instance()
    logger.info("Application started successfully")

@app.on_event("shutdown")
async def shutdown_event():
    """Release application resources"""
    logger.info("Shutting down application - releasing resources")
    # Release model resources
    ModelService.get_instance().release_resources()
    logger.info("Resources released successfully")

class ModelInfo(BaseModel):
    id: str
    name: str
    provider: str
    size: Optional[str] = None

class ResummarizeRequest(BaseModel):
    transcript_file: str
    model: Optional[str] = None
    session_id: Optional[str] = None

class SessionResponse(BaseModel):
    session_id: str
    status: str
    created_at: str
    model: Optional[str] = None
    device_ids: List[str] = []
    recording_file: Optional[str] = None
    transcript_file: Optional[str] = None
    is_recording: bool = False

class SessionListResponse(BaseModel):
    sessions: List[SessionResponse]
    
class SessionCreateRequest(BaseModel):
    session_id: Optional[str] = None
    
from ..services.model_service import ModelService

class TranscriptionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self.sessions: Dict[str, Session] = {}
        self.connection_sessions: Dict[WebSocket, str] = {}
        
        # Create model service instance (but models won't be loaded yet)
        self.model_service = ModelService.get_instance()
        
        # Load saved sessions from disk
        self._load_saved_sessions()
        
    def _load_saved_sessions(self):
        """Load all saved sessions from disk (metadata only)"""
        logger.info("Loading saved sessions from disk (metadata only)")
        # Ensure the sessions directory exists
        Session.ensure_sessions_dir()
        
        # Get the list of saved session IDs
        session_ids = Session.list_saved_sessions()
        logger.info(f"Found {len(session_ids)} saved sessions")
        
        # Load each session (models will be loaded on demand)
        for session_id in session_ids:
            try:
                session = Session.load_from_disk(session_id)
                if session:
                    self.sessions[session_id] = session
                    logger.info(f"Loaded session metadata for {session_id}")
            except Exception as e:
                logger.error(f"Error loading session {session_id}: {e}")

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.connection_sessions:
            del self.connection_sessions[websocket]
        self.active_connections.remove(websocket)

    def create_session(self, session_id: Optional[str] = None) -> Session:
        """Create a new session and return it"""
        session = Session(session_id)
        self.sessions[session.session_id] = session
        logger.info(f"Created new session with ID: {session.session_id}")
        
        # Save the session to disk
        session.save_to_disk()
        
        return session
        
    def get_session(self, session_id: str) -> Optional[Session]:
        """Get a session by ID"""
        return self.sessions.get(session_id)
        
    def delete_session(self, session_id: str) -> bool:
        """Delete a session by ID"""
        if session_id not in self.sessions:
            return False
        
        # Stop recording if the session is recording
        if self.sessions[session_id].is_recording:
            self.sessions[session_id].stop_recording()
            
        # Remove from connections mapping
        for conn, sess_id in list(self.connection_sessions.items()):
            if sess_id == session_id:
                del self.connection_sessions[conn]
        
        # Get the session before deleting it
        session = self.sessions[session_id]
        
        # Clear references to help garbage collection
        session._transcriber = None
        session._summarizer = None
        
        # Remove the session from memory
        del self.sessions[session_id]
        logger.info(f"Deleted session with ID: {session_id}")
        
        # Delete the session file from disk
        try:
            file_path = os.path.join(Session.SESSIONS_DIR, f"{session_id}.json")
            if os.path.exists(file_path):
                os.remove(file_path)
                logger.info(f"Deleted session file: {file_path}")
        except Exception as e:
            logger.error(f"Error deleting session file for {session_id}: {e}")
            
        # If we have no sessions, release all model resources
        if not self.sessions:
            logger.info("No active sessions - releasing model resources")
            self.model_service.release_resources()
            
        return True
    
    async def broadcast(self, message: Dict, session_id: Optional[str] = None):
        """Broadcast a message to all connections or connections for a specific session"""
        target_connections = []
        
        if session_id:
            # Send only to connections subscribed to this session
            target_connections = [conn for conn, sess_id in self.connection_sessions.items() 
                                 if sess_id == session_id]
        else:
            # Send to all connections
            target_connections = self.active_connections
            
        for connection in target_connections:
            try:
                await connection.send_json(message)
            except Exception as e:
                logger.error(f"Error broadcasting to connection: {e}")
                
    async def broadcast_status(self, message: str, session_id: Optional[str] = None):
        """Broadcast a status message to all connections or connections for a specific session"""
        if session_id:
            await self.broadcast({
                "type": "status",
                "message": message,
                "session_id": session_id
            }, session_id)
        else:
            await self.broadcast({
                "type": "status",
                "message": message
            })

    async def start_recording(self, session_id: str, device_ids: List[str], model: Optional[str] = None):
        """Start recording for a session"""
        session = self.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
            
        # Check if another session is already recording
        recording_sessions = [s for s in self.sessions.values() if s.is_recording]
        if recording_sessions and recording_sessions[0].session_id != session_id:
            logger.warning(f"Another session {recording_sessions[0].session_id} is already recording")
            
        result = session.start_recording(device_ids, model)
        if not result["success"]:
            raise HTTPException(status_code=400, detail=result.get("error", "Failed to start recording"))
        
        # Save the updated session to disk
        session.save_to_disk()
            
        await self.broadcast_status("Recording started", session_id)
        logger.info(f"Recording started for session {session_id} with devices: {device_ids}")
        return {"session_id": session_id, "status": session.status}

    async def stop_recording(self, session_id: str):
        """Stop recording for a session"""
        session = self.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
            
        result = session.stop_recording()
        if not result["success"]:
            raise HTTPException(status_code=400, detail=result.get("error", "Failed to stop recording"))
        
        # Save the updated session to disk
        session.save_to_disk()
            
        await self.broadcast_status("Recording stopped", session_id)
        logger.info(f"Recording stopped for session {session_id}")
        
        # Process the recorded file
        recording_file = result.get("recording_file")
        if recording_file:
            await self.process_audio_file(session_id, recording_file)
        
        return {"session_id": session_id, "status": session.status}

    async def process_audio_file(self, session_id: str, file_path: str):
        """Process an audio file for a specific session"""
        session = self.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
        
        try:
            await self.broadcast_status("Processing audio file...", session_id)
            logger.info(f"Processing audio file for session {session_id}: {file_path}")

            # Process the file in the session
            success = session.process_audio_file(file_path)
            if not success:
                logger.error(f"Failed to process audio file for session {session_id}")
                await self.broadcast_status("Failed to process audio file", session_id)
                # Save the updated (error) session to disk
                session.save_to_disk()
                return False

            # Send all transcript segments through WebSocket
            logger.info(f"Transcript segments for session {session_id}: {len(session.transcript)}")
            for segment in session.transcript:
                await self.broadcast({
                    "type": "transcription",
                    "data": segment,
                    "session_id": session_id
                }, session_id)
                # Small delay to prevent overwhelming the WebSocket
                await asyncio.sleep(0.01)

            await self.broadcast_status("Generating summary...", session_id)
            
            # Send summary through WebSocket
            await self.broadcast({
                "type": "summary",
                "data": session.summary,
                "session_id": session_id
            }, session_id)
            
            # Save the completed session to disk
            session.save_to_disk()

            logger.info(f"Audio file processing completed successfully for session {session_id}")
            return True

        except Exception as e:
            logger.error(f"Error processing audio file for session {session_id}: {e}")
            import traceback
            logger.error(f"Processing error traceback: {traceback.format_exc()}")
            session.status = "error"
            # Save the updated (error) session to disk
            session.save_to_disk()
            return False

    def get_available_models(self) -> List[Dict]:
        """Get available models for summarization using the model service"""
        # Get models directly from model service instead of creating a temporary session
        summarizer = self.model_service.get_summarizer()
        return summarizer.get_available_models()
        
    def _update_transcript_file_summary(self, file_path: str, new_summary: str) -> bool:
        """Update the summary section of a transcript file with a new summary."""
        try:
            with open(file_path, 'r') as f:
                content = f.read()
            
            # Split content into pre-summary and summary sections
            if '## Summary' in content:
                pre_summary, _ = content.split('## Summary', 1)
                updated_content = pre_summary + '## Summary\n\n' + new_summary
                
                # Write back the updated content
                with open(file_path, 'w') as f:
                    f.write(updated_content)
                logger.info(f"Updated summary in transcript file: {file_path}")
                return True
            else:
                logger.warning(f"No Summary section found in file: {file_path}")
                return False
        except Exception as e:
            logger.error(f"Error updating transcript file summary: {e}")
            return False
    
    async def resummarize_transcript(self, session_id: str, transcript_file: str, model: str = None) -> Dict:
        """Resummarize a transcript file using the specified model and overwrite the original file"""
        session = self.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
            
        if not os.path.exists(transcript_file):
            raise HTTPException(status_code=404, detail=f"Transcript file not found: {transcript_file}")
            
        # Parse the transcript file
        transcript = session.summarizer.parse_transcript_file(transcript_file)
        if not transcript:
            raise HTTPException(status_code=400, detail="Failed to parse transcript file")
            
        # Generate a new summary
        await self.broadcast_status(f"Generating new summary for {os.path.basename(transcript_file)}...", session_id)
        logger.info(f"Generating new summary for transcript file: {transcript_file} for session {session_id}")
        
        # Store transcript in session
        session.transcript = transcript
        session.transcript_file = transcript_file
        
        # Generate summary
        session.model = model
        summary = session.generate_summary()
        
        # Update the original transcript file with the new summary
        success = self._update_transcript_file_summary(transcript_file, summary)
        if not success:
            logger.warning(f"Failed to update original transcript file with new summary for session {session_id}")
        else:
            await self.broadcast_status(f"Updated summary in {os.path.basename(transcript_file)}", session_id)
        
        # Save the updated session to disk
        session.save_to_disk()
            
        # Send summary through WebSocket
        await self.broadcast({
            "type": "summary",
            "data": summary,
            "session_id": session_id
        }, session_id)
            
        return {
            "session_id": session_id,
            "transcript_file": transcript_file,
            "summary": summary,
            "model": model or session.summarizer.model,
            "file_updated": success
        }
    
    def get_available_transcripts(self) -> List[Dict]:
        """Get a list of available transcript files"""
        transcripts = []
        
        # Search for transcript files
        transcript_files = glob.glob("transcripts/transcript_*.md")
        
        for file_path in transcript_files:
            try:
                with open(file_path, 'r') as f:
                    # Read the first few lines to get the date
                    lines = [next(f) for _ in range(10) if f]
                    
                date_line = next((line for line in lines if line.startswith("Date:")), None)
                date = date_line.split("Date:")[1].strip() if date_line else "Unknown"
                
                session_id_line = next((line for line in lines if line.startswith("Session ID:")), None)
                session_id = session_id_line.split("Session ID:")[1].strip() if session_id_line else None
                
                # Get file size
                file_size = os.path.getsize(file_path)
                
                transcript_info = {
                    "path": file_path,
                    "filename": os.path.basename(file_path),
                    "date": date,
                    "size": file_size
                }
                
                if session_id:
                    transcript_info["session_id"] = session_id
                    
                transcripts.append(transcript_info)
            except Exception as e:
                logger.error(f"Error processing transcript file {file_path}: {e}")
                
        return sorted(transcripts, key=lambda x: x.get("date", ""), reverse=True)

manager = TranscriptionManager()

app.include_router(devices_router)

# Session management endpoints
@app.post("/sessions", response_model=SessionResponse)
async def create_session(request: SessionCreateRequest = None):
    """Create a new transcription session"""
    if request is None:
        request = SessionCreateRequest()
    session = manager.create_session(request.session_id)
    return session.to_dict()

@app.get("/sessions", response_model=SessionListResponse)
async def list_sessions():
    """List all available sessions"""
    sessions = [session.to_dict() for session in manager.sessions.values()]
    return {"sessions": sessions}

@app.get("/sessions/{session_id}")
async def get_session(session_id: str = Path(..., description="The ID of the session to get"), include_data: bool = Query(False)):
    """Get a specific session by ID
    
    Args:
        session_id: The ID of the session to get
        include_data: If True, include the full transcript and summary data
    """
    session = manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    return session.to_dict(include_data=include_data)

@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str = Path(..., description="The ID of the session to delete")):
    """Delete a session by ID"""
    result = manager.delete_session(session_id)
    if not result:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    return {"success": True, "message": f"Session {session_id} deleted"}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                # Try to parse the message as JSON
                message = json.loads(data)
                
                # Handle session subscription
                if message.get("type") == "subscribe" and "session_id" in message:
                    session_id = message["session_id"]
                    session = manager.get_session(session_id)
                    if not session:
                        await websocket.send_json({
                            "type": "error", 
                            "message": f"Session {session_id} not found"
                        })
                    else:
                        # Store the subscription
                        manager.connection_sessions[websocket] = session_id
                        await websocket.send_json({
                            "type": "subscribed", 
                            "session_id": session_id
                        })
                        
                        # Send current session state
                        if session.transcript:
                            # Send all transcript segments
                            for segment in session.transcript:
                                await websocket.send_json({
                                    "type": "transcription",
                                    "data": segment,
                                    "session_id": session_id
                                })
                                
                        if session.summary:
                            # Send the summary
                            await websocket.send_json({
                                "type": "summary",
                                "data": session.summary,
                                "session_id": session_id
                            })
                            
                # Handle session unsubscribe
                elif message.get("type") == "unsubscribe":
                    if websocket in manager.connection_sessions:
                        session_id = manager.connection_sessions[websocket]
                        del manager.connection_sessions[websocket]
                        await websocket.send_json({
                            "type": "unsubscribed",
                            "session_id": session_id
                        })
                
            except json.JSONDecodeError:
                # Not valid JSON, just keep connection alive
                pass
                
            # Keep connection alive
            await websocket.send_json({"type": "ping"})
    except WebSocketDisconnect:
        manager.disconnect(websocket)

@app.post("/upload")
async def upload_file(file: UploadFile = File(...), session_id: Optional[str] = None):
    """Upload an audio file for transcription and summarization"""
    # Create a new session if one wasn't provided
    if not session_id:
        session = manager.create_session()
        session_id = session.session_id
    else:
        session = manager.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
            
    logger.info(f"Received file upload: {file.filename} for session {session_id}")
    
    # Save the uploaded file to a temporary location
    with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(file.filename)[1]) as temp_file:
        content = await file.read()
        temp_file.write(content)
        temp_file_path = temp_file.name

    try:
        success = await manager.process_audio_file(session_id, temp_file_path)
        if success:
            logger.info(f"File processed successfully for session {session_id}")
            return {
                "status": "File processed successfully",
                "session_id": session_id,
                "transcript_file": session.transcript_file
            }
        logger.error(f"Failed to process file for session {session_id}")
        return {"status": "Failed to process file", "session_id": session_id}
    finally:
        # Clean up the temporary file
        os.unlink(temp_file_path)

@app.get("/models")
async def get_models():
    """Get available models for summarization"""
    models = manager.get_available_models()
    return {"models": models}

@app.get("/transcripts")
async def get_transcripts():
    """Get available transcript files"""
    transcripts = manager.get_available_transcripts()
    return {"transcripts": transcripts}

@app.post("/resummarize")
async def resummarize(request: ResummarizeRequest):
    """Re-summarize a transcript file using a specified model"""
    # Create a new session if one wasn't provided
    session_id = request.session_id
    if not session_id:
        session = manager.create_session()
        session_id = session.session_id
        
    result = await manager.resummarize_transcript(session_id, request.transcript_file, request.model)
    return result

class StartRecordingRequest(BaseModel):
    device_ids: List[str]
    model: Optional[str] = None
    session_id: Optional[str] = None

@app.post("/start")
async def start_recording(request: StartRecordingRequest):
    """Start recording audio"""
    # Create a new session if one wasn't provided
    session_id = request.session_id
    if not session_id:
        session = manager.create_session()
        session_id = session.session_id
    
    result = await manager.start_recording(session_id, request.device_ids, request.model)
    return {
        "status": "Recording started", 
        "session_id": session_id,
        "model": request.model
    }

@app.post("/stop")
async def stop_recording(session_id: Optional[str] = None):
    """Stop recording audio"""
    # Find a recording session if session_id is not provided
    if not session_id:
        recording_sessions = [s for s in manager.sessions.values() if s.is_recording]
        if not recording_sessions:
            raise HTTPException(status_code=400, detail="No recording in progress")
        session_id = recording_sessions[0].session_id
    
    result = await manager.stop_recording(session_id)
    return {"status": "Recording stopped", "session_id": session_id}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
