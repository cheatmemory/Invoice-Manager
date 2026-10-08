"""Settings and configuration API endpoints."""

import logging
import os
from pathlib import Path
from typing import Optional, List
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException, Header

logger = logging.getLogger(__name__)


def _update_env_file(updates: dict[str, str]) -> None:
    """Update .env file with new key-value pairs.

    Preserves existing entries and comments, updates existing keys,
    and appends new keys at the end.
    """
    env_path = Path(__file__).parent.parent.parent / ".env"

    # Read existing content
    existing_lines = []
    existing_keys = set()
    if env_path.exists():
        with open(env_path, "r") as f:
            for line in f:
                stripped = line.strip()
                # Track existing keys (ignore comments and empty lines)
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    key = stripped.split("=", 1)[0].strip()
                    existing_keys.add(key)
                existing_lines.append(line)

    # Update existing lines or mark keys as handled
    updated_keys = set()
    new_lines = []
    for line in existing_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                # Replace this line with updated value
                new_lines.append(f"{key}={updates[key]}\n")
                updated_keys.add(key)
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)

    # Append new keys that weren't in the file
    for key, value in updates.items():
        if key not in updated_keys:
            new_lines.append(f"{key}={value}\n")

    # Write back
    with open(env_path, "w") as f:
        f.writelines(new_lines)

    logger.info(f"Updated .env file with keys: {list(updates.keys())}")


def _require_llm_config_token(
    required_token: str,
    header_token: Optional[str],
    authorization: Optional[str],
) -> None:
    """Verify LLM config token when configured via env."""
    if not required_token:
        return

    candidate = header_token
    if not candidate and authorization:
        auth_value = authorization.strip()
        if auth_value.lower().startswith("bearer "):
            candidate = auth_value[7:].strip()
        else:
            candidate = auth_value

    if not candidate or candidate != required_token:
        raise HTTPException(status_code=401, detail="未授权")

from app.config import get_settings, clear_settings_cache
from app.services.llm_service import get_llm_service, reset_llm_service, PROVIDERS
from app.services.model_registry import get_models_with_fallback

router = APIRouter()


class LLMProviderInfo(BaseModel):
    """Information about an LLM provider."""
    name: str
    display_name: str
    is_configured: bool
    model: Optional[str] = None
    base_url: Optional[str] = None


class LLMStatusResponse(BaseModel):
    """Response for LLM status check."""
    is_configured: bool
    active_provider: Optional[str] = None
    active_provider_display: Optional[str] = None
    configured_providers: List[str] = []
    available_providers: List[LLMProviderInfo] = []


class LLMConfigRequest(BaseModel):
    """Request to configure an LLM provider."""
    provider: str
    api_key: str
    model: Optional[str] = None
    base_url: Optional[str] = None


class LLMConfigResponse(BaseModel):
    """Response after configuring LLM."""
    success: bool
    message: str
    provider: Optional[str] = None


# Display names for providers
PROVIDER_DISPLAY_NAMES = {
    "openai": "OpenAI (GPT)",
    "anthropic": "Anthropic (Claude)",
    "google": "Google (Gemini)",
    "qwen": "阿里云 (通义千问)",
    "deepseek": "DeepSeek",
    "zhipu": "智谱 (GLM)",
}

# Default models for providers
DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-haiku-20240307",
    "google": "gemini-1.5-flash",
    "qwen": "qwen-turbo",
    "deepseek": "deepseek-chat",
    "zhipu": "glm-4-flash",
}


@router.get("/llm/status", response_model=LLMStatusResponse)
async def get_llm_status():
    """Get LLM configuration status."""
    settings = get_settings()
    llm_service = get_llm_service()

    active_provider = settings.get_active_llm_provider()
    configured_providers = llm_service.get_configured_providers()

    # Build available providers list
    available_providers = []
    for name in PROVIDERS.keys():
        provider_info = LLMProviderInfo(
            name=name,
            display_name=PROVIDER_DISPLAY_NAMES.get(name, name),
            is_configured=name in configured_providers,
            model=_get_provider_model(name) if name in configured_providers else DEFAULT_MODELS.get(name),
            base_url=_get_provider_base_url(name) if name in configured_providers else None,
        )
        available_providers.append(provider_info)

    return LLMStatusResponse(
        is_configured=settings.is_llm_configured(),
        active_provider=active_provider,
        active_provider_display=PROVIDER_DISPLAY_NAMES.get(active_provider, active_provider) if active_provider else None,
        configured_providers=configured_providers,
        available_providers=available_providers,
    )


