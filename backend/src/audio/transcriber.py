import whisper
from typing import Optional, Dict, List
import numpy as np
import threading
import queue
import time
import torch
from pyannote.audio import Pipeline
import os
from dotenv import load_dotenv
from ctc_forced_aligner import (
    load_alignment_model,
    generate_emissions,
    preprocess_text,
    get_alignments,
    get_spans,
    postprocess_results,
)
from deepmultilingualpunctuation import PunctuationModel
import nltk

# Load environment variables from .env file
load_dotenv()

# Download required NLTK data
try:
    nltk.data.find('tokenizers/punkt')
except LookupError:
    nltk.download('punkt')

class AudioTranscriber:
    def __init__(self, model_name: str = "large-v3"):  # Using the latest large model
        print("Initializing AudioTranscriber...")
        print(f"CUDA available: {torch.cuda.is_available()}")
        print(f"Current CUDA device: {torch.cuda.current_device() if torch.cuda.is_available() else 'None'}")
        
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            self.device = "cuda"
            torch.cuda.set_device(0)
            print(f"Using GPU: {torch.cuda.get_device_name(0)}")
        else:
            self.device = "cpu"
            print("Using CPU")
            
        print(f"Loading Whisper model: {model_name}")
        self.model = whisper.load_model(model_name)
        print("Whisper model loaded, moving to device...")
        
        # Keep model in full precision
        self.model = self.model.to(self.device)
        print(f"Whisper model moved to {self.device}")
        
        # Initialize diarization pipeline
        print("Initializing diarization pipeline...")
        try:
            hf_token = os.getenv("HUGGINGFACE_TOKEN")
            if not hf_token:
                raise ValueError("HUGGINGFACE_TOKEN not found in .env file")
            
            self.diarization = Pipeline.from_pretrained(
                "pyannote/speaker-diarization",
                use_auth_token=hf_token
            ).to(self.device)
            
            # Configure diarization parameters
            self.diarization.instantiate({
                "segmentation": {
                    "min_duration_off": 0.1,      # Minimum silence duration
                    "threshold": 0.4,             # Speech activity detection threshold
                    "min_duration_on": 0.2,       # Minimum speech duration
                },
                "clustering": {
                    "method": "centroid",         # Use centroid clustering
                    "min_cluster_size": 6,        # Minimum segment duration per speaker
                    "threshold": 0.7              # Speaker differentiation threshold
                }
            })
            print("Diarization pipeline initialized")
        except Exception as e:
            print(f"Error initializing diarization: {e}")
            self.diarization = None
            
        # Initialize punctuation model
        self.punct_model = PunctuationModel(model="kredor/punctuate-all")
            
        self.transcription_queue = queue.Queue()
        self.current_transcript: List[Dict] = []

    def merge_segments(self, segments: List[Dict]) -> List[Dict]:
        """Merge consecutive segments from the same speaker"""
        if not segments:
            return segments
            
        merged = []
        current = segments[0].copy()
        
        for next_segment in segments[1:]:
            # If same speaker and close in time, merge
            if (next_segment['speaker'] == current['speaker'] and 
                next_segment['start'] - current['end'] < 1.0):  # 1 second gap threshold
                current['text'] += ' ' + next_segment['text']
                current['end'] = next_segment['end']
            else:
                merged.append(current)
                current = next_segment.copy()
        
        merged.append(current)
        return merged

    def process_audio_file(self, audio_file: str, batch_size: int = 8, language: str = None):
        """Process a complete audio file"""
        try:
            print(f"Processing audio file: {audio_file}")
            
            # Load and convert audio
            audio_waveform = whisper.load_audio(audio_file)
            
            # Perform diarization if available
            speakers = []
            if self.diarization:
                try:
                    print("Performing speaker diarization...")
                    diarization = self.diarization({"audio": audio_file})
                    
                    # Convert diarization results to speaker segments
                    for turn, _, speaker in diarization.itertracks(yield_label=True):
                        speakers.append({
                            "start": turn.start,
                            "end": turn.end,
                            "speaker": f"Speaker {speaker.split('_')[-1]}"
                        })
                    print(f"Found {len(speakers)} speaker segments")
                except Exception as e:
                    print(f"Diarization error: {e}")
                    import traceback
                    print(f"Diarization error traceback: {traceback.format_exc()}")
            
            # Transcribe with Whisper
            print("Starting transcription...")
            result = self.model.transcribe(
                audio_waveform,
                language=language,
                fp16=False,
                beam_size=5,
                best_of=5,
                temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
                compression_ratio_threshold=2.4,
                condition_on_previous_text=True,
                logprob_threshold=-1.0,
                no_speech_threshold=0.6,
                word_timestamps=True
            )
            print("Transcription complete")
            
            # Process segments
            segments = []
            if "segments" in result:
                for segment in result["segments"]:
                    text = segment["text"].strip()
                    if text:
                        start_time = segment["start"]
                        end_time = segment["end"]
                        
                        # Find the speaker for this segment
                        speaker = "Speaker 1"  # Default speaker
                        if speakers:
                            segment_mid = (start_time + end_time) / 2
                            for s in speakers:
                                if s["start"] <= segment_mid <= s["end"]:
                                    speaker = s["speaker"]
                                    break
                        
                        # Add punctuation
                        words = [text]
                        labeled_words = self.punct_model.predict(words)
                        text = labeled_words[0][0]
                        if labeled_words[0][1] in ".!?":
                            text += labeled_words[0][1]
                        
                        segments.append({
                            "text": text,
                            "timestamp": time.time(),
                            "speaker": speaker,
                            "start": start_time,
                            "end": end_time
                        })
            
            # Merge segments from the same speaker
            merged_segments = self.merge_segments(segments)
            
            # Queue the merged segments
            for segment in merged_segments:
                self.current_transcript.append(segment)
                self.transcription_queue.put(segment)
                print(f"Processed segment: {segment['speaker']}: {segment['text']}")
            
            print("Processing complete")
            return True
            
        except Exception as e:
            print(f"Error processing audio: {e}")
            import traceback
            print(f"Processing error traceback: {traceback.format_exc()}")
            return False
        finally:
            # Clear GPU memory
            if self.device == "cuda":
                torch.cuda.empty_cache()

    def get_latest_transcription(self):
        """Get the latest transcription from queue"""
        try:
            return self.transcription_queue.get_nowait()
        except queue.Empty:
            return None

    def get_full_transcript(self):
        """Get the complete transcript"""
        return self.current_transcript

    def clear_transcript(self):
        """Clear the transcript"""
        self.current_transcript = []
