# Frontend Hooks Documentation

## Overview

Custom React hooks in Mnemosyne provide reusable logic and state management for the application. These hooks encapsulate complex behaviors and API interactions, making components cleaner and more focused on presentation.

## Core Hooks

### useDevices (`useDevices.ts`)

The useDevices hook manages the discovery and selection of audio input devices.

#### Features

- Fetches available audio devices from the backend
- Handles device selection for recording
- Provides device refresh functionality
- Tracks device connection/disconnection events
- Caches device preferences

#### API

```typescript
const {
  devices,
  loading,
  error,
  selectedDevices,
  setSelectedDevices,
  refreshDevices
} = useDevices();
```

| Return Value | Type | Description |
|--------------|------|-------------|
| `devices` | `AudioDevice[]` | Array of available audio devices |
| `loading` | `boolean` | Loading state while fetching devices |
| `error` | `string \| null` | Error message if device fetching fails |
| `selectedDevices` | `string[]` | Array of selected device IDs |
| `setSelectedDevices` | `(deviceIds: string[]) => void` | Function to update selected devices |
| `refreshDevices` | `() => Promise<void>` | Function to refresh the device list |

#### Usage Example

```tsx
function DeviceSelector() {
  const { 
    devices, 
    loading, 
    error, 
    selectedDevices, 
    setSelectedDevices, 
    refreshDevices 
  } = useDevices();

  if (loading) return <p>Loading devices...</p>;
  if (error) return <p>Error: {error}</p>;

  return (
    <div>
      <button onClick={refreshDevices}>Refresh Devices</button>
      <ul>
        {devices.map(device => (
          <li key={device.id}>
            <input
              type="checkbox"
              checked={selectedDevices.includes(device.id.toString())}
              onChange={() => {
                const newSelected = selectedDevices.includes(device.id.toString())
                  ? selectedDevices.filter(id => id !== device.id.toString())
                  : [...selectedDevices, device.id.toString()];
                setSelectedDevices(newSelected);
              }}
            />
            {device.name}
          </li>
        ))}
      </ul>
    </div>
  );
}
```

### useSession (`useSession.ts`)

The useSession hook provides comprehensive session management for recording, transcription, and summarization.

#### Features

- Creates, loads, and manages sessions
- Handles session recordings
- Tracks session status and metadata
- Manages transcript and summary data
- Provides participant management

#### API

```typescript
const {
  sessions,
  currentSession,
  loading,
  error,
  createSession,
  loadSession,
  deleteSession,
  startRecording,
  stopRecording,
  isRecording,
  renameSession,
  updateParticipant,
  extractParticipants
} = useSession();
```

| Return Value | Type | Description |
|--------------|------|-------------|
| `sessions` | `Session[]` | Array of available sessions |
| `currentSession` | `Session \| null` | Currently selected session |
| `loading` | `boolean` | Loading state for session operations |
| `error` | `string \| null` | Error message if operations fail |
| `createSession` | `() => Promise<Session>` | Function to create a new session |
| `loadSession` | `(sessionId: string) => Promise<void>` | Function to load a session by ID |
| `deleteSession` | `(sessionId: string) => Promise<void>` | Function to delete a session |
| `startRecording` | `(deviceIds: string[], model?: string) => Promise<void>` | Function to start recording |
| `stopRecording` | `() => Promise<void>` | Function to stop recording |
| `isRecording` | `boolean` | Whether a recording is in progress |
| `renameSession` | `(sessionId: string, name: string) => Promise<void>` | Function to rename a session |
| `updateParticipant` | `(participantId: string, name: string) => Promise<void>` | Function to update a participant |
| `extractParticipants` | `() => Promise<void>` | Function to extract participants from transcript |

#### Usage Example

```tsx
function SessionManager() {
  const { 
    sessions, 
    currentSession, 
    loading, 
    createSession, 
    loadSession, 
    deleteSession,
    startRecording,
    stopRecording,
    isRecording
  } = useSession();
  const { selectedDevices } = useDevices();

  if (loading) return <p>Loading sessions...</p>;

  return (
    <div>
      <button onClick={createSession}>New Session</button>
      
      <ul>
        {sessions.map(session => (
          <li key={session.session_id}>
            <button onClick={() => loadSession(session.session_id)}>
              {session.name || session.session_id}
            </button>
            <button onClick={() => deleteSession(session.session_id)}>Delete</button>
          </li>
        ))}
      </ul>
      
      {currentSession && (
        <div>
          <h2>Current Session: {currentSession.name || currentSession.session_id}</h2>
          {isRecording ? (
            <button onClick={stopRecording}>Stop Recording</button>
          ) : (
            <button 
              onClick={() => startRecording(selectedDevices)}
              disabled={selectedDevices.length === 0}
            >
              Start Recording
            </button>
          )}
        </div>
      )}
    </div>
  );
}
```

