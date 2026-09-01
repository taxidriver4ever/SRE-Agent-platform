"""根据统一模型名称选择 Provider。"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelRoute:
    """一次路由决策，包含厂商和传给厂商的真实模型名。"""

    provider: str
    model: str


class ModelRouter:
    """使用显式前缀和安全默认规则选择模型厂商。"""

    PREFIXES = {"openai", "claude", "deepseek", "vllm", "ollama"}

    def route(self, requested_model: str) -> ModelRoute:
        """解析 ``provider/model``，无前缀时根据模型名推断厂商。"""
        if "/" in requested_model:
            prefix, model = requested_model.split("/", 1)
            if prefix in self.PREFIXES and model:
                return ModelRoute(provider=prefix, model=model)

        lowered = requested_model.lower()
        if lowered.startswith("claude"):
            return ModelRoute(provider="claude", model=requested_model)
        if lowered.startswith("deepseek"):
            return ModelRoute(provider="deepseek", model=requested_model)

        # 未显式指定时默认 OpenAI；本地 Provider 必须使用显式前缀，避免把
        # 未知公网模型意外路由到本机服务。
        return ModelRoute(provider="openai", model=requested_model)
