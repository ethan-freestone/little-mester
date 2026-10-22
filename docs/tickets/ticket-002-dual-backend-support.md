# TICKET-002: Support both Ollama AND llama.cpp simultaneously

**Status**: DONE
**Priority**: HIGH
**Completed**: 2026-10-21

## Goal

- Keep both backends available for learning/comparison
- Allow CLI model switching between any model on either backend
- `models.txt` uses section headers: `[ollama]` and `[llamacpp]`

## Implementation

1. ✅ `models.txt` with both backends
2. ✅ `scripts/regen-configs` generates dual-provider opencode.json
3. ✅ `compose.yaml` has both `ollama` and `llamacpp` services
4. ✅ Model download for both (ollama-pull + llamacpp-pull)
5. ✅ `lab` script runs both pull services
6. ✅ OpenCode `/model` command works across both providers
