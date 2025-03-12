import os
import re
from typing import Dict, List, Optional, Tuple, Literal
import requests
import json
from dotenv import load_dotenv

load_dotenv()

# Prompt templates for different detail levels
DETAILED_PROMPT_TEMPLATE = """You are an expert meeting summarizer tasked with creating a detailed, comprehensive, and highly structured analysis of the following conversation transcript.

TRANSCRIPT:
{transcript}

INSTRUCTIONS:
1. Create a thorough and detailed summary organized into these clearly labeled sections:
   - OVERVIEW: A comprehensive 5-7 sentence summary of the conversation that captures all key themes, context, purpose, and outcomes
   - PARTICIPANTS & DYNAMICS: Identify all speakers and their roles, analyze communication dynamics, note any significant interactions or patterns
   - KEY TOPICS: For each significant topic discussed:
      * Provide a detailed explanation with context and importance
      * Include sub-topics and their relationships
      * Capture nuanced perspectives from different speakers
      * Include relevant numbers, metrics, or technical details mentioned
   - DECISIONS & CONCLUSIONS: Thoroughly document all decisions with:
      * Complete context around how the decision was reached
      * Any disagreements or alternative options discussed
      * Specific attribution to speakers with relevant quotes
      * Implications or next steps related to each decision
   - ACTION ITEMS: Comprehensive list of action items including:
      * Both explicit and implied tasks
      * Detailed description of each action required
      * Responsible parties (if mentioned)
      * Deadlines, priorities, or dependencies
      * Related resources or references mentioned

2. Format requirements:
   - Use markdown formatting with headers (##) for each section
   - Use bullet points for lists
   - Bold (**Speaker Name**) when attributing statements or actions to specific speakers
   - Use sub-bullets (indentation) to organize hierarchical information
   - Use quotes for important verbatim statements

3. Focus on accuracy, depth, and comprehensiveness:
   - Maintain the original meaning while providing deep analysis
   - Preserve ALL technical terms, numbers, and specific language used by speakers
   - If speakers disagree on a topic, thoroughly analyze the different perspectives
   - Note changes in opinion or progression of ideas throughout the conversation
   - Capture tone, sentiment, and emotional context when relevant"""

STANDARD_PROMPT_TEMPLATE = """You are an expert meeting summarizer tasked with creating a structured and detailed analysis of the following conversation transcript.

TRANSCRIPT:
{transcript}

INSTRUCTIONS:
1. Create a detailed summary with these labeled sections:
   - OVERVIEW: A 4-5 sentence summary capturing key themes, context, and outcomes
   - PARTICIPANTS: Identify speakers and their roles
   - KEY TOPICS: For each major topic discussed:
      * Provide context and importance
      * Include relevant technical details
      * Note different perspectives from speakers
   - DECISIONS: Document decisions with speaker attribution and context
   - ACTION ITEMS: List explicit and implied tasks with responsible parties

2. Format requirements:
   - Use markdown with ## headers for sections
   - Use bullet points and bold (**Speaker Name**) for attribution
   - Include relevant quotes when helpful

3. Focus on accuracy and completeness:
   - Preserve technical terms and specific language
   - Present multiple viewpoints when speakers disagree
   - Maintain original meaning and context"""

COMPACT_PROMPT_TEMPLATE = """Provide a concise but comprehensive summary of this conversation transcript:

TRANSCRIPT:
{transcript}

Structure your summary with these markdown headers:
## Overview
## Participants
## Key Topics
## Decisions
## Action Items

For each section, include essential information only:
- Use bullet points
- Bold speaker names (**Name**)
- Preserve all technical terms
- Include all decisions and tasks mentioned

Focus on accuracy and clarity."""

