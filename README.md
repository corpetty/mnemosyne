# Mnemosyne

A real-time audio transcription and diarization system that can capture both system audio and microphone input simultaneously.

NOTE: This project is a super alpha terrible quality. Lots of work needs to be done to ensure the produced content is accurate. Don't have high expectations and if you have ideas on how to improve quality I'm all ears, or better yet, make a PR. 

## Features

- Real-time audio transcription using OpenAI's Whisper large-v3 model
- Speaker diarization using pyannote.audio 3.1
- Automatic summarization using Llama 3.2 (via Ollama)
- Supports multiple audio sources simultaneously (e.g., system audio + microphone)
- Real-time display of transcriptions with speaker identification
- Exports transcripts in markdown format with timestamps
- Progress tracking and status updates during processing

## Prerequisites

- Python 3.10 (required for compatibility with pyannote.audio)
- NVIDIA GPU with CUDA support (recommended)
- PipeWire audio system (for Linux)
- Node.js and npm (for frontend)
- Hugging Face account and API token (for diarization model)
- Ollama with Llama 3.2 model (for summarization)

## Installation

1. Install Ollama and Llama 3.2:
```bash
# Install Ollama
curl -fsSL https://ollama.com/install.sh | sh

# Pull Llama 2 model
ollama pull llama3.2
```

2. Create and activate a Python virtual environment:
```bash
python3.10 -m venv venv
source venv/bin/activate
```

3. Install PyTorch and torchaudio:
```bash
pip install --upgrade pip wheel setuptools
pip install torch --index-url https://download.pytorch.org/whl/cu118
pip install torchaudio --index-url https://download.pytorch.org/whl/cu118
```

4. Install Python dependencies:
```bash
pip install Cython
pip install "numpy>=1.22,<1.24"
pip install -r backend/requirements.txt
```

5. Install frontend dependencies:
```bash
cd frontend
npm install
```

## Configuration

1. Create a `.env` file in the root directory:
```bash
HUGGINGFACE_TOKEN=your_token_here
OLLAMA_HOST=http://localhost:11434  # Ollama API endpoint
```

2. Hugging Face Setup:
   - Go to https://huggingface.co/settings/tokens
   - Create a new token with read access
   - Copy the token and paste it in your `.env` file

3. Accept terms for these models:
   - https://huggingface.co/pyannote/speaker-diarization-3.1
   - https://huggingface.co/pyannote/segmentation-3.0

4. Verify Ollama setup:
```bash
# Check Ollama is running
curl http://localhost:11434/api/tags

# Verify Llama 3.2 is available
ollama list
```

## Running the Application

1. Start Ollama (if not running):
```bash
systemctl start ollama
```

2. Start the backend:
```bash
cd backend
PYTHONPATH=. uvicorn src.api.main:app --reload
```

3. Start the frontend (in a new terminal):
```bash
cd frontend
npm start
```

4. Open http://localhost:3000 in your browser

## Usage

1. Select audio sources:
   - Choose system audio source(s) to capture desktop audio
   - Choose microphone input to capture your voice
   - You can select multiple sources to capture simultaneously

2. Click "Start Recording" to begin transcription
   - The system will record audio from selected sources
   - Audio is saved as a WAV file in the recordings directory

3. Click "Stop Recording" when finished
   - The system will process the recording
   - Status updates show progress
   - Transcription appears with speaker identification
   - A summary is generated using Llama 2
   - Files are saved in the transcripts directory

## Output Files

The system generates several files:
- `recordings/recording_[timestamp].wav` - The recorded audio file
- `transcripts/transcript_[timestamp].md` - The transcript with:
  - Speaker identification
  - Timestamps
  - Full transcript
  - Generated summary

## Features in Detail

### Audio Capture
- Multiple source recording
- Proper audio mixing
- 16-bit WAV format
- Automatic gain control

### Transcription
- Using Whisper large-v3 model
- Word-level timestamps
- High accuracy for multiple languages
- Optimized for GPU processing

### Speaker Diarization
- Using pyannote.audio 3.1
- Advanced speaker separation
- Handles multiple speakers
- Optimized clustering parameters

### Summarization
- Using Llama 3.2 via Ollama
- Context-aware summaries
- Maintains speaker attribution
- Handles long conversations

### User Interface
- Real-time status updates
- Clear speaker identification
- Timestamp display
- Processing progress indicators
- Device selection interface

## Troubleshooting

### Common Issues

1. **No system audio sources available:**
   - Make sure PipeWire is running: `systemctl --user status pipewire`
   - Check available sources: `pw-cli list-objects | grep -A 3 "Monitor"`

2. **GPU memory errors:**
   - Free up GPU memory by closing other applications
   - Monitor GPU usage with `nvidia-smi`

3. **Installation errors:**
   - Make sure you're using Python 3.10
   - Install PyTorch before other dependencies
   - Check CUDA compatibility with `nvidia-smi`

4. **Audio quality issues:**
   - Check input device levels
   - Verify proper device selection
   - Monitor audio peaks during recording

5. **Summarization issues:**
   - Verify Ollama is running: `systemctl status ollama`
   - Check Llama 3.2 model is installed: `ollama list`
   - Verify API endpoint in .env file

### Getting Help

If you encounter issues:
1. Check the console output for error messages
2. Look for similar issues in the project's issue tracker
3. Include relevant error messages and system information when reporting issues

## License

[Insert License Information]

## Acknowledgments

This project uses:
- [OpenAI Whisper](https://github.com/openai/whisper) for transcription
- [Pyannote Audio](https://github.com/pyannote/pyannote-audio) for speaker diarization
- [Faster Whisper](https://github.com/guillaumekln/faster-whisper) for optimized inference
- [CTC Forced Aligner](https://github.com/MahmoudAshraf97/ctc-forced-aligner) for timestamp alignment
- [Ollama](https://ollama.com/) for running Llama 2
- [Llama 2](https://ai.meta.com/llama/) for summarization
