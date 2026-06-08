"""LM Studio inference over the official ``lmstudio`` SDK (PRD 0017).

The SDK-transport counterpart to :mod:`bob.llm_client`'s OpenAI-compatible
``LMStudioClient``, selected by ``LLM_LMSTUDIO_TRANSPORT=sdk`` in
:mod:`bob.llm.factory`. Issue 0111 ships the chat POC + the history converter;
streaming, tool-calling and the per-role lifecycle land in issues 0112-0115.
"""

from __future__ import annotations

from bob.llm.lmstudio_sdk.client import LMStudioSDKClient

__all__ = ["LMStudioSDKClient"]
