"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import project_metric, resource

# project_metric 的固定路径必须先于通用 /api/{module} 路由注册。
ROUTERS = [project_metric, resource]