@router.post("/llm/configure", response_model=LLMConfigResponse)
async def configure_llm(
    request: LLMConfigRequest,
    x_llm_config_token: Optional[str] = Header(default=None, alias="X-LLM-Config-Token"),
    authorization: Optional[str] = Header(default=None),
):
    """Configure an LLM provider and persist to .env file."""
    provider = request.provider.lower()

    _require_llm_config_token(
        os.environ.get("LLM_CONFIG_TOKEN", ""),
        x_llm_config_token,
        authorization,
    )

    if provider not in PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的LLM提供商: {provider}。支持的提供商: {', '.join(PROVIDERS.keys())}"
        )

    if not request.api_key:
        raise HTTPException(status_code=400, detail="API密钥不能为空")

    try:
        # Build env updates for both os.environ and .env file
        env_updates: dict[str, str] = {"LLM_PROVIDER": provider}

        # Provider-specific configuration
        provider_config = {
            "openai": ("OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL"),
            "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "ANTHROPIC_BASE_URL"),
            "google": ("GOOGLE_API_KEY", "GOOGLE_MODEL", "GOOGLE_BASE_URL"),
            "qwen": ("QWEN_API_KEY", "QWEN_MODEL", "QWEN_BASE_URL"),
            "deepseek": ("DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL"),
            "zhipu": ("ZHIPU_API_KEY", "ZHIPU_MODEL", "ZHIPU_BASE_URL"),
        }

        key_name, model_name, base_url_name = provider_config[provider]
        env_updates[key_name] = request.api_key
        if request.model:
            env_updates[model_name] = request.model
        if request.base_url and base_url_name:
            env_updates[base_url_name] = request.base_url

        # Set environment variables for current process
        for key, value in env_updates.items():
            os.environ[key] = value

        # Persist to .env file for restart persistence
        _update_env_file(env_updates)

        # Clear caches to reload settings
        clear_settings_cache()
        reset_llm_service()

        # Verify configuration
        new_settings = get_settings()
        if not new_settings.is_llm_configured():
            raise HTTPException(status_code=500, detail="配置失败，请检查API密钥是否正确")

        return LLMConfigResponse(
            success=True,
            message=f"已成功配置 {PROVIDER_DISPLAY_NAMES.get(provider, provider)}",
            provider=provider,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"LLM configuration failed: {e}")
        raise HTTPException(status_code=500, detail="配置失败，请检查配置参数是否正确") from e


@router.post("/llm/test")
async def test_llm_connection():
    """Test the current LLM configuration with a simple request."""
    llm_service = get_llm_service()

    if not llm_service.is_available:
        raise HTTPException(status_code=400, detail="未配置LLM提供商")

    try:
        provider = llm_service.active_provider
        if not provider:
            raise HTTPException(status_code=500, detail="无法获取LLM提供商实例")

        # Simple test prompt
        response = provider.chat_completion(
            "You are a helpful assistant.",
            "Reply with exactly: OK"
        )

        return {
            "success": True,
            "provider": provider.get_provider_name(),
            "provider_display": PROVIDER_DISPLAY_NAMES.get(provider.get_provider_name(), provider.get_provider_name()),
            "message": "LLM连接测试成功",
            "response": response[:100],  # Limit response length
        }

    except Exception as e:
        logger.error(f"LLM connection test failed: {e}")
        raise HTTPException(
            status_code=500,
            detail="LLM连接测试失败，请检查API密钥和网络连接"
        ) from e


