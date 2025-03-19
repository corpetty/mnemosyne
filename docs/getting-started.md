# Getting Started with Mnemosyne

## Overview

Mnemosyne is an application for audio recording, transcription, and summarization. This guide will help you set up and run the application on your local machine.

## Prerequisites

Before you begin, ensure you have the following installed:

- **Python 3.8+** - For the backend
- **Node.js 16+** - For the frontend
- **npm or yarn** - For package management
- **Git** - For version control

## Installation

### 1. Clone the Repository

```bash
git clone https://github.com/corpetty/mnemosyne.git
cd mnemosyne
```

### 2. Backend Setup

First, set up a Python virtual environment and install the backend dependencies:

```bash
# Create a virtual environment
python -m venv venv

# Activate the virtual environment
# On Windows:
venv\Scripts\activate
# On macOS/Linux:
source venv/bin/activate

# Install backend dependencies
cd backend
pip install -r requirements.txt
cd ..
```

### 3. Frontend Setup

Install the frontend dependencies:

```bash
cd frontend
npm install
# or if you're using yarn:
# yarn install
cd ..
```

## Configuration

### Backend Configuration

By default, the backend looks for configuration in environment variables or a `.env` file in the project root. Create a `.env` file with the following settings:

```
# Server settings
PORT=8000
HOST=0.0.0.0

# Audio settings
AUDIO_SAMPLE_RATE=44100
AUDIO_CHANNELS=1

# LLM settings
LLM_PROVIDER=ollama
LLM_MODEL=mistral
LLM_API_KEY=your_api_key_if_needed
```

### Frontend Configuration

The frontend configuration is handled through environment variables. Create a `.env` file in the `frontend` directory:

```
REACT_APP_API_URL=http://localhost:8000
REACT_APP_WS_URL=ws://localhost:8000/ws
```

## Running the Application

### Start the Backend

```bash
# Ensure you're in the project root and the virtual environment is activated
cd backend
python -m src.api.main
```

The backend will start on http://localhost:8000.

### Start the Frontend

```bash
# In a separate terminal
cd frontend
npm start
# or if you're using yarn:
# yarn start
```

The frontend will start on http://localhost:3000 and should automatically open in your browser.

## Verifying the Installation

1. Open your browser and navigate to http://localhost:3000
2. You should see the Mnemosyne application interface
3. Click on "New Session" to create a recording session
4. Select audio devices and test recording functionality

## Troubleshooting

### Common Issues

#### Backend Won't Start

- Ensure Python 3.8+ is installed and in your PATH
- Check that all dependencies are installed: `pip install -r requirements.txt`
- Verify port 8000 is not in use by another application

#### Frontend Won't Start

- Ensure Node.js 16+ is installed and in your PATH
- Check that all dependencies are installed: `npm install`
- Verify port 3000 is not in use by another application

#### Audio Devices Not Detected

- Ensure your audio devices are connected and working
- Check system permissions for microphone access
- Try refreshing the device list in the application

#### Transcription or Summarization Errors

- Verify that the LLM configuration is correct
- If using an external API, check that the API key is valid
- Ensure the audio file format is supported

### Getting Help

If you encounter issues not covered here:

1. Check the logs for error messages
   - Backend logs are in the terminal where you ran the backend
   - Frontend logs are in the browser console (F12 or Cmd+Option+I)
2. Open an issue on the GitHub repository with:
   - A description of the problem
   - Steps to reproduce
   - Relevant logs
   - Your system information

## Next Steps

Now that you have Mnemosyne up and running, check out the [Usage Examples](usage-examples.md) to learn how to use the application effectively.
