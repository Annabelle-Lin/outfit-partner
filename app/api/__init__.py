# -*- coding: utf-8 -*-
"""API 路由包。

统一返回格式：{"code": 0, "msg": "ok", "data": ...}
- code=0 正常；code!=0 表示业务错误
- 增量二之前的桩接口统一返回 code=501（开发中），前端可直接按 code 分支
"""
from fastapi import Response
import json


def ok(data=None, msg: str = "ok"):
    """正常响应。"""
    return {"code": 0, "msg": msg, "data": data}


def todo(msg: str = "接口开发中（增量二实现）"):
    """桩接口占位响应。"""
    return {"code": 501, "msg": msg, "data": None}