def _get_provider_model(provider_name: str) -> Optional[str]:
    """Get the configured model for a provider."""
    settings = get_settings()
    model_map = {
        "openai": settings.openai_model,
        "anthropic": settings.anthropic_model,
        "google": settings.google_model,
        "qwen": settings.qwen_model,
        "deepseek": settings.deepseek_model,
        "zhipu": settings.zhipu_model,
    }
    return model_map.get(provider_name)


def _get_provider_base_url(provider_name: str) -> Optional[str]:
    """Get the configured base URL for a provider."""
    settings = get_settings()
    url_map = {
        "openai": settings.openai_base_url,
        "anthropic": settings.anthropic_base_url,
        "google": settings.google_base_url,
        "qwen": settings.qwen_base_url,
        "deepseek": settings.deepseek_base_url,
        "zhipu": settings.zhipu_base_url,
    }
    return url_map.get(provider_name) or None


class ConfigTestRequest(BaseModel):
    """Request to test LLM config before saving."""
    provider: str
    api_key: str
    model: Optional[str] = None
    base_url: Optional[str] = None


class ConfigTestResponse(BaseModel):
    """Response for config test."""
    success: bool
    message: str
    response_time_ms: Optional[int] = None


# Default base URLs for providers that require them
_DEFAULT_BASE_URLS = {
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "deepseek": "https://api.deepseek.com",
}


def _test_openai_compatible(client, model: str) -> str:
    """Test an OpenAI-compatible client, with fallback to Responses API."""
    try:
        # Try Chat Completions API first (standard OpenAI)
        stream = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Reply with exactly: OK"},
            ],
            temperature=0.1,
            max_tokens=10,
            stream=True,
        )
        parts = []
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                parts.append(chunk.choices[0].delta.content)
        return "".join(parts).strip()
    except Exception as e:
        err_msg = str(e)
        # If proxy doesn't support Chat Completions, try Responses API
        if "Unsupported parameter" not in err_msg and "messages" not in err_msg.lower():
            raise
        # Fall back to Responses API (used by Codex-compatible proxies)
        if not hasattr(client, 'responses'):
            raise RuntimeError(
                "此代理使用 Responses API，但当前 openai SDK 版本不支持。"
                "请升级: pip install -U openai"
            ) from e
        stream = client.responses.create(
            model=model,
            input=[{"role": "user", "content": "Reply with exactly: OK"}],
            stream=True,
        )
        parts = []
        for event in stream:
            if getattr(event, 'type', '') == 'response.output_text.delta':
                parts.append(event.delta)
        return "".join(parts).strip()


