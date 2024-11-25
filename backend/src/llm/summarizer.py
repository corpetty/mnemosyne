import os
from typing import Dict, List, Optional
import requests
from dotenv import load_dotenv

load_dotenv()

class Summarizer:
    def __init__(self, provider: str = "ollama"):
        self.provider = provider
        self.api_key = os.getenv("OPENAI_API_KEY")
        self.ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")

    def summarize_transcript(self, transcript: List[Dict]) -> str:
        """Summarize the transcript using the specified LLM provider"""
        if not transcript:
            return "No transcript available to summarize."

        # Prepare the transcript text
        full_text = "\n".join([f"{segment['speaker']}: {segment['text']}" 
                              for segment in transcript])
        
        prompt = f"""Please provide a concise summary of the following conversation:

{full_text}

Key points to include:
1. Main topics discussed
2. Important decisions or conclusions
3. Action items (if any)
"""

        if self.provider == "ollama":
            return self._summarize_with_ollama(prompt)
        else:
            return self._summarize_with_openai(prompt)

    def _summarize_with_ollama(self, prompt: str) -> str:
        """Use local Ollama model for summarization"""
        try:
            response = requests.post(
                f"{self.ollama_url}/api/generate",
                json={
                    "model": "llama3.2",
                    "prompt": prompt,
                    "stream": False
                }
            )
            return response.json()["response"]
        except Exception as e:
            return f"Error generating summary with Ollama: {str(e)}"

    def _summarize_with_openai(self, prompt: str) -> str:
        """Use OpenAI API for summarization"""
        if not self.api_key:
            return "OpenAI API key not configured"

        try:
            response = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "gpt-3.5-turbo",
                    "messages": [{"role": "user", "content": prompt}]
                }
            )
            return response.json()["choices"][0]["message"]["content"]
        except Exception as e:
            return f"Error generating summary with OpenAI: {str(e)}"
