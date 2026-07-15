# src/generation.py

import logging

import ollama
from google import genai
from google.genai import types

from src.config import Config

logger = logging.getLogger(__name__)

# The source corpus is Dostoevsky: murder, suicide, abuse. Default filters block
# these passages, and a blocked response has no .text -- it raises. Kept as-is
# from the pre-migration code.
SAFETY_SETTINGS = [
    types.SafetySetting(category=category, threshold="BLOCK_NONE")
    for category in (
        "HARM_CATEGORY_DANGEROUS_CONTENT",
        "HARM_CATEGORY_SEXUALLY_EXPLICIT",
        "HARM_CATEGORY_HATE_SPEECH",
        "HARM_CATEGORY_HARASSMENT",
    )
]


class Generator:
    """Teacher LLM for building the fine-tune Q&A dataset.

    Note for RAG work: this is NOT the client to use. It returns "" on every
    failure, so callers cannot tell a rate limit from a dead model, and it has
    no request pacing. src/rag/teacher.py exists for that.
    """

    def __init__(self):
        self.backend = Config.LLM_BACKEND
        self.model_name = Config.MODEL_NAME

        if self.backend == 'gemini':
            self.client = genai.Client(api_key=Config.GOOGLE_API_KEY)
            self.gemini_config = types.GenerateContentConfig(safety_settings=SAFETY_SETTINGS)

    def _call_gemini(self, prompt: str) -> str:
        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=self.gemini_config,
            )

            return response.text
        except Exception as e:
            logger.error("Gemini API error: %s", e)
            return ""

    def _call_ollama(self, prompt: str) -> str:
        try:
            response = ollama.chat(model = self.model_name, messages=[
                {'role': 'user', 'content': prompt}
            ])

            return response['message']['content']
        except Exception as e:
            logger.error("Ollama error: %s", e)

            return ""

    def generate_completion(self, prompt: str) -> str:
        if self.backend == 'gemini':
            return self._call_gemini(prompt)
        elif self.backend == 'ollama':
            return self._call_ollama(prompt)
        else:
            raise ValueError(f"Unknown backend: {self.backend}")

    def generate_qa_pair(self, context_chunk: str):
        '''
        generate qna pair
        '''

        # generate question
        q_prompt = (
            f"You are a man who gets to meet Dostoevsky in person, and you are deeply intrigued of his ideas. "
            f"Using the provided context: '{context_chunk}', generate a single question that deeply captures an idea or theme presented in the context, "
            f"do not mention a character or an event in the context, just an idea it portrays. "
            f"The question should be thought-provoking and focused on extracting a crucial aspect from the text."
        )
        question = self.generate_completion(q_prompt)
        if not question: return None, None

        # generate answer
        a_prompt = (
            f"You are Fyodor Dostoevsky, incorporating his literary tone as well, someone has asked you to give a detailed answer "
            f"(in less than 350 words) to the question: '{question}', given the context: '{context_chunk}'. "
            f"Answer in first person view, as if you are Dostoevsky himself, give no editorial comments and references to external sources."
        )
        answer = self.generate_completion(a_prompt)

        return question, answer
