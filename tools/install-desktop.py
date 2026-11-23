"""Install repository launchers into the current user's desktop without replacing files."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PROJECT = "/home/nvidia/Desktop/机器人头"


def desktop_path():
    if shutil.which("xdg-user-dir"):
        path = subprocess.check_output(["xdg-user-dir", "DESKTOP"], text=True).rstrip("\n")
        if path and Path(path).is_absolute():
            return Path(path)
    return Path.home() / "Desktop"


def render(template, project):
    # Desktop Entry unescapes string values before parsing the quoted Exec argument.
    path = str(project)
    if any(ord(char) < 32 for char in path):
        raise ValueError("项目路径包含控制字符，无法写入桌面图标。")
    quoted = "".join("\\" + c if c in '\\"$`' else c for c in path)
    exec_path = quoted.replace("\\", "\\\\").replace("%", "%%")
    output = []
    for line in template.read_text(encoding="utf-8").splitlines():
        if line.startswith("Exec="):
            marker = '"' + TEMPLATE_PROJECT + "/scripts/"
            if line.count(marker) != 1:
                raise ValueError(f"无法识别启动器的命令：{template.name}")
            script = line.split(marker)[1].split('"')[0]
            if not (project / "scripts" / script).is_file():
                raise FileNotFoundError(project / "scripts" / script)
            line = line.replace(marker, '"' + exec_path + "/scripts/")
        elif line == "Path=" + TEMPLATE_PROJECT:
            line = "Path=" + path.replace("\\", "\\\\")
        output.append(line)
    return "\n".join(output) + "\n"


def main():
    parser = argparse.ArgumentParser(description="安装本项目的桌面快捷方式，跳过已有文件。")
    parser.add_argument("--desktop", type=Path, help="指定目标目录，默认使用当前用户的桌面")
    args = parser.parse_args()
    destination = args.desktop if args.desktop is not None else desktop_path()
    templates = sorted((ROOT / "desktop").glob("*.desktop"))
    if not templates:
        parser.exit(1, "缺少 desktop/ 中的桌面模板。\n")
    launchers = [(p.name, render(p, ROOT / "机器人头")) for p in templates]
    destination.mkdir(parents=True, exist_ok=True)
    # Link the finished file exclusively so simultaneous installers cannot overwrite one another.
    for name, content in launchers:
        target = destination / name
        with tempfile.TemporaryDirectory(dir=destination) as scratch:
            prepared = Path(scratch) / name
            prepared.write_text(content, encoding="utf-8")
            prepared.chmod(0o755)
            try:
                os.link(prepared, target)
            except FileExistsError:
                print(f"已存在，跳过 {target}")
                continue
        print(f"已安装 {target}")
        if shutil.which("gio"):
            result = subprocess.run(["gio", "set", str(target), "metadata::trusted", "true"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if result.returncode:
                print("如桌面提示，请右键图标选择“允许启动”。")


if __name__ == "__main__":
    main()