@router.post("/llm/test-config", response_model=ConfigTestResponse)
async def test_llm_config(request: ConfigTestRequest):
    """Test LLM configuration before saving using the actual provider client library."""
    import time

    provider = request.provider.lower()
    if provider not in PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的LLM提供商: {provider}。支持的提供商: {', '.join(PROVIDERS.keys())}"
        )

    if not request.api_key:
        raise HTTPException(status_code=400, detail="API密钥不能为空")

    model = request.model or DEFAULT_MODELS.get(provider, "")
    base_url = request.base_url.strip().rstrip("/") if request.base_url else None

    start = time.monotonic()
    try:
        if provider in ("openai", "qwen", "deepseek"):
            from openai import OpenAI
            kwargs = {"api_key": request.api_key}
            effective_url = base_url or _DEFAULT_BASE_URLS.get(provider)
            if effective_url:
                kwargs["base_url"] = effective_url
            # Override User-Agent for custom base URLs to avoid Cloudflare WAF
            # blocking the default "OpenAI/Python" user agent
            if base_url:
                kwargs["default_headers"] = {"User-Agent": "python-httpx/0.27.0"}
            client = OpenAI(**kwargs)
            result_text = _test_openai_compatible(client, model)

        elif provider == "anthropic":
            import anthropic
            kwargs = {"api_key": request.api_key}
            if base_url:
                kwargs["base_url"] = base_url
            client = anthropic.Anthropic(**kwargs)
            response = client.messages.create(
                model=model,
                max_tokens=10,
                system="You are a helpful assistant.",
                messages=[{"role": "user", "content": "Reply with exactly: OK"}],
            )
            result_text = response.content[0].text.strip()

        elif provider == "google":
            import google.generativeai as genai
            configure_kwargs = {"api_key": request.api_key}
            if base_url:
                configure_kwargs["client_options"] = {"api_endpoint": base_url}
            genai.configure(**configure_kwargs)
            gmodel = genai.GenerativeModel(model)
            response = gmodel.generate_content("Reply with exactly: OK")
            result_text = response.text.strip()

        elif provider == "zhipu":
            from zhipuai import ZhipuAI
            kwargs = {"api_key": request.api_key}
            if base_url:
                kwargs["base_url"] = base_url
            client = ZhipuAI(**kwargs)
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": "Reply with exactly: OK"},
                ],
                temperature=0.1,
                max_tokens=10,
            )
            result_text = response.choices[0].message.content.strip()

        else:
            raise HTTPException(status_code=400, detail=f"不支持的LLM提供商: {provider}")

        elapsed_ms = int((time.monotonic() - start) * 1000)
        return ConfigTestResponse(
            success=True,
            message=f"连接成功，响应: {result_text[:50]}",
            response_time_ms=elapsed_ms,
        )

    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        error_msg = str(e)
        if "401" in error_msg or "Unauthorized" in error_msg or "authentication" in error_msg.lower():
            msg = "API密钥无效或未授权"
        elif "403" in error_msg or "Forbidden" in error_msg:
            msg = "API密钥权限不足或访问被拒绝"
        elif "404" in error_msg or "Not Found" in error_msg:
            msg = "API地址无效，请检查URL是否正确（对于OpenAI兼容接口，base_url通常需要包含 /v1 路径）"
        elif "connect" in error_msg.lower() or "timeout" in error_msg.lower():
            msg = "无法连接到该地址，请检查URL和网络连接"
        elif "<!DOCTYPE" in error_msg or "<html" in error_msg:
            msg = "API地址返回了网页而非API响应，请检查URL是否正确"
        elif "model" in error_msg.lower() and ("not found" in error_msg.lower() or "does not exist" in error_msg.lower()):
            msg = f"模型不存在，请检查模型名称是否正确"
        else:
            # Clean up error message - remove HTML and limit length
            clean_msg = error_msg.split('\n')[0][:150]
            msg = f"测试失败: {clean_msg}"
        logger.error(f"Config test failed for {provider}: {e}")
        return ConfigTestResponse(success=False, message=msg, response_time_ms=elapsed_ms)


class ModelInfo(BaseModel):
    """Information about a model."""
    id: str
    name: str
    vision: bool
    context_length: Optional[int] = None
    pricing: Optional[dict] = None


class ModelsResponse(BaseModel):
    """Response for available models."""
    models: List[ModelInfo]
    source: str  # "openrouter" or "fallback"


@router.get("/models", response_model=ModelsResponse)
async def get_available_models(
    provider: Optional[str] = None,
    vision_only: bool = False
):
    """Get available models, optionally filtered by provider or vision capability.

    Args:
        provider: Filter by provider name (openai, anthropic, google, qwen, deepseek, zhipu)
        vision_only: If true, only return models that support image input

    Returns:
        List of available models with their capabilities
    """
    if provider:
        if provider.lower() not in PROVIDERS:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的LLM提供商: {provider}。支持的提供商: {', '.join(PROVIDERS.keys())}"
            )
        models = get_models_with_fallback(provider, vision_only)
    else:
        # Get models for all providers
        all_models = []
        for prov in PROVIDERS.keys():
            all_models.extend(get_models_with_fallback(prov, vision_only))
        models = all_models

    # Determine source based on model ID format (OpenRouter uses "provider/model" format)
    source = "openrouter" if models and "/" in models[0].get("id", "") else "fallback"

    return ModelsResponse(
        models=[ModelInfo(**m) for m in models],
        source=source
    )
