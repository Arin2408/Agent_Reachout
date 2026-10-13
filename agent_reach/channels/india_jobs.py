# -*- coding: utf-8 -*-
"""India jobs — Chennai / Noida / Gurugram 岗位与在招创业公司。

两条后端，调用姿势见 skill/references/career.md「印度求职」章节：

- python-jobspy（MIT，库而非 CLI）：一次查 Naukri + LinkedIn，
  按城市过滤。check() 只用 importlib.util.find_spec 判断装没装——不 import、
  不联网，避免体检时触发 pandas 等重依赖加载或真实抓取。
- Jina Reader（零配置兜底）：读 Wellfound / YC Work at a Startup 的城市页，
  拿「在该城市招人的创业公司」列表。
"""

import importlib.util
import sys

from agent_reach.utils.url import host_matches

from .base import Channel

_JOBSPY_PACKAGE = "python-jobspy"


def _jobspy_installed() -> bool:
    try:
        return importlib.util.find_spec("jobspy") is not None
    except (ImportError, ValueError):
        return False


class IndiaJobsChannel(Channel):
    name = "india_jobs"
    description = "印度求职（Chennai/Noida/Gurugram + 创业公司）"
    backends = [_JOBSPY_PACKAGE, "Jina Reader"]
    tier = 0

    def can_handle(self, url: str) -> bool:
        return host_matches(url, "naukri.com", "wellfound.com")

    def check(self, config=None):
        if _jobspy_installed():
            self.active_backend = _JOBSPY_PACKAGE
            return "ok", (
                "python-jobspy 可用（Naukri/LinkedIn）；"
                f"用这个 Python 调用：{sys.executable}。"
                "创业公司列表走 Jina Reader 读 Wellfound/YC 城市页"
            )
        self.active_backend = "Jina Reader"
        return "warn", (
            "仅 Jina Reader 可用（Wellfound/YC 创业公司城市页）。"
            "要搜 Naukri/LinkedIn 岗位，安装：\n"
            "  agent-reach install --channels=india_jobs\n"
            f"或：{sys.executable} -m pip install {_JOBSPY_PACKAGE}"
        )
