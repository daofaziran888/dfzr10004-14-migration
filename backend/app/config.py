"""运行配置：端口、跨域、运行环境。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _default_data_dir() -> str:
    # 可用 AUDIT_DATA_DIR 覆盖；容器内默认指向挂载卷，本地默认 backend/data
    return os.environ.get("AUDIT_DATA_DIR", os.path.join(os.getcwd(), "data"))


@dataclass(frozen=True)
class Settings:
    app_name: str = "智能采集质量管控平台"
    env: str = os.environ.get("APP_ENV", "local")
    port: int = 8000
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ]
    )
    page_size_default: int = 20
    page_size_max: int = 200
    # 审计追加式事件日志、迁移检查点与旧数据快照的存放目录
    audit_data_dir: str = field(default_factory=_default_data_dir)


settings = Settings()