### useWebSocket (`useWebSocket.ts`)

The useWebSocket hook provides real-time communication with the backend through WebSockets.

#### Features

- Manages WebSocket connection lifecycle
- Handles connection state and reconnection
- Provides message sending and receiving
- Offers session-specific subscriptions
- Processes real-time transcription and summary updates

#### API

```typescript
const {
  connected,
  error,
  subscribe,
  unsubscribe,
  sendMessage,
  lastMessage,
  transcriptUpdates,
  summaryUpdates,
  statusUpdates
} = useWebSocket();
```

| Return Value | Type | Description |
|--------------|------|-------------|
| `connected` | `boolean` | Whether the WebSocket is connected |
| `error` | `string \| null` | Error message if connection fails |
| `subscribe` | `(sessionId: string) => void` | Function to subscribe to a session |
| `unsubscribe` | `() => void` | Function to unsubscribe from current session |
| `sendMessage` | `(message: any) => void` | Function to send a message |
| `lastMessage` | `any` | Last message received |
| `transcriptUpdates` | `TranscriptSegment[]` | Array of transcript segments received |
| `summaryUpdates` | `string` | Latest summary update |
| `statusUpdates` | `string[]` | Array of status messages |

#### Usage Example

```tsx
function TranscriptionViewer({ sessionId }) {
  const { 
    connected, 
    error, 
    subscribe, 
    unsubscribe, 
    transcriptUpdates, 
    summaryUpdates,
    statusUpdates
  } = useWebSocket();
  
  // Subscribe to the session when component mounts
  useEffect(() => {
    if (sessionId) {
      subscribe(sessionId);
    }
    
    // Unsubscribe when component unmounts or sessionId changes
    return () => {
      unsubscribe();
    };
  }, [sessionId, subscribe, unsubscribe]);
  
  if (!connected) return <p>Connecting to server...</p>;
  if (error) return <p>Connection error: {error}</p>;
  
  return (
    <div>
      <div className="status">
        {statusUpdates.map((status, i) => (
          <p key={i}>{status}</p>
        ))}
      </div>
      
      <div className="transcript">
        {transcriptUpdates.map((segment, i) => (
          <div key={i}>
            <strong>{segment.speaker}:</strong> {segment.text}
          </div>
        ))}
      </div>
      
      {summaryUpdates && (
        <div className="summary">
          <h3>Summary</h3>
          <p>{summaryUpdates}</p>
        </div>
      )}
    </div>
  );
}
```

## Helper Hooks

### useLocalStorage

A utility hook for persistent state storage in the browser's localStorage.

```typescript
const [value, setValue] = useLocalStorage<T>(key: string, initialValue: T);
```

### useDebounce

A utility hook that debounces a value, useful for delaying API calls or expensive operations.

```typescript
const debouncedValue = useDebounce<T>(value: T, delay: number);
```

### useAsyncEffect

A utility hook for safely handling asynchronous operations in effects.

```typescript
useAsyncEffect(effect: () => Promise<void | (() => void)>, deps?: any[]);
```

## Hook Integration

The hooks are designed to work together seamlessly:

- **useDevices** provides device information for **useSession** when starting recordings
- **useSession** uses **useWebSocket** for real-time updates during recording and processing
- **useWebSocket** provides transcript and summary data that components can render

## Error Handling

Each hook implements comprehensive error handling:

- Connection errors in WebSockets
- API request failures
- Device access permission issues
- Session state management errors

Errors are propagated to the components through error states, allowing appropriate UI feedback.

## Performance Considerations

The hooks are optimized for performance in several ways:

- Memoized values and callbacks using `useMemo` and `useCallback`
- Debounced operations for expensive or frequent updates
- Cleanup functions to prevent memory leaks
- Selective re-rendering based on relevant state changes