class Summarizer:
    def __init__(self, provider: str = "ollama", model: str = None):
        self.provider = provider
        self.api_key = os.getenv("OPENAI_API_KEY")
        self.ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
        
        # Default models
        self.default_ollama_model = "llama3.3"
        self.default_openai_model = "gpt-3.5-turbo"
        
        # Use provided model or default
        self.model = model or (self.default_ollama_model if provider == "ollama" else self.default_openai_model)
        
    def _estimate_token_count(self, text: str) -> int:
        """Rough estimation of token count for a given text.
        This uses a simple approximation that works for most English text."""
        # Count words
        word_count = len(re.findall(r'\b\w+\b', text))
        
        # Count non-alphanumeric characters that might be tokenized separately
        special_chars = len(re.findall(r'[^\w\s]', text))
        
        # Apply a multiplier to account for tokenization differences
        # Most tokenizers split words into subwords, resulting in more tokens than words
        return int((word_count + special_chars) * 1.3)  # Words plus 30% for tokenization
        
    def _select_prompt_template(self, transcript_text: str) -> Tuple[str, Literal["detailed", "standard", "compact"]]:
        """Select the appropriate prompt template based on estimated token count"""
        estimated_tokens = self._estimate_token_count(transcript_text)
        print(f"Estimated token count for transcript: {estimated_tokens}")
        
        if estimated_tokens < 800:  # Allow for prompt overhead
            return DETAILED_PROMPT_TEMPLATE, "detailed"
        elif estimated_tokens < 1500:  # Medium length
            return STANDARD_PROMPT_TEMPLATE, "standard"
        else:  # Long transcript
            return COMPACT_PROMPT_TEMPLATE, "compact"

    def get_available_models(self) -> List[Dict]:
        """Fetch available models based on the provider"""
        models = []
        
        # Add Ollama models
        try:
            ollama_response = requests.get(f"{self.ollama_url}/api/tags", timeout=5)
            if ollama_response.status_code == 200:
                ollama_models = ollama_response.json().get("models", [])
                models.extend([{
                    "id": model["name"],
                    "name": model["name"],
                    "provider": "ollama",
                    "size": model.get("size", "Unknown")
                } for model in ollama_models])
        except Exception as e:
            print(f"Error fetching Ollama models: {e}")
            
        # Add OpenAI models if API key is configured
        if self.api_key:
            openai_models = [
                {"id": "gpt-3.5-turbo", "name": "GPT-3.5 Turbo", "provider": "openai"},
                {"id": "gpt-4", "name": "GPT-4", "provider": "openai"}
            ]
            models.extend(openai_models)
            
        return models
    
    def summarize_transcript(self, transcript: List[Dict], model: str = None) -> str:
        """Summarize the transcript using the specified LLM provider and model"""
        # Use the provided model or the default one
        model_to_use = model or self.model
        if not transcript:
            return "No transcript available to summarize."

        # Prepare the transcript text
        full_text = "\n".join([f"{segment['speaker']}: {segment['text']}" 
                              for segment in transcript])
        
        # Select appropriate prompt template based on transcript length
        prompt_template, detail_level = self._select_prompt_template(full_text)
        print(f"Using {detail_level} prompt template for summary")
        
        # Format the prompt with transcript
        prompt = prompt_template.format(transcript=full_text)

        if self.provider == "ollama":
            summary = self._summarize_with_ollama(prompt, model_to_use)
            # Check if we got a simplified fallback summary
            if "simplified fallback summary" in summary:
                print("Received fallback summary. Trying with a more compact prompt.")
                # Try a more compact prompt
                if detail_level != "compact":
                    compact_prompt = COMPACT_PROMPT_TEMPLATE.format(transcript=full_text)
                    return self._summarize_with_ollama(compact_prompt, model_to_use)
            return summary
        else:
            return self._summarize_with_openai(prompt, model_to_use)

    def _summarize_with_ollama(self, prompt: str, model: str = None) -> str:
        """Use local Ollama model for summarization"""
        model_to_use = model or self.default_ollama_model
        
        try:
            print(f"Sending request to Ollama with prompt length: {len(prompt)} characters")
            response = requests.post(
                f"{self.ollama_url}/api/generate",
                json={
                    "model": model_to_use,
                    "prompt": prompt,
                    "stream": False
                },
                timeout=60  # Increased timeout for longer processing
            )
            
            # Check for token limit errors
            if response.status_code != 200:
                error_text = response.text.lower() if hasattr(response, 'text') else ""
                if 'token' in error_text and ('limit' in error_text or 'exceed' in error_text or 'truncat' in error_text):
                    print(f"Token limit error detected: {response.text}")
                    # Try OpenAI fallback if Ollama fails due to token limits and API key is available
                    if self.api_key:
                        print("Attempting fallback to OpenAI due to token limit")
                        return self._summarize_with_openai(prompt, self.default_openai_model)
                    
                # Other errors
                print(f"Ollama error: {response.status_code} - {response.text if hasattr(response, 'text') else 'No response text'}")
                return self._generate_simple_summary(prompt)
                
            return response.json()["response"]
        except Exception as e:
            # Try OpenAI fallback if Ollama fails with an exception
            print(f"Exception in Ollama summarization: {str(e)}")
            if self.api_key:
                try:
                    return self._summarize_with_openai(prompt, self.default_openai_model)
                except Exception as openai_e:
                    print(f"OpenAI fallback also failed: {str(openai_e)}")
            return self._generate_simple_summary(prompt)
            
    def _generate_simple_summary(self, prompt: str) -> str:
        """Generate a simple summary when LLM services are unavailable"""
        # Extract the transcript content from the prompt
        transcript_text = ""
        in_transcript = False
        for line in prompt.split('\n'):
            if line.strip() == "TRANSCRIPT:":
                in_transcript = True
                continue
            elif line.strip() == "INSTRUCTIONS:" or line.strip().startswith("1. Create"):
                in_transcript = False
                break
            
            if in_transcript and line.strip():
                transcript_text += line + "\n"
        
        # Very simple extraction-based summary approach
        lines = transcript_text.strip().split('\n')
        
        # Deduplicate identical lines
        unique_lines = []
        seen = set()
        for line in lines:
            line_text = line.strip()
            if line_text and line_text not in seen:
                seen.add(line_text)
                unique_lines.append(line)
        
        # Get speaker names
        speakers = set()
        for line in unique_lines:
            if ':' in line:
                speaker = line.split(':', 1)[0].strip()
                speakers.add(speaker)
        
        # Select a sample of unique content (first, middle, last)
        sample_size = min(3, len(unique_lines))
        samples = []
        if sample_size > 0:
            samples.append(unique_lines[0])
        if sample_size > 1:
            samples.append(unique_lines[len(unique_lines)//2])
        if sample_size > 2:
            samples.append(unique_lines[-1])
        
        # Build a more detailed markdown summary even in fallback mode
        summary = "## Overview\n"
        summary += f"This transcript contains a conversation between {len(speakers)} participant(s). "
        summary += "The discussion covers several topics as sampled below. "
        summary += "Due to limitations in the fallback summary mode, this represents only a basic extraction of content. "
        summary += "For a complete and thorough analysis, please try again when the LLM service is available. "
        summary += "This simplified summary attempts to extract key information from the beginning, middle, and end of the conversation.\n\n"
        
        summary += "## Participants & Dynamics\n"
        for speaker in speakers:
            summary += f"- **{speaker}**: Participated in the conversation\n"
        summary += "- Interaction dynamics could not be analyzed in fallback mode\n\n"
        
        summary += "## Key Topics\n"
        for i, sample in enumerate(samples):
            if ':' in sample:
                speaker, content = sample.split(':', 1)
                content = content.strip()
                if content:
                    # Extract a topic-like phrase (first 15-20 words for more detail)
                    words = content.split()
                    topic = ' '.join(words[:min(20, len(words))])
                    position = "beginning" if i == 0 else "middle" if i == 1 else "end"
                    summary += f"- Discussion at {position} of transcript: {topic}...\n"
                    summary += f"  - Mentioned by **{speaker.strip()}**\n"
                    
                    # Try to extract subtopics by looking at sentence structure
                    sentences = content.split('.')
                    if len(sentences) > 1:
                        for j, sentence in enumerate(sentences[:2]):  # Get first two sentences as subtopics
                            if sentence.strip():
                                subtopic = sentence.strip()
                                if len(subtopic.split()) > 3:  # Only if it's a substantial sentence
                                    summary += f"    - {subtopic}\n"
        
        summary += "\n## Decisions & Conclusions\n"
        summary += "- None could be automatically identified in fallback summary mode\n"
        summary += "- A full LLM analysis is required to accurately extract decisions and conclusions\n\n"
        
        summary += "## Action Items\n"
        summary += "- None could be automatically identified in fallback summary mode\n"
        summary += "- A full LLM analysis is required to accurately extract action items\n\n"
        
        summary += "\n---\n*Note: This is a simplified fallback summary generated because the LLM service was unavailable.*"
        
        return summary

    def _summarize_with_openai(self, prompt: str, model: str = None) -> str:
        """Use OpenAI API for summarization"""
        if not self.api_key:
            return "OpenAI API key not configured"

        model_to_use = model or self.default_openai_model

        try:
            print(f"Sending request to OpenAI with prompt length: {len(prompt)} characters")
            response = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": model_to_use,
                    "messages": [{"role": "user", "content": prompt}]
                }
            )
            if response.status_code == 200:
                return response.json()["choices"][0]["message"]["content"]
            else:
                error_message = f"OpenAI API error: {response.status_code} - {response.text}"
                print(error_message)
                return self._generate_simple_summary(prompt)
        except Exception as e:
            error_message = f"Error generating summary with OpenAI: {str(e)}"
            print(error_message)
            return self._generate_simple_summary(prompt)
            
    def parse_transcript_file(self, file_path: str) -> List[Dict]:
        """Parse a transcript file and extract the transcript segments"""
        transcript = []
        
        try:
            with open(file_path, 'r') as f:
                content = f.read()
                
            in_transcript_section = False
            current_speaker = None
            current_time = None
            current_text = ""
            
            for line in content.split('\n'):
                if line.startswith('## Transcript'):
                    in_transcript_section = True
                    continue
                elif line.startswith('## Summary'):
                    in_transcript_section = False
                    break
                    
                if in_transcript_section and line.strip():
                    # Check if this is a speaker line
                    if line.startswith('**') and '**' in line[2:]:
                        # Save previous segment if it exists
                        if current_speaker and current_text:
                            transcript.append({
                                'speaker': current_speaker,
                                'timestamp': current_time or 0,
                                'text': current_text.strip()
                            })
                            
                        # Parse new speaker line
                        speaker_part = line[2:].split('**')[0].strip()
                        time_part = None
                        
                        if '(' in line and ')' in line:
                            time_text = line.split('(')[1].split(')')[0]
                            if 's - ' in time_text:
                                # Format: "0.0s - 0.8s"
                                start, end = time_text.split('s - ')
                                start = float(start)
                                end = float(end.replace('s', ''))
                                time_part = start
                            elif ':' in time_text:
                                # Format: "0:00:00"
                                # Convert to seconds for simplicity
                                time_part = 0
                        
                        current_speaker = speaker_part
                        current_time = time_part or 0
                        current_text = ""
                    elif current_speaker:
                        # This is text content for the current speaker
                        current_text += line + " "
            
            # Add the final segment
            if current_speaker and current_text:
                transcript.append({
                    'speaker': current_speaker,
                    'timestamp': current_time or 0,
                    'text': current_text.strip()
                })
                
            return transcript
        except Exception as e:
            print(f"Error parsing transcript file: {e}")
            return []
