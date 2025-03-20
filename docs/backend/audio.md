# Audio Processing Documentation

## Overview

The audio processing module handles capturing audio from system devices, processing audio files, and transcribing speech to text. It consists of three main components:

1. **AudioCapture**: Handles recording from system audio devices
2. **AudioTranscriber**: Processes audio files and generates transcriptions
3. **Audio Helpers**: Utility functions for audio processing

## AudioCapture (`capture.py`)

The AudioCapture class provides functionality for recording audio from one or more system devices.

### Key Features

- Multi-device recording
- OPUS audio format for storage efficiency (compressed)
- Configurable output format
- Session-based recording management

### Methods

| Method | Description |
|--------|-------------|
| `start_recording(device_ids)` | Start recording from specified devices |
| `stop_recording()` | Stop the current recording and return the file path |

### Usage Example

```python
from audio.capture import AudioCapture

# Create a capture instance
capture = AudioCapture()

# Start recording from devices
device_ids = ["device1", "device2"]
capture.start_recording(device_ids)

# ... Recording in progress ...

# Stop recording
recording_file = capture.stop_recording()
print(f"Recording saved to: {recording_file}")
```

## AudioTranscriber (`transcriber.py`)

The AudioTranscriber class handles processing audio files and generating transcriptions using speech-to-text models.

### Key Features

- Configurable transcription models
- Speaker diarization (identifying different speakers)
- Timestamp generation for each segment
- Automatic punctuation and formatting

### Methods

| Method | Description |
|--------|-------------|
| `process_audio_file(file_path)` | Process an audio file and generate a transcript |
| `get_full_transcript()` | Get the complete transcript with all segments |
| `get_transcription_progress()` | Get the current progress of the transcription process |

### Transcript Format

The transcript is returned as a list of segment objects:

```python
[
    {
        "text": "Hello, this is speaker one.",
        "timestamp": 1647532098.45,
        "speaker": "Speaker 1",
        "start": 0.0,
        "end": 2.5
    },
    {
        "text": "And this is speaker two responding.",
        "timestamp": 1647532100.95,
        "speaker": "Speaker 2",
        "start": 2.5,
        "end": 5.0
    }
]
```

### Usage Example

```python
from audio.transcriber import AudioTranscriber

# Create a transcriber instance
transcriber = AudioTranscriber()

# Process an audio file
success = transcriber.process_audio_file("recording.wav")

if success:
    # Get the full transcript
    transcript = transcriber.get_full_transcript()
    
    # Print each segment
    for segment in transcript:
        print(f"{segment['speaker']} ({segment['start']}s - {segment['end']}s): {segment['text']}")
```

## Audio Helpers (`helpers.py`)

The helpers module provides utility functions for audio processing.

### Functions

| Function | Description |
|----------|-------------|
| `convert_audio_format(input_file, output_file, format, bitrate)` | Convert audio from one format to another with configurable bitrate |
| `normalize_audio(file_path)` | Normalize the volume of an audio file |
| `split_audio_by_silence(file_path)` | Split audio file at silent points |
| `merge_audio_files(file_paths, output_path)` | Merge multiple audio files into one |

### Audio Format Conversion

The `convert_audio_format` function uses pydub to convert between different audio formats:

```python
def convert_audio_format(input_file, output_file=None, format="opus", bitrate="64k"):
    """
    Convert audio from one format to another using pydub.
    
    Args:
        input_file: Path to the input audio file
        output_file: Path to save the output file (if None, will replace extension of input_file)
        format: Target format (opus, mp3, etc.)
        bitrate: Bitrate for compression (default: 64k - good for speech)
        
    Returns:
        Path to the converted file
    """
```

#### Supported Formats

- **OPUS**: Default format, optimized for speech (64kbps)
- **WAV**: Uncompressed format (larger files, highest quality)
- **MP3**: Common compressed format (good compatibility)
- **FLAC**: Lossless compression (medium size, no quality loss)

#### Storage Efficiency

Using compressed formats provides significant storage savings:

| Format | Size (1 min) | Quality | Compatibility |
|--------|--------------|---------|---------------|
| WAV    | ~10 MB       | Highest | Excellent     |
| OPUS   | ~0.5-1 MB    | Very Good | Good        |
| MP3    | ~1-1.5 MB    | Good    | Excellent     |
| FLAC   | ~3-5 MB      | Highest | Good          |

## Dependencies

The audio processing module depends on several libraries:

- **PyAudio**: For capturing audio from system devices
- **librosa**: For audio processing and analysis
- **SoundFile**: For reading and writing audio files
- **Whisper** or similar: For speech-to-text transcription
- **PyDub**: For audio manipulation

## Configuration

Audio processing can be configured through environment variables or configuration files:

- **AUDIO_SAMPLE_RATE**: Sample rate for audio recording (default: 44100)
- **AUDIO_CHANNELS**: Number of channels for audio recording (default: 1)
- **AUDIO_FORMAT**: Format for audio recording (default: WAV)
- **TRANSCRIPTION_MODEL**: Model to use for transcription (default: "base")

## Error Handling

The audio processing module includes robust error handling:

- Recording errors (device unavailable, permission issues)
- File format errors (unsupported formats, corrupt files)
- Transcription errors (model failures, processing errors)

Errors are logged with descriptive messages and propagated up to the calling code.

## Performance Considerations

- Audio processing can be resource-intensive, especially for large files
- Transcription uses AI models that may require significant CPU/GPU resources
- Consider implementing queue-based processing for handling multiple requests
