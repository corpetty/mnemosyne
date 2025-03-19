from typing import Optional, Dict, Any
import logging

from ..audio.transcriber import AudioTranscriber
from ..llm.summarizer import Summarizer

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class ModelService:
    """Singleton service to manage shared model instances"""
    
    _instance = None
    
    @classmethod
    def get_instance(cls) -> 'ModelService':
        """Get or create the singleton instance"""
        if cls._instance is None:
            logger.info("Creating new ModelService instance")
            cls._instance = ModelService()
        return cls._instance
    
    def __init__(self):
        """Initialize the model service (should only be called once)"""
        if ModelService._instance is not None:
            raise RuntimeError("ModelService is a singleton. Use get_instance() instead.")
        
        # Initialize with None - models will be loaded on demand
        self._transcriber: Optional[AudioTranscriber] = None
        self._summarizer: Optional[Summarizer] = None
        
        logger.info("ModelService initialized (models will be loaded on demand)")
    
    def get_transcriber(self) -> AudioTranscriber:
        """Get or create the shared transcriber instance"""
        if self._transcriber is None:
            logger.info("Loading AudioTranscriber model (first use)")
            self._transcriber = AudioTranscriber()
        return self._transcriber
    
    def get_summarizer(self, provider: str = "ollama", model: str = None) -> Summarizer:
        """Get or create the shared summarizer instance"""
        if self._summarizer is None:
            logger.info(f"Loading Summarizer model (first use): provider={provider}, model={model}")
            self._summarizer = Summarizer(provider=provider, model=model)
        elif model is not None and self._summarizer.model != model:
            # If requesting a different model, update the current instance
            logger.info(f"Updating summarizer model: {self._summarizer.model} -> {model}")
            self._summarizer.model = model
        return self._summarizer
    
    def release_resources(self):
        """Release model resources to free memory"""
        logger.info("Releasing model resources")
        self._transcriber = None
        self._summarizer = None
