from typing import List, Dict, Optional, Any, ClassVar
import os
import json
import uuid
import logging
import glob
from datetime import datetime

from ..audio.transcriber import AudioTranscriber
from ..llm.summarizer import Summarizer
from ..audio.capture import AudioCapture
from ..services.model_service import ModelService

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class Session:
    """Class to encapsulate a single recording/transcription session"""
    
    # Class variables for configuration
    SESSIONS_DIR: ClassVar[str] = "sessions"
    
    @classmethod
    def ensure_sessions_dir(cls):
        """Ensure the sessions directory exists"""
        os.makedirs(cls.SESSIONS_DIR, exist_ok=True)
    
    def __init__(self, session_id: str = None):
        # Session metadata
        self.session_id = session_id or str(uuid.uuid4())
        self.created_at = datetime.now()
        self.status = "created"
        self.model: Optional[str] = None
        self.device_ids: List[str] = []
        self.name: Optional[str] = None  # Custom name for the session
        self.participants: List[Dict[str, str]] = []  # List of participants
        
        # Session data
        self.recording_file: Optional[str] = None
        self.transcript_file: Optional[str] = None
        self.transcript: List[Dict[str, Any]] = []
        self.summary: str = ""
        
        # Session state
        self.is_recording = False
        
        # Light-weight components created directly
        self.audio_capture = AudioCapture()
        
        # Heavy components are not created here - they will be accessed via ModelService when needed
        self._transcriber = None
        self._summarizer = None
    
    @property
    def transcriber(self):
        """Get the transcriber from the model service (lazy loading)"""
        if self._transcriber is None:
            logger.debug(f"Lazy loading transcriber for session {self.session_id}")
            self._transcriber = ModelService.get_instance().get_transcriber()
        return self._transcriber
    
    @property
    def summarizer(self):
        """Get the summarizer from the model service (lazy loading)"""
        if self._summarizer is None:
            logger.debug(f"Lazy loading summarizer for session {self.session_id}")
            self._summarizer = ModelService.get_instance().get_summarizer(model=self.model)
        return self._summarizer
    
    def to_dict(self, include_data: bool = False) -> Dict[str, Any]:
        """Convert session to dictionary for API responses
        
        Args:
            include_data: If True, include the full transcript and summary data
        """
        result = {
            "session_id": self.session_id,
            "created_at": self.created_at.isoformat(),
            "status": self.status,
            "model": self.model,
            "device_ids": self.device_ids,
            "recording_file": self.recording_file,
            "transcript_file": self.transcript_file,
            "is_recording": self.is_recording,
            "summary_length": len(self.summary),
            "transcript_length": len(self.transcript),
            "name": self.name,
            "participants": self.participants
        }
        
        # Include full data if requested
        if include_data:
            result["transcript"] = self.transcript
            result["summary"] = self.summary
            
            # If transcript file exists but transcript is empty, try to load it
            if self.transcript_file and not self.transcript and os.path.exists(self.transcript_file):
                try:
                    # Parse the transcript file to get transcript segments
                    self.transcript = self.summarizer.parse_transcript_file(self.transcript_file)
                    result["transcript"] = self.transcript
                    
                    # Extract summary from the transcript file
                    with open(self.transcript_file, "r") as f:
                        content = f.read()
                        if "## Summary" in content:
                            summary_text = content.split("## Summary", 1)[1].strip()
                            self.summary = summary_text
                            result["summary"] = summary_text
                except Exception as e:
                    logger.error(f"Error loading transcript file {self.transcript_file}: {e}")
                    
        return result
    
    def start_recording(self, device_ids: List[str], model: Optional[str] = None) -> Dict[str, Any]:
        """Start a recording with the specified devices"""
        if self.is_recording:
            logger.warning(f"Session {self.session_id} is already recording")
            return {"success": False, "error": "Already recording"}
        
        try:
            self.device_ids = device_ids
            self.model = model
            self.audio_capture.start_recording(device_ids)
            self.is_recording = True
            self.status = "recording"
            self.recording_file = self.audio_capture.output_file
            
            logger.info(f"Recording started for session {self.session_id} with devices: {device_ids}")
            return {"success": True}
        except Exception as e:
            logger.error(f"Error starting recording for session {self.session_id}: {e}")
            self.status = "error"
            return {"success": False, "error": str(e)}
    
    def stop_recording(self) -> Dict[str, Any]:
        """Stop the current recording"""
        if not self.is_recording:
            logger.warning(f"Session {self.session_id} is not recording")
            return {"success": False, "error": "Not recording"}
        
        try:
            recorded_file = self.audio_capture.stop_recording()
            self.is_recording = False
            self.status = "processing"
            
            logger.info(f"Recording stopped for session {self.session_id}")
            return {"success": True, "recording_file": recorded_file}
        except Exception as e:
            logger.error(f"Error stopping recording for session {self.session_id}: {e}")
            self.status = "error"
            return {"success": False, "error": str(e)}
    
    def process_audio_file(self, file_path: str) -> bool:
        """Process an audio file for this session"""
        try:
            logger.info(f"Processing audio file for session {self.session_id}: {file_path}")
            self.status = "processing"
            
            # Process the file
            success = self.transcriber.process_audio_file(file_path)
            if not success:
                logger.error(f"Failed to process audio file for session {self.session_id}")
                self.status = "error"
                return False
            
            # Get transcript
            self.transcript = self.transcriber.get_full_transcript()
            logger.info(f"Transcript segments for session {self.session_id}: {len(self.transcript)}")
            
            # Generate summary
            self.summary = self.generate_summary()
            
            # Save transcript to file
            self.transcript_file = self.save_transcript()
            
            self.status = "completed"
            logger.info(f"Audio processing completed for session {self.session_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error processing audio file for session {self.session_id}: {e}")
            import traceback
            logger.error(f"Processing error traceback: {traceback.format_exc()}")
            self.status = "error"
            return False
    
    def generate_summary(self) -> str:
        """Generate summary from transcript"""
        if not self.transcript:
            logger.warning(f"No transcript available to summarize for session {self.session_id}")
            return "No transcript available to summarize."
        
        return self.summarizer.summarize_transcript(self.transcript, self.model)
    
    def save_transcript(self) -> str:
        """Save transcript to markdown file and return the file path"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"transcripts/transcript_{self.session_id}_{timestamp}.md"
        
        os.makedirs("transcripts", exist_ok=True)
        
        with open(filename, "w") as f:
            f.write("# Audio Transcription\n\n")
            f.write(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            f.write(f"Session ID: {self.session_id}\n\n")
            if self.recording_file:
                f.write(f"Recording: {self.recording_file}\n\n")
            if self.model:
                f.write(f"Model: {self.model}\n\n")
            f.write("## Transcript\n\n")
            for segment in self.transcript:
                timestamp = datetime.fromtimestamp(segment['timestamp']).strftime('%H:%M:%S')
                if 'start' in segment and 'end' in segment:
                    timestamp = f"{segment['start']:.1f}s - {segment['end']:.1f}s"
                f.write(f"**{segment['speaker']}** ({timestamp}):\n")
                f.write(f"{segment['text']}\n\n")
            f.write("## Summary\n\n")
            f.write(self.summary)
        
        logger.info(f"Transcript saved to {filename} for session {self.session_id}")
        return filename
        
    def to_storage_dict(self) -> Dict[str, Any]:
        """Convert session to dictionary for storage purposes
        
        Returns a dictionary that contains all the necessary information to
        reconstruct this session when loaded from disk.
        """
        return {
            "session_id": self.session_id,
            "created_at": self.created_at.isoformat(),
            "status": self.status,
            "model": self.model,
            "device_ids": self.device_ids,
            "recording_file": self.recording_file,
            "transcript_file": self.transcript_file,
            "is_recording": self.is_recording,
            "transcript": self.transcript,
            "summary": self.summary,
            "name": self.name,
            "participants": self.participants
        }
    
    @classmethod
    def from_storage_dict(cls, data: Dict[str, Any]) -> 'Session':
        """Create a session from a storage dictionary
        
        Args:
            data: Dictionary containing session data
            
        Returns:
            A new Session instance with the restored data
        """
        session = cls(session_id=data.get("session_id"))
        session.created_at = datetime.fromisoformat(data.get("created_at"))
        session.status = data.get("status", "created")
        session.model = data.get("model")
        session.device_ids = data.get("device_ids", [])
        session.recording_file = data.get("recording_file")
        session.transcript_file = data.get("transcript_file")
        session.is_recording = data.get("is_recording", False)
        session.transcript = data.get("transcript", [])
        session.summary = data.get("summary", "")
        session.name = data.get("name")
        session.participants = data.get("participants", [])
        
        return session
    
    def save_to_disk(self) -> bool:
        """Save the session to disk
        
        Returns:
            True if successful, False otherwise
        """
        try:
            # Ensure the sessions directory exists
            self.ensure_sessions_dir()
            
            # Create the file path
            file_path = os.path.join(self.SESSIONS_DIR, f"{self.session_id}.json")
            
            # Convert session to dictionary
            session_data = self.to_storage_dict()
            
            # Write the file
            with open(file_path, 'w') as f:
                json.dump(session_data, f, indent=2)
                
            logger.info(f"Session saved to disk: {file_path}")
            return True
        except Exception as e:
            logger.error(f"Error saving session to disk: {e}")
            return False
    
    @classmethod
    def load_from_disk(cls, session_id: str) -> Optional['Session']:
        """Load a session from disk
        
        Args:
            session_id: The ID of the session to load
            
        Returns:
            The loaded Session or None if not found or error
        """
        try:
            # Create the file path
            file_path = os.path.join(cls.SESSIONS_DIR, f"{session_id}.json")
            
            # Check if the file exists
            if not os.path.exists(file_path):
                logger.warning(f"Session file not found: {file_path}")
                return None
                
            # Read the file
            with open(file_path, 'r') as f:
                session_data = json.load(f)
                
            # Create a new session from the data
            session = cls.from_storage_dict(session_data)
            logger.info(f"Session loaded from disk: {file_path}")
            return session
        except Exception as e:
            logger.error(f"Error loading session from disk: {e}")
            return None
    
    def rename(self, new_name: str) -> bool:
        """Rename the session
        
        Args:
            new_name: The new name for the session
            
        Returns:
            True if successful, False otherwise
        """
        try:
            self.name = new_name
            logger.info(f"Session {self.session_id} renamed to '{new_name}'")
            return self.save_to_disk()
        except Exception as e:
            logger.error(f"Error renaming session {self.session_id}: {e}")
            return False

    def extract_participants(self) -> List[Dict[str, str]]:
        """Extract unique participants from the transcript
        
        Returns:
            A list of participant dictionaries with id and name
        """
        unique_speakers = set()
        
        # First pass: collect all unique speaker identifiers
        for segment in self.transcript:
            if 'speaker' in segment and segment['speaker']:
                unique_speakers.add(segment['speaker'])
                
        # Convert to participant dictionaries
        participants = []
        for speaker in sorted(unique_speakers):
            participants.append({
                "id": speaker,
                "name": speaker  # Initially use the same value for id and name
            })
            
        # Update the session's participants list
        self.participants = participants
        logger.info(f"Extracted {len(participants)} participants from transcript for session {self.session_id}")
        
        # Save updated participants to disk
        self.save_to_disk()
        
        return participants
    
    def update_participant(self, participant_id: str, name: str) -> bool:
        """Update a participant's information
        
        Args:
            participant_id: The ID of the participant to update (e.g., "Speaker 1")
            name: The new name for the participant
            
        Returns:
            True if successful, False otherwise
        """
        try:
            for participant in self.participants:
                if participant["id"] == participant_id:
                    participant["name"] = name
                    logger.info(f"Updated participant {participant_id} to name '{name}' in session {self.session_id}")
                    return self.save_to_disk()
            
            # Participant not found
            logger.warning(f"Participant {participant_id} not found in session {self.session_id}")
            return False
        except Exception as e:
            logger.error(f"Error updating participant {participant_id} in session {self.session_id}: {e}")
            return False
    
    @classmethod
    def list_saved_sessions(cls) -> List[str]:
        """List all saved session IDs
        
        Returns:
            A list of session IDs
        """
        cls.ensure_sessions_dir()
        try:
            # Get all JSON files in the sessions directory
            session_files = glob.glob(os.path.join(cls.SESSIONS_DIR, "*.json"))
            
            # Extract session IDs from filenames
            session_ids = [os.path.splitext(os.path.basename(f))[0] for f in session_files]
            
            return session_ids
        except Exception as e:
            logger.error(f"Error listing saved sessions: {e}")
            return []
