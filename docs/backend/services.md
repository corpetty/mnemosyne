# Services Documentation

## Overview

The services module provides singleton services that manage shared resources throughout the application. These services ensure efficient resource usage, prevent duplication, and handle the lifecycle of resource-intensive components.

## ModelService (`model_service.py`)

The ModelService is a singleton that manages AI model instances used for transcription and summarization. It ensures that these resource-intensive models are loaded only when needed and shared across the application.

### Key Features

- Singleton pattern implementation
- Lazy loading of models (on-demand initialization)
- Shared model instances across the application
- Resource management and cleanup

### Methods

| Method | Description |
|--------|-------------|
| `get_instance()` | Get or create the singleton instance of ModelService |
| `get_transcriber()` | Get or create the shared AudioTranscriber instance |
| `get_summarizer(provider, model)` | Get or create the shared Summarizer instance, with optional model selection |
| `release_resources()` | Release model resources to free memory |

### Singleton Pattern

The ModelService uses the singleton pattern to ensure only one instance exists in the application:

```python
@classmethod
def get_instance(cls) -> 'ModelService':
    """Get or create the singleton instance"""
    if cls._instance is None:
        logger.info("Creating new ModelService instance")
        cls._instance = ModelService()
    return cls._instance
```

### Lazy Loading

Models are loaded only when first requested, not when the service is initialized:

```python
def get_transcriber(self) -> AudioTranscriber:
    """Get or create the shared transcriber instance"""
    if self._transcriber is None:
        logger.info("Loading AudioTranscriber model (first use)")
        self._transcriber = AudioTranscriber()
    return self._transcriber
```

### Model Switching

The service allows switching the summarizer model if a different one is requested:

```python
def get_summarizer(self, provider: str = "ollama", model: str = None) -> Summarizer:
    """Get or create the shared summarizer instance"""
    if self._summarizer is None:
        logger.info(f"Loading Summarizer model (first use): provider={provider}, model={model}")
        self._summarizer = Summarizer(provider=provider, model=model)
    elif model is not None and self._summarizer.model != model:
        # If requesting a different model, update the current instance
        logger.info(f"Updating summarizer model: {self._summarizer.model} -> {model}")
        self._summarizer.model = model
    return self._summarizer
```

### Resource Management

The service provides a method to release resources when they're no longer needed:

```python
def release_resources(self):
    """Release model resources to free memory"""
    logger.info("Releasing model resources")
    self._transcriber = None
    self._summarizer = None
```

### Usage Example

```python
from services.model_service import ModelService

# Get the shared instance
service = ModelService.get_instance()

# Get the transcriber (loads it if not already loaded)
transcriber = service.get_transcriber()

# Use the transcriber
transcriber.process_audio_file("recording.wav")

# Get the summarizer with a specific model
summarizer = service.get_summarizer(model="gpt-3.5-turbo")

# Use the summarizer
summary = summarizer.summarize_transcript(transcript)

# When finished with all operations
service.release_resources()
```

## Integration with Application Components

The ModelService integrates with several application components:

1. **Session Management**: Sessions use the ModelService to access transcription and summarization models
2. **API Layer**: The TranscriptionManager in main.py uses ModelService for model access
3. **Application Lifecycle**: Models are initialized at startup and released at shutdown

## Performance Considerations

- The singleton pattern ensures that resource-intensive models are loaded only once
- Lazy loading prevents unnecessary resource usage until models are actually needed
- Resource release allows freeing memory when models are no longer needed
- Model sharing improves performance by eliminating redundant model loading

## Error Handling

The ModelService includes error handling for:

- Singleton violation (attempting to create multiple instances)
- Model loading failures
- Model switching issues

Errors are logged with descriptive messages to aid debugging.

## Future Enhancements

Potential enhancements for the services module:

1. **Service Registry**: A central registry for all singleton services
2. **Configuration Service**: For managing application configuration
3. **Caching Service**: For caching frequent operations
4. **Queue Service**: For handling background tasks and operations
