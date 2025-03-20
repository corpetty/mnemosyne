# Frontend Components Documentation

## Overview

The frontend of Mnemosyne is built with React and TypeScript, using a component-based architecture. This document provides an overview of the key components that make up the user interface.

## Core Components

### SessionList (`SessionList.tsx`)

The SessionList component displays a list of available recording sessions and allows users to manage them.

#### Features

- Displays session cards with metadata (creation time, status, name)
- Allows selecting a session to view its details
- Provides options for renaming and deleting sessions
- Shows recording status indicators
- Handles sorting and filtering of sessions

#### Props

```typescript
interface SessionListProps {
  sessions: Session[];
  activeSessionId: string | null;
  onSessionSelect: (sessionId: string) => void;
  onCreateSession: () => void;
  onDeleteSession: (sessionId: string) => void;
  isLoading: boolean;
  updateSession?: (session: Session) => void;
}
```

#### Usage Example

```tsx
<SessionList
  sessions={sessions}
  activeSessionId={activeSessionId}
  onSessionSelect={handleSessionSelect}
  onCreateSession={handleCreateSession}
  onDeleteSession={handleDeleteSession}
  isLoading={isSessionsLoading}
  updateSession={updateSession}
/>
```

### ParticipantList (`ParticipantList.tsx`)

The ParticipantList component manages the participants in a recording session, allowing users to view and edit participant information.

#### Features

- Displays a list of session participants
- Allows renaming participants for better readability
- Shows participant identifiers
- Handles automatic participant extraction from transcripts
- Provides a form for manual participant addition

#### Props

```typescript
interface ParticipantListProps {
  participants: Participant[];
  sessionId: string;
  onUpdateParticipant: (participantId: string, name: string) => void;
  onExtractParticipants: () => void;
}
```

#### Usage Example

```tsx
<ParticipantList
  participants={sessionParticipants}
  sessionId={currentSession.session_id}
  onUpdateParticipant={handleUpdateParticipant}
  onExtractParticipants={handleExtractParticipants}
/>
```

### DeviceSelection (`DeviceSelection.tsx`)

The DeviceSelection component provides an interface for selecting audio input devices for recording.

#### Features

- Lists available audio input devices
- Allows selecting multiple devices for recording
- Shows device details (channels, default status)
- Provides device refresh functionality
- Indicates which devices are currently in use

#### Props

```typescript
interface DeviceSelectionProps {
  devices: AudioDevice[];
  selectedDevices: string[];
  onDeviceSelect: (deviceIds: string[]) => void;
  onRefreshDevices: () => void;
  disabled?: boolean;
}
```

#### Usage Example

```tsx
<DeviceSelection
  devices={audioDevices}
  selectedDevices={selectedDeviceIds}
  onDeviceSelect={handleDeviceSelect}
  onRefreshDevices={handleRefreshDevices}
  disabled={isRecording}
/>
```

### Summary (`Summary.tsx`)

The Summary component displays the generated summary of a transcribed recording.

#### Features

- Renders markdown-formatted summary text
- Provides options for copying summary to clipboard
- Offers re-summarization with different models
- Shows model information
- Includes loading and error states

#### Props

```typescript
interface SummaryProps {
  summary: string;
  loading: boolean;
  error?: string;
  modelId?: string;
  availableModels: Model[];
  onResummarize: (modelId: string) => void;
}
```

#### Usage Example

```tsx
<Summary
  summary={currentSession.summary}
  loading={summaryLoading}
  modelId={currentSession.model}
  availableModels={models}
  onResummarize={handleResummarize}
/>
```

### Transcript (`Transcript.tsx`)

The Transcript component displays the detailed transcript of a recording with speaker information and timestamps.

#### Features

- Shows transcript segments with speaker identification
- Displays timestamps for each segment
- Handles real-time updates during transcription
- Provides search and filtering capabilities
- Offers transcript export options

#### Props

```typescript
interface TranscriptProps {
  transcript: TranscriptSegment[];
  loading: boolean;
  error?: string;
  participants?: Participant[];
}
```

#### Usage Example

```tsx
<Transcript
  transcript={currentSession.transcript}
  loading={transcriptLoading}
  participants={currentSession.participants}
/>
```

## Supporting Components

### FileUpload (`FileUpload.tsx`)

The FileUpload component provides an interface for uploading audio files for processing.

#### Features

- Drag-and-drop file upload
- File format validation
- Upload progress indication
- Error handling and reporting
- Integration with session management

#### Props

```typescript
interface FileUploadProps {
  onFileSelected: (file: File) => void;
  onCancel: () => void;
  uploading: boolean;
  progress?: number;
  error?: string;
}
```

## Component Hierarchy

The components are organized in the following hierarchy:

```
App
├── SessionList
├── DeviceSelection
├── FileUpload (conditionally displayed)
├── Transcript
├── Summary
└── ParticipantList
```

## Component Communication

Components communicate through:

1. **Props**: Parent components pass data and callbacks to child components
2. **Custom Hooks**: Shared state management across components
3. **Context API**: For state that needs to be accessible to many components

## Styling

Components are styled using:

- **Tailwind CSS**: For responsive layout and utility classes
- **CSS Modules**: For component-specific styles
- **Global Styles**: For application-wide styling and theme

## Error Handling

Components implement error handling through:

- Loading states with appropriate UI indicators
- Error states with user-friendly messages
- Fallback UI when data is unavailable
- Retry mechanisms for failed operations

## Accessibility

Components prioritize accessibility through:

- Semantic HTML elements
- ARIA attributes for custom interactions
- Keyboard navigation support
- Color contrast compliance
- Screen reader-friendly content

## Future Enhancements

Planned enhancements for the components include:

1. **Dark Mode**: Theme toggling capability
2. **Localization**: Support for multiple languages
3. **Customizable Views**: User preferences for component display
4. **Mobile Optimization**: Improved responsive layouts for mobile devices
