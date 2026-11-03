# -*- coding: utf-8 -*-
"""服务器版没有本地模型（2C2G、无显卡，跑不动 3GB 的 Qwen）。

保留 console.py 用到的全部接口，页面照常显示，只是所有启动动作都返回失败，
状态里带 disabled=True 和原因，面板可以照实展示。
"""
DISABLED_REASON = "手机版使用你配置的联网 AI，请在「模型」页设置接口"
MODEL_NAME = ""


def status(cfg=None):
    return {
        "installed": False,
        "missing": [],
        "device": "",
        "device_available": {"vulkan": False, "cpu": False},
        "running": False,
        "ready": False,
        "pid": 0,
        "port": 0,
        "model": MODEL_NAME,
        "model_mb": 0,
        "download": {"active": False, "file": "", "done": 0, "total": 0, "pct": 0,
                     "error": "", "msg": ""},
        "log": [],
        "dir": "",
        "thinking": False,
        "thinking_budget": 0,
        "idle_sleep": 0,
        "mem_mb": 0,
        "sleeping": False,
        "disabled": True,
        "reason": DISABLED_REASON,
    }


def start(cfg=None):
    return False, DISABLED_REASON


def stop(cfg=None):
    return True


def download_all():
    return False, DISABLED_REASON
