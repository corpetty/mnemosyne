import torch
from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor, pipeline
from typing import Optional, Dict, List
import numpy as np
import threading
import queue
import time
import os
import librosa
from dotenv import load_dotenv
from pyannote.audio import Pipeline
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
import difflib

# Load environment variables from .env file
load_dotenv()

# Download required NLTK data
try:
    nltk.data.find('tokenizers/punkt')
except LookupError:
    nltk.download('punkt')

class AudioTranscriber:
    def __init__(self, model_name: str = "distil-whisper/distil-large-v3"):
        print("Initializing AudioTranscriber...")
        print(f"CUDA available: {torch.cuda.is_available()}")
        print(f"Current CUDA device: {torch.cuda.current_device() if torch.cuda.is_available() else 'None'}")
        
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            self.device = torch.device("cuda")
            torch.cuda.set_device(0)
            print(f"Using GPU: {torch.cuda.get_device_name(0)}")
        else:
            self.device = torch.device("cpu")
            print("Using CPU")
        
        self.torch_dtype = torch.float16 if torch.cuda.is_available() else torch.float32
        
        print(f"Loading Distil-Whisper model: {model_name}")
        self.model = AutoModelForSpeechSeq2Seq.from_pretrained(
            model_name, 
            torch_dtype=self.torch_dtype, 
            low_cpu_mem_usage=True, 
            use_safetensors=True
        )
        self.model.to(self.device)
        
        self.processor = AutoProcessor.from_pretrained(model_name)
        
        # Configure pipeline with explicit return_timestamps=True
        # And ensure WhisperTimeStampLogitsProcessor is used
        self.pipe = pipeline(
            "automatic-speech-recognition",
            model=self.model,
            tokenizer=self.processor.tokenizer,
            feature_extractor=self.processor.feature_extractor,
            max_new_tokens=128,
            torch_dtype=self.torch_dtype,
            device=self.device,
            return_timestamps=True,  # Explicitly request timestamps
            chunk_length_s=30,       # Fixed chunk length
            stride_length_s=5        # Fixed stride length for overlap
        )
        
        print("Distil-Whisper model loaded and pipeline created")
        
        # Initialize diarization pipeline
        print("Initializing diarization pipeline...")
        try:
            hf_token = os.getenv("HUGGINGFACE_TOKEN")
            if not hf_token:
                raise ValueError("HUGGINGFACE_TOKEN not found in .env file")
            
            self.diarization = Pipeline.from_pretrained(
                "pyannote/speaker-diarization-3.1",
                use_auth_token=hf_token
            ).to(self.device)
            
            print("Diarization pipeline initialized")
        except Exception as e:
            print(f"Error initializing diarization: {e}")
            import traceback
            print(f"Diarization error traceback: {traceback.format_exc()}")
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
        current = None
        
        for segment in sorted(segments, key=lambda x: x['start']):
            # If this is the first segment or from a different speaker, start a new current segment
            if not current or segment['speaker'] != current['speaker'] or segment['start'] - current['end'] > 0.75:
                if current:
                    merged.append(current)
                current = segment.copy()
            else:
                # Merge with current segment
                current['text'] += ' ' + segment['text']
                current['end'] = segment['end']  # Extend the end time
        
        if current:
            merged.append(current)
            
        return merged

    def text_similarity(self, text1: str, text2: str) -> float:
        """Calculate similarity ratio between two texts"""
        return difflib.SequenceMatcher(None, text1, text2).ratio()

    def is_similar_to_any(self, text: str, existing_texts: List[str], threshold: float = 0.85) -> bool:
        """Check if text is similar to any text in the list"""
        for existing_text in existing_texts:
            if self.text_similarity(text, existing_text) > threshold:
                return True
        return False

    def _deduplicate_segments(self, segments: List[Dict]) -> List[Dict]:
        """Remove duplicate and highly similar transcript segments, considering speaker identity"""
        if not segments:
            return segments
            
        deduplicated = []
        # Keep track of text seen per speaker
        seen_texts_by_speaker = {}
        
        for segment in segments:
            text = segment['text'].strip()
            speaker = segment['speaker']
            
            # Skip empty segments
            if not text:
                continue
                
            # Initialize seen texts list for this speaker if needed
            if speaker not in seen_texts_by_speaker:
                seen_texts_by_speaker[speaker] = []
                
            # Add if not similar to any existing text from this speaker
            if not self.is_similar_to_any(text, seen_texts_by_speaker[speaker]):
                seen_texts_by_speaker[speaker].append(text)
                deduplicated.append(segment)
                
        return deduplicated

    def find_speaker_for_time(self, time_point: float, speakers: List[Dict]) -> str:
        """Find which speaker was active at the given time point"""
        default_speaker = "Speaker 1"
        
        if not speakers:
            return default_speaker
            
        # Find all speakers active at this time point
        active_speakers = []
        for s in speakers:
            if s["start"] <= time_point <= s["end"]:
                active_speakers.append(s["speaker"])
                
        if not active_speakers:
            # Find closest speaker
            best_speaker = default_speaker
            best_distance = float('inf')
            
            for s in speakers:
                # Distance to start or end, whichever is closer
                distance = min(abs(s["start"] - time_point), abs(s["end"] - time_point))
                if distance < best_distance:
                    best_distance = distance
                    best_speaker = s["speaker"]
                    
            return best_speaker
        
        # Return most common active speaker (handles overlapping segments)
        return max(set(active_speakers), key=active_speakers.count)

    def split_text(self, text: str, max_chunk_size: int = 256) -> List[str]:
        """Split text into chunks of maximum size"""
        words = text.split()
        chunks = []
        current_chunk = []

        for word in words:
            if len(' '.join(current_chunk + [word])) <= max_chunk_size:
                current_chunk.append(word)
            else:
                chunks.append(' '.join(current_chunk))
                current_chunk = [word]

        if current_chunk:
            chunks.append(' '.join(current_chunk))

        return chunks

    def add_punctuation(self, text: str) -> str:
        """Add punctuation to text, handling large chunks"""
        chunks = self.split_text(text)
        punctuated_chunks = []

        for chunk in chunks:
            labeled_words = self.punct_model.predict([chunk])
            punctuated_chunk = labeled_words[0][0]
            if labeled_words[0][1] in ".!?":
                punctuated_chunk += labeled_words[0][1]
            punctuated_chunks.append(punctuated_chunk)

        return ' '.join(punctuated_chunks)

    def process_audio_file(self, audio_file_path: str, batch_size: int = 8, language: str = None):
        """Process a complete audio file"""
        try:
            print(f"Processing audio file: {audio_file_path}")
            
            # Clear any previous transcript
            self.clear_transcript()
            
            # Load audio file using librosa
            audio, sr = librosa.load(audio_file_path, sr=16000, mono=True)
            audio = librosa.util.normalize(audio)
            
            # Perform diarization if available
            speakers = []
            if self.diarization:
                try:
                    print("Performing speaker diarization...")
                    diarization = self.diarization({"audio": audio_file_path})
                    
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
            
            # Transcribe with Distil-Whisper
            print("Starting transcription...")
            try:
                # Use the pipeline with existing configurations
                result = self.pipe(
                    audio,
                    return_timestamps=True  # Explicitly request timestamps again
                )
            except Exception as e:
                print(f"Error during transcription: {e}")
                import traceback
                print(f"Transcription error traceback: {traceback.format_exc()}")
                # Try again with smaller chunks if there was an error
                print("Retrying with smaller chunks...")
                result = self.pipe(
                    audio,
                    chunk_length_s=15,  # Smaller chunks
                    stride_length_s=3,  # Smaller stride
                    return_timestamps=True
                )
            print("Transcription complete")
            
            # Process segments
            segments = []
            
            # Check if the result has chunks
            if isinstance(result, dict) and "chunks" in result:
                print(f"Processing {len(result['chunks'])} chunks")
                
                # Get all chunks with timestamps
                for chunk in result["chunks"]:
                    text = chunk["text"].strip()
                    if not text:
                        continue  # Skip empty chunks
                    
                    # Ensure we have timestamps
                    if "timestamp" in chunk:
                        start_time = chunk["timestamp"][0]
                        end_time = chunk["timestamp"][1]
                    else:
                        # If timestamps are missing (should not happen with return_timestamps=True)
                        start_time = 0.0
                        end_time = 0.0
                        print(f"Warning: Missing timestamps for chunk: {text}")
                        
                    # Find the speaker for this segment using a more robust method
                    # Handle cases where start_time or end_time might be None
                    if start_time is None or end_time is None:
                        # If at least one timestamp is available, use it
                        if start_time is not None:
                            time_point = start_time
                        elif end_time is not None:
                            time_point = end_time
                        else:
                            # If both are None, use a fallback approach
                            time_point = 0.0
                        print(f"Warning: Handling missing timestamp for chunk: {text}")
                    else:
                        # Original calculation when both timestamps are available
                        time_point = (start_time + end_time) / 2
                    
                    speaker = self.find_speaker_for_time(time_point, speakers)
                    
                    # Add punctuation
                    text = self.add_punctuation(text)
                    
                    # Create segment with current time as processing timestamp
                    # Ensure we don't store None values for timestamps
                    safe_start = 0.0 if start_time is None else start_time
                    safe_end = 0.0 if end_time is None else end_time
                    
                    segments.append({
                        "text": text,
                        "timestamp": time.time(),
                        "speaker": speaker,
                        "start": safe_start,
                        "end": safe_end
                    })
            else:
                # Fallback if no chunks are available
                text = result["text"].strip() if isinstance(result, dict) and "text" in result else str(result)
                if text:
                    # Add punctuation
                    text = self.add_punctuation(text)
                    
                    # Infer speaker from diarization if possible
                    speaker = "Speaker 1"
                    if speakers:
                        # Use the speaker with the most talk time
                        speaker_talk_time = {}
                        for s in speakers:
                            speaker_name = s["speaker"]
                            talk_time = s["end"] - s["start"]
                            speaker_talk_time[speaker_name] = speaker_talk_time.get(speaker_name, 0) + talk_time
                        
                        if speaker_talk_time:
                            speaker = max(speaker_talk_time.items(), key=lambda x: x[1])[0]
                    
                    segments.append({
                        "text": text,
                        "timestamp": time.time(),
                        "speaker": speaker,
                        "start": 0,
                        "end": float(len(audio) / sr) if sr > 0 else 0  # Use audio length as end time
                    })
            
            print(f"Raw segments count: {len(segments)}")
            
            # Skip deduplication and merging if only one segment
            if len(segments) <= 1:
                final_segments = segments
            else:
                # Merge segments from the same speaker, preserving timestamps
                merged_segments = self.merge_segments(segments)
                print(f"After merging: {len(merged_segments)} segments")
                
                # Deduplicate segments with identical or similar text from the same speaker
                final_segments = self._deduplicate_segments(merged_segments)
                print(f"After deduplication: {len(final_segments)} segments")
            
            # Queue the final segments
            for segment in final_segments:
                self.current_transcript.append(segment)
                self.transcription_queue.put(segment)
                print(f"Final segment: {segment['speaker']} ({segment['start']:.1f}s-{segment['end']:.1f}s): {segment['text']}")
            
            print("Processing complete")
            return True
            
        except Exception as e:
            print(f"Error processing audio: {e}")
            import traceback
            print(f"Processing error traceback: {traceback.format_exc()}")
            return False
        finally:
            # Clear GPU memory
            if torch.cuda.is_available():
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
        # Also clear the queue
        while not self.transcription_queue.empty():
            try:
                self.transcription_queue.get_nowait()
            except queue.Empty:
                break
