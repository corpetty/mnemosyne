# Mnemosyne Usage Examples

This document provides practical examples of how to use Mnemosyne for different scenarios.

## Basic Recording Session

### Creating a New Session

1. Launch the application by navigating to http://localhost:3000 in your browser
2. Click the "New Session" button in the top navigation bar
3. A new session will be created and appear in the sessions list

### Recording Audio

1. Select one or more audio input devices from the device list
2. Click the "Start Recording" button
3. Speak or play audio through the selected devices
4. Click "Stop Recording" when finished
5. The system will automatically process the audio, transcribe it, and generate a summary

### Reviewing the Results

1. The transcript will appear in the main panel, showing each speaker's words with timestamps
2. The summary will appear in the summary panel, highlighting key points from the conversation
3. Navigate through the transcript using the scroll bar or search function
4. The transcript and summary are automatically saved and can be accessed later

## Advanced Features

### Managing Participants

1. After transcription is complete, click "Extract Participants" to automatically identify speakers
2. Click the edit icon next to a participant name to provide a more descriptive name
3. Click "Save" to update the participant information
4. The transcript view will now show the updated participant names

### Re-summarizing with Different Models

1. Open an existing session from the sessions list
2. In the summary panel, select a different model from the dropdown
3. Click "Re-summarize" to generate a new summary using the selected model
4. Compare the results from different models to find the best fit for your content

### Uploading External Audio Files

1. Click "Upload File" in the top navigation bar
2. Select an audio file from your computer (supported formats: WAV, MP3, M4A, FLAC, OPUS)
3. The system will process the file, transcribe it, and generate a summary
4. The results will appear in a new session
5. Note: Recordings are automatically converted to OPUS format for storage efficiency

## Use Case: Meeting Transcription

### Preparing for a Meeting

1. Create a new session before the meeting starts
2. Select the appropriate audio devices:
   - For in-person meetings: Select the room microphone
   - For online meetings: Select system audio capture
3. Test the audio levels to ensure clear recording

### During the Meeting

1. Click "Start Recording" when the meeting begins
2. The recording will continue for the duration of the meeting
3. You can see the recording status at the top of the screen

### After the Meeting

1. Click "Stop Recording" when the meeting ends
2. Wait for processing to complete (progress is shown in the status area)
3. Review the transcript and summary
4. Rename participants to match their actual names
5. Save or export the results as needed

## Use Case: Lecture Transcription

1. Create a new session before the lecture
2. Select the appropriate audio device capturing the lecturer's voice
3. Start recording when the lecture begins
4. Stop recording when the lecture ends
5. The transcript and summary provide a detailed record of the lecture content
6. Use the search function to find specific topics mentioned

## Use Case: Interview Processing

1. Upload a recorded interview audio file
2. Once processed, extract and rename participants (interviewer and interviewee)
3. Review the transcript for accuracy
4. Use the summary to quickly identify key points discussed
5. Search for specific terms or topics within the transcript

## Working with Saved Sessions

### Finding Past Sessions

1. All sessions are automatically saved
2. Use the sessions list to browse past recordings
3. Sessions are sorted by creation date (newest first)
4. Search for specific sessions using the search bar

### Renaming Sessions

1. Hover over a session in the sessions list
2. Click the "Rename" icon
3. Enter a descriptive name for the session
4. Click "Save" or press Enter

### Deleting Sessions

1. Hover over a session in the sessions list
2. Click the "Delete" icon
3. Confirm the deletion in the dialog
4. The session and associated files will be permanently removed

## Tips and Best Practices

### For Better Audio Quality

- Use high-quality microphones when possible
- Position microphones close to speakers
- Minimize background noise during recording
- Test audio levels before important recordings

### For Better Transcription Results

- Speak clearly and at a moderate pace
- Avoid multiple people speaking simultaneously
- Introduce participants at the beginning of a session
- Provide context for technical terms or jargon

### For Better Summaries

- Choose the appropriate model for your content type
- Longer, more detailed discussions benefit from more powerful models
- Technical content may require specialized models
- Re-summarize with different models to compare results

### Resource Management

- Release resources when you're done with a session
- Close the application when not in use
- For very long recordings, consider splitting into multiple sessions
- Monitor system resource usage for optimal performance
