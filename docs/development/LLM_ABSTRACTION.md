# LLM Provider Abstraction

## Overview

econ-data-mcp now supports **flexible LLM backends**, allowing you to switch between OpenRouter, local models (Ollama, LM Studio), and other LLM providers without changing code.

## Benefits

1. **Cost Savings**: Use local models instead of paid APIs
2. **Privacy**: Keep all data processing on-premises
3. **Flexibility**: Switch providers easily via configuration
4. **Future-Proof**: Add new providers without refactoring

## Architecture

### Components

```
backend/services/llm/
├── base.py              # Abstract LLM provider interface
├── openrouter_provider.py  # OpenRouter implementation
├── local_provider.py    # Local model support (Ollama/LM Studio)
├── factory.py           # Provider selection logic
└── __init__.py          # Package exports
```

### Flow

1. **Configuration** loads from `.env`:
   - `LLM_PROVIDER`: Which provider to use
   - `LLM_MODEL`: Which specific model
   - `LLM_BASE_URL`: For local providers

2. **Factory** creates appropriate provider instance

3. **OpenRouterService** uses provider for query parsing (backward compatible)

4. **Queries** processed identically regardless of backend

## Configuration

### Environment Variables

Add to your `.env` file:

```bash
# LLM Provider Configuration
LLM_PROVIDER=openrouter  # Options: openrouter, ollama, lm-studio
LLM_MODEL=openai/gpt-4o-mini
# LLM_BASE_URL=http://localhost:11434  # For local providers
# LLM_TIMEOUT=30
```

### Supported Providers

#### OpenRouter (Default)
```bash
LLM_PROVIDER=openrouter
LLM_MODEL=openai/gpt-4o-mini
OPENROUTER_API_KEY=sk-or-...
```

**Available Models**: GPT-4, Claude, Llama, Mistral, and 100+ others via OpenRouter

#### Ollama (Local)
```bash
LLM_PROVIDER=ollama
LLM_MODEL=llama2
LLM_BASE_URL=http://localhost:11434
```

**Setup**: Install Ollama from https://ollama.ai/
```bash
ollama pull llama2
ollama serve
```

#### LM Studio (Local)
```bash
LLM_PROVIDER=lm-studio
LLM_MODEL=local-model
LLM_BASE_URL=http://localhost:1234
```

**Setup**: Download LM Studio from https://lmstudio.ai/

## Usage Examples

### Using OpenRouter (Default)

No changes needed - works out of the box with current configuration.

### Switching to Local Ollama

1. Install and start Ollama:
```bash
curl https://ollama.ai/install.sh | sh
ollama pull llama2
ollama serve
```

2. Update `.env`:
```bash
LLM_PROVIDER=ollama
LLM_MODEL=llama2
LLM_BASE_URL=http://localhost:11434
```

3. Restart backend - queries now use local model!

### Using LM Studio

1. Download and start LM Studio with a model loaded

2. Update `.env`:
```bash
LLM_PROVIDER=lm-studio
LLM_MODEL=your-model-name
LLM_BASE_URL=http://localhost:1234
```

3. Restart backend

## API Interface

### BaseLLMProvider

All providers implement this interface:

```python
class BaseLLMProvider(ABC):
    @abstractmethod
    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 2000,
        json_mode: bool = False,
        **kwargs
    ) -> LLMResponse:
        """Generate completion from LLM"""
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if provider is accessible"""
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """Return provider name"""
        pass

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Return specific model being used"""
        pass
```

### Creating Custom Providers

To add a new provider:

1. Create `backend/services/llm/custom_provider.py`:

```python
from .base import BaseLLMProvider, LLMResponse

class CustomProvider(BaseLLMProvider):
    def __init__(self, config):
        super().__init__(config)
        self.api_key = config.get("api_key")

    async def generate(self, prompt, system_prompt=None, **kwargs):
        # Your implementation here
        pass

    async def health_check(self):
        # Check if your API is accessible
        pass

    @property
    def name(self):
        return "Custom"

    @property
    def model_name(self):
        return self._model
```

2. Register in `factory.py`:

```python
from .custom_provider import CustomProvider

def create_llm_provider(provider_type, config):
    if provider_type == "custom":
        return CustomProvider(config)
    # ... existing providers
```

3. Use it:

```bash
LLM_PROVIDER=custom
```

## Migration Guide

### From Old Code

**Before** (hardcoded OpenRouter):
```python
service = OpenRouterService(api_key="...")
result = await service.parse_query(query)
```

**After** (flexible providers):
```python
# Automatically uses configured provider
service = OpenRouterService(api_key="...", settings=settings)
result = await service.parse_query(query)  # Same interface!
```

### Backward Compatibility

Old code continues to work - `settings` parameter is optional:

```python
# Still works without settings
service = OpenRouterService(api_key="...")
```

## Performance Considerations

### OpenRouter
- **Latency**: ~500-2000ms depending on model
- **Cost**: Pay per token
- **Availability**: 99.9% uptime

### Local Models (Ollama)
- **Latency**: ~100-500ms (local network)
- **Cost**: Free (compute costs only)
- **Availability**: Depends on your hardware

### Recommendations

- **Production**: Use OpenRouter for reliability
- **Development**: Use local models to save costs
- **High Volume**: Consider local models for cost savings
- **Privacy-Critical**: Use local models for data security

## Troubleshooting

### "OpenRouter API error"
- Check `OPENROUTER_API_KEY` is valid
- Verify you have credits at https://openrouter.ai

### "Connection refused" (Local)
- Ensure Ollama/LM Studio is running
- Check `LLM_BASE_URL` is correct
- Verify firewall settings

### "Model not found" (Ollama)
- Pull the model: `ollama pull llama2`
- Check available models: `ollama list`

### Slow Responses (Local)
- Ensure adequate RAM (8GB minimum for most models)
- Consider smaller models (llama2:7b vs llama2:70b)
- Check CPU/GPU usage

## Testing

Test your LLM provider:

```bash
# Health check
curl http://localhost:3001/api/health

# Test query
curl -X POST http://localhost:3001/api/query \
  -H "Content-Type: application/json" \
  -d '{"query": "Show me US GDP for 2023"}'
```

## Future Enhancements

Planned features:

- [ ] Direct OpenAI API support (bypass OpenRouter)
- [ ] Direct Anthropic Claude API support
- [ ] Azure OpenAI support
- [ ] Hugging Face Inference API support
- [ ] LLM response caching for common queries
- [ ] Automatic fallback between providers
- [ ] Cost tracking per provider

## Summary

The LLM abstraction makes econ-data-mcp:
- ✅ **Flexible**: Switch providers via config
- ✅ **Cost-Effective**: Use free local models
- ✅ **Private**: Keep data on-premises
- ✅ **Professional**: Clean, maintainable architecture
- ✅ **Future-Proof**: Easy to add new providers

## Implementation Date

October 19, 2025

## Related Files

- `backend/services/llm/` - LLM abstraction layer
- `backend/config.py` - Configuration settings
- `backend/services/openrouter.py` - Updated to use abstraction
- `backend/services/query.py` - Updated to pass settings
- `backend/main.py` - Updated to inject settings
- `.env.example` - Configuration examples
