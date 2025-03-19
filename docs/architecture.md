# Mnemosyne Architecture

## Overview

Mnemosyne is a web application designed for audio recording, transcription, and summarization. It enables users to capture audio from different devices, process it with advanced transcription models, and generate insightful summaries.

The application follows a client-server architecture with a FastAPI backend and a React/TypeScript frontend.

## System Components

```
┌───────────────────┐      ┌──────────────────────────────────────┐
│                   │      │                                      │
│    Frontend       │      │               Backend                │
│    (React/TS)     │◄────►│              (FastAPI)              │
│                   │      │                                      │
└───────────────────┘      └──────────────────────────────────────┘
                                           │
                                           │
                                           ▼
                           ┌──────────────────────────────────────┐
                           │           Core Services              │
                           │                                      │
                           │  ┌────────────┐  ┌────────────────┐  │
                           │  │   Audio    │  │    Model       │  │
                           │  │  Capture   │  │   Service      │  │
                           │  └────────────┘  └────────────────┘  │
                           │                                      │
                           │  ┌────────────┐  ┌────────────────┐  │
                           │  │   Audio    │  │                │  │
                           │  │Transcriber │  │   Summarizer   │  │
                           │  └────────────┘  └────────────────┘  │
                           └──────────────────────────────────────┘
```

## Key Components

### Backend

1. **API Layer** (`/backend/src/api/`)
   - **main.py**: Main FastAPI application with endpoints for session management, recording, and file processing
   - **session.py**: Session management for recording and processing
   - **devices.py**: Audio device discovery and management

2. **Audio Processing** (`/backend/src/audio/`)
   - **capture.py**: Audio capture from system devices
   - **transcriber.py**: Audio transcription using speech-to-text models
   - **helpers.py**: Utility functions for audio processing

3. **LLM Integration** (`/backend/src/llm/`)
   - **summarizer.py**: Text summarization using large language models

4. **Services** (`/backend/src/services/`)
   - **model_service.py**: Service for managing AI models with efficient resource usage

### Frontend

1. **Core Components** (`/frontend/src/components/`)
   - **SessionList.tsx**: Manages and displays recording sessions
   - **ParticipantList.tsx**: Displays and manages session participants
   - **DeviceSelection.tsx**: Interface for selecting audio input devices
   - **Summary.tsx**: Displays generated summaries
   - **Transcript.tsx**: Displays transcribed text

2. **App Logic** (`/frontend/src/`)
   - **App.tsx**: Main application component
   - **types.ts**: TypeScript interface definitions
   - **FileUpload.tsx**: Component for uploading audio files

3. **Hooks** (`/frontend/src/hooks/`)
   - **useDevices.ts**: Custom hook for audio device management
   - **useSession.ts**: Custom hook for session management
   - **useWebSocket.ts**: Custom hook for WebSocket communication

4. **Utilities** (`/frontend/src/utils/`)
   - **formatters.ts**: Formatting utilities
   - **markdown.ts**: Markdown processing utilities

## Data Flow

1. **Recording Flow**
   - User selects audio devices and starts recording
   - Backend captures audio from selected devices
   - Audio is saved to a file for processing

2. **Transcription Flow**
   - Audio file is processed by the transcriber
   - Transcribed segments are sent to the frontend via WebSocket
   - Transcript is saved to a file

3. **Summarization Flow**
   - Transcript is processed by the summarizer
   - Summary is sent to the frontend via WebSocket
   - Transcript and summary are saved together

4. **Session Management**
   - Sessions are created, loaded, and managed by the backend
   - Session state is persisted to disk for recovery
   - Frontend displays and interacts with session data

## Technologies

- **Backend**: Python, FastAPI, WebSockets
- **Frontend**: TypeScript, React, Tailwind CSS
- **AI/ML**: Speech-to-text models, Large Language Models for summarization
- **Storage**: File-based storage for sessions, recordings, and transcripts

## API Communication

- **REST API**: For session management, device discovery, and file operations
- **WebSockets**: For real-time updates during recording and processing
- **File Upload**: For processing existing audio files

## Extensibility

The architecture is designed to be extensible in several ways:

1. **Models**: New transcription or summarization models can be added to the `model_service.py`
2. **Audio Sources**: Additional audio capture methods can be implemented in `capture.py`
3. **Data Processing**: New processing steps can be added to the transcription or summarization pipeline
