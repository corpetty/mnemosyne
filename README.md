# Mnemosyne

A real-time audio transcription and diarization system that can capture both system audio and microphone input simultaneously, with enhanced summarization capabilities.

## Name meaning
(from Claude)
>Mnemosyne is the personification of memory in Greek mythology. She was a Titaness, the daughter of Uranus (Heaven) and Gaia (Earth), and the mother of the nine Muses by Zeus. 
>
>The name "Mnemosyne" derives from the Greek word "mnēmē," meaning "memory" or "remembrance." In ancient Greek culture, she represented the concept of memory before writing was widespread, when oral traditions and memorization were essential cultural tools.
>
>Beyond mythology, the term has been used in various contexts:
>
>1. In psychology, the "Mnemosyne effect" sometimes refers to processes related to memory formation and recall
>
>2. "Project Mnemosyne" is an open-source spaced repetition software program designed to help with memorization
>
>3. The term appears in various works of literature, art, and music as a reference to memory or the preservation of knowledge
>
>The concept of Mnemosyne highlights the ancient Greek understanding of memory as a divine force fundamental to cultural preservation, artistic inspiration, and human identity.

## Features

- Real-time audio transcription using OpenAI's Whisper large-v3 model
- Speaker diarization using pyannote.audio 3.1
- Advanced summarization using Llama 3.3 (via Ollama) with dynamic prompt selection
- Supports multiple audio sources simultaneously (e.g., system audio + microphone)
- Real-time display of transcriptions with speaker identification
- Intelligent summary depth adjustment based on transcript length
- Ability to regenerate and update summaries for existing transcripts
- Exports transcripts in markdown format with timestamps
- Progress tracking and status updates during processing

## Recent Improvements

- **Session Persistence**: Sessions are now automatically saved to disk and loaded when the application starts, ensuring they persist between application restarts
- **Enhanced Summarization**: Implemented a three-tiered prompt system (detailed standard compact) that dynamically adjusts based on transcript length
- **Token Optimization**: Added automatic token count estimation to select the most appropriate prompt format
- **Improved Error Handling**: Better detection and handling of token limit errors with automatic fallback mechanisms
- **Status Feedback**: Added real-time status updates during summary generation with loading indicators
- **File Overwriting**: Added ability to regenerate summaries and update the original transcript files
- **UI Enhancements**: Added expand/collapse functionality for large summaries and improved styling for nested elements

## Prerequisites

- Python 3.10 (required for compatibility with pyannote.audio)
- NVIDIA GPU with CUDA support (recommended)
- PipeWire audio system (for Linux)
- Node.js and npm (for frontend)
- Hugging Face account and API token (for diarization model)
- Ollama with Llama 3.3 model (for summarization)

## Installation

1. Install Ollama and Llama 3.3:
```bash
# Install Ollama
curl -fsSL https://ollama.com/install.sh | sh

# Pull Llama 3.3 model
ollama pull llama3.3
```

2. Create and activate a Python virtual environment:
```bash
python3.10 -m venv venv
source venv/bin/activate
```Mnemosyne is the personification of memory in Greek mythology. She was a Titaness, the daughter of Uranus (Heaven) and Gaia (Earth), and the mother of the nine Muses by Zeus. 

The name "Mnemosyne" derives from the Greek word "mnēmē," meaning "memory" or "remembrance." In ancient Greek culture, she represented the concept of memory before writing was widespread, when oral traditions and memorization were essential cultural tools.

Beyond mythology, the term has been used in various contexts:

1. In psychology, the "Mnemosyne effect" sometimes refers to processes related to memory formation and recall

2. "Project Mnemosyne" is an open-source spaced repetition software program designed to help with memorization

3. The term appears in various works of literature, art, and music as a reference to memory or the preservation of knowledge

The concept of Mnemosyne highlights the ancient Greek understanding of memory as a divine force fundamental to cultural preservation, artistic inspiration, and human identity.
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
OLLAMA_URL=http://localhost:11434  # Ollama API endpoint
OPENAI_API_KEY=your_key_here       # Optional, for OpenAI fallback
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

# Verify Llama 3.3 is available
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
   - A summary is generated using Llama 3.3
   - Files are saved in the transcripts directory

4. Regenerate Summaries (New Feature):
   - Select an existing transcript file from the dropdown
   - Optionally select a different model
   - Click "Generate New Summary" to create an improved summary
   - The original transcript file will be updated with the new summary

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
- Using Llama 3.3 via Ollama
- Dynamic prompt selection based on transcript length:
  - Detailed mode: Comprehensive analysis for shorter transcripts
  - Standard mode: Balanced detail for medium-length transcripts
  - Compact mode: Essential information for long transcripts
- Intelligent token count estimation
- Error handling with automatic fallback mechanisms
- Status feedback during generation
- Ability to overwrite existing summaries
- Structured output with:
  - Overview
  - Participants & dynamics analysis
  - Detailed key topics with hierarchical organization
  - Comprehensive decisions & conclusions
  - Action items with context and dependencies

### User Interface
- Real-time status updates
- Clear speaker identification
- Timestamp display
- Processing progress indicators
- Device selection interface
- Summary regeneration capabilities
- Expand/collapse functionality for large summaries

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
   - Check Llama 3.3 model is installed: `ollama list`
   - Verify API endpoint in .env file
   - For token limit errors, try a different model or regenerate with a compact prompt
   - If using OpenAI fallback, ensure your API key is correctly set in .env
   - Monitor CPU/GPU usage during summarization as it can be resource-intensive

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
- [Ollama](https://ollama.com/) for running Llama 3.3
- [Llama 3.3](https://ai.meta.com/llama/) for summarization
