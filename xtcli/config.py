"""xtcli configuration (xtcli.toml) and xTranslator install layout."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, asdict
from pathlib import Path

# workspace switch (first cmdline arg, TESVT_Const.setGameByCommand)
# -> UserPrefs/<subfolder> (aGAMES_SUBFOLDER)
GAMES = {
    "skyrimse": "SkyrimSE",
    "skyrim": "Skyrim",
    "fallout4": "Fallout4",
    "falloutnv": "FalloutNV",
    "fallout76": "Fallout76",
    "starfield": "Starfield",
}

CONFIG_NAME = "xtcli.toml"


@dataclass
class Config:
    xtranslator_exe: str = ""
    port: int = 8737
    game: str = "skyrimse"
    source_lang: str = "english"
    dest_lang: str = "japanese"
    # seconds without any API request (after the run started) before assuming
    # the batch stalled; the sentinel rule normally ends the run much earlier
    idle_timeout: float = 300.0
    launch_timeout: float = 90.0
    overall_timeout: float = 7200.0

    path: Path | None = field(default=None, repr=False, compare=False)

    # ---- derived paths -------------------------------------------------
    @property
    def exe(self) -> Path:
        return Path(self.xtranslator_exe)

    @property
    def install_dir(self) -> Path:
        return self.exe.parent

    @property
    def game_prefs_dir(self) -> Path:
        return self.install_dir / "UserPrefs" / GAMES[self.game]

    @property
    def batchprocessor_tmp(self) -> Path:
        return self.game_prefs_dir / "batchprocessor" / "~processor.tmp"

    @property
    def common_prefs(self) -> Path:
        return self.install_dir / "UserPrefs" / "commonPrefs.ini"

    @property
    def api_prefs(self) -> Path:
        return self.install_dir / "UserPrefs" / "commonApiPrefs.ini"

    @property
    def api_translator_txt(self) -> Path:
        return self.install_dir / "Misc" / "ApiTranslator.txt"

    @property
    def api_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1/chat/completions"

    def validate(self) -> None:
        if self.game not in GAMES:
            raise SystemExit(f"unknown game '{self.game}' (choose from {', '.join(GAMES)})")
        if not self.xtranslator_exe:
            raise SystemExit("xTranslator path not configured - run 'xtcli setup --exe <path>' first")
        if not self.exe.is_file():
            raise SystemExit(f"xTranslator not found: {self.exe}")


def _candidates() -> list[Path]:
    paths = []
    if env := os.environ.get("XTCLI_CONFIG"):
        paths.append(Path(env))
    paths.append(Path.cwd() / CONFIG_NAME)
    paths.append(Path(__file__).resolve().parent.parent / CONFIG_NAME)
    return paths


def load_config() -> Config:
    for p in _candidates():
        if p.is_file():
            data = tomllib.loads(p.read_text(encoding="utf-8"))
            cfg = Config(**{k: v for k, v in data.items() if k in Config.__dataclass_fields__})
            cfg.path = p
            return cfg
    return Config()


def save_config(cfg: Config, path: Path | None = None) -> Path:
    path = path or cfg.path or Path(__file__).resolve().parent.parent / CONFIG_NAME
    d = asdict(cfg)
    d.pop("path", None)
    lines = []
    for k, v in d.items():
        if isinstance(v, str):
            escaped = v.replace("\\", "\\\\")
            lines.append(f'{k} = "{escaped}"')
        else:
            lines.append(f"{k} = {v}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cfg.path = path
    return path
