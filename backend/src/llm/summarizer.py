import os
from typing import Dict, List, Optional, Tuple
import requests
import json
from dotenv import load_dotenv

load_dotenv()

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
        
        # Calculate transcript length to determine summary depth
        word_count = len(full_text.split())
        detail_level = "detailed" if word_count < 1000 else "concise"
        
        prompt = f"""You are an expert meeting summarizer tasked with creating a clear, structured, and informative summary of the following conversation transcript.

TRANSCRIPT:
{full_text}

INSTRUCTIONS:
1. Create a {detail_level} summary organized into these clearly labeled sections:
   - OVERVIEW: A 2-3 sentence high-level summary of the conversation
   - KEY TOPICS: List the main topics discussed with bullet points for each topic
   - DECISIONS & CONCLUSIONS: Note any decisions made or conclusions reached, with speaker attribution
   - ACTION ITEMS: List specific tasks, responsibilities, deadlines mentioned (if any)

2. Format requirements:
   - Use markdown formatting with headers (##) for each section
   - Use bullet points for lists
   - Bold (**Speaker Name**) when attributing statements or actions to specific speakers
   - Keep the summary focused and relevant, avoiding unnecessary details

3. Focus on accuracy and objectivity:
   - Maintain the original meaning without adding your own interpretations
   - Preserve important technical terms and language used by speakers
   - If speakers disagree on a topic, clearly present both viewpoints with proper attribution

4. Use this exact structure for your response:

## Overview
[2-3 sentence summary]

## Key Topics
- [Topic 1]
  - [Important point]
  - [Important point]
- [Topic 2]
  - [Important point]
  - [Important point]

## Decisions & Conclusions
- [Decision 1] (by **Speaker Name**)
- [Decision 2] (agreed by **Speaker A** and **Speaker B**)

## Action Items
- **Speaker Name** will [action] by [deadline if mentioned]
- [Action item] (owner not specified)

If any section would be empty, include the heading but state "None identified in the transcript."
"""

        if self.provider == "ollama":
            return self._summarize_with_ollama(prompt, model_to_use)
        else:
            return self._summarize_with_openai(prompt, model_to_use)

    def _summarize_with_ollama(self, prompt: str, model: str = None) -> str:
        """Use local Ollama model for summarization"""
        model_to_use = model or self.default_ollama_model
        
        try:
            response = requests.post(
                f"{self.ollama_url}/api/generate",
                json={
                    "model": model_to_use,
                    "prompt": prompt,
                    "stream": False
                },
                timeout=30  # Add timeout to prevent hanging
            )
            
            if response.status_code != 200:
                # Try OpenAI fallback if Ollama fails
                if self.api_key:
                    return self._summarize_with_openai(prompt, self.default_openai_model)
                return self._generate_simple_summary(prompt)
                
            return response.json()["response"]
        except Exception as e:
            # Try OpenAI fallback if Ollama fails with an exception
            if self.api_key:
                try:
                    return self._summarize_with_openai(prompt, self.default_openai_model)
                except:
                    pass
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
        
        # Build a simple markdown summary
        summary = "## Overview\n"
        summary += f"This conversation includes {len(speakers)} speaker(s) discussing various topics.\n\n"
        
        summary += "## Key Topics\n"
        for sample in samples:
            if ':' in sample:
                _, content = sample.split(':', 1)
                content = content.strip()
                if content:
                    # Extract a topic-like phrase (first 8-10 words)
                    words = content.split()
                    topic = ' '.join(words[:min(10, len(words))])
                    summary += f"- {topic}...\n"
        
        summary += "\n## Decisions & Conclusions\n"
        summary += "None identified (automatic fallback summary).\n\n"
        
        summary += "## Action Items\n"
        summary += "None identified (automatic fallback summary).\n\n"
        
        summary += "\n---\n*Note: This is a simplified fallback summary generated because the LLM service was unavailable.*"
        
        return summary

    def _summarize_with_openai(self, prompt: str, model: str = None) -> str:
        """Use OpenAI API for summarization"""
        if not self.api_key:
            return "OpenAI API key not configured"

        model_to_use = model or self.default_openai_model

        try:
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
