"""
Deployment and synchronization script for the Harness Antigravity Plugin.
Copies the workspace harness plugin bundle into ~/.gemini/config/plugins/harness
and verifies the installation.
"""

from __future__ import annotations
import os
import shutil
import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent
TARGET_PLUGIN_DIR = Path.home() / ".gemini" / "config" / "plugins" / "harness"


def install() -> None:
    print(f"Deploying Harness Plugin to: {TARGET_PLUGIN_DIR}")
    TARGET_PLUGIN_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Copy plugin.json
    shutil.copy2(WORKSPACE_ROOT / "plugin.json", TARGET_PLUGIN_DIR / "plugin.json")
    print("  + Copied plugin.json")

    # 2. Copy hooks.json
    shutil.copy2(WORKSPACE_ROOT / "hooks.json", TARGET_PLUGIN_DIR / "hooks.json")
    print("  + Copied hooks.json")

    # 3. Copy README.md
    shutil.copy2(WORKSPACE_ROOT / "README.md", TARGET_PLUGIN_DIR / "README.md")
    print("  + Copied README.md")

    # 4. Copy assets
    target_assets = TARGET_PLUGIN_DIR / "assets"
    target_assets.mkdir(parents=True, exist_ok=True)
    for asset in (WORKSPACE_ROOT / "assets").glob("*"):
        shutil.copy2(asset, target_assets / asset.name)
    print("  + Copied assets/")

    # 5. Copy rules
    target_rules = TARGET_PLUGIN_DIR / "rules"
    target_rules.mkdir(parents=True, exist_ok=True)
    for rule in (WORKSPACE_ROOT / "rules").glob("*"):
        shutil.copy2(rule, target_rules / rule.name)
    print("  + Copied rules/")

    # 6. Copy skills
    target_skills = TARGET_PLUGIN_DIR / "skills"
    target_skills.mkdir(parents=True, exist_ok=True)
    for skill_folder in (WORKSPACE_ROOT / "skills").iterdir():
        if skill_folder.is_dir():
            dest = target_skills / skill_folder.name
            dest.mkdir(parents=True, exist_ok=True)
            for item in skill_folder.glob("*"):
                shutil.copy2(item, dest / item.name)
    print("  + Copied skills/")

    # 7. Copy agents
    target_agents = TARGET_PLUGIN_DIR / "agents"
    target_agents.mkdir(parents=True, exist_ok=True)
    for agent in (WORKSPACE_ROOT / "agents").glob("*"):
        shutil.copy2(agent, target_agents / agent.name)
    print("  + Copied agents/")

    # 8. Copy scripts
    target_scripts = TARGET_PLUGIN_DIR / "scripts"
    target_scripts.mkdir(parents=True, exist_ok=True)
    for script in (WORKSPACE_ROOT / "scripts").glob("*"):
        shutil.copy2(script, target_scripts / script.name)
    print("  + Copied scripts/")

    # 9. Copy harness python package
    target_harness = TARGET_PLUGIN_DIR / "harness"
    if target_harness.exists():
        shutil.rmtree(target_harness)
    shutil.copytree(WORKSPACE_ROOT / "harness", target_harness)
    print("  + Copied harness/ package")

    print("\n[SUCCESS] Successfully deployed Harness plugin to Antigravity global plugins directory!")


if __name__ == "__main__":
    install()
