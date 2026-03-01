# Available Models Table

This table summarizes all LLM models known to the system.

| Provider | Model Family | Versions | Status | Type |
|----------|-------------|----------|--------|------|
| Anthropic | Claude Opus | claude-opus-4-5-20251101, claude-3-opus-20240229 | Available | Text Generation |
| Anthropic | Claude Sonnet | claude-sonnet-4-5-20250929, claude-sonnet-4-20250514 (+3 more) | ✓ CURRENT | Text Generation |
| Anthropic | Claude Haiku | claude-3-5-haiku-20241022, claude-3-haiku-20240307 | Available | Text Generation |
| OpenAI | GPT-4o | gpt-4o, gpt-4o-mini (+1 more) | ✓ Available | Text |
| OpenAI | o1 | o1, o1-mini (+1 more) | ✓ Available | Reasoning |
| OpenAI | GPT-4 Turbo | gpt-4-turbo, gpt-4-turbo-2024-04-09 | ✓ Available | Text |
| OpenAI | GPT-4 | gpt-4, gpt-4-0613 | ✓ Available | Text |
| OpenAI | GPT-3.5 Turbo | gpt-3.5-turbo | ✓ Available | Text |
| OpenAI | Whisper | whisper-1 | ✓ Available | Audio-To-Text |
| Google | Gemini 2.0 | gemini-2.0-flash, gemini-2.0-pro | Known | Multimodal |
| Google | Gemini 1.5 | gemini-1.5-pro, gemini-1.5-flash | Known | Multimodal |
| Meta | Llama 3.3 | llama-3.3-70b | Known | Text (OSS) |
| Meta | Llama 3.1 | llama-3.1-405b, llama-3.1-70b (+1 more) | Known | Text (OSS) |
| Mistral AI | Mistral Large | mistral-large-2411 | Known | Text |
| Mistral AI | Mixtral | mixtral-8x7b, mixtral-8x22b | Known | Text |

## Status Legend

- **✓ CURRENT**: Currently running model
- **✓ Available**: Confirmed available through API keys and configuration
- **Available**: Known to be available
- **Known**: Model exists but not confirmed available in this system

## Type Legend

- **Text Generation**: General text generation models
- **Text**: Text-only models
- **Reasoning**: Advanced reasoning models
- **Audio-To-Text**: Speech-to-text transcription models
- **Multimodal**: Models that handle multiple input types (text, images, etc.)
- **(OSS)**: Open Source Software
