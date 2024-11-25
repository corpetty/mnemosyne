# Mnemosyne

A real-time audio transcription and diarization system that can capture both system audio and microphone input simultaneously.

## Features

- Real-time audio transcription using OpenAI's Whisper
- Speaker diarization using NVIDIA's NeMo
- Supports multiple audio sources simultaneously (e.g., system audio + microphone)
- Real-time display of transcriptions with speaker identification
- Automatic summarization of conversations
- Exports transcripts in both text and SRT formats

## Prerequisites

- Python 3.11 (Python 3.13 is not yet fully supported by all dependencies)
- NVIDIA GPU with CUDA support (recommended)
- PipeWire audio system (for Linux)
- Node.js and npm (for frontend)

## Installation

1. Create and activate a Python virtual environment:
```bash
python3.11 -m venv venv
source venv/bin/activate
```

2. Install PyTorch and torchaudio:
```bash
pip install --upgrade pip
pip install torch --index-url https://download.pytorch.org/whl/cu118
pip install torchaudio --index-url https://download.pytorch.org/whl/cu118
```

3. Install Python dependencies:
```bash
pip install -r backend/requirements.txt
```

4. Install frontend dependencies:
```bash
cd frontend
npm install
```

## Configuration

1. Create a `.env` file in the root directory:
```bash
HUGGINGFACE_TOKEN=your_token_here
```

To get your Hugging Face token:
1. Go to https://huggingface.co/settings/tokens
2. Create a new token with read access
3. Copy the token and paste it in your `.env` file

## Running the Application

1. Start the backend:
```bash
cd backend
uvicorn src.api.main:app --reload
```

2. Start the frontend (in a new terminal):
```bash
cd frontend
npm start
```

3. Open http://localhost:3000 in your browser

## Usage

1. Select audio sources:
   - Choose system audio source(s) to capture desktop audio
   - Choose microphone input to capture your voice
   - You can select multiple sources to capture simultaneously

2. Click "Start Recording" to begin transcription
   - The system will transcribe audio in real-time
   - Speakers will be automatically identified
   - Transcriptions appear in the text box as they're processed

3. Click "Stop Recording" when finished
   - The system will generate a final transcript
   - A summary will be generated
   - Files are saved in the transcripts directory

## Output Files

The system generates several files:
- `recordings/recording_[timestamp].wav` - The recorded audio file
- `transcripts/transcript_[timestamp].md` - The transcript with speaker identification
- `transcripts/transcript_[timestamp].srt` - SRT format for video subtitling

## Troubleshooting

### Common Issues

1. **No system audio sources available:**
   - Make sure PipeWire is running: `systemctl --user status pipewire`
   - Check available sources: `pw-cli list-objects | grep -A 3 "Monitor"`

2. **GPU memory errors:**
   - Reduce batch size in transcriber.py
   - Free up GPU memory by closing other applications

3. **Installation errors:**
   - Make sure you're using Python 3.11
   - Install PyTorch and torchaudio before other dependencies
   - Check CUDA compatibility with `nvidia-smi`

4. **Dependency conflicts:**
   - If you encounter conflicts, try installing dependencies one by one
   - Make sure to install PyTorch first
   - Some packages may need to be upgraded or downgraded

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
- [NVIDIA NeMo](https://github.com/NVIDIA/NeMo) for speaker diarization
- [Faster Whisper](https://github.com/guillaumekln/faster-whisper) for optimized inference
- [CTC Forced Aligner](https://github.com/MahmoudAshraf97/ctc-forced-aligner) for timestamp alignment
