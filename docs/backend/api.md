# Backend API Documentation

## Overview

The backend API is built with FastAPI and provides endpoints for session management, audio recording, transcription, and summarization. It also includes WebSocket support for real-time communication.

## API Modules

### Main API (`main.py`)

The main API module defines the FastAPI application and includes the core endpoints for the application.

#### Key Components

- **TranscriptionManager**: Central manager class that handles sessions, recordings, and processing
- **FastAPI Application**: Defines routes and middleware
- **WebSocket Endpoint**: Handles real-time communication

#### Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/sessions` | Create a new transcription session |
| GET | `/sessions` | List all available sessions |
| GET | `/sessions/{session_id}` | Get a specific session by ID |
| DELETE | `/sessions/{session_id}` | Delete a session by ID |
| POST | `/sessions/{session_id}/rename` | Rename a session |
| POST | `/sessions/{session_id}/participants/extract` | Extract participants from a session's transcript |
| PUT | `/sessions/{session_id}/participants/{participant_id}` | Update a participant's information |
| GET | `/devices` | Get available audio devices |
| GET | `/ws` | WebSocket endpoint for real-time communication |
| POST | `/upload` | Upload an audio file for transcription and summarization |
| GET | `/models` | Get available models for summarization |
| GET | `/transcripts` | Get available transcript files |
| POST | `/resummarize` | Re-summarize a transcript file using a specified model |
| POST | `/start` | Start recording audio |
| POST | `/stop` | Stop recording audio |

### Session Management (`session.py`)

The Session module provides functionality for managing recording and transcription sessions.

#### Key Components

- **Session Class**: Represents a single recording/transcription session
- **Session Storage**: Methods for saving and loading sessions from disk
- **Session Processing**: Methods for processing audio files and generating transcripts and summaries

#### Methods

| Method | Description |
|--------|-------------|
| `start_recording` | Start a recording with the specified devices |
| `stop_recording` | Stop the current recording |
| `process_audio_file` | Process an audio file for this session |
| `generate_summary` | Generate summary from transcript |
| `save_transcript` | Save transcript to markdown file |
| `save_to_disk` | Save the session to disk |
| `load_from_disk` | Load a session from disk |
| `extract_participants` | Extract unique participants from the transcript |
| `update_participant` | Update a participant's information |

### Devices API (`devices.py`)

The devices API provides endpoints for discovering and managing audio devices.

#### Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/devices` | Get all available audio input devices |

## Data Models

### Session Model

```python
{
    "session_id": str,  # Unique identifier for the session
    "created_at": str,  # ISO 8601 timestamp when the session was created
    "status": str,      # Current status of the session (created, recording, processing, completed, error)
    "model": str,       # The model used for transcription/summarization
    "device_ids": List[str],  # IDs of audio devices used for recording
    "recording_file": str,    # Path to the recording file
    "transcript_file": str,   # Path to the transcript file
    "is_recording": bool,     # Whether the session is currently recording
    "summary_length": int,    # Length of the summary in characters
    "transcript_length": int, # Number of transcript segments
    "name": str,              # Custom name for the session
    "participants": List[{    # List of participants in the session
        "id": str,            # Unique identifier for the participant
        "name": str           # Human-readable name of the participant
    }]
}
```

### Transcript Segment Model

```python
{
    "text": str,        # The transcribed text
    "timestamp": float, # Unix timestamp of the segment
    "speaker": str,     # Speaker identifier (e.g., "Speaker 1")
    "start": float,     # Start time of the segment in seconds
    "end": float        # End time of the segment in seconds
}
```

## WebSocket Communication

The WebSocket endpoint provides real-time communication between the frontend and backend. It supports the following message types:

### Client to Server

| Message Type | Description | Parameters |
|--------------|-------------|------------|
| `subscribe` | Subscribe to a specific session | `session_id`: ID of the session to subscribe to |
| `unsubscribe` | Unsubscribe from the current session | None |

### Server to Client

| Message Type | Description | Data |
|--------------|-------------|------|
| `transcription` | New transcription segment | Transcript segment object |
| `summary` | Generated summary | Summary text |
| `status` | Status update | Status message and session ID |
| `error` | Error message | Error details |
| `ping` | Keep-alive message | None |
| `subscribed` | Confirmation of subscription | Session ID |
| `unsubscribed` | Confirmation of unsubscription | Session ID |

## Error Handling

API errors are returned as HTTP error responses with appropriate status codes:

- **400**: Bad Request - Invalid input parameters
- **404**: Not Found - Resource not found (e.g., session, participant)
- **500**: Internal Server Error - Server-side error

Error responses include a `detail` field with a descriptive error message.

## Authentication and Security

Currently, the API does not implement authentication. For production environments, it is recommended to add authentication and authorization mechanisms.

The CORS middleware is configured to allow requests from any origin, which is suitable for development but should be restricted in production.
