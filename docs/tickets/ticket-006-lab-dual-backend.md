# TICKET-006: Update lab script for dual-backend setup

**Status**: DONE
**Priority**: MEDIUM
**Completed**: 2026-10-21

## Changes

- ✅ `./lab setup` runs both `ollama-pull` and `llamacpp-pull`
- ✅ `./lab pull` runs both pull services
- ✅ `./lab build` regenerates configs before building
- ✅ Egress network used for both pull services (HuggingFace + Ollama registry)
