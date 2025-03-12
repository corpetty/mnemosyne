import sounddevice as sd
import numpy as np
from typing import Optional, List, Dict
import queue
import threading
import subprocess
import json
import wave
import os
from datetime import datetime
from scipy import signal
import time

class AudioCapture:
    _cached_devices: List[Dict] = []
    _last_cache_time: float = 0
    CACHE_DURATION = 60  # Cache duration in seconds

    def __init__(self, sample_rate: int = 16000):
        self.sample_rate = sample_rate
        self.is_recording = False
        self._recording_threads: List[threading.Thread] = []
        self.buffer_size = 8192
        self.active_sources = []
        self.source_buffers = {}
        self.buffer_lock = threading.Lock()
        self.output_file = None
        self.wave_file = None

    @classmethod
    def list_devices(cls, force_refresh: bool = False) -> List[Dict]:
        """Get list of available audio devices including monitor sources"""
        current_time = time.time()
        if force_refresh or not cls._cached_devices or (current_time - cls._last_cache_time) > cls.CACHE_DURATION:
            cls._cached_devices = cls._fetch_devices()
            cls._last_cache_time = current_time
        return cls._cached_devices

    @staticmethod
    def _fetch_devices() -> List[Dict]:
        """Fetch the list of audio devices"""
        devices = []
        try:
            # Get PipeWire sources using pw-dump
            try:
                result = subprocess.run(['pw-dump'], capture_output=True, text=True)
                if result.returncode == 0:
                    pw_devices = json.loads(result.stdout)
                    
                    # Find monitor sources and inputs
                    for device in pw_devices:
                        if device.get('type') == 'PipeWire:Interface:Node':
                            props = device.get('info', {}).get('props', {})
                            
                            # Check if it's an audio source
                            media_class = props.get('media.class', '')
                            if ('Audio/Source' in media_class or 
                                'Stream/Output/Audio' in media_class or
                                'Audio/Sink' in media_class):
                                
                                device_id = str(device.get('id', ''))
                                name = props.get('node.description', props.get('node.name', ''))
                                
                                # Check if it's a monitor
                                is_monitor = (
                                    'monitor' in name.lower() or
                                    'monitor' in props.get('node.name', '').lower() or
                                    'Output/Audio' in media_class or
                                    'Audio/Sink' in media_class
                                )
                                
                                if is_monitor:
                                    print(f"Found monitor device: {name} ({device_id})")
                                    print(f"Media class: {media_class}")
                                
                                devices.append({
                                    'id': device_id,
                                    'name': name,
                                    'channels': int(props.get('audio.channels', 2)),
                                    'default': False,
                                    'is_monitor': is_monitor,
                                    'media_class': media_class
                                })
                
                print("Found PipeWire devices:", json.dumps(devices, indent=2))
            except Exception as e:
                print(f"Error getting PipeWire sources: {e}")
                import traceback
                print(f"PipeWire error traceback: {traceback.format_exc()}")

            print("Available devices:", json.dumps(devices, indent=2))
            return devices
        except Exception as e:
            print(f"Error listing devices: {e}")
            import traceback
            print(f"Error traceback: {traceback.format_exc()}")
            return []

    def start_recording(self, device_ids: Optional[List[str]] = None):
        """Start recording audio from specified sources to a WAV file"""
        if not device_ids:
            print("No devices specified for recording")
            return

        try:
            # Create recordings directory if it doesn't exist
            os.makedirs('recordings', exist_ok=True)
            
            # Create a new WAV file
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.output_file = f"recordings/recording_{timestamp}.wav"
            
            self.wave_file = wave.open(self.output_file, 'wb')
            self.wave_file.setnchannels(1)  # Mono
            self.wave_file.setsampwidth(2)  # 16-bit
            self.wave_file.setframerate(self.sample_rate)
            
            self.is_recording = True
            self.active_sources = []
            self.source_buffers = {}

            for device_id in device_ids:
                self.active_sources.append(device_id)
                self._start_pipewire_recording(device_id)
                    
            print(f"Started recording to {self.output_file} from {len(self.active_sources)} sources")
            
        except Exception as e:
            print(f"Error starting audio streams: {e}")
            import traceback
            print(f"Start recording error traceback: {traceback.format_exc()}")
            self.is_recording = False
            if self.wave_file:
                self.wave_file.close()
            raise

    def _start_pipewire_recording(self, device_id: str):
        """Start recording from a PipeWire source"""
        cmd = [
            'pw-record',
            '--target', device_id,
            '--rate', str(self.sample_rate),
            '--channels', '1',
            '--format', 's16',  # 16-bit signed integer
            '--latency', '128/48000',
            '-'  # Output to stdout
        ]
        
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=self.buffer_size * 2  # 2 bytes per sample for s16
        )
        print(f"Started PipeWire recording from: {device_id}")
        
        def read_pipewire_audio():
            try:
                while self.is_recording and device_id in self.active_sources:
                    raw_data = process.stdout.read(self.buffer_size * 2)  # 2 bytes per sample
                    if raw_data:
                        audio_data = np.frombuffer(raw_data, dtype=np.int16)
                        if len(audio_data) > 0:
                            with self.buffer_lock:
                                self.source_buffers[device_id] = audio_data
                                
                                # If we have data from all sources, mix and write to file
                                if len(self.source_buffers) == len(self.active_sources):
                                    buffers_to_mix = list(self.source_buffers.values())
                                    mixed_audio = self._mix_audio(buffers_to_mix)
                                    self.wave_file.writeframes(mixed_audio.tobytes())
                                    self.source_buffers.clear()
                                    
            except Exception as e:
                print(f"Error reading PipeWire audio data: {e}")
                import traceback
                print(f"PipeWire read error traceback: {traceback.format_exc()}")
            finally:
                try:
                    process.terminate()
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                except Exception as e:
                    print(f"Error cleaning up PipeWire process: {e}")
        
        thread = threading.Thread(target=read_pipewire_audio)
        thread.start()
        self._recording_threads.append(thread)

    def _mix_audio(self, buffers: List[np.ndarray]) -> np.ndarray:
        """Mix multiple audio streams together with improved quality"""
        if not buffers:
            return np.array([], dtype=np.int16)
        
        # Ensure all buffers are the same length
        min_length = min(len(buf) for buf in buffers)
        aligned_buffers = [buf[:min_length] for buf in buffers]
        
        # Convert to float32 for processing
        float_buffers = [buf.astype(np.float32) / 32768.0 for buf in aligned_buffers]
        
        # Apply noise reduction to each buffer
        denoised_buffers = [self._reduce_noise(buf) for buf in float_buffers]
        
        # Calculate weights based on signal-to-noise ratio
        weights = [np.std(buf) for buf in denoised_buffers]
        total_weight = sum(weights)
        if total_weight > 0:
            weights = [w / total_weight for w in weights]
        else:
            weights = [1.0 / len(denoised_buffers)] * len(denoised_buffers)
        
        # Mix the streams with calculated weights
        mixed = np.sum([buf * weight for buf, weight in zip(denoised_buffers, weights)], axis=0)
        
        # Apply volume normalization
        mixed = self._normalize_volume(mixed)
        
        # Convert back to int16
        mixed = (mixed * 32767).astype(np.int16)
        
        return mixed

    def _reduce_noise(self, audio: np.ndarray) -> np.ndarray:
        """Apply simple noise reduction"""
        # Estimate noise from the first 1000 samples (assuming it's background noise)
        noise_sample = audio[:1000]
        noise_power = np.mean(noise_sample ** 2)
        
        # Apply spectral gating
        sig_stft = np.fft.rfft(audio)
        sig_power = np.abs(sig_stft) ** 2
        mask = sig_power > (noise_power * 2)  # Adjust threshold as needed
        sig_stft_denoised = sig_stft * mask
        
        return np.fft.irfft(sig_stft_denoised)

    def _normalize_volume(self, audio: np.ndarray) -> np.ndarray:
        """Normalize audio volume"""
        max_amplitude = np.max(np.abs(audio))
        if max_amplitude > 0:
            return audio / max_amplitude
        return audio

    def stop_recording(self) -> Optional[str]:
        """Stop recording and return the path to the recorded file"""
        if self.is_recording:
            self.is_recording = False
            self.active_sources = []
            
            # Wait for all recording threads to finish
            for thread in self._recording_threads:
                thread.join()
            self._recording_threads = []
            
            # Close the wave file
            if self.wave_file:
                self.wave_file.close()
                self.wave_file = None
            
            print("Stopped recording")
            return self.output_file
        
        return None
