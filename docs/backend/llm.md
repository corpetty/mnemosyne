# LLM Integration Documentation

## Overview

The LLM (Large Language Model) integration module provides functionality for summarizing transcribed text and extracting key information. It leverages state-of-the-art language models to generate concise, coherent summaries of conversations.

## Summarizer (`summarizer.py`)

The Summarizer class processes transcripts and generates summaries using large language models.

### Key Features

- Configurable summarization models
- Context-aware summarization
- Multi-turn conversation handling
- Key point extraction
- Configurable output formats

### Methods

| Method | Description |
|--------|-------------|
| `summarize_transcript(transcript, model=None)` | Generate a summary from a transcript |
| `parse_transcript_file(file_path)` | Parse a transcript file and extract segments |
| `get_available_models()` | Get a list of available summarization models |
| `switch_model(model_name)` | Switch to a different summarization model |

### Summarization Process

1. **Preprocessing**: The transcript is preprocessed to normalize text, handle speaker turns, and organize the content.
2. **Chunking**: Long transcripts are broken into smaller chunks to handle context windows.
3. **Summarization**: Each chunk is summarized using the selected language model.
4. **Integration**: Individual summaries are combined into a coherent overall summary.
5. **Refinement**: The summary is refined to ensure clarity, coherence, and adherence to length constraints.

### Model Selection

The Summarizer supports multiple language models, which can be selected based on the specific requirements:

| Model | Features | Use Case |
|-------|----------|----------|
| Basic | Fast, lightweight | Short, simple conversations |
| Standard | Balanced performance | General-purpose summarization |
| Advanced | Highest quality, more resource-intensive | Complex, technical discussions |

### Summary Format

The generated summary typically includes:

1. **Overview**: A high-level summary of the entire conversation
2. **Key Points**: Main topics and takeaways
3. **Action Items**: Tasks, decisions, or follow-ups identified
4. **Timeline**: Optional chronological ordering of key events

### Usage Example

```python
from llm.summarizer import Summarizer

# Create a summarizer instance
summarizer = Summarizer()

# Option 1: Summarize a list of transcript segments
transcript = [
    {"speaker": "Speaker 1", "text": "Welcome to our meeting about the project timeline."},
    {"speaker": "Speaker 2", "text": "I think we need to extend the deadline by two weeks."},
    {"speaker": "Speaker 1", "text": "That sounds reasonable given the recent challenges."}
]
summary = summarizer.summarize_transcript(transcript)

# Option 2: Summarize from a transcript file
transcript_file = "transcripts/meeting_20250315.md"
parsed_transcript = summarizer.parse_transcript_file(transcript_file)
summary = summarizer.summarize_transcript(parsed_transcript)

print(summary)
```

## Model Management

The summarizer module works closely with the Model Service to efficiently manage model resources:

- Models are loaded on demand to minimize resource usage
- Model switching is handled gracefully
- Resource cleanup happens automatically when models are no longer needed

## Configuration

The LLM integration can be configured through environment variables or configuration files:

- **LLM_DEFAULT_MODEL**: Default model to use for summarization
- **LLM_SUMMARY_MAX_LENGTH**: Maximum length for generated summaries
- **LLM_API_KEY**: API key for external LLM services (if applicable)
- **LLM_TEMPERATURE**: Controls randomness in summary generation (0.0-1.0)

## Error Handling

The LLM module includes comprehensive error handling:

- Model loading failures
- Processing errors
- Context length limitations
- API rate limiting (for external services)

Errors are logged with descriptive messages and appropriate fallback strategies are employed when possible.

## Performance Considerations

- LLM processing can be computationally intensive
- Consider implementing caching for repeated summarizations
- For external API-based models, be aware of rate limits and costs
- CPU vs. GPU acceleration can significantly impact performance

## Integration with Other Components

The summarizer integrates with:

1. **Session Management**: Sessions track which model was used for summarization
2. **Transcription**: Processes the output of the AudioTranscriber
3. **Model Service**: For efficient model resource management
4. **API Layer**: Exposes summarization functionality through the REST API

## Future Enhancements

Potential future enhancements for the LLM module include:

1. **Multi-language support**: Summarization in different languages
2. **Sentiment analysis**: Detecting emotions and tone in the transcript
3. **Personalized summaries**: Tailoring summaries to user preferences
4. **Hierarchical summarization**: Multiple summary levels (brief/detailed)
